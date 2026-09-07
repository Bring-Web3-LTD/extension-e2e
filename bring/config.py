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

# The name is load-bearing in four places at once, so it is worth writing down
# what it becomes:
#     API base path   api.bringweb3.io/qa-extension/v1/extension
#     stack           qa-extension-temp-stack
#     database        qa_extension_temp        (hyphens become underscores)
#     extension zip   s3://.../extensions/qa-extension/
# Hyphens, not underscores: CloudFormation refuses an underscore in a stack
# name ("must satisfy [a-zA-Z][-a-zA-Z0-9]*"), so `qa_extension` would fail
# fifteen minutes into a deploy rather than at validation.
ENV_NAME = os.getenv("BRING_ENV_NAME", "qa-extension")
PLATFORM = os.getenv("BRING_PLATFORM", "ecko")

# The environment destroys itself; a run that crashes must not leak a stack.
DESTROY_AFTER_HOURS = int(os.getenv("BRING_DESTROY_AFTER_HOURS", "8"))

DEPLOY_TIMEOUT = 900          # the deployer task itself
STACK_TIMEOUT = 1800          # CloudFormation finishes after the task exits
# A fresh environment waits this out unconditionally before anything is
# tested. The domain caches are built after the stack finishes, per country and
# per SDK version, so no single probe can prove the combination this run needs
# is among the ones that are ready yet.
SETTLE_SECONDS = int(os.getenv("BRING_SETTLE_SECONDS", "240"))
# And then it is confirmed: how long to keep asking for the retailer list
# before giving up and running anyway.
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


# The wallets these tests use, for the platform under test.
#
# Not invented, and not the one baked into the mock extension either: that is a
# Cardano address (`addr1...`), and this suite runs against ecko, whose
# addresses are Kadena public keys (`k:...`). An address from the wrong chain is
# not a cosmetic mismatch — a purchase row seeded against it is a row the server
# will never match to the wallet the extension reports, so the notification
# under test is correctly never shown and the test blames the product for it.
#
# Both are overridable, because the platform decides the shape.
WALLET = os.getenv("BRING_WALLET", "k:7dcf6f1f2c8f4a02d4b29eb2ae34e41718137f1e8d28157779de09ac57754d6b")

# A second one, used only to prove that switching accounts changes what the
# offer shows. Never seeded against, so it only has to be different.
OTHER_WALLET = os.getenv("BRING_WALLET_OTHER", "k:29647637d1b93d284f8bf83430136b665412b93db2371ed1b7897d5fa2888598")
