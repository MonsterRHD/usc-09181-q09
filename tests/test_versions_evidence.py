"""Versioning & evidence: new uploads never replace cited evidence."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402


def test_versions_append_without_overwriting():
    w = World()
    _, doc = w.request_with_document()
    v1 = w.svc.list_versions(doc["id"])[0]
    assert v1["version"] == 1
    w.svc.upload_version(doc["id"], b"corrected payroll v2", w.fin["id"],
                         "supplier correction")
    versions = w.svc.list_versions(doc["id"])
    assert [v["version"] for v in versions] == [1, 2]
    # v1 bytes are untouched
    v1_now = w.svc.get_version_number(doc["id"], 1)
    assert v1_now["sha256"] == v1["sha256"]
    w.close()


def test_review_pins_exact_version_and_keeps_resolving_v1_after_v2():
    w = World()
    _, doc = w.request_with_document()
    review = w.svc.open_review(doc["id"], 1, w.legal["id"],
                               "contract clause check")
    w.svc.decide_review(review["id"], "flagged", w.legal["id"],
                        "change-of-control clause needs attention")
    citation = w.svc.cite_evidence(review["id"], doc["id"], 1)
    assert citation["quoted_version"] == 1
    assert citation["quoted_filename"] == "payroll.xlsx"

    # supplier uploads v2 — must not touch the cited v1
    w.svc.upload_version(doc["id"], b"salary schedule v2 with new total",
                         w.fin["id"], "revision")
    resolved = w.svc.resolve_citation(citation["id"], w.legal["id"])
    assert b"salary schedule v1" in resolved["content"]
    assert resolved["decision"] == "watermarked"
    assert resolved["version"]["version"] == 1
    # current pointer moved, cited version didn't
    assert w.svc.get_document(doc["id"])["current_version"] == 2
    w.close()


def test_withdrawn_document_rejects_new_uploads():
    w = World()
    _, doc = w.request_with_document()
    w.svc.supplier_withdraw(doc["id"], w.admin["id"], "party consent revoked")
    try:
        w.svc.upload_version(doc["id"], b"v2 should fail", w.fin["id"], "x")
        raise AssertionError("expected Conflict")
    except Exception as e:
        assert e.code == "conflict"
    w.close()


def test_withdrawn_document_denies_access_via_doc_and_link():
    w = World()
    _, doc = w.request_with_document()
    link = w.svc.create_link(doc["id"], 1, w.fin["id"], "external counsel review",
                             48)
    assert w.svc.access_link(link["id"])["allowed"]
    w.svc.supplier_withdraw(doc["id"], w.admin["id"], "consent revoked")
    from dataroom.errors import AuthorizationError, LinkRevoked
    try:
        w.svc.access_document(w.fin["id"], doc["id"], "after withdraw")
        raise AssertionError("expected denial")
    except AuthorizationError as e:
        assert e.code == "denied_withdrawn"
    try:
        w.svc.access_link(link["id"])
        raise AssertionError("expected controlled unavailability")
    except LinkRevoked as e:
        assert str(e) == "this link is no longer available"
    w.close()




def test_impact_chain_records_upload_and_withdraw():
    w = World()
    _, doc = w.request_with_document()          # this itself uploads v1
    w.svc.upload_version(doc["id"], b"v2", w.fin["id"], "second")
    w.svc.supplier_withdraw(doc["id"], w.admin["id"], "consent issue")
    chain = w.svc.impact_chain("document", doc["id"])
    kinds = [e["kind"] for e in chain]
    assert kinds == ["upload_version", "upload_version", "withdraw"]
    assert "consent issue" in chain[-1]["detail"]
    # upload events carry the version number for the chain of evidence
    assert chain[0]["detail"].__contains__('"version": 1')
    w.close()


def test_invalid_clearance_rejected_at_creation():
    w = World()
    from dataroom.errors import ValidationFailed
    try:
        w.svc.create_document(
            w.request_with_document()[0]["id"], "x", "secret", 9,
            w.fin["id"])
        raise AssertionError("expected validation error")
    except ValidationFailed:
        pass
    w.close()
