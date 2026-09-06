"""Bring up the environment the suite tests against, and fetch its extension.

One rule decides everything here: **never deploy over an environment that is
already up.** A run joins a healthy stack, waits for one that is mid-deploy,
and refuses a broken one rather than repairing it — repair belongs to the
deployer's own CLI, which knows about the shared database, the base-path
mapping and the secrets that a hand-rolled cleanup silently skips.
"""
import io
import shutil
import time
import zipfile
from pathlib import Path

import boto3
import requests
from botocore.exceptions import ClientError

from bring import config as cfg


class EnvError(RuntimeError):
    """The environment cannot be brought up, and no browser should open."""


class Environment:
    """A temporary Bring environment, addressed by name."""

    def __init__(self, name: str = None, region: str = None):
        self.name = name or cfg.ENV_NAME
        self.region = region or cfg.AWS_REGION
        self.ecs = boto3.client("ecs", region_name=self.region)
        self.created = False        # True only when this run deployed it

    # ── state ───────────────────────────────────────────────────────

    def stack_status(self):
        """CloudFormation's word on this environment, or None if absent.

        None must mean "there is no such stack" and nothing else. A throttle or
        an expired token also returns nothing, and the caller turns nothing into
        "deploy one" — which replaces the lambdas underneath a run that is using
        them. So anything that is not a clean absence is raised.
        """
        cf = boto3.client("cloudformation", region_name=self.region)
        try:
            stacks = cf.describe_stacks(StackName=cfg.stack_name(self.name)).get("Stacks", [])
            return stacks[0].get("StackStatus", "") if stacks else None
        except ClientError as e:
            if "does not exist" in e.response.get("Error", {}).get("Message", ""):
                return None
            raise EnvError(
                f"Could not read the state of '{cfg.stack_name(self.name)}': {e}\n"
                f"Refusing to continue — an unreadable stack is not an absent one."
            ) from e

    def state(self) -> str:
        """'ready' | 'building' | 'broken' | 'none'.

        'building' is its own answer on purpose. Folded into 'broken' it makes
        the caller tear down a stack that is mid-update, and a stack whose IAM
        role vanishes under an update lands in UPDATE_ROLLBACK_FAILED, which no
        later deploy recovers. Two runs starting a minute apart is enough.
        """
        status = self.stack_status()
        if status is None:
            return "none"
        print(f"   stack {cfg.stack_name(self.name)}: {status}")
        if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            return "ready"
        if status.endswith("_IN_PROGRESS") and not status.startswith("DELETE"):
            return "building"
        return "broken"

    # ── bringing it up ──────────────────────────────────────────────

    def ensure(self) -> "Environment":
        """Reuse the environment if it is up; deploy it if it is not."""
        key = cfg.api_key(cfg.PLATFORM)
        if not key:
            raise EnvError(
                f"No API key for platform '{cfg.PLATFORM}': set "
                f"{cfg.PLATFORM.upper()}_API_KEY in .env.\n"
                f"It is sent as FRONTEND_IDENTIFIER and the deployer cannot build "
                f"a usable extension without it."
            )

        print(f"Looking for environment '{self.name}'")
        state = self.state()

        if state == "building":
            print("   another run is deploying it — waiting rather than cleaning up")
            settled = self.wait_for_stack()
            state = "ready" if settled in ("CREATE_COMPLETE", "UPDATE_COMPLETE") else "broken"

        if state == "ready":
            print(f"   '{self.name}' is already up — reusing it")
            # A settled stack is not a working environment: a failed deploy can
            # drop the database and still leave UPDATE_COMPLETE behind. Ask it
            # for the retailer list before trusting it.
            if not self.wait_until_serving(budget=60):
                raise EnvError(
                    f"'{self.name}' looks healthy but does not serve retailers.\n"
                    f"It is probably half torn down. Destroy it with the deployer:\n"
                    f"  node cli.js destroy --env {cfg.deployer_env_name(self.name)} --skip-username"
                )
            return self

        if state == "broken":
            raise EnvError(
                f"'{self.name}' is in state {self.stack_status()} and cannot be "
                f"deployed over.\nDestroy it with the deployer, then re-run:\n"
                f"  node cli.js destroy --env {cfg.deployer_env_name(self.name)} --skip-username"
            )

        self._deploy_and_wait(key)
        self.created = True
        return self

    def _deploy_and_wait(self, key: str) -> None:
        task = self._start_task(key)
        try:
            self._wait_for_task(task)
        except EnvError as first:
            # The deployer fails intermittently and says nothing useful about
            # it — the same command minutes later builds cleanly. Without one
            # retry every flake is a red run somebody investigates.
            print(f"   deploy failed ({first}) — retrying once")
            task = self._start_task(key)
            self._wait_for_task(task)

        # The task exits before CloudFormation finishes. Testing at that point
        # means testing against half-replaced lambdas.
        self.wait_for_stack()
        # And a finished stack still has its per-country domain caches to build.
        # An extension pointed at it now matches no retailer, and every test
        # reports "no popup" for a product that is fine.
        self.wait_until_serving(budget=cfg.READY_BUDGET)

    def _start_task(self, key: str) -> str:
        print(f"Deploying '{self.name}' "
              f"(backend={cfg.BACKEND_BRANCH}, frontend={cfg.FRONTEND_BRANCH})")
        overrides = [
            {"name": "ENV_NAME", "value": self.name},
            {"name": "DESTROY_AFTER_HOURS", "value": str(cfg.DESTROY_AFTER_HOURS)},
            {"name": "BRANCH", "value": cfg.BACKEND_BRANCH},
            {"name": "GITHUB_REPO", "value": cfg.BACKEND_REPO},
            {"name": "FRONTEND_BRANCH", "value": cfg.FRONTEND_BRANCH},
            # Not optional. The backend answers with an iframeUrl under this
            # environment's own path, so an environment without its own
            # frontend serves whatever CloudFront falls back to — production's
            # popup UI, at whatever version that happens to be — and the run
            # then reports on an API and a UI from different places.
            {"name": "SKIP_FRONTEND", "value": "false"},
            # Produces mock-extension-*.zip in S3, which is what the browser loads.
            {"name": "EXTENSION_ZIP_UPLOAD", "value": "true"},
            {"name": "FRONTEND_IDENTIFIER", "value": key},
        ]
        try:
            response = self.ecs.run_task(
                cluster=cfg.CLUSTER_ARN,
                taskDefinition=cfg.TASK_DEFINITION,
                launchType="FARGATE",
                networkConfiguration={"awsvpcConfiguration": {
                    "subnets": cfg.SUBNETS,
                    "securityGroups": cfg.SECURITY_GROUPS,
                    "assignPublicIp": "ENABLED",
                }},
                overrides={"containerOverrides": [
                    {"name": cfg.CONTAINER_NAME, "environment": overrides},
                ]},
            )
        except ClientError as e:
            raise EnvError(f"Could not start the deploy task: {e}") from e

        tasks = response.get("tasks", [])
        if not tasks:
            raise EnvError(f"ECS refused the task: {response.get('failures', [])}")
        arn = tasks[0]["taskArn"]
        print(f"   task {arn.rsplit('/', 1)[-1]} started")
        return arn

    def _wait_for_task(self, arn: str) -> None:
        started = time.time()
        while time.time() - started < cfg.DEPLOY_TIMEOUT:
            tasks = self.ecs.describe_tasks(cluster=cfg.CLUSTER_ARN, tasks=[arn]).get("tasks", [])
            if not tasks:
                raise EnvError(f"deploy task {arn} disappeared")
            task = tasks[0]
            status = task.get("lastStatus", "UNKNOWN")
            elapsed = int(time.time() - started)

            if status == "STOPPED":
                containers = task.get("containers", [])
                code = containers[0].get("exitCode", -1) if containers else -1
                if code == 0:
                    print(f"   deploy task finished in {elapsed}s")
                    return
                reason = containers[0].get("reason", "unknown") if containers else "no container"
                raise EnvError(f"deploy task exited {code}: {reason}")

            print(f"   {status} ({elapsed}s)")
            time.sleep(cfg.POLL_INTERVAL)

        raise EnvError(f"deploy did not finish within {cfg.DEPLOY_TIMEOUT}s")

    def wait_for_stack(self, timeout: int = None) -> str:
        """Wait until CloudFormation stops changing. Returns the settled status."""
        deadline = time.time() + (timeout or cfg.STACK_TIMEOUT)
        last = None
        while time.time() < deadline:
            status = self.stack_status()
            if status is None or not status.endswith("_IN_PROGRESS"):
                print(f"   stack settled: {status}")
                return status
            if status != last:
                print(f"   stack {status}, waiting...")
                last = status
            time.sleep(cfg.POLL_INTERVAL)
        return self.stack_status()

    def wait_until_serving(self, budget: int) -> bool:
        """Wait for the environment to answer with a retailer list.

        The retailer list is what a popup depends on, so that is what "ready"
        has to mean. Probing it uses the same URL, base path and key the
        extension will use, which is the only way to learn that the combination
        this run needs actually exists.
        """
        url = f"{cfg.env_api_url(self.name)}/domains"
        key = cfg.api_key(cfg.PLATFORM)
        print(f"   waiting for '{self.name}' to serve retailers (up to {budget}s)...")
        deadline = time.time() + budget
        while time.time() < deadline:
            try:
                r = requests.get(url, params={"timestamp": str(int(time.time() * 1000))},
                                 headers={"x-api-key": key}, timeout=cfg.HTTP_TIMEOUT)
                if r.status_code == 200 and (r.json() or {}).get("relevantDomains"):
                    print(f"   serving retailers after {int(budget - (deadline - time.time()))}s")
                    return True
            except Exception:
                pass        # not up yet; only the deadline ends this
            time.sleep(cfg.READY_POLL_INTERVAL)
        print(f"   '{self.name}' was not serving retailers within {budget}s")
        return False

    # ── the extension it built ──────────────────────────────────────

    def download_extension(self, into: Path) -> Path:
        """Fetch the mock extension this environment built, unpacked and ready to load."""
        s3 = boto3.client("s3", region_name=self.region)
        prefix = f"{cfg.EXTENSION_S3_PREFIX}/{self.name}/"
        print(f"Fetching extension from s3://{cfg.EXTENSION_BUCKET}/{prefix}")

        try:
            listing = s3.list_objects_v2(Bucket=cfg.EXTENSION_BUCKET, Prefix=prefix, MaxKeys=50)
        except ClientError as e:
            raise EnvError(f"Could not list the extension bucket: {e}") from e

        zips = [o for o in listing.get("Contents", []) if o["Key"].endswith(".zip")]
        if not zips:
            raise EnvError(
                f"No extension zip under s3://{cfg.EXTENSION_BUCKET}/{prefix} — the "
                f"environment was deployed without EXTENSION_ZIP_UPLOAD."
            )

        newest = max(zips, key=lambda o: o["LastModified"])
        print(f"   using {newest['Key']}")
        buffer = io.BytesIO()
        s3.download_fileobj(cfg.EXTENSION_BUCKET, newest["Key"], buffer)

        if into.exists():
            shutil.rmtree(into)
        into.mkdir(parents=True, exist_ok=True)
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as zf:
            zf.extractall(into)

        return find_manifest_dir(into)


def find_manifest_dir(root: Path) -> Path:
    """The directory Chrome can load, wherever the zip put it."""
    if (root / "manifest.json").exists():
        return root
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "manifest.json").exists():
            return child
    raise EnvError(f"No manifest.json under {root}")
