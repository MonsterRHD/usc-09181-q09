"""Durability: after the program runs again, reviews and access records
are still queryable (everything lives in SQLite)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402
from dataroom import Clock, DataRoomService, Store  # noqa: E402


def test_restart_reconstructs_full_state():
    fd, path = tempfile.mkstemp(prefix="dataroom-restart-", suffix=".db")
    os.close(fd)
    try:
        w = World(path, now="2026-09-18T09:00:00Z")
        req, doc = w.request_with_document()
        review = w.svc.open_review(doc["id"], 1, w.legal["id"], "pending item")
        w.svc.access_document(w.fin["id"], doc["id"], "QoE check")
        try:
            w.svc.access_document(w.tax["id"], doc["id"], "overreach")
        except Exception:
            pass
        pid, rid, doc_id, rev_id, legal_id, fin_id, tax_id = (
            w.pid, req["id"], doc["id"], review["id"],
            w.legal["id"], w.fin["id"], w.tax["id"])
        w.store.close()

        # --- simulate a fresh process ---
        store2 = Store(path)
        svc2 = DataRoomService(store2, Clock("2026-09-19T10:00:00Z"))

        pending = svc2.pending_reviews(pid)
        assert len(pending) == 1 and pending[0]["id"] == rev_id

        records = svc2.access_records(pid, doc_id)
        assert {r["decision"] for r in records} == {"watermarked",
                                                    "denied_clearance"}
        # permissions and audit survive
        grant = svc2.get_permission(pid, fin_id)
        assert grant["role"] == "reviewer" and grant["revoked_at"] is None
        assert svc2.permission_audit(pid, fin_id)
        # document version content survives via content-addressed blob
        ver = svc2.get_version_number(doc_id, 1)
        assert ver["sha256"]
        store2.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass
