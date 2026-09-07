import json
import os
import requests
import config

RAILWAY_GRAPHQL_URL = "https://backboard.railway.app/graphql/v2"


def load_state():
    raw = os.environ.get(config.STATE_VAR_NAME, "")
    if not raw:
        return {"games": {}}
    try:
        return json.loads(raw)
    except Exception:
        return {"games": {}}


def save_state(state):
    if not (config.RAILWAY_TOKEN and config.RAILWAY_PROJECT_ID
            and config.RAILWAY_SERVICE_ID and config.RAILWAY_ENVIRONMENT_ID):
        print("Railway persistence not configured — state will not survive a restart.")
        return
    value = json.dumps(state)
    query = "mutation($input: VariableUpsertInput!) { variableUpsert(input: $input) }"
    variables = {
        "input": {
            "projectId": config.RAILWAY_PROJECT_ID,
            "environmentId": config.RAILWAY_ENVIRONMENT_ID,
            "serviceId": config.RAILWAY_SERVICE_ID,
            "name": config.STATE_VAR_NAME,
            "value": value,
        }
    }
    headers = {"Authorization": f"Bearer {config.RAILWAY_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = requests.post(RAILWAY_GRAPHQL_URL, json={"query": query, "variables": variables},
                              headers=headers, timeout=15)
        if not resp.ok:
            print("Railway state save failed:", resp.status_code, resp.text[:300])
    except Exception as e:
        print("Railway state save error:", e)
