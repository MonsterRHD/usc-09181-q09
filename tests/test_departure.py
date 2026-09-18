"""Off-boarding: departed members lose access immediately."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402
from dataroom.errors import AuthorizationError, ValidationFailed  # noqa: E402


def test_departed_member_blocked_everywhere_and_cannot_be_regranted():
    w = World()
    p2 = w.svc.create_project("Project Bamboo", "JP", "Asia/Tokyo",
                              w.admin["id"])
    w.svc.grant_permission(p2["id"], w.fin["id"], "reviewer", 2,
                           w.admin["id"])
    _, doc = w.request_with_document()
    assert w.svc.access_document(w.fin["id"], doc["id"], "before leaving")["allowed"]

    w.svc.mark_user_departed(w.fin["id"])

    # blocked in both projects immediately
    for pid in (w.pid, p2["id"]):
        grant = w.svc.get_permission(pid, w.fin["id"])
        assert grant["revoked_at"] is not None
    try:
        w.svc.access_document(w.fin["id"], doc["id"], "after leaving")
        raise AssertionError("expected departed denial")
    except AuthorizationError as e:
        assert e.code == "denied_departed"

    # admin cannot reactivate access for a departed account
    try:
        w.svc.grant_permission(w.pid, w.fin["id"], "reviewer", 2,
                               w.admin["id"])
        raise AssertionError("expected validation failure")
    except ValidationFailed:
        pass

    # the earlier successful access and the denial remain auditable
    decisions = {r["decision"] for r in w.svc.access_records(w.pid, doc["id"])}
    assert "watermarked" in decisions and "denied_departed" in decisions
    # and the grant history was not erased
    assert any(a["action"] == "revoke"
               for a in w.svc.permission_audit(w.pid, w.fin["id"]))
    w.close()
