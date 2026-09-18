"""Share links: expiry, revocation, quota, controlled hints."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402
from dataroom.errors import LinkExpired, LinkRevoked, ValidationFailed  # noqa: E402


def _link(w, ttl=48, **kw):
    _, doc = w.request_with_document()
    return doc, w.svc.create_link(doc["id"], 1, w.fin["id"],
                                 kw.get("purpose", "external counsel review"),
                                 ttl, kw.get("watermark_policy", "stamp"),
                                 kw.get("max_accesses"))


def test_link_delivers_watermarked_pinned_version():
    w = World()
    doc, link = _link(w)
    out = w.svc.access_link(link["id"])
    assert out["allowed"]
    assert out["version"]["version"] == 1
    assert "external counsel review" in out["content"].decode()
    # link stays pinned to v1 after a new version lands
    w.svc.upload_version(doc["id"], b"v2 bytes", w.fin["id"], "new")
    out = w.svc.access_link(link["id"])
    assert out["version"]["version"] == 1
    assert b"v1" in out["content"]
    w.close()


def test_expired_link_returns_only_controlled_hint():
    w = World()
    _, link = _link(w, ttl=24)
    w.clock.advance(hours=25)
    try:
        w.svc.access_link(link["id"])
        raise AssertionError("expected expiry")
    except LinkExpired as e:
        assert str(e) == "this link is no longer available"
        assert e.http_status == 410
    # the expired attempt is still audited
    records = w.svc.access_records(decision="expired")
    assert records and records[0]["reason"] == "past expiry"
    w.close()


def test_revoked_link_returns_same_hint_and_keeps_prior_access_audit():
    w = World()
    _, link = _link(w)
    w.svc.access_link(link["id"])
    w.svc.revoke_link(link["id"], w.admin["id"], "deal abandoned")
    try:
        w.svc.access_link(link["id"])
        raise AssertionError("expected revocation")
    except LinkRevoked as e:
        assert str(e) == "this link is no longer available"
    # prior successful retrieval and the revoked try are both present
    recs = w.svc.access_records()
    dc = [r["decision"] for r in recs if r["link_id"] == link["id"]]
    assert "watermarked" in dc and "revoked" in dc
    w.close()


def test_unknown_link_token_gives_identical_hint():
    w = World()
    try:
        w.svc.access_link("lnk_does_not_exist")
        raise AssertionError("expected generic unavailability")
    except LinkExpired as e:
        assert str(e) == "this link is no longer available"
    w.close()


def test_link_access_quota():
    w = World()
    _, link = _link(w, max_accesses=2)
    assert w.svc.access_link(link["id"])["allowed"]
    assert w.svc.access_link(link["id"])["allowed"]
    try:
        w.svc.access_link(link["id"])
        raise AssertionError("expected quota exhaustion")
    except LinkExpired:
        pass
    w.close()


def test_link_requires_purpose():
    w = World()
    _, doc = w.request_with_document()
    try:
        w.svc.create_link(doc["id"], 1, w.fin["id"], "  ", 24)
        raise AssertionError("expected validation error")
    except ValidationFailed:
        pass
    w.close()
