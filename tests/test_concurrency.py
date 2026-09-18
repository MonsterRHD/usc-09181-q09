"""Concurrency: parallel downloads, permission races, upload ordering."""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import World  # noqa: E402
from dataroom.errors import AuthorizationError  # noqa: E402


def test_concurrent_downloads_all_audited():
    w = World()
    _, doc = w.request_with_document()
    outcomes = []
    barrier = threading.Barrier(8)

    def worker(uid, purpose):
        barrier.wait()
        try:
            out = w.svc.access_document(uid, doc["id"], purpose)
            outcomes.append(("ok", out["decision"]))
        except AuthorizationError as e:
            outcomes.append(("denied", e.code))

    threads = []
    # 4 finance allowed, 4 tax denied
    for i in range(4):
        threads.append(threading.Thread(
            target=worker, args=(w.fin["id"], f"parallel finance {i}")))
        threads.append(threading.Thread(
            target=worker, args=(w.tax["id"], f"parallel tax {i}")))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes).count(("ok", "watermarked")) == 4
    assert sum(1 for kind, code in outcomes if kind == "denied") == 4
    records = w.svc.access_records(w.pid, doc["id"])
    decisions = [r["decision"] for r in records]
    assert decisions.count("watermarked") == 4
    assert decisions.count("denied_clearance") == 4
    w.close()


def test_concurrent_uploads_get_monotonic_versions():
    w = World()
    _, doc = w.request_with_document()
    errors = []
    versions = []
    barrier = threading.Barrier(6)

    def uploader(i):
        barrier.wait()
        try:
            v = w.svc.upload_version(doc["id"], f"content {i}".encode(),
                                     w.fin["id"], f"u{i}")
            versions.append(v["version"])
        except Exception as e:  # pragma: no cover - would be a bug
            errors.append(e)

    threads = [threading.Thread(target=uploader, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(versions) == [2, 3, 4, 5, 6, 7]
    assert w.svc.get_document(doc["id"])["current_version"] == 7
    w.close()


def test_revocation_racing_with_downloads():
    w = World()
    _, doc = w.request_with_document()
    results = []
    stop = threading.Event()
    first_ok = threading.Event()

    def downloader():
        i = 0
        while not stop.is_set():
            try:
                w.svc.access_document(w.fin["id"], doc["id"], f"race {i}")
                results.append("ok")
                first_ok.set()
            except AuthorizationError:
                results.append("denied")
            i += 1

    t = threading.Thread(target=downloader)
    t.start()
    # revoke only after at least one download has gone through
    assert first_ok.wait(5)
    w.svc.revoke_permission(w.pid, w.fin["id"], w.admin["id"])
    # let the racing loop observe the revocation too
    deadline = time.monotonic() + 5
    while "denied" not in results and time.monotonic() < deadline:
        pass
    stop.set()
    t.join()
    # and a call strictly *after* the revoke is guaranteed denied — the
    # grant is re-read on every open, nothing is cached
    try:
        w.svc.access_document(w.fin["id"], doc["id"], "definitely after")
        raise AssertionError("expected denial")
    except AuthorizationError as e:
        assert e.code == "denied_membership"
    assert "ok" in results and "denied" in results
    # every attempt, either way, was recorded
    recs = w.svc.access_records(w.pid, doc["id"])
    assert len(recs) == len(results) + 1
    w.close()


def test_withdrawal_racing_with_link_access():
    w = World()
    _, doc = w.request_with_document()
    link = w.svc.create_link(doc["id"], 1, w.fin["id"], "counsel", 48)
    outcomes = []
    stop = threading.Event()
    saw_ok = threading.Event()

    def opener():
        i = 0
        while not stop.is_set():
            try:
                w.svc.access_link(link["id"], f"use {i}")
                outcomes.append("ok")
                saw_ok.set()
            except Exception:
                outcomes.append("unavailable")
            i += 1

    t = threading.Thread(target=opener)
    t.start()
    assert saw_ok.wait(5)
    w.svc.supplier_withdraw(doc["id"], w.admin["id"], "consent revoked")
    deadline = time.monotonic() + 5
    while "unavailable" not in outcomes and time.monotonic() < deadline:
        pass
    stop.set()
    t.join()
    # once withdrawn, no success can slip through afterwards
    from dataroom.errors import LinkRevoked
    try:
        w.svc.access_link(link["id"])
        raise AssertionError("expected post-withdraw denial")
    except LinkRevoked:
        pass
    assert "ok" in outcomes and "unavailable" in outcomes
    w.close()


def test_concurrent_link_uses_respect_quota():
    w = World()
    _, doc = w.request_with_document()
    link = w.svc.create_link(doc["id"], 1, w.fin["id"], "counsel burst",
                             48, max_accesses=3)
    allowed, denied = [], []
    barrier = threading.Barrier(10)

    def hit():
        barrier.wait()
        try:
            w.svc.access_link(link["id"])
            allowed.append(1)
        except Exception:
            denied.append(1)

    threads = [threading.Thread(target=hit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 3
    assert w.svc.get_link(link["id"])["access_count"] == 3
    w.close()
