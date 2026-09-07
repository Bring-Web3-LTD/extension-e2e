# Turning on the button

One-time setup, done once by somebody with AWS admin rights. Until it is done,
"Release check" appears in the Actions tab and fails immediately: GitHub has no
permission to bring an environment up.

Everything else is already in the repo — the workflow, the tests, the
environment logic. This is only about access.

Roughly twenty minutes.

---

## What it is actually for

The workflow needs to do four things in AWS:

| It does this | Because |
|---|---|
| Runs the `dev-env-deployer` ECS task | that is what builds the temporary environment |
| Watches a CloudFormation stack | to know when the environment is ready |
| Reads one S3 bucket | that is where the environment leaves the built extension |
| Reads one Secrets Manager secret | the deployer's database credentials |

No writes to anything else, and nothing outside the dev account.

---

## The pieces

Fill these in as you go — step 3 needs the value from step 2.

| | Value |
|---|---|
| AWS account | `083114526744` |
| Region | `eu-central-1` |
| ECS cluster | `dev-env-deployer` |
| S3 bucket | `bring-popup-iframe-tests` |
| GitHub repo | `Bring-Web3-LTD/extension-e2e` |

---

## 1. Let GitHub identify itself to AWS

Skip if another repo in this account already uses GitHub Actions with OIDC —
the provider is account-wide and only needs creating once.

**IAM → Identity providers → Add provider**

- Provider type: **OpenID Connect**
- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

Add provider.

This is what lets a workflow prove which repository it is running from, so no
access key or password is ever stored in GitHub.

---

## 2. Create the role that workflow may assume

**IAM → Roles → Create role → Web identity**

- Identity provider: the one from step 1
- Audience: `sts.amazonaws.com`
- GitHub organization: `Bring-Web3-LTD`
- GitHub repository: `extension-e2e`

Next, skip attaching a policy for now, and name the role:

```
extension-e2e-release-check
```

Create it, then open it and check **Trust relationships**. It must name this
repository and nothing wider:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::083114526744:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:Bring-Web3-LTD@122225882/extension-e2e@1359257651:*"
      }
    }
  }]
}
```

The `sub` line is the security boundary: only workflows in this repository can
assume the role. A `*` on its own there would let **any** repository on GitHub
assume it — check this line before moving on.

The numbers in it are not decoration. This organisation has OIDC subject
customisation turned on, so GitHub sends the org and repo *ids* alongside their
names — `repo:Bring-Web3-LTD@122225882/extension-e2e@1359257651:...`. Names can
be changed and reused; the ids cannot, which is why the setting exists. A policy
written with the names alone matches nothing, and the run fails with
`Not authorized to perform sts:AssumeRoleWithWebIdentity` while every part of
the setup looks correct.

If those ids ever need checking, CloudTrail has them: look up
`AssumeRoleWithWebIdentity` and read `userIdentity.principalId` on a failed
attempt. It is the exact string the policy has to match.

Then **Add permissions → Create inline policy → JSON**, paste this, and name it
`extension-e2e-release-check`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RunTheDeployer",
      "Effect": "Allow",
      "Action": ["ecs:RunTask", "ecs:DescribeTasks", "ecs:DescribeTaskDefinition"],
      "Resource": "*",
      "Condition": {
        "ArnEquals": {
          "ecs:cluster": "arn:aws:ecs:eu-central-1:083114526744:cluster/dev-env-deployer"
        }
      }
    },
    {
      "Sid": "LetTheDeployerUseItsOwnRoles",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": [
        "arn:aws:iam::083114526744:role/dev-env-deployer-task-role",
        "arn:aws:iam::083114526744:role/dev-env-deployer-execution-role"
      ],
      "Condition": {
        "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" }
      }
    },
    {
      "Sid": "WatchTheEnvironmentComeUp",
      "Effect": "Allow",
      "Action": [
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents"
      ],
      "Resource": "*"
    },
    {
      "Sid": "FetchTheBuiltExtension",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::bring-popup-iframe-tests",
        "arn:aws:s3:::bring-popup-iframe-tests/*"
      ]
    },
    {
      "Sid": "DeployerDatabaseCredentials",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:eu-central-1:083114526744:secret:mySqlSharedDevRds*"
    }
  ]
}
```

`iam:PassRole` names the deployer's two roles rather than allowing any role in
the account. Starting a task means handing it a role to run as, so a `*` here
plus the `ecs:RunTask` above would be permission to run something in that
cluster as *any* role ECS can assume, including one far more privileged than
the deployer's. The two named here are the ones the task definition actually
uses.

Copy the role's **ARN** from the top of the page. It looks like:

```
arn:aws:iam::083114526744:role/extension-e2e-release-check
```

---

## 3. Give the repository its two secrets

**GitHub → the `extension-e2e` repo → Settings → Secrets and variables →
Actions → New repository secret**

| Name | Value |
|---|---|
| `AWS_ROLE_ARN` | the ARN from step 2 |
| `ECKO_API_KEY` | the extension's API key — the same value in the local `.env` |

Names must match exactly; the workflow looks them up by name.

### Optional — the notification tests

Three more, only needed to seed reward rows into the temporary environment's
database. Without them those tests skip and say why, and everything else runs.

| Name | Value |
|---|---|
| `DB_SSH_KEY` | the bastion's private key, whole file including the BEGIN/END lines |
| `DB_SSH_HOST` | the bastion host |
| `DB_SSH_USER` | the bastion user |

---

## 4. Press it

**Actions → Release check → Run workflow.**

Defaults are `main` and `main`, and the environment is `qa-extension`. Change
them to test a branch before it merges.

About half an hour, and you do not have to watch. When it finishes:

- the top of the run says **PASS**, or lists exactly what broke
- **Artifacts** holds a screenshot, a storage dump and a Playwright trace for
  each failure

Open a trace with `npx playwright show-trace trace.zip` — it replays the whole
test, every click and network call.

---

## When it does not work

**"No jobs were run"** — the workflow file did not parse. It is not your
setup; the file changed and broke.

**`Not authorized to perform sts:AssumeRoleWithWebIdentity`** — the trust
policy in step 2 does not match this repository. Check the `sub` line for a
typo in the org or repo name.

**`is not authorized to perform: ecs:RunTask`** — the inline policy did not
attach, or attached to a different role than the one in `AWS_ROLE_ARN`.

**Environment fails to come up** — the deployer itself, not this tool. Its
own logs are in the ECS task; the run prints the task id.

**Lots of tests skipped** — a shop answered with a bot check instead of its
site. The run says which. Not a failure of the extension, but those checks did
not happen; see the retailer list in `bring/retailers.py`.
