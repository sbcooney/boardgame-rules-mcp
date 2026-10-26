"""`bgrules deploy`: a private copy of the user's games on Google Cloud Run.

The copy is reached from the Claude mobile app through a custom connector whose
URL holds a secret token. Each run packages ~/.bgrules (PDFs and index) with this
package, builds it in Google's cloud (`gcloud run deploy --source`), and makes sure
the cost limits are in place: one small instance at most, scale to zero, and a
budget whose Pub/Sub notice triggers a function that turns billing off for the
project once the budget is passed.

Everything runs as the signed-in gcloud user. No API key or service-account key
is ever created; the only secret is the connector token, kept in
~/.bgrules-remote (BGRULES_REMOTE_HOME), outside the repo.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from importlib import metadata
from pathlib import Path

from . import store

SERVICE = "bgrules"
DEFAULT_REGION = "us-central1"
DEFAULT_BUDGET = 5
TOPIC = "bgrules-budget"
STOP_FUNCTION = "bgrules-budget-stop"
STOP_ACCOUNT = "bgrules-budget-stop"
REPOSITORY = "cloud-run-source-deploy"  # where `gcloud run deploy --source` puts images

APIS = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    # the hard stop: budget -> Pub/Sub -> function -> Cloud Billing API
    "billingbudgets.googleapis.com",
    "pubsub.googleapis.com",
    "cloudfunctions.googleapis.com",
    "eventarc.googleapis.com",
    "cloudbilling.googleapis.com",
]

# The service's ceiling: one small instance, nothing running (or billed) while idle.
RUN_LIMITS = ["--min-instances", "0", "--max-instances", "1", "--cpu", "1", "--memory", "512Mi",
              "--concurrency", "4", "--timeout", "60"]

DOCKERFILE = """\
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bgrules /app/bgrules
COPY home /data
ENV BGRULES_HOME=/data PYTHONPATH=/app
CMD ["python", "-m", "bgrules.cli", "serve", "--http"]
"""

# Each deploy uploads the rulebooks again and builds a new image: keep only the newest.
SOURCE_LIFECYCLE = {"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}
IMAGE_CLEANUP = [
    {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"tagState": "any", "olderThan": "1d"}},
    # Two, not one: the newest image may be mid-deploy while the previous one still serves.
    {"name": "keep-newest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
]


class DeployError(Exception):
    pass


def remote_home() -> Path:
    return Path(os.environ.get("BGRULES_REMOTE_HOME", Path.home() / ".bgrules-remote"))


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    path.chmod(0o600)


def saved_token() -> str | None:
    """The connector's secret, made on the first deploy and reused after, so the link
    never changes."""
    path = remote_home() / "token"
    return path.read_text().strip() if path.exists() else None


def make_token() -> str:
    return secrets.token_urlsafe(32)  # 43 characters, 256 bits


def save_token(value: str) -> None:
    _write_private(remote_home() / "token", value + "\n")


def load_config() -> dict:
    path = remote_home() / "config.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_config(config: dict) -> None:
    _write_private(remote_home() / "config.json", json.dumps(config, indent=2) + "\n")


# Not direct dependencies, but what `serve --http` runs on: pinned like them.
HTTP_SERVER = ["uvicorn", "starlette"]


def requirements() -> list[str]:
    """This package's dependencies and its HTTP server, pinned to the versions
    installed here, minus the macOS-only OCR (indexing is done on the user's
    computer, not the server)."""
    out = []
    for req in [*(metadata.requires("boardgame-rules-mcp") or []), *HTTP_SERVER]:
        if "darwin" in req or "extra ==" in req:
            continue
        name, extras = re.match(r"([A-Za-z0-9._-]+)(\[[^\]]*\])?", req).groups()
        out.append(f"{name}{extras or ''}=={metadata.version(name)}")
    return out


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)  # the PDFs are large; a hard link costs nothing
    except OSError:
        shutil.copy2(src, dst)


def build_context(dest: Path, home: Path | None = None) -> list[str]:
    """Fill `dest` with what Cloud Build needs: a Dockerfile, this package, and a copy
    of the user's games (PDFs, index, their own manifest). Returns the games."""
    home = home or store.home()
    index = home / "index.sqlite"
    if not index.exists():
        raise DeployError(f"No games installed in {home}. Add one first, e.g. `bgrules add catan`.")
    db = sqlite3.connect(index)
    try:
        games = sorted({r[0] for r in db.execute("SELECT game FROM docs")})
        if not games:
            raise DeployError(f"No games installed in {home}. Add one first, e.g. `bgrules add catan`.")
        data = dest / "home"
        data.mkdir(parents=True)
        out = sqlite3.connect(data / "index.sqlite")
        db.backup(out)  # a consistent snapshot, even if the index is being written
        out.close()
    finally:
        db.close()
    if (home / "manifest.yaml").exists():
        shutil.copy2(home / "manifest.yaml", data / "manifest.yaml")
    shutil.copytree(home / "pdfs", data / "pdfs", copy_function=_link_or_copy)
    shutil.copytree(Path(__file__).parent, dest / "bgrules", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dest / "requirements.txt").write_text("\n".join(requirements()) + "\n")
    (dest / "Dockerfile").write_text(DOCKERFILE)
    (dest / ".gcloudignore").write_text("# upload everything here\n")
    return games


class GCloud:
    """Runs gcloud commands as the signed-in user."""

    def __init__(self, project: str | None = None):
        self.project = project

    def __call__(self, *args: str, check: bool = True, quiet: bool = True) -> subprocess.CompletedProcess:
        cmd = ["gcloud", *args]
        if self.project:
            cmd.append(f"--project={self.project}")
        if quiet:
            cmd.append("--quiet")
        for attempt in range(6):
            result = subprocess.run(cmd, capture_output=True, text=True)
            # A service account just created can take a few seconds to be usable in IAM bindings.
            if result.returncode == 0 or "add-iam-policy-binding" not in args or "does not exist" not in result.stderr:
                break
            time.sleep(5)
        if check and result.returncode != 0:
            raise DeployError(f"`{' '.join(cmd[:4])} ...` failed:\n{result.stderr.strip()}")
        return result

    def value(self, *args: str) -> str:
        return self(*args).stdout.strip()

    def ok(self, *args: str) -> bool:
        return self(*args, check=False).returncode == 0


def _step(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _ask(prompt: str, default: str) -> str:
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def _choose_billing_account(gcloud: GCloud) -> str:
    accounts = gcloud.value("billing", "accounts", "list", "--filter=open=true", "--format=value(name,displayName)").splitlines()
    if not accounts:
        raise DeployError("Your Google account has no open billing account. Create one at "
                          "https://console.cloud.google.com/billing, then run this again.")
    ids = [line.split("\t")[0].removeprefix("billingAccounts/") for line in accounts]
    if len(ids) == 1:
        return ids[0]
    for i, line in enumerate(accounts, 1):
        _step(f"  {i}. {line.replace(chr(9), '  ')}")
    choice = _ask("Which billing account should pay for it?", "1")
    try:
        return ids[int(choice) - 1]
    except (ValueError, IndexError):
        raise DeployError(f"No billing account {choice!r}.") from None


def setup_project(gcloud: GCloud, config: dict, budget_given: bool = False) -> None:
    """A project of its own, linked to billing, with only the APIs this needs."""
    project = config["project"]
    if not gcloud.ok("projects", "describe", project):
        _step(f"Creating Google Cloud project {project}")
        gcloud("projects", "create", project, "--name=Board game rules")
    billing = gcloud.value("billing", "projects", "describe", project, "--format=value(billingAccountName)")
    if not billing:
        if config.get("deployed_project") == project:
            # Most likely the hard stop. This month's cost is still at the budget, so billing turned
            # back on would be turned off again at the budget's next notice, within hours.
            if not budget_given:
                raise DeployError(
                    f"Billing for project {project} is off, most likely because this month's spending reached the "
                    f"budget ({config['budget']} {config['currency']}) and the hard stop turned it off. Turned back on "
                    "now, it would be stopped again within hours. Once a new month has started, or to raise the "
                    "budget, run: bgrules deploy --budget <amount>")
            _step("Turning billing back on for this project")
        _step(f"Linking billing account {config['billing_account']}")
        gcloud("billing", "projects", "link", project, f"--billing-account={config['billing_account']}")
    gcloud.project = project
    _step("Enabling Cloud Run, Cloud Build, Artifact Registry and the budget's hard stop")
    gcloud("services", "enable", *APIS)
    number = gcloud.value("projects", "describe", project, "--format=value(projectNumber)")
    config["project_number"] = number
    # Builds run as the Compute Engine default service account; it needs to build and push images.
    builder = f"{number}-compute@developer.gserviceaccount.com"
    if gcloud.ok("iam", "service-accounts", "describe", builder):
        gcloud("projects", "add-iam-policy-binding", project, f"--member=serviceAccount:{builder}",
               "--role=roles/run.builder", "--condition=None", "--format=none")


def setup_hard_stop(gcloud: GCloud, config: dict, region: str) -> None:
    """Budget -> Pub/Sub -> function that unlinks billing from this project
    (Google's "cap costs" pattern). Checked, and repaired, on every deploy."""
    project, account = config["project"], config["billing_account"]
    if not gcloud.ok("pubsub", "topics", "describe", TOPIC):
        gcloud("pubsub", "topics", "create", TOPIC)
    topic = f"projects/{project}/topics/{TOPIC}"

    name = f"bgrules {project}"
    amount = f"--budget-amount={config['budget']}{config['currency']}"
    budgets = gcloud(f"--billing-project={project}", "billing", "budgets", "list", f"--billing-account={account}",
                     "--format=json").stdout
    budget = next((b for b in json.loads(budgets or "[]") if b.get("displayName") == name), None)
    if budget is None:
        _step(f"Creating a {config['budget']} {config['currency']}/month budget with emails at 50% and 90%, "
              "and a hard stop at 100%")
        gcloud(f"--billing-project={project}", "billing", "budgets", "create", f"--billing-account={account}",
               f"--display-name={name}", amount, f"--filter-projects=projects/{project}",
               "--threshold-rule=percent=0.5", "--threshold-rule=percent=0.9", "--threshold-rule=percent=1.0",
               f"--notifications-rule-pubsub-topic={topic}")
    elif _budget_amount(budget) != float(config["budget"]) or budget.get("notificationsRule", {}).get("pubsubTopic") != topic:
        _step(f"Updating the budget to {config['budget']} {config['currency']}/month, with the hard stop")
        gcloud(f"--billing-project={project}", "billing", "budgets", "update", budget["name"],
               f"--billing-account={account}", amount, f"--notifications-rule-pubsub-topic={topic}")

    # The function's own identity, allowed to do one thing: unlink billing from this project.
    # Cloud Billing asks for Project Billing Manager on the project plus Billing Account Viewer
    # on the account (viewing, not changing it). It can also receive the budget's Pub/Sub events.
    member = f"serviceAccount:{STOP_ACCOUNT}@{project}.iam.gserviceaccount.com"
    if not gcloud.ok("iam", "service-accounts", "describe", f"{STOP_ACCOUNT}@{project}.iam.gserviceaccount.com"):
        gcloud("iam", "service-accounts", "create", STOP_ACCOUNT, "--display-name=Turns billing off when the budget is passed")
    for role in ("roles/billing.projectManager", "roles/run.invoker", "roles/eventarc.eventReceiver"):
        gcloud("projects", "add-iam-policy-binding", project, f"--member={member}", f"--role={role}",
               "--condition=None", "--format=none")
    gcloud("billing", "accounts", "add-iam-policy-binding", account, f"--member={member}", "--role=roles/billing.viewer",
           "--format=none")

    # Deployed again if it's missing, broken, or older than this package's copy.
    source = Path(__file__).parent / "budget_stop"
    digest = hashlib.sha256(b"".join(f.read_bytes() for f in sorted(source.glob("*")) if f.is_file())).hexdigest()
    state = gcloud("functions", "describe", STOP_FUNCTION, f"--region={region}", "--format=value(state)", check=False)
    if state.returncode != 0 or state.stdout.strip() != "ACTIVE" or config.get("stop_function") != digest:
        _step("Deploying the hard-stop function")
        gcloud("functions", "deploy", STOP_FUNCTION, "--gen2", "--runtime=python313", f"--region={region}",
               f"--source={source}", "--entry-point=stop_billing",
               f"--trigger-topic={TOPIC}", f"--service-account={member.split(':')[1]}",
               f"--trigger-service-account={member.split(':')[1]}", f"--set-env-vars=GOOGLE_CLOUD_PROJECT={project}",
               "--max-instances=1", "--memory=256Mi", "--no-allow-unauthenticated")
        config["stop_function"] = digest


def _budget_amount(budget: dict) -> float:
    amount = budget.get("amount", {}).get("specifiedAmount", {})
    return int(amount.get("units", 0)) + amount.get("nanos", 0) / 1e9


def _parse_budget(amount: str) -> int | float:
    try:
        budget = float(amount.strip().lstrip("$€£"))
    except ValueError:
        raise DeployError(f"Not an amount: {amount!r}") from None
    if budget <= 0:
        raise DeployError("The budget must be more than 0.")
    return int(budget) if budget.is_integer() else budget


def setup_cleanup(gcloud: GCloud, region: str, workdir: Path) -> None:
    """Don't let old uploads and images pile up in storage."""
    lifecycle = workdir / "lifecycle.json"
    lifecycle.write_text(json.dumps(SOURCE_LIFECYCLE))
    buckets = gcloud.value("storage", "buckets", "list", "--format=value(name)").split()
    for bucket in buckets:
        if bucket.startswith("run-sources-") or bucket.endswith("_cloudbuild"):
            gcloud("storage", "buckets", "update", f"gs://{bucket}", f"--lifecycle-file={lifecycle}")
    policy = workdir / "cleanup.json"
    policy.write_text(json.dumps(IMAGE_CLEANUP))
    if gcloud.ok("artifacts", "repositories", "describe", REPOSITORY, f"--location={region}"):
        gcloud("artifacts", "repositories", "set-cleanup-policies", REPOSITORY, f"--location={region}",
               f"--policy={policy}", "--no-dry-run")


def deploy(new_token: bool = False, project: str | None = None, region: str | None = None,
           budget: str | None = None) -> str:
    """Deploy (or redeploy) the private instance. Returns the connector URL."""
    if not shutil.which("gcloud"):
        raise DeployError("gcloud isn't installed. On a Mac: brew install --cask gcloud-cli "
                          "(or see https://cloud.google.com/sdk/docs/install), then run: gcloud init")
    gcloud = GCloud()
    if not gcloud.value("auth", "list", "--filter=status:ACTIVE", "--format=value(account)"):
        raise DeployError("gcloud isn't signed in. Run: gcloud init")

    config = load_config()
    if project:
        config["project"] = project
    config.setdefault("project", f"bgrules-{secrets.token_hex(3)}")
    config["region"] = region = region or config.get("region", DEFAULT_REGION)
    if "billing_account" not in config:
        config["billing_account"] = _choose_billing_account(gcloud)
    if "currency" not in config:
        # A budget is in its billing account's currency.
        config["currency"] = gcloud.value("billing", "accounts", "describe", config["billing_account"],
                                          "--format=value(currencyCode)") or "USD"
    if budget is not None:
        config["budget"] = _parse_budget(budget)
    elif "budget" not in config:
        config["budget"] = _parse_budget(_ask(
            f"Monthly budget in {config['currency']}; billing for the project is turned off when it's passed",
            str(DEFAULT_BUDGET)))
    save_config(config)

    setup_project(gcloud, config, budget_given=budget is not None)
    save_config(config)
    setup_hard_stop(gcloud, config, region)
    save_config(config)

    # A new token is saved only once the service has it, so a failed deploy leaves the old link working.
    secret = None if new_token else saved_token()
    fresh = secret is None
    secret = secret or make_token()
    with tempfile.TemporaryDirectory(prefix="bgrules-deploy-") as tmp:
        tmp = Path(tmp)
        games = build_context(tmp / "source")
        env_file = tmp / "env.yaml"  # outside the uploaded folder
        _write_private(env_file, json.dumps({"BGRULES_TOKEN": secret}) + "\n")  # JSON is YAML
        _step(f"Uploading {len(games)} games and building the server in Google Cloud (a few minutes)")
        gcloud("run", "deploy", SERVICE, f"--source={tmp / 'source'}", f"--region={region}", "--allow-unauthenticated",
               *RUN_LIMITS, f"--env-vars-file={env_file}")
        if fresh:
            save_token(secret)
        setup_cleanup(gcloud, region, tmp)
    url = gcloud.value("run", "services", "describe", SERVICE, f"--region={region}", "--format=value(status.url)")
    connector = f"{url}/{secret}/mcp"
    same_link = not fresh and config.get("service_url") == url
    config.update(deployed_project=config["project"], service_url=url)
    save_config(config)

    print(f"\nGoogle Cloud project: {config['project']} (region {region})")
    print(f"Games on your private instance: {', '.join(games)}")
    print("\nConnector URL (keep it private: it gives access to your rulebooks):")
    print(connector)
    if same_link:
        print("\nSame link as before: the connector on claude.ai keeps working.")
    else:
        print("\nNew link: add it on claude.ai (Settings > Connectors > Add custom connector), replacing any "
              "earlier one. It then shows in the mobile app.")
    return connector
