"""The private remote instance: `serve --http`, its token, remote mode, `bgrules deploy`
and the budget's hard-stop function. Nothing here touches Google Cloud."""

import asyncio
import base64
import importlib.util
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from bgrules import deploy, server, store

TOKEN = "t" * 40


# --- token gate -------------------------------------------------------------

@pytest.fixture
def client():
    with TestClient(server.http_app(TOKEN)) as c:
        yield c


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_only_the_token_path_answers(client):
    ok = client.post(f"/{TOKEN}/mcp", json=INIT, headers=HEADERS)
    assert ok.status_code == 200
    assert ok.json()["result"]["serverInfo"]["name"] == "bgrules"

    for path in ["/mcp", f"/{'x' * 40}/mcp", f"/{TOKEN[:-1]}/mcp", f"/{TOKEN}", f"/{TOKEN}/", f"/{TOKEN}/mcp/x",
                 f"/{TOKEN}/other", "/", "/favicon.ico"]:
        missing = client.post(path, json=INIT, headers=HEADERS)
        assert (missing.status_code, missing.text) == (404, "Not Found"), path
        assert "server" not in missing.headers
    assert client.get("/").status_code == 404


def test_a_short_or_missing_token_is_refused(monkeypatch):
    with pytest.raises(ValueError):
        server.http_app("short")
    monkeypatch.delenv("BGRULES_TOKEN", raising=False)
    run = subprocess.run([sys.executable, "-m", "bgrules.cli", "serve", "--http"], capture_output=True, text=True, timeout=30)
    assert run.returncode != 0 and "BGRULES_TOKEN" in run.stderr
    run = subprocess.run([sys.executable, "-m", "bgrules.cli", "serve", "--http"], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "BGRULES_TOKEN": "x" * 31})
    assert run.returncode != 0 and "BGRULES_TOKEN" in run.stderr


# --- remote mode (in a subprocess: it changes the server for good) ------------

def test_remote_mode_drops_local_tools_and_explains_redeploying():
    code = ("import asyncio, json; from bgrules import server, store; server.remote_mode(); "
            "print(json.dumps({'tools': [t.name for t in asyncio.run(server.mcp.list_tools())], "
            "'instructions': server.mcp.instructions, 'remote': store.REMOTE}))")
    out = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
    assert "add_game" not in out["tools"] and "open_in_rulebook" not in out["tools"]
    assert {"list_games", "search_rules", "get_section", "lookup_term", "show_passage", "get_page", "verify_quote"} <= set(out["tools"])
    assert "run the deploy command again" in out["instructions"]
    assert "Nothing changes on their phone" in out["instructions"]
    assert "say plainly that it isn't from the rulebook" in " ".join(out["instructions"].split())
    assert "open_in_rulebook" not in out["instructions"] and "add_game" not in out["instructions"]
    assert out["remote"] is True


def test_local_instructions_keep_adding_games():
    assert "add_game" in server.INSTRUCTIONS and "open_in_rulebook" in server.INSTRUCTIONS
    assert "deploy" not in server.INSTRUCTIONS


# --- PDF path fallback -------------------------------------------------------

def test_pdf_found_under_a_moved_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path))
    pdf = tmp_path / "pdfs" / "catan" / "2020" / "rules.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    assert store.pdf_path("/Users/someone/.bgrules/pdfs/catan/2020/rules.pdf") == str(pdf)
    assert store.pdf_path(str(pdf)) == str(pdf)
    # Nothing there either: the stored path stands, and rendering reports it missing.
    assert store.pdf_path("/Users/someone/.bgrules/pdfs/wingspan/1st/rulebook.pdf") == "/Users/someone/.bgrules/pdfs/wingspan/1st/rulebook.pdf"
    assert store.pdf_path("/elsewhere/rules.pdf") == "/elsewhere/rules.pdf"
    assert store.pdf_path("C:\\Users\\me\\.bgrules\\pdfs\\catan\\2020\\rules.pdf") == str(pdf)


def test_remote_not_installed_points_to_the_deploy(monkeypatch, tmp_path):
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path))
    db = store.connect()
    with pytest.raises(store.LookupError_, match="add_game"):
        store.search(db, "catan", "robber")
    monkeypatch.setattr(store, "REMOTE", True)
    with pytest.raises(store.LookupError_, match="deploy") as e:
        store.search(db, "catan", "robber")
    # The steps themselves, not a pointer to instructions some apps never pass on.
    said = str(e.value)
    assert "add_game" not in said and "instructions" not in said
    assert "bgrules add <game>" in said and "bgrules deploy" in said and "link stays the same" in said
    assert "isn't from the rulebook" in said


def test_remote_missing_edition_says_how_to_add(fake_home, monkeypatch):
    db = store.connect()
    with pytest.raises(store.LookupError_) as e:
        store.resolve_edition(db, "catan", "2025")
    assert "deploy" not in str(e.value)
    monkeypatch.setattr(store, "REMOTE", True)
    with pytest.raises(store.LookupError_, match="Edition '2025'") as e:
        store.resolve_edition(db, "catan", "2025")
    assert store.REMOTE_HOW_TO_ADD in str(e.value)


def test_remote_list_games_says_how_to_add(monkeypatch, tmp_path):
    monkeypatch.setenv("BGRULES_HOME", str(tmp_path))
    assert "how_to_add" not in server.list_games()
    monkeypatch.setattr(store, "REMOTE", True)
    assert server.list_games()["how_to_add"] == store.REMOTE_HOW_TO_ADD



def test_remote_show_passage_puts_the_image_first_and_says_where_it_is(tmp_path, monkeypatch):
    # The Claude apps keep the image inside the collapsed tool call (#41).
    from mcp.server.mcpserver import Image

    from bgrules import ingest
    from test_own_rulebook import make_pdf, quiet

    monkeypatch.setenv("BGRULES_HOME", str(tmp_path / "home"))
    pdf = make_pdf(tmp_path / "rules.pdf", [[("Setup", "R", 16), ("The player who most recently cooked goes first.", "R", 10)]])
    ingest.add("Cooking", file=str(pdf), log=quiet)
    cid = store.search(store.connect(), "cooking", "who goes first")["results"][0]["citation_id"]
    local = server.show_passage(cid, "most recently cooked")
    assert isinstance(local[1], Image) and "where_to_see" not in local[0]
    monkeypatch.setattr(store, "REMOTE", True)
    image, summary = server.show_passage(cid, "most recently cooked")
    assert isinstance(image, Image) and summary["where_to_see"] == server.REMOTE_WHERE_TO_SEE
    assert "shown above" in summary["where_to_see"] and "Show passage" in summary["where_to_see"]

# --- deploy: token, package, gcloud commands --------------------------------

@pytest.fixture
def remote_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BGRULES_REMOTE_HOME", str(tmp_path / "remote"))
    return tmp_path / "remote"


def test_token_is_private(remote_home):
    assert deploy.saved_token() is None
    token = deploy.make_token()
    assert len(token) >= server.MIN_TOKEN_LENGTH and deploy.make_token() != token
    deploy.save_token(token)
    assert stat.S_IMODE((remote_home / "token").stat().st_mode) == 0o600
    assert deploy.saved_token() == token


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "pdfs" / "catan" / "2020").mkdir(parents=True)
    (home / "pdfs" / "catan" / "2020" / "rules.pdf").write_bytes(b"%PDF-1.4 fake")
    (home / "manifest.yaml").write_text("{}\n")
    db = sqlite3.connect(home / "index.sqlite")
    db.execute("CREATE TABLE docs (game TEXT, edition TEXT, doc_id TEXT, pdf_path TEXT)")
    db.execute("INSERT INTO docs VALUES ('catan', '2020', 'rules', '/Users/me/.bgrules/pdfs/catan/2020/rules.pdf')")
    db.commit()
    db.close()
    monkeypatch.setenv("BGRULES_HOME", str(home))
    return home


def test_build_context_packages_the_games(fake_home, tmp_path):
    dest = tmp_path / "ctx"
    assert deploy.build_context(dest) == ["catan"]
    assert (dest / "home" / "pdfs" / "catan" / "2020" / "rules.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert (dest / "home" / "manifest.yaml").exists()
    rows = sqlite3.connect(dest / "home" / "index.sqlite").execute("SELECT game FROM docs").fetchall()
    assert rows == [("catan",)]
    assert (dest / "bgrules" / "server.py").exists() and not list(dest.rglob("__pycache__"))
    reqs = (dest / "requirements.txt").read_text()
    assert "mcp==" in reqs and "pypdfium2==" in reqs and "pyobjc" not in reqs
    assert 'CMD ["python", "-m", "bgrules.cli", "serve", "--http"]' in (dest / "Dockerfile").read_text()


def test_build_context_needs_a_game(tmp_path):
    with pytest.raises(deploy.DeployError, match="No games installed"):
        deploy.build_context(tmp_path / "ctx", home=tmp_path / "empty")


def _money(text: str) -> dict:
    """An amount as the Budget API returns it: whole units as a string, plus nanos."""
    units, _, frac = text.partition(".")
    return {"units": units, **({"nanos": int(frac.ljust(9, "0"))} if frac else {})}


class FakeGCloud:
    """Stands in for subprocess.run(["gcloud", ...]): a brand-new account with one
    billing account, remembering what the deploy creates."""

    def __init__(self, currency="USD"):
        self.calls = []
        self.env_file = None
        self.currency = currency
        self.billing = ""  # the project's billing account, once linked
        self.budgets = []
        self.function = ""  # its state, once deployed
        self.fail_run_deploy = False
        self.budget_updates = 0

    def __call__(self, cmd, capture_output=True, text=True):
        assert cmd[0] == "gcloud"
        args = [a for a in cmd[1:] if a != "--quiet" and not a.startswith("--project=")]
        self.calls.append(args)
        out, code = "", 0
        joined = " ".join(args)
        if joined.startswith("auth list"):
            out = "me@example.com"
        elif joined.startswith("billing accounts list"):
            out = "billingAccounts/0000-1111-2222\tMy account"
        elif joined.startswith("billing projects link"):
            self.billing = "billingAccounts/0000-1111-2222"
            self.linked_with_budget = self.budgets[0]["amount"]["specifiedAmount"] if self.budgets else None
        elif "describe" in args:
            if "--format=value(projectNumber)" in args:
                out = "123456"
            elif "--format=value(status.url)" in args:
                out = "https://bgrules-abc-uc.a.run.app"
            elif "--format=value(currencyCode)" in args:
                out = self.currency
            elif "--format=value(billingAccountName)" in args:
                out = self.billing
            elif args[0] == "functions" and self.function:
                out = self.function
            else:
                code = 1  # nothing else exists
        elif "budgets list" in joined:
            out = json.dumps(self.budgets)
        elif "budgets create" in joined:
            amount = _money(next(a for a in args if a.startswith("--budget-amount="))[16:-3])
            topic = next(a for a in args if a.startswith("--notifications-rule-pubsub-topic="))[34:]
            self.budgets.append({"name": "billingAccounts/0000-1111-2222/budgets/b1", "displayName": next(
                a for a in args if a.startswith("--display-name="))[15:], "amount": {"specifiedAmount": amount},
                "notificationsRule": {"pubsubTopic": topic},
                "thresholdRules": [{"thresholdPercent": float(a.split("=")[-1])} for a in args if a.startswith("--threshold-rule=")]})
        elif "budgets update" in joined:
            self.budget_updates += 1
            self.budgets[0]["amount"]["specifiedAmount"] = _money(next(
                a for a in args if a.startswith("--budget-amount="))[16:-3])
            self.budgets[0]["thresholdRules"] = [{"thresholdPercent": float(a.split("=")[-1])}
                                                 for a in args if a.startswith("--add-threshold-rule=")]
        elif args[:2] == ["functions", "deploy"]:
            self.function = "ACTIVE"
        elif joined.startswith("run deploy"):
            if self.fail_run_deploy:
                return subprocess.CompletedProcess(cmd, 1, "", "build failed")
            self.env_file = next(a.split("=", 1)[1] for a in args if a.startswith("--env-vars-file="))
            self.env = json.loads(Path(self.env_file).read_text())
            self.source = next(a.split("=", 1)[1] for a in args if a.startswith("--source="))
            self.uploaded = sorted(str(p.relative_to(self.source)) for p in Path(self.source).rglob("*"))
        elif joined.startswith("storage buckets list"):
            out = "run-sources-p-us-central1\nsomething-else"
        return subprocess.CompletedProcess(cmd, code, out, "")


@pytest.fixture
def gcloud(monkeypatch):
    fake = FakeGCloud()
    monkeypatch.setattr(deploy.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(deploy.subprocess, "run", fake)
    monkeypatch.setattr("builtins.input", lambda prompt: "")  # take the suggested budget
    return fake


def test_deploy_sets_limits_and_hard_stop_before_deploying(fake_home, remote_home, gcloud, capsys):
    fake = gcloud
    url = deploy.deploy()

    token = (remote_home / "token").read_text().strip()
    assert url == f"https://bgrules-abc-uc.a.run.app/{token}/mcp"
    printed = capsys.readouterr().out
    assert url in printed and "New link" in printed
    config = json.loads((remote_home / "config.json").read_text())
    assert config["budget"] == deploy.DEFAULT_BUDGET and config["billing_account"] == "0000-1111-2222"

    names = [" ".join(c[:3]) for c in fake.calls]
    assert names.index("projects create " + config["project"]) < names.index("run deploy bgrules")
    budget = next(c for c in fake.calls if c[1:4] == ["billing", "budgets", "create"])
    assert f"--filter-projects=projects/{config['project']}" in budget
    assert "--threshold-rule=percent=1.0" in budget and "--budget-amount=5USD" in budget
    stop = next(c for c in fake.calls if c[:2] == ["functions", "deploy"])
    assert "--trigger-topic=bgrules-budget" in stop and "--retry" in stop
    assert names.index("functions deploy bgrules-budget-stop") < names.index("run deploy bgrules")

    run = next(c for c in fake.calls if c[:2] == ["run", "deploy"])
    for flag, value in [("--max-instances", "1"), ("--min-instances", "0"), ("--memory", "512Mi"), ("--cpu", "1")]:
        assert run[run.index(flag) + 1] == value
    # The token reaches the service only through the env file, never the uploaded source.
    assert fake.env == {"BGRULES_TOKEN": token}
    assert not Path(fake.env_file).is_relative_to(fake.source)
    assert "home/index.sqlite" in fake.uploaded and "Dockerfile" in fake.uploaded

    assert any(c[:3] == ["storage", "buckets", "update"] and "gs://run-sources-p-us-central1" in c for c in fake.calls)
    assert not any("gs://something-else" in c for c in fake.calls)
    # Never a key: no service-account keys or API keys.
    assert not any("keys" in c for c in fake.calls)


def test_redeploy_keeps_the_link(fake_home, remote_home, gcloud, monkeypatch, capsys):
    first = deploy.deploy()
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked again"))
    capsys.readouterr()
    assert deploy.deploy() == first
    assert "Same link as before" in capsys.readouterr().out
    # The hard-stop function and budget are already in place: not deployed or created again.
    assert sum(c[:2] == ["functions", "deploy"] for c in gcloud.calls) == 1
    assert sum("create" in c and "budgets" in c for c in gcloud.calls) == 1
    second = deploy.deploy(new_token=True)
    assert second != first and "New link" in capsys.readouterr().out


def test_a_failed_new_token_deploy_keeps_the_new_token_for_the_retry(fake_home, remote_home, gcloud, capsys):
    first = deploy.deploy()
    gcloud.fail_run_deploy = True  # gcloud can fail after the new revision already serves
    with pytest.raises(deploy.DeployError):
        deploy.deploy(new_token=True)
    assert first.endswith(f"/{deploy.saved_token()}/mcp")  # the old token stays until a deploy finishes
    gcloud.fail_run_deploy = False
    capsys.readouterr()
    second = deploy.deploy()
    assert second != first and gcloud.env["BGRULES_TOKEN"] == deploy.saved_token()
    assert "New link" in capsys.readouterr().out
    assert not (remote_home / "token.pending").exists()
    assert deploy.deploy() == second


def test_a_failed_first_deploy_still_says_to_add_the_connector(fake_home, remote_home, gcloud, capsys):
    gcloud.fail_run_deploy = True
    with pytest.raises(deploy.DeployError):
        deploy.deploy()
    gcloud.fail_run_deploy = False
    deploy.deploy()
    assert "New link" in capsys.readouterr().out


def test_after_the_hard_stop_redeploying_needs_a_budget(fake_home, remote_home, gcloud):
    deploy.deploy()
    gcloud.billing = ""  # the hard stop unlinked it
    with pytest.raises(deploy.DeployError, match="--budget"):
        deploy.deploy()
    assert not any(c[:3] == ["billing", "projects", "link"] for c in gcloud.calls[-5:])
    deploy.deploy(budget="20")
    assert gcloud.billing
    assert gcloud.linked_with_budget == {"units": "20"}  # raised before billing went back on


def test_an_unchanged_budget_is_left_alone(fake_home, remote_home, gcloud):
    deploy.deploy(budget="7.5")
    deploy.deploy()
    assert gcloud.budget_updates == 0


@pytest.mark.parametrize("amount", ["inf", "nan", "-1", "0", "five"])
def test_budget_must_be_a_positive_amount(amount):
    with pytest.raises(deploy.DeployError):
        deploy._parse_budget(amount)


def test_a_broken_hard_stop_function_is_deployed_again(fake_home, remote_home, gcloud):
    deploy.deploy()
    gcloud.function = "FAILED"
    deploy.deploy()
    assert sum(c[:2] == ["functions", "deploy"] for c in gcloud.calls) == 2
    gcloud.function = "DEPLOYING"
    with pytest.raises(deploy.DeployError, match="busy"):
        deploy.deploy()
    assert sum(c[:2] == ["functions", "deploy"] for c in gcloud.calls) == 2


def test_budget_is_in_the_billing_accounts_currency(fake_home, remote_home, gcloud):
    gcloud.currency = "EUR"
    deploy.deploy()
    budget = next(c for c in gcloud.calls if "budgets" in c and "create" in c)
    assert "--budget-amount=5EUR" in budget


def test_deploy_without_gcloud(monkeypatch):
    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
    with pytest.raises(deploy.DeployError, match="brew install --cask gcloud-cli"):
        deploy.deploy()


# --- the hard-stop function -------------------------------------------------

spec = importlib.util.spec_from_file_location("budget_stop", Path(deploy.__file__).parent / "budget_stop" / "main.py")
budget_stop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget_stop)


class FakeBilling:
    def __init__(self, enabled=True, read_status=200):
        self.enabled = enabled
        self.read_status = read_status
        self.puts = []

    def get(self, url):
        return FakeResponse({"billingEnabled": self.enabled}, self.read_status)

    def put(self, url, json):
        self.puts.append((url, json))
        return FakeResponse({})


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.ok = status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.body


class Event:
    def __init__(self, notice):
        self.data = {"message": {"data": base64.b64encode(json.dumps(notice).encode()).decode()}}


def test_hard_stop_leaves_billing_on_under_budget(monkeypatch):
    billing = FakeBilling()
    monkeypatch.setattr(budget_stop, "_session", lambda: billing)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "bgrules-x")
    budget_stop.stop_billing(Event({"costAmount": 4.99, "budgetAmount": 5.0, "budgetDisplayName": "bgrules"}))
    assert billing.puts == []


def test_hard_stop_turns_billing_off_at_the_budget(monkeypatch):
    billing = FakeBilling()
    monkeypatch.setattr(budget_stop, "_session", lambda: billing)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "bgrules-x")
    budget_stop.stop_billing(Event({"costAmount": 5.2, "budgetAmount": 5.0}))
    assert billing.puts == [("https://cloudbilling.googleapis.com/v1/projects/bgrules-x/billingInfo", {"billingAccountName": ""})]


def test_hard_stop_turns_billing_off_even_if_it_cannot_read_the_status():
    billing = FakeBilling(read_status=403)
    said = budget_stop.handle({"costAmount": 20, "budgetAmount": 10}, "p", billing)
    assert billing.puts == [("https://cloudbilling.googleapis.com/v1/projects/p/billingInfo", {"billingAccountName": ""})]
    assert "billing turned off" in said and "HTTP 403" in said


def test_hard_stop_function_can_read_the_billing_status(fake_home, remote_home, gcloud):
    deploy.deploy()
    member = "serviceAccount:bgrules-budget-stop@"
    roles = {next(a for a in c if a.startswith("--role=")) for c in gcloud.calls
             if c[:2] == ["projects", "add-iam-policy-binding"] and any(a.startswith(f"--member={member}") for a in c)}
    # Billing Manager can change the billing link but not read the project; Browser can.
    assert {"--role=roles/billing.projectManager", "--role=roles/browser"} <= roles


def test_hard_stop_does_nothing_when_billing_is_already_off():
    billing = FakeBilling(enabled=False)
    assert "already off" in budget_stop.handle({"costAmount": 9, "budgetAmount": 5}, "p", billing)
    assert billing.puts == []


# --- end to end: serve --http on a copy of the installed games --------------

def _catan_installed() -> bool:
    return any(r["game"] == "catan" and r["edition"] == "2020" for r in store.installed(store.connect()))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not _catan_installed(), reason="Catan 2020 not installed")
def test_serve_http_end_to_end(tmp_path):
    deploy.build_context(tmp_path / "ctx")  # the same copy a deploy uploads; its index has this machine's paths
    port = _free_port()
    env = {**os.environ, "BGRULES_HOME": str(tmp_path / "ctx" / "home"), "BGRULES_TOKEN": TOKEN, "PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "bgrules.cli", "serve", "--http"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        assert httpx2.get(f"http://127.0.0.1:{port}/wrong/mcp").status_code == 404
        result = asyncio.run(_remote_calls(f"http://127.0.0.1:{port}/{TOKEN}/mcp"))
    finally:
        proc.terminate()
        proc.wait(10)
    instructions, tools, search, shown = result
    assert "run the deploy command again" in instructions  # what the client actually receives
    assert "add_game" not in tools and "open_in_rulebook" not in tools
    top = json.loads(search.content[0].text)["results"][0]
    assert top["section_path"] == ["Almanac", "Robber"] and "open_link" not in top
    kinds = [block.type for block in shown.content]
    assert kinds[0] == "image" and kinds[-1] == "text"  # image first: the apps show it inside the tool call
    assert "where_to_see" in shown.content[-1].text


async def _remote_calls(url):
    async with streamable_http_client(url) as (read, write, *_):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            search = await session.call_tool("search_rules", {"game": "catan", "edition": "2020", "query": "What happens when I move the robber?"})
            cid = json.loads(search.content[0].text)["results"][0]["citation_id"]
            shown = await session.call_tool("show_passage", {"citation_id": cid, "quote": "The robber begins the game in the desert."})
            return init.instructions, tools, search, shown


NOT_READY = ("ERROR: Failed to create topic: ... organization policy ... If the topic's project was recently created, "
             "you may need to wait a few minutes for the project's organization policy to be properly initialized, "
             "and then retry this operation.")


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleeping advances it, and each gcloud call takes `call_seconds`."""
    clock = type("Clock", (), {"now": 0.0, "call_seconds": 0, "slept": []})()
    def sleep(seconds):
        clock.slept.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(deploy.time, "sleep", sleep)
    monkeypatch.setattr(deploy.time, "monotonic", lambda: clock.now)
    return clock


def fake_run(clock, codes, stderr):
    def run(cmd, **kw):
        clock.now += clock.call_seconds
        code = codes.pop(0) if isinstance(codes, list) else codes
        return subprocess.CompletedProcess(cmd, code, "", stderr if code else "")
    return run


def test_gcloud_waits_for_a_new_project(monkeypatch, clock, capsys):
    monkeypatch.setattr(deploy.subprocess, "run", fake_run(clock, [1, 1, 0], NOT_READY))
    assert deploy.GCloud("p")("pubsub", "topics", "create", "t").returncode == 0
    assert clock.slept == [20, 20]
    assert capsys.readouterr().err.count("isn't ready yet") == 1


def test_gcloud_gives_up_on_a_project_that_never_gets_ready(monkeypatch, clock):
    monkeypatch.setattr(deploy.subprocess, "run", fake_run(clock, 1, NOT_READY))
    with pytest.raises(deploy.DeployError, match="recently created"):
        deploy.GCloud("p")("pubsub", "topics", "create", "t")
    assert sum(clock.slept) == 600


def test_gcloud_retry_limit_counts_slow_commands(monkeypatch, clock):
    clock.call_seconds = 300  # a build that fails after five minutes
    monkeypatch.setattr(deploy.subprocess, "run", fake_run(clock, 1, NOT_READY))
    with pytest.raises(deploy.DeployError):
        deploy.GCloud("p")("run", "deploy", "s")
    assert len(clock.slept) == 1


def test_gcloud_waits_for_a_new_service_account(monkeypatch, clock):
    monkeypatch.setattr(deploy.subprocess, "run", fake_run(clock, [1, 1, 0], "Service account x does not exist."))
    assert deploy.GCloud("p")("projects", "add-iam-policy-binding", "p").returncode == 0
    assert clock.slept == [5, 5]


def test_gcloud_does_not_retry_other_failures(monkeypatch, clock):
    monkeypatch.setattr(deploy.subprocess, "run", fake_run(clock, 1, "PERMISSION_DENIED"))
    with pytest.raises(deploy.DeployError):
        deploy.GCloud("p")("pubsub", "topics", "create", "t")
    assert clock.slept == []
