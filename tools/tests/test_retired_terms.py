"""Names: SCALE is "Shared Concepts, Attributes, and Links with Evidence", one shared framework for evidence-backed
scientific knowledge graphs across domains (catalysis first), developed at github.com/trace-ai-org/scale. Its Python
distribution is scale-catal, with the commands scale and scale-catal.

Two rules hold in every live file. No file tells a reader to install the unrelated PyPI package schema (R1). And the
documentation does not describe a restricted-data integration: the variable that names the optional restricted copy
appears only in code, tests and workflows (R2). This repository keeps no archive of another repository's releases (its
own history starts at 0.1.0 on 2026-10-08)."""

import re
from pathlib import Path

from conftest import REPO
from scale import layout

R1 = re.compile(r"\b(?:pip3? install|uv pip install|uv tool install|uv add|uvx)(?:\s+--?[\w=.-]+)*\s+schema(?![\w-])", re.I)
R2 = re.compile(r"SCALE_RESTRICTED_DATA")

TEXT = (".py", ".md", ".yaml", ".yml", ".toml", ".in", ".cfg", ".cff", ".json", ".txt", ".sh")
NAMES = ("CODEOWNERS", "NOTICE", "LICENSE", "MANIFEST.in", ".gitignore")
CODE = re.compile(r"^(tools/|tools/tests/|core/tests/|domain/tests/|conftest\.py$|setup\.py$|\.github/workflows/)")
THIS = "tools/tests/test_retired_terms.py"
ALLOWED = {   # (file, text): deliberate mentions
    # agents are told never to install the unrelated package schema
    ("AGENTS.md", "Never write `pip install schema` or `uvx schema`"),
}


def _live_files() -> list[Path]:
    out = []
    for p in sorted(REPO.rglob("*")):
        rel = p.relative_to(REPO)
        if (not p.is_file() or (p.suffix not in TEXT and p.name not in NAMES)
                or any(x.startswith(".") and x not in (".github", ".gitignore") for x in rel.parts)
                or any(x.endswith(".egg-info") for x in rel.parts) or rel.parts[0] in layout.BUILD_DIRS
                or layout.is_local(rel)):
            continue
        out.append(p)
    return out


def _hits(rel: str, text: str) -> list[str]:
    # this file states R1's rule with examples of what it refuses
    rules = ([] if rel == THIS else [R1]) + ([] if CODE.match(rel) else [R2])
    return [f"{rel}:{i}: {line.strip()[:160]}" for i, line in enumerate(text.splitlines(), 1)
            if any(rx.search(line) for rx in rules) and not any(f == rel and t in line for f, t in ALLOWED)]


def test_live_files_use_no_retired_wording():
    files = _live_files()
    rels = {p.relative_to(REPO).as_posix() for p in files}
    assert {"README.md", "AGENTS.md", "CONTRIBUTING.md", "tools/mcp_server.py", "governance/GOVERNANCE.md",
            ".github/CODEOWNERS", "pyproject.toml", "CHANGELOG.md", "docs/decisions/0001-scale-0.1.0.md", THIS} <= rels
    hits = [h for p in files for h in _hits(p.relative_to(REPO).as_posix(), p.read_text(errors="ignore"))]
    assert hits == [], "wording the rules refuse:\n" + "\n".join(hits)


def test_every_allowed_mention_still_exists():
    """An ALLOWED entry whose text is gone would silently allow it elsewhere in the file later: keep the list exact."""
    stale = [(f, t) for f, t in sorted(ALLOWED) if t not in (REPO / f).read_text(errors="ignore")]
    assert stale == []


def test_no_archive_of_another_repository_is_kept():
    """What this repository does not carry: a record-format release archive, a multi-domain folder and shared actions.
    (Frozen versions, releases and decision records of this repository's own history are expected once it has them.)"""
    for gone in ("releases/legacy", "domains", ".github/actions"):
        assert not (REPO / gone).exists(), gone


def test_the_names_and_the_rules():
    from scale import layout as lay, updates, versions
    from scale.pgraph import SPEC_LINEAGE
    assert versions.DISTRIBUTION == "scale-catal" and SPEC_LINEAGE == "scale"
    assert lay.REPOSITORY_URL == "https://github.com/trace-ai-org/scale"
    assert updates.REMOTE_URL == "https://github.com/trace-ai-org/scale.git"
    assert "github_repo: trace-ai-org/scale\n" in (REPO / ".github/scale-project.yml").read_text()
    text = (REPO / "pyproject.toml").read_text()
    assert 'name = "scale-catal"' in text and 'scale-catal = "scale.cli:main"' in text
    assert "Shared Concepts, Attributes, and Links with Evidence" in text
    readme = (REPO / "README.md").read_text()
    assert ("**Shared Concepts, Attributes, and Links with Evidence: one shared framework for evidence-backed scientific\n"
            "knowledge graphs across domains.**") in readme
    for f in ("NOTICE", "CITATION.cff", "scale-release.yaml", "tools/__init__.py"):
        assert "Shared Concepts, Attributes, and Links with Evidence" in (REPO / f).read_text(), f
    # the rules: what they catch, and what they leave alone
    for refused in ("pip install schema", "uvx schema call status", "uv tool install --upgrade schema"):
        assert R1.search(refused), refused
    for fine in ("Shared Concepts, Attributes, and Links with Evidence", "uvx scale-catal call status",
                 "pip install scale-catal", "the catalysis domain", "access consortium", "the SCALE science kernel",
                 "follows_domains", "--domain catalysis", "https://github.com/trace-ai-org/scale", "the schema's version",
                 "scale:project/methanol-synthesis-pre1966", "examples/methanol-synthesis-pre1966/README.md"):
        assert not R1.search(fine) and not R2.search(fine), fine
    assert _hits("docs/commands.md", "set SCALE_RESTRICTED_DATA") and not _hits("tools/layout.py", "SCALE_RESTRICTED_DATA")
    assert _hits("README.md", "pip install schema") and not _hits("AGENTS.md", "Never write `pip install schema` or `uvx schema`")


def test_history_keeps_the_names_things_had():
    """The repository this one replaced numbered its own scale-graph schemas 0.1.0-0.8.0. A rename of the schema (to
    scale-catal from 0.2.0) never reaches that history: a sentence about the past keeps the old name."""
    import re
    for f in ("AGENTS.md", "README.md", "governance/repository-settings.md"):
        text = re.sub(r"\s+", " ", (REPO / f).read_text())
        assert "scale-graph numbering 0.1.0-0.8.0" in text, f
        assert "scale-catal numbering 0.1.0-0.8.0" not in text, f
