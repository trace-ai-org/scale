"""scale.curation_tools: the one set of tools a model curates a source with, in the service and in the browser."""

from conftest import REPO
from scale import curate
from scale.curation_tools import STOP, CurationTools

TEXT = ("[page 1]\nResults. The copper catalyst gave 42 % selectivity to methanol at about 250 °C.\n\n"
        "[page 2]\nWe conclude that copper is the active phase.\n")


def started(tmp_path):
    (tmp_path / "paper.txt").write_text(TEXT, encoding="utf-8")
    curate.start(tmp_path / "w", text=tmp_path / "paper.txt", repo=REPO)
    return CurationTools(tmp_path / "w", REPO, where="a test")


def test_the_tools_are_defined_once_with_done_last_and_the_skill_as_the_system_prompt(tmp_path):
    ct = started(tmp_path)
    names = [t["name"] for t in ct.definitions()]
    assert names == ["read_pages", "describe", "graph_summary", "get_records", "upsert", "remove", "check", STOP]
    assert all(t["input_schema"]["type"] == "object" for t in ct.definitions())
    assert "title" in ct.definitions()[-1]["input_schema"]["properties"]          # done names the graph concisely
    assert "You curate in a test" in ct.system() and "curate" in ct.system().lower()
    assert ct.first().endswith("The source has 2 pages of text.")


def test_records_are_written_checked_against_the_page_text_and_removed_but_the_source_stays(tmp_path):
    ct = started(tmp_path)
    sid = ct.info["source_id"]
    assert "[page 2]\nWe conclude" in ct.call("read_pages", {"first": 2, "last": 2})
    claim = {"id": f"{sid.replace('/source/', '/')}/claim/cu-active", "label": "Claim",
             "props": {"name": "copper is the active phase", "statement": "Copper is the active phase.",
                       "claim_kind": "conclusion", "claim_status": "proposed",
                       "evidence": [{"kind": "text", "source": sid, "locator": "p. 2",
                                     "quote": "copper is the active phase"}]}}
    assert ct.call("upsert", {"nodes": [claim]})["nodes"] == 4
    res = ct.call("check", {})
    assert res["quotes_checked_against_text"] and res["records"]["with_evidence"] == 1
    assert not [e for e in res["errors"] if "quote" in str(e).lower()]       # the quote is found in the page text
    gone = ct.call("remove", {"ids": [claim["id"], sid]})
    assert gone["removed"] == [claim["id"]] and gone["kept"] == [sid]
    try:
        ct.call("nonesuch", {})
    except ValueError as e:
        assert "unknown tool" in str(e)
    else:
        raise AssertionError("an unknown tool must raise")
