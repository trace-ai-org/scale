"""The repository's intake of a graph contributed by pull request, run by ``.github/workflows/commons-intake.yml``.

The graph explorer's *Contribute* (with no web service running) downloads the reader's graph, its quotes withheld, and
opens GitHub's upload page for ``graphs/catal-commons/incoming/``: GitHub opens the pull request under the reader's
own login, which is their credit. This intake then, for that pull request:

1. reads its files as data (never running anything from it): only ``graphs/catal-commons/incoming/<name>.json``,
   each a scale-catal graph of one source, at most ``MAX_BYTES``;
2. folds each into catal-commons with ``scale graph contribute`` (``commons.contribute``), crediting the pull
   request's author: the source is taken as in copyright (``RIGHTS``), so no quote is kept, and the commons must build
   and validate with it; a source the commons holds, or an open contribution proposes, is said and not added again;
3. opens the fold pull request (a ``contrib/<slug>`` branch, with the bot's token, ``SCALE_COMMONS_TOKEN``, so the
   checks run on it), for a person to review, and closes the contributor's pull request with a comment that links it.

Without the bot's token it only comments what folding would add. ``python -m scaleweb.intake --pr N --repo O/R``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Callable

from scale import commons
from scale.util import ScaleError, find_repo_root

from .github import BRANCH_PREFIX, RAW, Repo, http

COMMONS = "graphs/catal-commons"
INCOMING = re.compile(rf"{COMMONS}/incoming/[A-Za-z0-9][A-Za-z0-9._ ()-]{{0,150}}\.json")
MAX_BYTES = 8_000_000
# A graph contributed by pull request: nobody verified its source's licence, so it is taken as in copyright.
RIGHTS = {"class": "in_copyright", "licence": "not verified",
          "basis": "contributed by pull request from a reader's own curation; the source's licence was not verified, so "
                   "it is taken as in copyright"}


def _comment(repo: Repo, pr: int, body: str) -> None:
    repo._api("POST", f"/issues/{pr}/comments", {"body": body})


def _graphs(repo: Repo, pr: dict, request: Callable) -> tuple[list[tuple[str, dict]], list[str]]:
    """The pull request's contributed graphs, read as data, and what is wrong with its files."""
    files = repo._api("GET", f"/pulls/{pr['number']}/files?per_page=100")
    head = pr["head"]
    out, problems = [], []
    for f in files:
        path = f["filename"]
        if not INCOMING.fullmatch(path) or f["status"] not in ("added", "modified", "renamed"):
            problems.append(f"`{path}`: a contribution adds graphs only, as `{COMMONS}/incoming/<name>.json`")
            continue
        url = f"{RAW}/{head['repo']['full_name']}/{head['sha']}/{urllib.parse.quote(path)}"
        text = request("GET", url, repo.token, None, raw=True)
        if len(text.encode("utf-8")) > MAX_BYTES:
            problems.append(f"`{path}`: larger than {MAX_BYTES // 1_000_000} MB")
            continue
        try:
            doc = json.loads(text)
        except ValueError as e:
            problems.append(f"`{path}`: not JSON ({e})")
            continue
        if not isinstance(doc, dict) or (doc.get("graph") or {}).get("schema") != "scale-catal" \
                or len(commons.sources_of(doc)) != 1:
            problems.append(f"`{path}`: not a scale-catal graph of one source")
            continue
        out.append((path, doc))
    if not out and not problems:
        problems.append("no graph found")
    return out, problems


def _fold_text(author: str, pr: int, doc: dict, res: dict) -> tuple[str, str]:
    src = next(n for n in doc["nodes"] if n.get("label") == "Source").get("props") or {}
    name = (doc.get("graph") or {}).get("title") or src.get("title") or res["slug"]     # the concise title the page gave
    title = f"catal-commons: add {name[:70]}{'…' if len(name) > 70 else ''}"
    p = res
    body = "\n".join([
        f"**What it adds.** `{p['source']}` to `{COMMONS}`: {p['records']['triples']} triples ({p['records']['nodes']} "
        f"records and {p['records']['links']} links) in `parts/sources/{p['slug']}.json`, {len(p['shared_added'])} new "
        f"shared records and {len(p['shared_with_new_evidence'])} shared records with new evidence, the source's entry "
        "in `rights.yaml`, and the contribution in `contributors.yaml` (the leaderboard).",
        "",
        f"**Who.** Contributed by @{author} in #{pr}, curated in their own browser with their own model key by the "
        "curate-corpus skill. The source's licence was not verified, so it is taken as in copyright: "
        f"{p['quotes']['withheld']} quotes are withheld as sha256 and length, none kept. Check its values against the "
        "source.",
        "",
        f"**Repeats discarded.** {len(p['repeats']['records'])} shared records, {p['repeats']['links']} links and "
        f"{p['repeats']['values']} values catal-commons holds already."
        + (f" **{len(p['conflicts'])} values differ from the held records**, left out for the reviewer." if p["conflicts"] else ""),
        "",
        "**Checked.** The source is not in catal-commons on main (by DOI, identifier, text digest, or title and year) "
        "nor in an open contribution; the commons builds and validates with it (`scale graph contribute`).",
        "",
        f"Closes #{pr}. Opened by the commons intake (`service/scaleweb/intake.py`); a person reviews and merges it.",
    ])
    return title, body


def intake(pr_number: int, repo_name: str, read_token: str, bot_token: str = "", repo_root: Path | None = None,
           request: Callable = http, today: str | None = None) -> dict:
    """Fold the graphs a pull request contributes into catal-commons (module docstring); returns what was done."""
    repo_root = Path(repo_root) if repo_root else find_repo_root()
    today = today or datetime.date.today().isoformat()
    reader = Repo(repo_name, read_token, COMMONS, request)
    writer = Repo(repo_name, bot_token or read_token, COMMONS, request)
    pr = reader._api("GET", f"/pulls/{pr_number}")
    author = pr["user"]["login"]
    graphs, problems = _graphs(reader, pr, request)
    if problems:
        _comment(writer, pr_number, "**Not folded into catal-commons.**\n\n" + "\n".join(f"- {p}" for p in problems)
                 + "\n\nContribute from the graph explorer (its *Contribute* button prepares the file), or see "
                 f"`{COMMONS}/incoming/README.md`.")
        return {"state": "refused", "problems": problems}
    d = Path(tempfile.mkdtemp(prefix="scale-intake-"))
    lines, opened = [], []
    try:
        base_sha = reader.fetch_folder(d)
        proposed = reader.open_contributions()
        for path, doc in graphs:
            try:
                res = commons.contribute(doc, d, dict(RIGHTS), repo_root, contributor=author, on=today)
            except ScaleError as e:
                lines.append(f"- `{path}`: not folded: {e}")
                continue
            if res.get("already"):
                lines.append(f"- `{path}`: catal-commons holds this source already ("
                             + ", ".join(f"`{h['file']}`, the same {', '.join(h['by'])}" for h in res["already"]) + ").")
                continue
            hit = next((p for p in proposed if commons.find(doc, {"sources": p["sources"]})), None)
            if hit:
                lines.append(f"- `{path}`: proposed already in #{hit['number']}.")
                continue
            if not bot_token:
                lines.append(f"- `{path}`: would add `{res['source']}`, {res['records']['triples']} triples "
                             f"({res['records']['nodes']} records, {res['records']['links']} links), crediting @{author}. "
                             "The fold pull request opens once the repository's `SCALE_COMMONS_TOKEN` is set.")
                continue
            title, body = _fold_text(author, pr_number, doc, res)
            try:
                out = writer.open_pull_request(base_sha, f"{BRANCH_PREFIX}{res['slug']}", res["files"], title, body,
                                               f"{title}\n\nContributed by @{author} in #{pr_number}.")
            except FileExistsError as e:
                lines.append(f"- `{path}`: {e}")
                continue
            opened.append(out)
            lines.append(f"- `{path}`: folded into #{out['number']}, {res['records']['triples']} triples credited to @{author}.")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    head = "**Thank you.** " if opened else "**catal-commons intake.** "
    _comment(writer, pr_number, head + "\n".join(lines) + (
        "\n\nThis pull request closes: the fold pull request carries the contribution, for a person to review."
        if opened else ""))
    if opened:
        writer._api("PATCH", f"/pulls/{pr_number}", {"state": "closed"})
    return {"state": "folded" if opened else "commented", "opened": opened, "lines": lines}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--repo", required=True)
    a = ap.parse_args(argv)
    res = intake(a.pr, a.repo, os.environ["GITHUB_TOKEN"], os.environ.get("SCALE_COMMONS_TOKEN", ""))
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
