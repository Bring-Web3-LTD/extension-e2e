"""Everything the run needs to find AWS, the environment and the extension.

The ARNs are the same ones the QA automation deploys with — this tool talks to
the existing dev-env-deployer rather than standing up infrastructure of its own.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")

# dev-env-deployer: the ECS task that builds a whole environment from branches.
CLUSTER_ARN = "arn:aws:ecs:eu-central-1:083114526744:cluster/dev-env-deployer"
TASK_DEFINITION = "dev-env-deployer"
CONTAINER_NAME = "dev-env-deployer"
SUBNETS = ["subnet-0a1fc76a7776f48a6"]
SECURITY_GROUPS = ["sg-04c54631ba41aace0"]

# Where the deployer drops the mock extension it built for the environment.
EXTENSION_BUCKET = "bring-popup-iframe-tests"
EXTENSION_S3_PREFIX = "extensions"

API_BASE_URL = "https://api.bringweb3.io"

BACKEND_REPO = "Bring-Web3-LTD/bringweb3"
FRONTEND_REPO = "Bring-Web3-LTD/chromeExtension"

# Both branches are pinned to main for now: this tool answers "is the extension
# healthy today", not "what did this PR change". Overridable so it can answer
# the second question later without a code change.
BACKEND_BRANCH = os.getenv("BRING_BACKEND_BRANCH", "main")
FRONTEND_BRANCH = os.getenv("BRING_FRONTEND_BRANCH", "main")

ENV_NAME = os.getenv("BRING_ENV_NAME", "qa-e2e")
PLATFORM = os.getenv("BRING_PLATFORM", "ecko")

# The environment destroys itself; a run that crashes must not leak a stack.
DESTROY_AFTER_HOURS = int(os.getenv("BRING_DESTROY_AFTER_HOURS", "8"))

DEPLOY_TIMEOUT = 900          # the deployer task itself
STACK_TIMEOUT = 1800          # CloudFormation finishes after the task exits
READY_BUDGET = int(os.getenv("BRING_READY_BUDGET", "240"))
POLL_INTERVAL = 30
READY_POLL_INTERVAL = 5
HTTP_TIMEOUT = 15


def api_key(platform: str = PLATFORM) -> str:
    """The platform key, sent to the deployer as FRONTEND_IDENTIFIER.

    Read here rather than at the deploy call so a missing key fails in a
    sentence instead of fifteen minutes into a Fargate task.
    """
    return os.getenv(f"{platform.upper()}_API_KEY", "")


def env_api_url(env_name: str = None) -> str:
    """The extension API this environment answers on."""
    return f"{API_BASE_URL}/{env_name or ENV_NAME}/v1/extension"


# ── deployer naming (helpers.js) ────────────────────────────────────
# The deployer appends '-temp' and caps the name at 36 characters, and the
# CloudFormation stack is named after the result. Looking a stack up under the
# raw name finds nothing and deploys a second one over the first.
TEMP_SUFFIX = "-temp"
MAX_ENV_LEN = 36


def deployer_env_name(raw: str) -> str:
    name = raw if raw.endswith(TEMP_SUFFIX) else raw + TEMP_SUFFIX
    if len(name) <= MAX_ENV_LEN:
        return name
    return name[: MAX_ENV_LEN - len(TEMP_SUFFIX)] + TEMP_SUFFIX


def stack_name(raw: str) -> str:
    return f"{deployer_env_name(raw)}-stack"
