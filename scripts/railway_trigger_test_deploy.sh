#!/usr/bin/env bash
# Trigger xagent-test deploy for a specific git commit (Railway GraphQL).
# Used by .github/workflows/deploy-xagent-test.yml after git push.
set -euo pipefail

RAILWAY_TOKEN="${RAILWAY_TOKEN:?RAILWAY_TOKEN required}"

COMMIT_SHA="${1:-${GITHUB_SHA:-}}"
BRANCH="${2:-${GITHUB_REF_NAME:-feature/entry-guard-15m}}"

if [[ -z "$COMMIT_SHA" ]]; then
  echo "ERROR: commit SHA required (arg1, GITHUB_SHA, or git HEAD)"
  exit 1
fi

PROJECT_ID="${RAILWAY_PROJECT_ID:-0bec1abb-35cc-400c-ab4e-4c4dcfec9373}"
SERVICE_ID="${RAILWAY_SERVICE_ID:-0ad364f2-ea33-4b7c-ba50-b71a71a87711}"
ENV_ID="${RAILWAY_ENV_ID:-6ef480bb-8dd7-4da8-a48c-ce2c52655316}"
SHORT_SHA="${COMMIT_SHA:0:7}"

echo "=== Railway deploy trigger ==="
echo "Service:  xagent-test (test)"
echo "Branch:   ${BRANCH}"
echo "Commit:   ${SHORT_SHA} (${COMMIT_SHA})"

export PROJECT_ID SERVICE_ID ENV_ID COMMIT_SHA BRANCH RAILWAY_TOKEN
RESULT="$(python3 - <<'PY'
import json
import os
import sys
token = os.environ["RAILWAY_TOKEN"]
project_id = os.environ["PROJECT_ID"]
service_id = os.environ["SERVICE_ID"]
env_id = os.environ["ENV_ID"]
commit_sha = os.environ["COMMIT_SHA"]
branch = os.environ["BRANCH"]
short_sha = commit_sha[:7]

def gql(query: str, variables: dict | None = None) -> dict:
    import subprocess

    body = json.dumps({"query": query, "variables": variables or {}})
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            "https://backboard.railway.com/graphql/v2",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"]))
    return data["data"]

deploy_id = gql(
    """
    mutation($serviceId: String!, $environmentId: String!, $commitSha: String!) {
      serviceInstanceDeployV2(
        serviceId: $serviceId
        environmentId: $environmentId
        commitSha: $commitSha
      )
    }
    """,
    {
        "serviceId": service_id,
        "environmentId": env_id,
        "commitSha": commit_sha,
    },
)["serviceInstanceDeployV2"]

for name, value in [
    ("GIT_COMMIT", short_sha),
    ("GIT_BRANCH", branch),
    ("BOT_STACK", "test"),
]:
    gql(
        """
        mutation($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """,
        {
            "input": {
                "projectId": project_id,
                "environmentId": env_id,
                "serviceId": service_id,
                "name": name,
                "value": value,
                "skipDeploys": True,
            }
        },
    )

print(deploy_id)
PY
)"

echo "Deployment started: ${RESULT}"
echo "Done. Track: railway deployment list --service xagent-test --environment test"