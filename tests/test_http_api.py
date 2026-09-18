"""End-to-end checks against the real HTTP server (ephemeral port)."""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import temp_db  # noqa: E402
from dataroom.app import build_server  # noqa: E402


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None, actor=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-Actor", actor)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                headers = dict(resp.headers)
                if "application/json" in ctype:
                    return resp.status, json.loads(raw), headers, raw
                return resp.status, None, headers, raw
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), dict(e.headers), raw
            except json.JSONDecodeError:
                return e.code, None, dict(e.headers), raw


def _start(db):
    server = build_server(db, "127.0.0.1", 0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, Client(f"http://127.0.0.1:{port}")


def test_full_flow_over_http():
    db = temp_db()
    server, c = _start(db)
    try:
        _, admin, _, _ = c.call("POST", "/users",
                                {"id": "u_admin", "name": "Ada", "team": "admin"})
        c.call("POST", "/users",
               {"id": "u_fin", "name": "Finn", "team": "finance"})
        c.call("POST", "/users",
               {"id": "u_tax", "name": "Theo", "team": "tax"})
        _, project, _, _ = c.call("POST", "/projects", {
            "name": "Merlion", "country": "SG", "timezone": "Asia/Singapore"})
        pid = project["id"]
        for uid, role, cl in (("u_fin", "reviewer", 2), ("u_tax", "viewer", 1)):
            c.call("POST", f"/projects/{pid}/permissions",
                   {"user_id": uid, "role": role, "max_clearance": cl},
                   actor="u_admin")
        _, req, _, _ = c.call("POST", "/requests", {
            "project_id": pid, "title": "Payroll", "country": "SG",
            "clearance_required": 2, "deadline": "2026-10-20T17:00"},
            actor="u_fin")
        _, doc, _, _ = c.call("POST", "/documents", {
            "request_id": req["id"], "filename": "payroll.xlsx",
            "classification": "secret", "clearance_level": 2}, actor="u_fin")
        c.call("POST", f"/documents/{doc['id']}/versions",
               {"content": "salary v1", "summary": "first"}, actor="u_fin")

        # allowed download carries watermark headers
        status, _, headers, raw = c.call(
            "GET", f"/documents/{doc['id']}?actor=u_fin&purpose=QoE")
        assert status == 200 and b"salary v1" in raw
        assert "Finn" in headers["X-Watermark"]
        assert headers["X-Content-Sha256"]

        # denied download is JSON with a stable error code
        status, payload, _, _ = c.call(
            "GET", f"/documents/{doc['id']}?actor=u_tax&purpose=peek")
        assert status == 403 and payload["error"] == "denied_clearance"

        # missing actor is a 400
        status, payload, _, _ = c.call(
            "GET", f"/documents/{doc['id']}?purpose=peek")
        assert status == 400 and payload["error"] == "bad_request"

        # audit over HTTP
        _, recs, _, _ = c.call("GET", f"/records?project_id={pid}")
        kinds = {r["decision"] for r in recs["records"]}
        assert "watermarked" in kinds and "denied_clearance" in kinds
    finally:
        server.shutdown()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db + suffix)
            except OSError:
                pass


def test_expired_link_hint_over_http():
    db = temp_db()
    server, c = _start(db)
    try:
        c.call("POST", "/users",
               {"id": "u_admin", "name": "Ada", "team": "admin"})
        c.call("POST", "/users",
               {"id": "u_fin", "name": "Finn", "team": "finance"})
        _, project, _, _ = c.call("POST", "/projects", {
            "name": "P", "country": "SG", "timezone": "Asia/Singapore"})
        c.call("POST", f"/projects/{project['id']}/permissions",
               {"user_id": "u_fin", "role": "reviewer", "max_clearance": 2},
               actor="u_admin")
        _, req, _, _ = c.call("POST", "/requests", {
            "project_id": project["id"], "title": "T", "country": "SG",
            "clearance_required": 2, "deadline": "2026-10-20T17:00"},
            actor="u_fin")
        _, doc, _, _ = c.call("POST", "/documents", {
            "request_id": req["id"], "filename": "c.pdf",
            "classification": "confidential", "clearance_level": 2},
            actor="u_fin")
        c.call("POST", f"/documents/{doc['id']}/versions",
               {"content": "contract"}, actor="u_fin")
        _, link, _, _ = c.call("POST", "/links", {
            "document_id": doc["id"], "version": 1, "purpose": "counsel",
            "ttl_hours": -5}, actor="u_fin")
        # ttl -5 is invalid -> create a valid one and expire via quota
        _, link, _, _ = c.call("POST", "/links", {
            "document_id": doc["id"], "version": 1, "purpose": "counsel",
            "ttl_hours": 24, "max_accesses": 1}, actor="u_fin")
        assert c.call("GET", f"/links/{link['id']}")[0] == 200
        status, payload, _, _ = c.call("GET", f"/links/{link['id']}")
        assert status == 410
        assert payload["error"] == "link_unavailable"
        assert payload["message"] == "this link is no longer available"
    finally:
        server.shutdown()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db + suffix)
            except OSError:
                pass


def test_citation_endpoint_serves_frozen_version():
    db = temp_db()
    server, c = _start(db)
    try:
        c.call("POST", "/users", {"id": "u_admin", "name": "Ada", "team": "admin"})
        c.call("POST", "/users", {"id": "u_legal", "name": "Lia", "team": "legal"})
        _, project, _, _ = c.call("POST", "/projects", {
            "name": "P", "country": "SG", "timezone": "Asia/Singapore"})
        c.call("POST", f"/projects/{project['id']}/permissions",
               {"user_id": "u_legal", "role": "reviewer", "max_clearance": 2},
               actor="u_admin")
        _, req, _, _ = c.call("POST", "/requests", {
            "project_id": project["id"], "title": "T", "country": "SG",
            "clearance_required": 2, "deadline": "2026-10-20T17:00"},
            actor="u_legal")
        _, doc, _, _ = c.call("POST", "/documents", {
            "request_id": req["id"], "filename": "c", "classification": "secret",
            "clearance_level": 2}, actor="u_legal")
        c.call("POST", f"/documents/{doc['id']}/versions",
               {"content": "evidence v1"}, actor="u_legal")
        _, review, _, _ = c.call("POST", "/reviews", {
            "document_id": doc["id"], "version": 1, "note": "check"},
            actor="u_legal")
        _, citation, _, _ = c.call("POST", f"/reviews/{review['id']}/citations",
                                   {"document_id": doc["id"], "version": 1})
        c.call("POST", f"/documents/{doc['id']}/versions",
               {"content": "rewritten v2"}, actor="u_legal")
        status, _, headers, raw = c.call(
            "GET", f"/citations/{citation['id']}?actor=u_legal&purpose=trial")
        assert status == 200 and b"evidence v1" in raw
        assert headers["X-Document-Version"] == "1"
        assert "trial" in headers["X-Watermark"]
    finally:
        server.shutdown()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db + suffix)
            except OSError:
                pass


def test_parallel_http_downloads():
    db = temp_db()
    server, c = _start(db)
    try:
        c.call("POST", "/users", {"id": "u_admin", "name": "A", "team": "admin"})
        c.call("POST", "/users", {"id": "u_fin", "name": "F", "team": "finance"})
        _, project, _, _ = c.call("POST", "/projects", {
            "name": "P", "country": "SG", "timezone": "Asia/Singapore"})
        c.call("POST", f"/projects/{project['id']}/permissions",
               {"user_id": "u_fin", "role": "reviewer", "max_clearance": 2},
               actor="u_admin")
        _, req, _, _ = c.call("POST", "/requests", {
            "project_id": project["id"], "title": "T", "country": "SG",
            "clearance_required": 2, "deadline": "2026-10-20T17:00"},
            actor="u_fin")
        _, doc, _, _ = c.call("POST", "/documents", {
            "request_id": req["id"], "filename": "f", "classification": "secret",
            "clearance_level": 2}, actor="u_fin")
        c.call("POST", f"/documents/{doc['id']}/versions",
               {"content": "x"}, actor="u_fin")

        statuses = []
        lock = threading.Lock()

        def hit(i):
            status, _, _, raw = c.call(
                "GET", f"/documents/{doc['id']}?actor=u_fin&purpose=p{i}")
            with lock:
                statuses.append((status, raw))

        threads = [threading.Thread(target=hit, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(s == 200 for s, _ in statuses)
        assert all(b"x" in raw and b"CONFIDENTIAL" in raw for _, raw in statuses)
        _, recs, _, _ = c.call(
            "GET", f"/records?project_id={project['id']}")
        assert len(recs["records"]) == 12
    finally:
        server.shutdown()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db + suffix)
            except OSError:
                pass
