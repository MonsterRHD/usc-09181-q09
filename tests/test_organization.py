"""Organization: requests, upload summaries and review conclusions are
grouped by project / country / clearance and stay queryable."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402


def test_request_pack_groups_documents_versions_and_review_conclusions():
    w = World()
    req1, doc1 = w.request_with_document(title="Payroll SG", country="SG")
    req2, _ = w.request_with_document(title="Contracts DE", country="DE",
                                      clearance=3, filename="employment.pdf",
                                      content=b"contract v1")
    w.svc.upload_version(doc1["id"], b"payroll v2", w.fin["id"],
                         "updated bonus schedule")

    review = w.svc.open_review(doc1["id"], 2, w.legal["id"], "bonus terms")
    w.svc.decide_review(review["id"], "flagged", w.legal["id"], "flag clause")
    w.svc.decide_review(review["id"], "approved", w.legal["id"],
                        "resolved after supplier note")
    w.svc.cite_evidence(review["id"], doc1["id"], 1)

    # requests are listed per project (order is by creation time; the
    # frozen clock gives identical timestamps here, so compare by title)
    requests = w.svc.list_requests(w.pid)
    titles = {r["title"] for r in requests}
    assert titles == {"Payroll SG", "Contracts DE"}
    by_country = {r["country"]: r for r in requests}
    assert by_country["DE"]["clearance_required"] == 3

    # documents hang off their request, with full version+summary history
    docs = w.svc.list_documents(req1["id"])
    assert len(docs) == 1 and docs[0]["current_version"] == 2
    versions = w.svc.list_versions(doc1["id"])
    assert [v["summary"] for v in versions] == [
        "initial upload", "updated bonus schedule"]

    # review packet exposes every conclusion in order and the citation
    history = w.svc.review_history(review["id"])
    conclusions = [c["conclusion"] for c in history["conclusions"]]
    assert conclusions == ["flagged", "approved"]
    assert len(history["citations"]) == 1
    assert history["citations"][0]["quoted_version"] == 1
    w.close()
