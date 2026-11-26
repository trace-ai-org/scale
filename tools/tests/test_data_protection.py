"""Private-data protection (docs/private-data.md): the data guard's hooks over staged snapshots and outgoing history,
the data repository scaffold and its audience rule, and the egress gate between documents and model endpoints."""

import io
import json
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from conftest import HEAD_VERSION, REPO
from scale import audience, datarepo, guard
from scale.api import ScaleToolkit
from scale.cli import main
from scale.egress import EgressDenied, EgressGate, load_endpoints
from scale.util import ScaleError

CANARY = "CANARY-7f3a91-private-yield-of-batch-B12"
PROJECT_A, PROJECT_B = "lab:project/a", "lab:project/b"


@pytest.fixture(autouse=True)
def _git_alone(tmp_path, monkeypatch):
    """Every git command of these tests reads no user or system configuration (a global core.hooksPath would
    otherwise receive the hooks) and commits under a fixed identity."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@example.org\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(repo, *args, check=True, input=None):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r


def doc(access="public", project=None, record_access=None, name="test 1", embargo=None, data_status="synthetic_example"):
    """A small scale-catal document: the header's stewardship, one record (its own stewardship when given)."""
    head = {"access": access, **({"project": project} if project else {})}
    props = {"name": name, "origin": "curated"}
    if record_access:
        props["stewardship"] = dict(record_access)
    if embargo:
        props["stewardship"] = {"access": "embargoed", "contributed_by": ["lab:inst/x"], **embargo}
    return {"directed": True, "multigraph": True,
            "graph": {"schema": "scale-catal", "schema_version": HEAD_VERSION, "data_status": data_status,
                      "stewardship": head},
            "nodes": [{"id": "lab:x/test/1", "label": "CatalyticTest", "props": props}], "links": []}


def write(repo: Path, rel: str, content) -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content if isinstance(content, str) else json.dumps(content, indent=1), encoding="utf-8")
    return p


def new_repo(tmp_path, name="r", policy: dict | None = None) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    if policy is not None:
        write(repo, audience.POLICY_FILE, json.dumps(policy))
    return repo


def data_policy(readers, **repo_keys):
    return {"scale_data_policy": 1, "repository": {"readers": readers, "copyright_cap": False, **repo_keys}}


def rules_of(res):
    return sorted({p["rule"] for p in res["problems"]})


# ---------------------------------------------------------------- the staged snapshot
def test_the_guard_judges_the_staged_snapshot_not_the_working_tree(tmp_path):
    repo = new_repo(tmp_path)
    write(repo, "graphs/a.json", doc("project", PROJECT_A, name=CANARY))
    git(repo, "add", "graphs/a.json")
    write(repo, "graphs/a.json", doc("public"))              # the working tree is public; the index is not
    res = guard.check_staged(repo)
    assert not res["ok"] and rules_of(res) == ["access"] and res["problems"][0]["path"] == "graphs/a.json"
    assert "project" in res["problems"][0]["message"]
    git(repo, "add", "graphs/a.json")                         # now the public version is staged ...
    write(repo, "graphs/a.json", doc("project", PROJECT_A))   # ... and the private one only in the working tree
    assert guard.check_staged(repo)["ok"]
    # what is committed already is not judged again by pre-commit; a later change to it is
    git(repo, "commit", "-qm", "public", "--no-verify")
    assert guard.check_staged(repo) == {"checked": "staged", "paths": 0, "problems": [], "ok": True}


def test_a_document_without_access_or_with_one_private_record_is_refused_and_a_malformed_one_too(tmp_path):
    repo = new_repo(tmp_path)
    d = doc("public")
    d["graph"].pop("stewardship")
    write(repo, "a.json", d)
    write(repo, "b.json", doc("public", record_access={"access": "institution", "contributed_by": ["lab:inst/x"]}))
    write(repo, "c.json", '{"graph": {"schema": "scale-catal"}, "nodes": [')          # names scale-catal, not JSON
    write(repo, "d.json", {"graph": {"schema": "scale-catal", "stewardship": {"access": "public"}}, "nodes": "x"})
    write(repo, "e.json", doc("public"))
    git(repo, "add", ".")
    res = guard.check_staged(repo)
    by = {p["path"]: p["rule"] for p in res["problems"]}
    assert by == {"a.json": "access", "b.json": "access", "c.json": "malformed", "d.json": "malformed"}
    assert "read as institution" in next(p["message"] for p in res["problems"] if p["path"] == "a.json")


def test_a_graph_kept_as_parts_is_rebuilt_from_the_staged_parts(tmp_path):
    repo = new_repo(tmp_path)
    head = {k: v for k, v in doc("public").items() if k not in ("nodes", "links")}
    src = {"id": "lab:g/source/s1", "label": "Source", "props": {"name": "s1"}}
    rec = {"id": "lab:g/s1/test/1", "label": "CatalyticTest",
           "props": {"name": CANARY, "stewardship": {"access": "project", "project": PROJECT_A}}}
    write(repo, "graphs/g/parts/header.json", head)
    write(repo, "graphs/g/parts/shared.json", {"schema_version": HEAD_VERSION, "nodes": [], "links": []})
    write(repo, "graphs/g/parts/sources/s1.json", {"source": "lab:g/source/s1", "schema_version": HEAD_VERSION,
                                                   "nodes": [src, rec], "links": []})
    git(repo, "add", ".")
    res = guard.check_staged(repo)
    assert [(p["rule"], p["path"]) for p in res["problems"]] == [("access", "graphs/g")]
    assert "1 records are not public (e.g. lab:g/s1/test/1)" in res["problems"][0]["message"]
    # the part made public in the working tree only: still refused; staged: accepted
    rec["props"]["stewardship"] = {"access": "public"}
    write(repo, "graphs/g/parts/sources/s1.json", {"source": "lab:g/source/s1", "schema_version": HEAD_VERSION,
                                                   "nodes": [src, rec], "links": []})
    assert not guard.check_staged(repo)["ok"]
    git(repo, "add", ".")
    assert guard.check_staged(repo)["ok"]
    git(repo, "commit", "-qm", "parts", "--no-verify")
    # a part at another schema version does not build: malformed; parts with no header are refused too
    write(repo, "graphs/g/parts/sources/s1.json", {"source": "lab:g/source/s1", "schema_version": "0.0.1",
                                                   "nodes": [src, rec], "links": []})
    write(repo, "graphs/h/parts/sources/s2.json", {"source": "lab:h/source/s2", "nodes": [], "links": []})
    git(repo, "add", ".")
    res = guard.check_staged(repo)
    assert sorted((p["rule"], p["path"]) for p in res["problems"]) == [
        ("malformed", "graphs/g/parts/"), ("malformed", "graphs/h/parts/")]


def test_raw_data_is_refused_by_name_by_content_and_by_layout_unless_a_reviewed_pattern_allows_it(tmp_path):
    repo = new_repo(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("runs.csv", "run,yield\n1,0.42\n")
    write(repo, "data/runs.csv", "run,yield\n1,0.42\n")
    write(repo, "notes/figures.md", buf.getvalue())                       # an archive renamed to look like notes
    write(repo, "notes/table.txt", "".join(f"{i},{i * 2},{i * 3}\n" for i in range(30)))
    write(repo, "notes/prose.txt", "A note, with commas, here.\nAnother line.\n" * 3)
    write(repo, "sim/OUTCAR", "free energy TOTEN = -1.0 eV\n")
    write(repo, "data/cube.bin.h5", b"\x89HDF\r\n\x1a\n" + b"\0" * 64)
    git(repo, "add", ".")
    res = guard.check_staged(repo)
    kinds = {p["path"]: p["message"].split(":")[0] for p in res["problems"]}
    assert rules_of(res) == ["raw_data"]
    assert kinds == {"data/runs.csv": "a table", "notes/figures.md": "a zip archive (an xlsx, docx or jar is one)",
                     "notes/table.txt": "a table (text laid out in columns)", "sim/OUTCAR": "simulation output",
                     "data/cube.bin.h5": "an HDF5 file"}
    # a git rename of an archive is judged by its content at the new path
    git(repo, "reset", "-q")
    write(repo, "notes/table.txt", "plain prose\n")
    for p in ("data/runs.csv", "sim/OUTCAR", "data/cube.bin.h5"):
        (repo / p).unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "x", "--no-verify")
    git(repo, "mv", "notes/figures.md", "notes/renamed.yaml")
    res = guard.check_staged(repo)
    assert [(p["path"], p["rule"]) for p in res["problems"]] == [("notes/renamed.yaml", "raw_data")]
    # the framework's reviewed fixture list allows a pattern (here a data repository's raw_data does the same)
    write(repo, "governance/public-fixtures.yaml", "fixtures:\n- paths:\n  - notes/*.yaml\n  reason: reviewed\n")
    git(repo, "add", ".")
    assert guard.check_staged(repo)["ok"]


def test_large_files_are_taken_for_raw_data(tmp_path):
    repo = new_repo(tmp_path)
    write(repo, "big.md", b"x" * (guard.LARGE + 1))
    git(repo, "add", ".")
    res = guard.check_staged(repo)
    assert rules_of(res) == ["raw_data"] and "MiB" in res["problems"][0]["message"]


def test_quoted_text_beyond_the_copyright_cap_is_refused(tmp_path):
    repo = new_repo(tmp_path)
    d = doc("public", data_status="research_data")
    d["nodes"].append({"id": "lab:x/source/p", "label": "Source", "props": {"name": "p"}})
    d["nodes"][0]["props"]["evidence"] = [{"source": "lab:x/source/p", "locator": "p. 1", "quote": "q" * 400}]
    write(repo, "g.json", d)
    git(repo, "add", ".")
    res = guard.check_staged(repo)
    assert rules_of(res) == ["copyright"] and "without a rights.yaml" in res["problems"][0]["message"]
    write(repo, "rights.yaml", "sources:\n  lab:x/source/p: {class: in_copyright, licence: all rights reserved, basis: t}\n")
    git(repo, "add", ".")
    res = guard.check_staged(repo)
    assert rules_of(res) == ["copyright"] and "longer than 300 characters" in res["problems"][0]["message"]


def test_this_repository_passes_the_guard_and_refuses_a_data_policy(tmp_path):
    res = guard.check_all(REPO)                                # every tracked path of this checkout
    assert res["ok"], guard.report_text(res)
    # a scale-data.yaml in the framework repository does not turn it into a data repository
    snap = guard.Snapshot({**{m: ("100644", "0" * 40) for m in guard.FRAMEWORK_MARKERS},
                           audience.POLICY_FILE: ("100644", "1" * 40)}, None, "test")
    assert [p["rule"] for p in guard.Rules(snap).problems] == ["policy"]


# ---------------------------------------------------------------- outgoing history
def test_a_push_checks_every_outgoing_commit_including_a_private_file_deleted_later(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = new_repo(tmp_path)
    write(repo, "README.md", "hi\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    write(repo, "graphs/private.json", doc("project", PROJECT_A, name=CANARY))
    write(repo, "raw/run.parquet", b"PAR1....PAR1")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "oops", "--no-verify")
    bad = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "rm", "-q", "-r", "graphs", "raw")
    git(repo, "commit", "-qm", "remove", "--no-verify")
    assert guard.check_all(repo)["ok"]                         # the tip is clean ...
    res = guard.check_push(repo, "origin", rng="origin/main..main")
    assert res["commits"] == 2 and not res["ok"]              # ... its history is not
    assert {(p["path"], p["commit"]) for p in res["problems"]} == {("graphs/private.json", bad), ("raw/run.parquet", bad)}
    # git's pre-push lines: the same, and nothing is sent once the hook is installed
    tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    old = git(repo, "rev-parse", "origin/main").stdout.strip()
    res = guard.check_push(repo, "origin", lines=guard.parse_push_lines(f"refs/heads/main {tip} refs/heads/main {old}\n"))
    assert not res["ok"] and res["commits"] == 2
    assert main(["guard", "install", "--path", str(repo)]) == 0
    r = git(repo, "push", "origin", "main", check=False)
    assert r.returncode != 0 and "graphs/private.json" in r.stderr
    assert git(remote, "rev-parse", "main").stdout.strip() == old          # the remote received nothing
    # the documented limit: --no-verify skips the hook
    assert git(repo, "push", "--no-verify", "origin", "main", check=False).returncode == 0


def test_the_hooks_refuse_a_commit_and_keep_a_hook_of_another_origin(tmp_path):
    repo = new_repo(tmp_path)
    hooks = guard.hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\necho other-hook-ran >&2\nexit 0\n")
    (hooks / "pre-commit").chmod(0o755)
    with pytest.raises(ScaleError, match="not the guard's"):
        guard.install(repo)
    res = guard.install(repo, force=True)
    assert res["chained"] == [str(hooks / "pre-commit.scale-chained")]
    text = (hooks / "pre-commit").read_text()
    assert guard.HOOK_MARK in text and "--no-verify" in text and "guard check --staged" in text
    write(repo, "g.json", doc("institution", name=CANARY))
    git(repo, "add", ".")
    r = git(repo, "commit", "-qm", "private", check=False)
    assert r.returncode != 0 and "[access] g.json" in r.stderr and "other-hook-ran" not in r.stderr
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0      # nothing committed
    write(repo, "g.json", doc("public"))
    git(repo, "add", ".")
    r = git(repo, "commit", "-qm", "public", check=False)
    assert r.returncode == 0 and "other-hook-ran" in r.stderr                                 # chained after the guard
    guard.install(repo)                                                                       # again: idempotent


# ---------------------------------------------------------------- the data repository
def test_data_init_scaffolds_a_private_repository_with_hooks_and_no_remote(tmp_path):
    d = tmp_path / "lab-data"
    assert main(["data", "init", str(d), "--project", PROJECT_A]) == 0
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for f in ("README.md", "scale-data.yaml", "rights.yaml", ".gitignore", "graphs/README.md"):
        assert (d / f).is_file(), f
    pol = audience.load_policy(d / "scale-data.yaml")
    assert pol["readers"] == {"access": "project", "project": PROJECT_A} and pol["remotes"] == [] \
        and pol["endpoints"] == [] and pol["defaults"]["stewardship"] == {"access": "project", "project": PROJECT_A}
    ignored = (d / ".gitignore").read_text()
    assert all(f"/{x}/" in ignored for x in ("raw", "index", "exports", "cache", "work"))
    assert git(d, "remote").stdout.strip() == ""
    assert guard.HOOK_MARK in (guard.hooks_dir(d) / "pre-commit").read_text()
    assert guard.HOOK_MARK in (guard.hooks_dir(d) / "pre-push").read_text()
    # project data of its own project commits; another project's, an institution's and raw data do not
    write(d, "graphs/a.json", doc("project", PROJECT_A, name=CANARY))
    write(d, "raw/run.csv", "a,b\n1,2\n")                                    # ignored: never staged
    git(d, "add", ".")
    assert git(d, "commit", "-qm", "init", check=False).returncode == 0
    assert "raw/run.csv" not in git(d, "ls-files").stdout
    write(d, "graphs/b.json", doc("project", PROJECT_B))
    write(d, "graphs/runs.xlsx", b"PK\x03\x04rest")
    git(d, "add", ".")
    r = git(d, "commit", "-qm", "more", check=False)
    assert r.returncode != 0 and "[audience] graphs/b.json" in r.stderr and "[raw_data] graphs/runs.xlsx" in r.stderr
    # a push goes only to a remote the policy lists
    git(d, "reset", "-q")
    remote = tmp_path / "team.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(d, "remote", "add", "team", str(remote))
    r = git(d, "push", "team", "main", check=False)
    assert r.returncode != 0 and "[destination]" in r.stderr
    pol_text = (d / "scale-data.yaml").read_text().replace("  remotes: []", f'  remotes: ["{remote}"]')
    (d / "scale-data.yaml").write_text(pol_text)
    git(d, "add", "scale-data.yaml")
    git(d, "commit", "-qm", "the team's remote")
    assert git(d, "push", "-q", "team", "main", check=False).returncode == 0
    # a data repository is never made inside another repository (above all this one)
    with pytest.raises(ScaleError, match="inside the git repository"):
        datarepo.init(d / "nested", project=PROJECT_A)
    with pytest.raises(ScaleError, match="names its project"):
        datarepo.init(tmp_path / "other", access="project")


def test_disjoint_audiences_and_embargoes_follow_the_subset_rule(tmp_path):
    today = "2026-06-01"
    a = {"access": "project", "project": PROJECT_A}
    assert audience.audience_problems(doc("public"), "g", a, today) == []
    assert audience.audience_problems(doc("project", PROJECT_A), "g", a, today) == []
    other = audience.audience_problems(doc("project", PROJECT_B), "g", a, today)
    assert len(other) == 2 and "project lab:project/b" in other[0] and "fewer readers" in other[0]
    inst = audience.audience_problems(doc("project", PROJECT_A, record_access={"access": "institution",
                                                                              "contributed_by": ["lab:inst/x"]}), "g", a, today)
    assert len(inst) == 1 and "institution lab:inst/x" in inst[0]
    # an institution repository may hold its institution's records and public ones, not a project's it is not in
    i = {"access": "institution", "contributed_by": ["lab:inst/x"]}
    assert audience.audience_problems(doc("institution", record_access={"access": "institution",
                                                                       "contributed_by": ["lab:inst/x"]})
                                      | {"graph": {**doc()["graph"], "stewardship": i}}, "g", i, today) == []
    assert audience.audience_problems(doc("project", PROJECT_A), "g", i, today)
    # an embargo in force admits its institution only; a lapsed one its access_after_embargo
    held = doc("project", PROJECT_A, embargo={"embargo_until": "2099-01-01", "access_after_embargo": "public"})
    assert "an embargo in force" in audience.audience_problems(held, "g", a, today)[0]
    lapsed = doc("project", PROJECT_A, embargo={"embargo_until": "2020-01", "access_after_embargo": "public"})
    assert audience.audience_problems(lapsed, "g", a, today) == []
    # the framework repository's readers are the public: a lapsed embargo is still not stated public there
    from scale import rights
    assert rights.doc_access_problems(lapsed, "g")
    # through the guard, in a data repository: the policy's readers decide
    repo = new_repo(tmp_path, policy=data_policy(a))
    write(repo, "g1.json", doc("project", PROJECT_B))
    write(repo, "g2.json", held)
    write(repo, "g3.json", doc("public"))
    git(repo, "add", ".")
    assert {p["path"] for p in guard.check_staged(repo)["problems"]} == {"g1.json", "g2.json"}


def test_a_data_policy_is_strict(tmp_path):
    good = data_policy({"access": "project", "project": PROJECT_A})
    assert audience.parse_policy(good)["copyright_cap"] is False
    for bad, why in [({**good, "egres": {}}, "a policy holds"),
                     ({**good, "scale_data_policy": 2}, "scale_data_policy: 1"),
                     (data_policy({"access": "project"}), "names its project"),
                     (data_policy({"access": "embargoed"}), "an embargo is a record's"),
                     ({**good, "egress": {"endpoints": [{"id": "x", "access": ["secret"]}]}}, "lists levels"),
                     ({**good, "egress": {"endpoints": [{"id": "x", "access": []}, {"id": "x", "access": []}]}}, "given once")]:
        with pytest.raises(ScaleError, match=why):
            audience.parse_policy(bad)
    # an unreadable policy holds the repository to public rules, and says so
    repo = new_repo(tmp_path)
    write(repo, audience.POLICY_FILE, "scale_data_policy: 1\nrepository: {readers: {access: projct}}\n")
    write(repo, "g.json", doc("project", PROJECT_A))
    git(repo, "add", ".")
    assert rules_of(guard.check_staged(repo)) == ["access", "policy"]


# ---------------------------------------------------------------- egress
def test_the_egress_gate_denies_non_public_payloads_by_default(tmp_path):
    private, public = doc("project", PROJECT_A, name=CANARY), doc("public")
    assert audience.payload_levels(private) == {"project"} and audience.payload_levels(public) == {"public"}
    EgressGate().check(public)                                   # public: anywhere
    with pytest.raises(EgressDenied, match="'unknown' is not approved for that level"):
        EgressGate().check(private)
    pol = tmp_path / "egress.yaml"
    pol.write_text("egress:\n  endpoints:\n  - id: local\n    url: http://localhost:11434\n    access: [project]\n")
    ends = load_endpoints(pol)
    EgressGate("local", ends).check(private)
    with pytest.raises(EgressDenied):
        EgressGate("anthropic", ends).check(private)
    with pytest.raises(EgressDenied, match="institution"):   # approving project does not approve institution
        EgressGate("local", ends).check(doc("institution"))
    # without a policy of its own, the gate reads the data repository holding the document
    repo = tmp_path / "data"
    repo.mkdir()
    (repo / audience.POLICY_FILE).write_text(json.dumps({**data_policy({"access": "project", "project": PROJECT_A}),
                                                         "egress": {"endpoints": [{"id": "local", "access": ["project"]}]}}))
    (repo / "graphs").mkdir()
    EgressGate("local").check(private, where=repo / "graphs" / "g.json")
    with pytest.raises(EgressDenied):
        EgressGate("other").check(private, where=repo / "graphs" / "g.json")


def test_the_agent_tools_given_a_gate_never_return_canary_records_to_an_unapproved_endpoint(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    private = doc("project", PROJECT_A, name=CANARY)
    (ws / "p.json").write_text(json.dumps(private))
    (ws / "pub.json").write_text(json.dumps(doc("public")))
    plan = {"match": {"label": "CatalyticTest", "as": "t"}, "return": ["t.name"]}
    kit = ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate("anthropic", []))
    for name, args in [("query", {"path": "p.json", "plan": plan}), ("query", {"document": private, "plan": plan}),
                       ("search", {"path": "p.json", "query": "yield"}), ("validate", {"path": "p.json"}),
                       ("trace_ai", {"path": "p.json"})]:
        out = kit.call(name, args)
        assert "egress denied" in out.get("error", ""), (name, out)
        assert CANARY not in json.dumps(out)
    assert "error" not in kit.call("query", {"path": "pub.json", "plan": plan})
    # no gate (a Python caller keeping the results itself): the tools answer as before
    assert CANARY in json.dumps(ScaleToolkit(workspace=ws, repo=REPO).call("query", {"path": "p.json", "plan": plan}))
    # the MCP server reads its gate from the environment: none approved, nothing private goes out
    from scale.mcp_server import MCPServer
    monkeypatch.delenv("SCALE_EGRESS_POLICY", raising=False)
    monkeypatch.setenv("SCALE_EGRESS_ENDPOINT", "anthropic")
    srv = MCPServer(ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate.from_env()))
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "scale_query", "arguments": {"path": "p.json", "plan": plan}}}
    reply = json.dumps(srv.handle(msg))
    assert "egress denied" in reply and CANARY not in reply
    # approved for the project level: the endpoint receives the records
    pol = tmp_path / "egress.yaml"
    pol.write_text("egress:\n  endpoints:\n  - id: anthropic\n    access: [project]\n")
    monkeypatch.setenv("SCALE_EGRESS_POLICY", str(pol))
    srv = MCPServer(ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate.from_env()))
    assert CANARY in json.dumps(srv.handle(msg))


def test_scale_call_is_gated_by_the_environment(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "p.json").write_text(json.dumps(doc("project", PROJECT_A, name=CANARY)))
    monkeypatch.delenv("SCALE_EGRESS_POLICY", raising=False)
    monkeypatch.delenv("SCALE_EGRESS_ENDPOINT", raising=False)
    args = json.dumps({"path": "p.json", "plan": {"match": {"label": "CatalyticTest", "as": "t"}, "return": ["t.name"]}})
    assert main(["--repo", str(REPO), "call", "--workspace", str(ws), "query", args]) == 1
    out = capsys.readouterr().out
    assert "egress denied" in out and CANARY not in out


def test_a_private_graph_is_never_contributed_to_the_commons():
    from scale import commons
    with pytest.raises(ScaleError, match="only a public graph"):
        commons.contribute(doc("project", PROJECT_A), REPO / "graphs" / "catal-commons", {"class": "open"})


# ---------------------------------------------------------------- the explorer
def test_the_explorer_gates_every_request_drawn_from_a_graph_that_is_not_public():
    js = (REPO / "site/explorer.js").read_text()
    from scale import site
    assert "egress.js" in site.PAGES and 'from "./egress.js";' in js
    loop = js[js.index("async function modelLoop"):js.index("const OWN_OUTPUT")]
    # every provider request of the loop is preceded by the gate
    assert loop.count("gate(provider);") == 2
    assert loop.index("gate(provider);") < loop.index("await claude(") and loop.rindex("gate(provider);") < loop.index("await chat(")
    own = js[js.index("async function askOwnKey"):js.index("const ANSWERED_EARLY")]
    assert own.index("egressGate(levels") < own.index("modelLoop(") and "gate: check" in own
    svc = js[js.index("async function ask(question)"):js.index("// The answer, the query plans")]
    assert svc.index("check()") < svc.index('call("POST", "/ask"')
    contrib = js[js.index("async function contribute()"):js.index("async function start()")]
    assert contrib.index("graphLevels()") < contrib.index("contributeSignedIn") and "only a public graph" in contrib
    # approvals live in this page only: never stored
    assert "const egressApprovals = new Set();" in js
    assert not any("egressApprovals" in ln for ln in js.splitlines() if "store." in ln or "Storage" in ln)


def test_the_explorer_egress_module_never_lets_canary_records_reach_a_mocked_provider():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    canary = doc("project", PROJECT_A, name=CANARY)
    script = (f"import * as E from {json.dumps((REPO / 'site/egress.js').as_uri())};\n"
              f"const doc = {json.dumps(canary)};\n"
              "const sent = []; const sink = async (body) => { sent.push(JSON.stringify(body)); return { ok: true }; };\n"
              "const approvals = new Set(); const out = {};\n"
              "const levels = E.closedLevels(doc, '2026-06-01');\n"
              "async function loop(check) {                    // the explorer's loop: the gate before each request\n"
              "  for (let turn = 0; turn < 2; turn++) { check(); await sink({ tool_result: doc.nodes[0].props.name }); } }\n"
              "try { await loop(E.gate(levels, 'provider:anthropic', 'Anthropic', approvals)); out.first = 'sent'; }\n"
              "catch (e) { out.first = e.name; out.message = e.message; }\n"
              "out.sentBefore = sent.length;\n"
              "E.approve(levels, 'provider:openai', approvals);  // another provider's approval opens nothing here\n"
              "try { await loop(E.gate(levels, 'provider:anthropic', 'Anthropic', approvals)); } catch (e) { out.second = e.name; }\n"
              "E.approve(levels, 'provider:anthropic', approvals);\n"
              "await loop(E.gate(levels, 'provider:anthropic', 'Anthropic', approvals));\n"
              "out.levels = levels; out.sent = sent;\n"
              "out.cases = {\n"
              "  public: E.closedLevels({graph: {stewardship: {access: 'public'}}, nodes: [{props: {}}], links: []}),\n"
              "  missing: E.closedLevels({graph: {}, nodes: [], links: []}),\n"
              "  malformed: E.closedLevels({graph: {stewardship: 'public'}, nodes: [], links: []}),\n"
              "  held: E.closedLevels({graph: {stewardship: {access: 'public'}}, nodes: [{props: {stewardship: {access: 'embargoed', embargo_until: '2099', access_after_embargo: 'public'}}}], links: []}, '2026-06-01'),\n"
              "  lapsed: E.closedLevels({graph: {stewardship: {access: 'public'}}, nodes: [{props: {stewardship: {access: 'embargoed', embargo_until: '2020-02', access_after_embargo: 'public'}}}], links: []}, '2026-06-01'),\n"
              "  badDate: E.closedLevels({graph: {stewardship: {access: 'public'}}, nodes: [{props: {stewardship: {access: 'embargoed', embargo_until: '2020-13', access_after_embargo: 'public'}}}], links: []}, '2026-06-01') };\n"
              "console.log(JSON.stringify(out));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["levels"] == ["project"] and out["first"] == "EgressDenied" and out["sentBefore"] == 0
    assert "Nothing drawn from the graph left this browser" in out["message"] and out["second"] == "EgressDenied"
    assert len(out["sent"]) == 2 and all(CANARY in s for s in out["sent"])      # only after the reader approved
    assert out["cases"] == {"public": [], "missing": ["institution"], "malformed": ["institution"],
                            "held": ["institution"], "lapsed": [], "badDate": ["institution"]}


def test_python_and_the_explorer_read_the_same_levels():
    """site/egress.js mirrors pgraph.openness: the same documents give the same closed levels."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cases = [doc("public"), doc("project", PROJECT_A), doc(), doc("consortium"),
             doc("project", PROJECT_A, record_access={"access": "public"}),
             doc("public", embargo={"embargo_until": "2026-05", "access_after_embargo": "consortium"}),
             doc("public", embargo={"embargo_until": "2026-06-01", "access_after_embargo": "public"}),
             {"graph": {"stewardship": ["x"]}, "nodes": [{"id": "a", "props": None}], "links": []}]
    script = (f"import {{ closedLevels }} from {json.dumps((REPO / 'site/egress.js').as_uri())};\n"
              f"console.log(JSON.stringify({json.dumps(cases)}.map((d) => closedLevels(d, '2026-06-01'))));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    from scale import pgraph
    with_today = []
    for d in cases:
        lv = audience.payload_levels(d, "2026-06-01")
        with_today.append([x for x in pgraph.OPENNESS if x != "public" and x in lv])
    assert json.loads(run.stdout) == with_today


def test_scale_check_runs_the_guard_over_every_tracked_file():
    from scale.devcheck import check_steps
    cmds = {s["name"]: s["cmds"] for s in check_steps(REPO, fast=True)}
    step = cmds["no undeclared raw data, no document the public may not read (scale guard)"]
    assert step[0][-4:] == ["check", "--all", "--path", str(REPO)]


# ---------------------------------------------------------------- what the review of the first version found
def test_documents_are_found_in_any_json_spelling_and_in_yaml_by_their_shape(tmp_path):
    import yaml as _yaml
    repo = new_repo(tmp_path)
    private = doc("project", PROJECT_A, name=CANARY)
    write(repo, "a.json", json.dumps(private).replace("scale-catal", "scale\\u002dcatal"))
    write(repo, "b.yaml", json.dumps(private))                    # JSON is YAML flow style
    write(repo, "c.yml", _yaml.safe_dump(private).replace("\n", "\r\n"))
    write(repo, "d.yaml", "graph: {schema: scale-catal\nnodes: [\n")   # a candidate that does not parse
    write(repo, "e.yaml", "title: a plain YAML file\nitems: [1, 2]\n")
    git(repo, "add", ".")
    by = {p["path"]: p["rule"] for p in guard.check_staged(repo)["problems"]}
    assert by == {"a.json": "access", "b.yaml": "access", "c.yml": "access", "d.yaml": "malformed"}


def test_deleting_a_policy_or_a_parts_header_rechecks_what_it_governed(tmp_path):
    repo = new_repo(tmp_path, policy=data_policy({"access": "project", "project": PROJECT_A}))
    write(repo, "g.json", doc("project", PROJECT_A, name=CANARY))
    head = {k: v for k, v in doc("project", PROJECT_A).items() if k not in ("nodes", "links")}
    write(repo, "h/parts/header.json", head)
    write(repo, "h/parts/shared.json", {"schema_version": HEAD_VERSION, "nodes": [], "links": []})
    write(repo, "h/parts/sources/s1.json", {"source": "lab:h/source/s1", "schema_version": HEAD_VERSION,
                                            "nodes": [{"id": "lab:h/source/s1", "label": "Source", "props": {}}],
                                            "links": []})
    git(repo, "add", ".")
    assert guard.check_staged(repo)["ok"]
    git(repo, "commit", "-qm", "data", "--no-verify")
    git(repo, "rm", "-q", "--cached", audience.POLICY_FILE)       # now held to public rules: the project data fails
    res = guard.check_staged(repo)
    assert {p["path"] for p in res["problems"]} == {"g.json", "h"}
    git(repo, "reset", "-q")
    git(repo, "rm", "-q", "--cached", "h/parts/header.json")      # the parts left cannot build
    assert [(p["rule"], p["path"]) for p in guard.check_staged(repo)["problems"]] == [("malformed", "h/parts/")]


def test_removing_the_policy_does_not_open_the_history_to_another_remote(tmp_path):
    repo = new_repo(tmp_path, policy=data_policy({"access": "project", "project": PROJECT_A}))
    write(repo, "g.json", doc("project", PROJECT_A, name=CANARY))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "data", "--no-verify")
    git(repo, "rm", "-q", "-r", ".")
    write(repo, "README.md", "nothing here\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "gone", "--no-verify")
    res = guard.check_push(repo, "public", url="https://example.org/public.git", rng="main")
    assert res["commits"] == 2 and [p["rule"] for p in res["problems"]] == ["destination"]


def test_tracking_refs_count_only_for_the_url_the_remote_fetches_from(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = new_repo(tmp_path)
    write(repo, "g.json", doc("institution", name=CANARY))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "private", "--no-verify")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "--no-verify", "origin", "main")       # origin/main now reaches the private commit
    tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    line = [("refs/heads/main", tip, "refs/heads/main", guard.ZERO)]
    assert guard.check_push(repo, "origin", url=str(remote), lines=line)["commits"] == 0
    other = guard.check_push(repo, "origin", url="https://example.org/other.git", lines=line)
    assert other["commits"] == 1 and not other["ok"]
    # remote URLs are compared exactly (case and a .git suffix name other repositories)
    assert guard._norm_url("ssh://h/team/Private.git/") == "ssh://h/team/Private.git" != guard._norm_url("/team/private")


def test_a_gated_tool_refuses_unreadable_or_replaced_documents_without_their_diagnostics(tmp_path):
    ws = tmp_path / "ws"
    (ws / "g" / "parts" / "sources").mkdir(parents=True)
    head = {k: v for k, v in doc("project", PROJECT_A).items() if k not in ("nodes", "links")}
    (ws / "g" / "parts" / "header.json").write_text(json.dumps(head))
    rec = {"id": f"lab:g/s1/{CANARY}", "label": "CatalyticTest", "props": {}}
    for s in ("s1", "s2"):                                        # the same record twice: the parts do not build
        (ws / "g" / "parts" / "sources" / f"{s}.json").write_text(json.dumps(
            {"source": f"lab:g/source/{s}", "schema_version": HEAD_VERSION, "nodes": [rec], "links": []}))
    kit = ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate("anthropic", []))
    out = kit.call("validate", {"path": "g"})
    assert "cannot be read to check its access" in out["error"] and CANARY not in json.dumps(out)
    # a public file replaced by a private one while the tool runs: nothing is returned
    (ws / "p.json").write_text(json.dumps(doc("public")))
    kit2 = ScaleToolkit(workspace=ws, repo=REPO, egress=EgressGate("anthropic", []))
    real = kit2._handlers["scale_validate"]

    def swap(**kw):
        (ws / "p.json").write_text(json.dumps(doc("project", PROJECT_A, name=CANARY)) + " ")
        return real(**kw)
    kit2._handlers["scale_validate"] = swap
    out = kit2.call("validate", {"path": "p.json"})
    assert "changed while the tool ran" in out["error"] and CANARY not in json.dumps(out)


def test_the_explorer_reads_embargoes_on_the_earlier_of_the_local_and_utc_dates():
    import os
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = (REPO / "site/explorer.js").read_text()
    loop = js[js.index("async function modelLoop"):js.index("const OWN_OUTPUT")]
    assert loop.count("gate(provider);") == 2                    # the provider the request actually goes to
    wt = js[js.index("async function contributeWithToken"):js.index("async function watchForPullRequest")]
    assert wt.index("graphLevels()") < wt.index('rpc({ op: "contribution"')
    script = (f"import {{ todayISO, closedLevels }} from {json.dumps((REPO / 'site/egress.js').as_uri())};\n"
              "const d = new Date('2026-11-25T01:00:00Z');\n"
              "const st = {graph: {stewardship: {access: 'public'}}, links: [], nodes: [{props: {stewardship: "
              "{access: 'embargoed', embargo_until: '0000-01-01', access_after_embargo: 'public'}}}]};\n"
              "console.log(JSON.stringify([todayISO(d), closedLevels(st, '2026-01-01')]));")
    run = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "TZ": "America/New_York"})
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == ["2026-11-24", ["institution"]]
