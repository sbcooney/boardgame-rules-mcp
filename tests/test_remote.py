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
            "'instructions': server.mcp.instructions, 'links': store.OPEN_LINKS}))")
    out = json.loads(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout)
    assert "add_game" not in out["tools"] and "open_in_rulebook" not in out["tools"]
    assert {"list_games", "search_rules", "get_section", "lookup_term", "show_passage", "get_page", "verify_quote"} <= set(out["tools"])
    assert "run the deploy command again" in out["instructions"]
    assert "Nothing changes on their phone" in out["instructions"]
    assert "open_in_rulebook" not in out["instructions"] and "add_game" not in out["instructions"]
    assert out["links"] is False


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


# --- deploy: token, package, gcloud commands --------------------------------

@pytest.fixture
def remote_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BGRULES_REMOTE_HOME", str(tmp_path / "remote"))
    return tmp_path / "remote"


def test_token_is_private_and_reused(remote_home):
    first = deploy.token()
    assert len(first) >= server.MIN_TOKEN_LENGTH
    assert stat.S_IMODE((remote_home / "token").stat().st_mode) == 0o600
    assert deploy.token() == first
    assert deploy.token(new=True) != first
    assert deploy.token() != first


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


class FakeGCloud:
    """Stands in for subprocess.run(["gcloud", ...]): a brand-new account with one billing account."""

    def __init__(self):
        self.calls = []
        self.env_file = None

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
        elif "describe" in args:
            if "--format=value(projectNumber)" in args:
                out = "123456"
            elif "--format=value(status.url)" in args:
                out = "https://bgrules-abc-uc.a.run.app"
            elif "--format=value(billingAccountName)" not in args:
                code = 1  # nothing exists yet
        elif "budgets list" in joined:
            out = "[]"
        elif joined.startswith("run deploy"):
            self.env_file = next(a.split("=", 1)[1] for a in args if a.startswith("--env-vars-file="))
            self.env = json.loads(Path(self.env_file).read_text())
            self.source = next(a.split("=", 1)[1] for a in args if a.startswith("--source="))
            self.uploaded = sorted(str(p.relative_to(self.source)) for p in Path(self.source).rglob("*"))
        elif joined.startswith("storage buckets list"):
            out = "run-sources-p-us-central1\nsomething-else"
        return subprocess.CompletedProcess(cmd, code, out, "")


def test_deploy_sets_limits_and_hard_stop_before_deploying(fake_home, remote_home, monkeypatch, capsys):
    fake = FakeGCloud()
    monkeypatch.setattr(deploy.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(deploy.subprocess, "run", fake)
    monkeypatch.setattr("builtins.input", lambda prompt: "")  # take the suggested budget

    url = deploy.deploy()

    token = (remote_home / "token").read_text().strip()
    assert url == f"https://bgrules-abc-uc.a.run.app/{token}/mcp"
    assert url in capsys.readouterr().out
    config = json.loads((remote_home / "config.json").read_text())
    assert config["budget"] == deploy.DEFAULT_BUDGET and config["billing_account"] == "0000-1111-2222"

    names = [" ".join(c[:3]) for c in fake.calls]
    assert names.index("projects create " + config["project"]) < names.index("run deploy bgrules")
    budget = next(c for c in fake.calls if c[1:4] == ["billing", "budgets", "create"])
    assert f"--filter-projects=projects/{config['project']}" in budget
    assert "--threshold-rule=percent=1.0" in budget and "--budget-amount=5USD" in budget
    stop = next(c for c in fake.calls if c[:2] == ["functions", "deploy"])
    assert "--trigger-topic=bgrules-budget" in stop
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


def test_redeploy_keeps_the_link(fake_home, remote_home, monkeypatch):
    monkeypatch.setattr(deploy.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(deploy.subprocess, "run", FakeGCloud())
    monkeypatch.setattr("builtins.input", lambda prompt: "3")
    first = deploy.deploy()
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked again"))
    assert deploy.deploy() == first
    assert deploy.deploy(new_token=True) != first


def test_deploy_without_gcloud(monkeypatch):
    monkeypatch.setattr(deploy.shutil, "which", lambda name: None)
    with pytest.raises(deploy.DeployError, match="brew install --cask gcloud-cli"):
        deploy.deploy()


# --- the hard-stop function -------------------------------------------------

spec = importlib.util.spec_from_file_location("budget_stop", Path(deploy.__file__).parent / "budget_stop" / "main.py")
budget_stop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget_stop)


class FakeBilling:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.puts = []

    def get(self, url):
        return FakeResponse({"billingEnabled": self.enabled})

    def put(self, url, json):
        self.puts.append((url, json))
        return FakeResponse({})


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

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
    tools, search, shown = result
    assert "add_game" not in tools and "open_in_rulebook" not in tools
    top = json.loads(search.content[0].text)["results"][0]
    assert top["section_path"] == ["Almanac", "Robber"] and "open_link" not in top
    kinds = [block.type for block in shown.content]
    assert kinds[0] == "text" and "image" in kinds


async def _remote_calls(url):
    async with streamable_http_client(url) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            search = await session.call_tool("search_rules", {"game": "catan", "edition": "2020", "query": "What happens when I move the robber?"})
            cid = json.loads(search.content[0].text)["results"][0]["citation_id"]
            shown = await session.call_tool("show_passage", {"citation_id": cid, "quote": "The robber begins the game in the desert."})
            return tools, search, shown
