"""Bulk import: idempotent batches, partial failures, dedup notifications."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402


def _items():
    return [
        {"ref": "SG-001", "title": "Payroll", "country": "SG",
         "clearance_required": 2, "deadline": "2026-10-20T17:00",
         "filename": "payroll.xlsx", "content": "v1 payroll",
         "classification": "secret", "doc_clearance": 2},
        {"ref": "SG-002", "title": "Tax filings", "country": "SG",
         "clearance_required": 1, "deadline": "2026-10-21T17:00"},
        {"ref": "BAD-1", "title": "Expired", "country": "SG",
         "clearance_required": 1, "deadline": "2020-01-01T00:00"},
    ]


def test_import_creates_once_and_skips_on_rerun():
    w = World()
    first = w.svc.import_batch(w.pid, w.admin["id"], "batch-oct", _items())
    assert first["created"] == 2 and first["documents"] == 1
    assert len(first["errors"]) == 1 and first["errors"][0]["ref"] == "BAD-1"

    second = w.svc.import_batch(w.pid, w.admin["id"], "batch-oct", _items())
    # the two good rows are skipped; the bad row was never marked as
    # imported, so a rerun retries it instead of hiding the problem
    assert second["created"] == 0 and second["skipped"] == 2
    assert len(second["errors"]) == 1 and second["errors"][0]["ref"] == "BAD-1"

    requests = w.svc.list_requests(w.pid)
    assert len(requests) == 2
    # imported document got exactly one version and one notification
    docs = [d for r in requests
            for d in w.svc.list_documents(r["id"])]
    assert len(docs) == 1
    assert len(w.svc.list_versions(docs[0]["id"])) == 1
    new_version_notes = [n for n in w.svc.list_notifications(w.pid)
                         if n["kind"] == "new_version"]
    assert len(new_version_notes) == 1
    w.close()


def test_partial_batch_does_not_block_later_rows():
    w = World()
    items = [
        {"ref": "A", "title": "First", "country": "SG", "clearance_required": 1,
         "deadline": "2026-10-20T17:00"},
        {"ref": "B", "title": "Bad", "country": "SG", "clearance_required": 5,
         "deadline": "2026-10-20T17:00"},
        {"ref": "C", "title": "Third", "country": "SG", "clearance_required": 1,
         "deadline": "2026-10-20T17:00"},
    ]
    out = w.svc.import_batch(w.pid, w.admin["id"], "b1", items)
    assert out["created"] == 2
    assert [e["ref"] for e in out["errors"]] == ["B"]
    # rerun: A and C known, B retried and fails again, no duplicates
    out2 = w.svc.import_batch(w.pid, w.admin["id"], "b1", items)
    assert out2["skipped"] == 2 and len(out2["errors"]) == 1
    assert len(w.svc.list_requests(w.pid)) == 2
    w.close()


def test_distinct_batches_can_share_content_without_notification_spam():
    w = World()
    item = {"ref": "X1", "title": "Payroll", "country": "SG",
            "clearance_required": 2, "deadline": "2026-10-20T17:00",
            "filename": "p.xlsx", "content": "bytes", "doc_clearance": 2}
    w.svc.import_batch(w.pid, w.admin["id"], "b-a", [item])
    w.svc.import_batch(w.pid, w.admin["id"], "b-b",
                       [{**item, "ref": "X2", "title": "Payroll copy"}])
    # two different documents => two notifications, each deduped to one
    notes = [n for n in w.svc.list_notifications(w.pid)
             if n["kind"] == "new_version"]
    assert len(notes) == 2
    w.close()
