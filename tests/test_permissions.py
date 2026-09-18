"""Least privilege: clearance boundaries, immediate adjustment, audit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402
from dataroom.errors import AuthorizationError  # noqa: E402


def test_clearance_boundary_blocks_payroll_for_tax():
    w = World()
    _, doc = w.request_with_document(clearance=2)
    # tax viewer (clearance 1) must not open the level-2 payroll file
    try:
        w.svc.access_document(w.tax["id"], doc["id"], "tax review")
        raise AssertionError("expected denial")
    except AuthorizationError as e:
        assert e.code == "denied_clearance"
    # finance reviewer (clearance 2) can
    out = w.svc.access_document(w.fin["id"], doc["id"], "QoE payroll check")
    assert out["allowed"]
    assert b"salary schedule v1" in out["content"]
    # the denial is auditable
    denials = w.svc.access_records(w.pid, doc["id"], "denied_clearance")
    assert len(denials) == 1 and denials[0]["actor_id"] == w.tax["id"]
    w.close()


def test_non_member_is_denied():
    w = World()
    outsider = w.svc.create_user("Otto Other", "finance", "usr_outsider")
    _, doc = w.request_with_document()
    try:
        w.svc.access_document(outsider["id"], doc["id"], "curiosity")
        raise AssertionError("expected denial")
    except AuthorizationError as e:
        assert e.code == "denied_membership"
    w.close()


def test_permission_downgrade_hits_unopened_content_immediately():
    w = World()
    _, doc = w.request_with_document(clearance=2)
    # finance can open today
    assert w.svc.access_document(w.fin["id"], doc["id"], "round 1")["allowed"]
    # admin tightens the grant to clearance 1
    w.svc.grant_permission(w.pid, w.fin["id"], "viewer", 1, w.admin["id"])
    # the very next open is denied — no caching of the old grant
    try:
        w.svc.access_document(w.fin["id"], doc["id"], "round 2")
        raise AssertionError("expected denial after downgrade")
    except AuthorizationError as e:
        assert e.code == "denied_clearance"
    w.close()


def test_permission_upgrade_takes_effect_immediately():
    w = World()
    _, doc = w.request_with_document(clearance=2)
    try:
        w.svc.access_document(w.tax["id"], doc["id"], "before upgrade")
        raise AssertionError("expected denial")
    except AuthorizationError:
        pass
    w.svc.grant_permission(w.pid, w.tax["id"], "reviewer", 2, w.admin["id"])
    out = w.svc.access_document(w.tax["id"], doc["id"], "after upgrade")
    assert out["allowed"]
    w.close()


def test_revocation_blocks_future_access_but_keeps_audit():
    w = World()
    _, doc = w.request_with_document()
    w.svc.access_document(w.fin["id"], doc["id"], "while permitted")
    w.svc.revoke_permission(w.pid, w.fin["id"], w.admin["id"])
    try:
        w.svc.access_document(w.fin["id"], doc["id"], "after revoke")
        raise AssertionError("expected denial")
    except AuthorizationError as e:
        assert e.code in ("denied_membership", "denied_departed")
    # historical allow + denial are both still there
    decisions = {r["decision"] for r in w.svc.access_records(w.pid, doc["id"])}
    assert "watermarked" in decisions
    audit = w.svc.permission_audit(w.pid, w.fin["id"])
    actions = [a["action"] for a in audit]
    assert actions == ["grant", "revoke"]
    assert audit[0]["after_json"] is not None  # grant snapshot
    assert audit[1]["before_json"] is not None  # pre-revoke snapshot
    w.close()


def test_viewer_cannot_request_superseded_version():
    w = World()
    _, doc = w.request_with_document()
    w.svc.upload_version(doc["id"], b"salary schedule v2", w.fin["id"], "fix")
    w.svc.grant_permission(w.pid, w.tax["id"], "viewer", 2, w.admin["id"])
    # default open gets the current version
    out = w.svc.access_document(w.tax["id"], doc["id"], "current only")
    assert b"v2" in out["content"]
    # asking explicitly for the old version is refused for a plain viewer
    try:
        w.svc.access_document(w.tax["id"], doc["id"], "dig old version", 1)
        raise AssertionError("expected deprecated_version denial")
    except AuthorizationError as e:
        assert e.code == "deprecated_version"
    # reviewer may still consult the old version
    out = w.svc.access_document(w.fin["id"], doc["id"], "history", 1)
    assert b"v1" in out["content"]
    w.close()


def test_watermark_identifies_viewer_purpose_and_time():
    w = World()
    _, doc = w.request_with_document()
    out = w.svc.access_document(w.fin["id"], doc["id"], "QoE payroll check")
    assert out["decision"] == "watermarked"
    assert "Finn Finance" in out["watermark"]
    assert "QoE payroll check" in out["watermark"]
    assert "viewer: Finn Finance" in out["content"].decode()
    rec = out["record"]
    assert rec["watermark"] == out["watermark"]
    w.close()
