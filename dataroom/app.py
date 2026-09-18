"""HTTP layer.

A compact JSON API in front of :class:`DataRoomService`.  Auth is the
``X-Actor`` user id header — the real deployment would replace just
this one hook with SSO, every rule below it stays the same.

Endpoints
---------
POST   /users
POST   /projects
POST   /projects/{id}/permissions
DELETE /projects/{id}/permissions/{user_id}
GET    /projects/{id}/permissions/audit
POST   /projects/{id}/clone
POST   /requests
GET    /requests/{id}
GET    /requests/{id}/documents
GET    /requests/{id}/scope-events
POST   /requests/{id}/extend
POST   /requests/{id}/scope
POST   /requests/{id}/close
POST   /documents
GET    /documents/{id}/versions
POST   /documents/{id}/versions
GET    /documents/{id}                 ?actor=&purpose=&version=  (watermarked bytes)
POST   /documents/{id}/withdraw
GET    /documents/{id}/impact
POST   /reviews                        (body pins document_id + version)
GET    /reviews/pending
GET    /reviews/{id}                   (conclusion history + citations)
POST   /reviews/{id}/decision
POST   /reviews/{id}/citations
GET    /citations/{id}?actor=          (frozen evidence bytes)
POST   /links
POST   /links/{id}/revoke
GET    /links/{id}                     (expired/revoked -> controlled hint only)
GET    /records
POST   /imports
POST   /users/depart
GET    /notifications
"""
from __future__ import annotations

import base64
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DataRoomError
from .services import DataRoomService
from .storage import Store


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class APIHandler(BaseHTTPRequestHandler):
    server_version = "MADataroom/1.0"

    # quiet the default noisy logger; access records are the real audit trail
    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------ #
    # request helpers
    # ------------------------------------------------------------------ #
    @property
    def svc(self) -> DataRoomService:
        return self.server.svc

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise DataRoomError("body must be JSON", code="bad_json", status=400)
        if not isinstance(data, dict):
            raise DataRoomError("body must be a JSON object", code="bad_json", status=400)
        return data

    def _actor(self, body):
        actor = self.headers.get("X-Actor") or body.pop("actor_id", None)
        if not actor:
            raise DataRoomError("X-Actor header is required", code="unauthorized",
                                status=401)
        return actor

    def _send_json(self, status, payload, extra_headers=None):
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc: DataRoomError):
        self._send_json(exc.http_status, {"error": exc.code, "message": str(exc)})

    # ------------------------------------------------------------------ #
    # routing
    # ------------------------------------------------------------------ #
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        query = {}
        if "?" in self.path:
            from urllib.parse import parse_qs
            query = {k: v[-1] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}
        try:
            self._route(method, path, query)
        except DataRoomError as exc:
            self._send_error(exc)
        except Exception as exc:  # never leak a stack trace to the caller
            self._send_json(500, {"error": "internal", "message": str(exc)})

    def _route(self, method, path, query):
        s = self.svc
        body = self._read_json() if method in ("POST", "DELETE") else {}
        rel = path.strip("/")
        p = rel.split("/") if rel else []

        # ---- users / projects -----------------------------------------
        if method == "POST" and p == ["users"]:
            return self._send_json(201, s.create_user(
                body["name"], body["team"], body.get("id")))
        if method == "POST" and p == ["projects"]:
            return self._send_json(201, s.create_project(
                body["name"], body["country"], body["timezone"]))
        if method == "POST" and len(p) == 3 and p[0] == "projects" and p[2] == "clone":
            actor = self._actor(body)
            proj, id_map = s.clone_project(p[1], body["name"], body["country"],
                                           body["timezone"], actor)
            return self._send_json(201, {"project": proj, "id_map": id_map})

        # ---- permissions ----------------------------------------------
        m = re.fullmatch(r"projects/([^/]+)/permissions", rel)
        if method == "POST" and m:
            actor = self._actor(body)
            return self._send_json(200, s.grant_permission(
                m.group(1), body["user_id"], body["role"],
                int(body["max_clearance"]), actor))
        m = re.fullmatch(r"projects/([^/]+)/permissions/([^/]+)", rel)
        if method == "DELETE" and m:
            actor = self._actor(body)
            return self._send_json(200, s.revoke_permission(
                m.group(1), m.group(2), actor))
        m = re.fullmatch(r"projects/([^/]+)/permissions/audit", rel)
        if method == "GET" and m:
            return self._send_json(200, {"audit": s.permission_audit(m.group(1))})

        # ---- requests --------------------------------------------------
        if method == "POST" and p == ["requests"]:
            actor = self._actor(body)
            return self._send_json(201, s.create_request(
                body["project_id"], body["title"], body["country"],
                int(body["clearance_required"]), body["deadline"], actor,
                body.get("id")))
        m = re.fullmatch(r"requests/([^/]+)", rel)
        if method == "GET" and m:
            return self._send_json(200, s.get_request(m.group(1)))
        m = re.fullmatch(r"requests/([^/]+)/documents", rel)
        if method == "GET" and m:
            return self._send_json(200, {
                "documents": s.list_documents(m.group(1))})
        m = re.fullmatch(r"requests/([^/]+)/scope-events", rel)
        if method == "GET" and m:
            return self._send_json(200, {
                "events": s.scope_events(m.group(1))})
        m = re.fullmatch(r"requests/([^/]+)/(extend|scope|close)", rel)
        if method == "POST" and m:
            actor = self._actor(body)
            rid, action = m.group(1), m.group(2)
            if action == "extend":
                out = s.extend_deadline(rid, body["deadline"], actor,
                                        body.get("reason", ""))
            elif action == "scope":
                out = s.change_scope(rid, actor, body.get("country"),
                                     body.get("clearance_required"),
                                     body.get("reason", ""))
            else:
                out = s.close_request(rid, actor)
            return self._send_json(200, out)

        # ---- documents -------------------------------------------------
        if method == "POST" and p == ["documents"]:
            actor = self._actor(body)
            return self._send_json(201, s.create_document(
                body["request_id"], body["filename"], body["classification"],
                int(body["clearance_level"]), actor))
        m = re.fullmatch(r"documents/([^/]+)/versions", rel)
        if method == "GET" and m:
            return self._send_json(200, {
                "versions": s.list_versions(m.group(1))})
        m = re.fullmatch(r"documents/([^/]+)/versions", rel)
        if method == "POST" and m:
            actor = self._actor(body)
            content = body.get("content", "")
            if isinstance(content, str) and body.get("encoding") == "base64":
                content = base64.b64decode(content)
            elif isinstance(content, str):
                content = content.encode("utf-8")
            return self._send_json(201, s.upload_version(
                m.group(1), content, actor, body.get("summary", ""),
                body.get("media_type", "text/plain")))
        m = re.fullmatch(r"documents/([^/]+)/withdraw", rel)
        if method == "POST" and m:
            actor = self._actor(body)
            return self._send_json(200, s.supplier_withdraw(
                m.group(1), actor, body.get("reason", "")))
        m = re.fullmatch(r"documents/([^/]+)/impact", rel)
        if method == "GET" and m:
            return self._send_json(200, {"events": s.impact_chain("document",
                                                                  m.group(1))})
        m = re.fullmatch(r"documents/([^/]+)", rel)
        if method == "GET" and m:
            actor = query.get("actor")
            purpose = query.get("purpose")
            if not actor or not purpose:
                raise DataRoomError("actor and purpose query params are required",
                                    code="bad_request", status=400)
            version = int(query["version"]) if query.get("version") else None
            result = s.access_document(actor, m.group(1), purpose, version)
            return self._deliver(result)

        # ---- reviews ---------------------------------------------------
        if method == "GET" and p == ["reviews", "pending"]:
            return self._send_json(200, {"reviews": s.pending_reviews(
                query.get("project_id"), query.get("reviewer_id"))})
        if method == "POST" and p == ["reviews"]:
            actor = self._actor(body)
            return self._send_json(201, s.open_review(
                body["document_id"], int(body["version"]), actor,
                body.get("note", "")))
        m = re.fullmatch(r"reviews/([^/]+)/decision", rel)
        if method == "POST" and m:
            actor = self._actor(body)
            return self._send_json(200, s.decide_review(
                m.group(1), body["conclusion"], actor, body.get("note", "")))
        m = re.fullmatch(r"reviews/([^/]+)/citations", rel)
        if method == "POST" and m:
            return self._send_json(201, s.cite_evidence(
                m.group(1), body["document_id"], int(body["version"])))
        m = re.fullmatch(r"reviews/([^/]+)", rel)
        if method == "GET" and m:
            return self._send_json(200, s.review_history(m.group(1)))

        # ---- links -----------------------------------------------------
        if method == "POST" and p == ["links"]:
            actor = self._actor(body)
            return self._send_json(201, s.create_link(
                body["document_id"], int(body["version"]), actor, body["purpose"],
                float(body["ttl_hours"]), body.get("watermark_policy", "stamp"),
                body.get("max_accesses")))
        m = re.fullmatch(r"links/([^/]+)/revoke", rel)
        if method == "POST" and m:
            actor = self._actor(body)
            return self._send_json(200, s.revoke_link(
                m.group(1), actor, body.get("reason", "")))
        m = re.fullmatch(r"links/([^/]+)", rel)
        if method == "GET" and m:
            result = s.access_link(m.group(1), query.get("purpose"))
            return self._deliver(result)

        # ---- audit / notifications / imports --------------------------
        m = re.fullmatch(r"citations/([^/]+)", rel)
        if method == "GET" and m:
            actor = query.get("actor")
            if not actor:
                raise DataRoomError("actor query param is required",
                                    code="bad_request", status=400)
            result = s.resolve_citation(m.group(1), actor,
                                        query.get("purpose", "evidence review"))
            return self._deliver(result)
        if method == "GET" and p == ["records"]:
            return self._send_json(200, {"records": s.access_records(
                query.get("project_id"), query.get("document_id"),
                query.get("decision"))})
        if method == "GET" and p == ["notifications"]:
            return self._send_json(200, {"notifications": s.list_notifications(
                query["project_id"])})
        if method == "POST" and p == ["imports"]:
            actor = self._actor(body)
            return self._send_json(200, s.import_batch(
                body["project_id"], actor, body["batch_key"], body["items"]))
        if method == "POST" and p == ["users", "depart"]:
            # off-boarding helper: X-Actor is the admin, body names the user
            actor = self._actor(body)
            return self._send_json(200, s.mark_user_departed(body["user_id"]))
        if method == "GET" and rel == "health":
            return self._send_json(200, {"status": "ok"})

        self._send_json(404, {"error": "not_found", "message": f"no route for {path}"})

    def _deliver(self, result):
        content = result.pop("content")
        if isinstance(content, str):
            content = content.encode("utf-8")

        def header_safe(value: str) -> str:
            # HTTP header values are latin-1; viewer-supplied purpose
            # text is transliterated rather than crashing the response.
            return value.encode("latin-1", errors="replace").decode("latin-1")[:500]

        headers = {
            "Content-Type": result.get("media_type", "application/octet-stream"),
            "X-Watermark": header_safe(result.get("watermark", "")),
            "X-Decision": result["decision"],
            "X-Document-Version": str(result["version"]["version"]),
            "X-Content-Sha256": result["version"]["sha256"],
        }
        self.send_response(200)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def build_server(db_path: str, host="127.0.0.1", port=8080):
    server = ThreadingHTTPServer((host, port), APIHandler)
    server.svc = DataRoomService(Store(db_path))
    return server
