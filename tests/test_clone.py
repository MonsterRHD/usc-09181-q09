"""Project copy: content/evidence duplicated, permissions not."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402
from dataroom.errors import AuthorizationError  # noqa: E402


def test_clone_copies_versions_and_evidence_but_not_permissions():
    w = World()
    req, doc = w.request_with_document()
    review = w.svc.open_review(doc["id"], 1, w.legal["id"], "clause review")
    w.svc.decide_review(review["id"], "flagged", w.legal["id"], "issue")
    citation = w.svc.cite_evidence(review["id"], doc["id"], 1)
    w.svc.upload_version(doc["id"], b"v2 payroll", w.fin["id"], "v2")
    w.svc.supplier_withdraw(doc["id"], w.admin["id"], "consent withdrawn")

    clone, id_map = w.svc.clone_project(w.pid, "Project Merlion Phase 2",
                                        "MY", "Asia/Kuala_Lumpur", w.admin["id"])
    assert clone["country"] == "MY" and clone["timezone"] == "Asia/Kuala_Lumpur"

    # finance has NO grant in the clone yet → denied even though they
    # were a reviewer in the source project
    new_doc_id = id_map["documents"][doc["id"]]
    try:
        w.svc.access_document(w.fin["id"], new_doc_id, "sneaky peek")
        raise AssertionError("expected denial in fresh clone")
    except AuthorizationError as e:
        assert e.code == "denied_membership"

    # versions, withdrawal state and citation hashes survive the copy
    new_doc = w.svc.get_document(new_doc_id)
    assert new_doc["current_version"] == 2
    assert new_doc["supplier_withdrawn"] == 1
    versions = w.svc.list_versions(new_doc_id)
    assert [v["version"] for v in versions] == [1, 2]
    new_review_id = id_map["reviews"][review["id"]]
    cites = w.svc.list_citations(new_review_id)
    assert len(cites) == 1
    assert cites[0]["sha256"] == citation["sha256"]
    assert cites[0]["quoted_version"] == 1

    # after an explicit fresh grant, reviewer can resolve the frozen v1
    w.svc.grant_permission(clone["id"], w.legal["id"], "reviewer", 3,
                           w.admin["id"])
    resolved = w.svc.resolve_citation(cites[0]["id"], w.legal["id"])
    assert b"salary schedule v1" in resolved["content"]

    # source project untouched
    assert w.svc.get_project(w.pid)["country"] == "SG"
    w.close()
