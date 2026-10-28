"""Cloud Run function: turn billing off for this project once its budget is passed.

The budget publishes a notice to Pub/Sub several times a day, with the month's
cost so far. When the cost reaches the budget, this unlinks the project from its
billing account (Google's "cap costs" pattern): everything in the project stops.
Other projects and the billing account itself are untouched.
`bgrules deploy --budget <amount>` turns billing back on.
"""

import base64
import json
import os

try:
    import functions_framework
except ImportError:  # tests, outside Google Cloud
    functions_framework = None

BILLING_API = "https://cloudbilling.googleapis.com/v1"


def over_budget(notice: dict) -> bool:
    return notice["costAmount"] >= notice["budgetAmount"]


def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


def handle(notice: dict, project: str, session) -> str:
    """What happened, for the log."""
    cost, budget = notice["costAmount"], notice["budgetAmount"]
    if not over_budget(notice):
        return f"Cost {cost} is under the budget {budget}: nothing to do."
    info = session.get(f"{BILLING_API}/projects/{project}/billingInfo")
    info.raise_for_status()
    if not info.json().get("billingEnabled"):
        return f"Cost {cost} reached the budget {budget}; billing is already off."
    # An empty billing account name unlinks the project from its billing account.
    done = session.put(f"{BILLING_API}/projects/{project}/billingInfo", json={"billingAccountName": ""})
    done.raise_for_status()
    return f"Cost {cost} reached the budget {budget}: billing turned off for {project}."


def stop_billing(cloud_event) -> None:
    notice = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
    print(handle(notice, os.environ["GOOGLE_CLOUD_PROJECT"], _session()))


if functions_framework is not None:
    stop_billing = functions_framework.cloud_event(stop_billing)
