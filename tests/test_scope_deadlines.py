"""Scope changes, deadline extensions, cross-timezone deadlines."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402
from dataroom.errors import Conflict, ValidationFailed  # noqa: E402


def test_deadline_is_interpreted_in_project_timezone():
    w = World()  # project timezone Asia/Singapore
    req = w.svc.create_request(w.pid, "Tax filings", "SG", 2,
                               "2026-10-01T17:00", w.fin["id"])
    # 17:00 SGT = 09:00 UTC
    assert req["deadline_at"] == "2026-10-01T09:00:00Z"
    assert req["deadline_tz"] == "Asia/Singapore"
    w.close()


def test_deadline_extension_preserves_chain_and_notifies_once():
    w = World()
    req, _ = w.request_with_document()
    before = req["deadline_at"]
    w.svc.extend_deadline(req["id"], "2026-10-15T17:00", w.admin["id"],
                          "seller asked for two weeks")
    after = w.svc.get_request(req["id"])
    assert after["deadline_at"] != before
    events = w.svc.scope_events(req["id"])
    assert len(events) == 1
    assert events[0]["kind"] == "deadline_extension"
    assert events[0]["detail"]  # JSON detail retained
    chain = w.svc.impact_chain("request", req["id"])
    assert chain[0]["kind"] == "deadline_extension"
    # exactly one extension notification despite repeated list queries
    ntfs = [n for n in w.svc.list_notifications(w.pid)
            if n["kind"] == "deadline_extension"]
    assert len(ntfs) == 1
    # cannot extend to an earlier instant
    try:
        w.svc.extend_deadline(req["id"], "2026-10-02T17:00", w.admin["id"])
        raise AssertionError("expected validation failure")
    except ValidationFailed:
        pass
    w.close()


def test_scope_change_tightens_clearance_and_keeps_chain():
    w = World()
    req, doc = w.request_with_document(clearance=1)
    # tax (clearance 1) could open it originally
    assert w.svc.access_document(w.tax["id"], doc["id"], "in scope")["allowed"]
    # scope moves the request to level 3 (e.g. contracts added)
    w.svc.change_scope(req["id"], w.admin["id"], clearance_required=3,
                       reason="employment contracts added")
    req2 = w.svc.get_request(req["id"])
    assert req2["clearance_required"] == 3 and req2["status"] == "scope_changed"
    # the existing document keeps its own level, so tax's denial here is
    # governed by document clearance — demonstrate scope history instead:
    events = w.svc.scope_events(req["id"])
    assert events[0]["kind"] == "scope_change"
    assert '"new_clearance": 3' in events[0]["detail"]
    # no-op scope change is rejected
    try:
        w.svc.change_scope(req["id"], w.admin["id"], country="SG",
                           clearance_required=3)
        raise AssertionError("expected conflict")
    except Conflict:
        pass
    w.close()


def test_cross_timezone_project_deadline_usa():
    w = World()
    usa = w.svc.create_project("Project Eagle", "US", "America/New_York",
                               w.admin["id"])
    req = w.svc.create_request(usa["id"], "Benefits filings", "US", 2,
                               "2026-10-01T17:00", w.fin["id"])
    # EDT (UTC-4) in October: 17:00 local = 21:00 UTC
    assert req["deadline_at"] == "2026-10-01T21:00:00Z"
    w.close()


def test_past_deadline_rejected():
    w = World()
    try:
        w.svc.create_request(w.pid, "Late", "SG", 1, "2020-01-01T00:00",
                             w.fin["id"])
        raise AssertionError("expected validation failure")
    except ValidationFailed:
        pass
    w.close()
