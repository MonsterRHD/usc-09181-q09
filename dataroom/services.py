"""Domain services for the cross-border M&A data room.

All rules from the operating brief live here:

* least privilege per (project, user): role + maximum clearance,
  checked on every open — permission changes take effect for content
  that has not been downloaded yet, while audit rows are never erased;
* documents are versioned and reviews/evidence are pinned to one exact
  version, so a new upload can never replace cited evidence;
* supplier withdrawal, scope changes and deadline extensions append an
  immutable impact chain instead of mutating history;
* share links pin a version, carry purpose / expiry / watermark policy
  and only ever return a controlled hint once unavailable;
* notifications deduplicate, imports are idempotent, deadlines live in
  the project timezone, and departed members lose access immediately.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from .clock import Clock, format_instant, parse_instant, parse_local_deadline
from .errors import (
    Conflict,
    DataRoomError,
    LinkExpired,
    LinkRevoked,
    NotFound,
    ValidationFailed,
    AuthorizationError,
)
from .watermark import build as build_watermark

CLASSIFICATIONS = ("public", "confidential", "secret")
TEAMS = ("finance", "legal", "tax", "admin")
ROLES = ("admin", "reviewer", "viewer")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class DataRoomService:
    def __init__(self, store, clock: Clock | None = None):
        self.store = store
        self.clock = clock or Clock()
        self._tx_depth = threading.local()

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    @contextmanager
    def _tx(self):
        """Serialised transactions, nestable via SAVEPOINTs.

        The bulk importer opens one transaction per item while itself
        running inside a retry loop; savepoints give an inner failure a
        clean rollback without poisoning the outer transaction.
        """
        s = self.store
        with s.lock:
            depth = getattr(self._tx_depth, "depth", 0)
            if depth == 0:
                s.execute("BEGIN IMMEDIATE")
                sp = None
            else:
                sp = f"sp{depth}"
                s.execute(f"SAVEPOINT {sp}")
            self._tx_depth.depth = depth + 1
            try:
                yield s
            except Exception:
                if sp:
                    s.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    s.execute(f"RELEASE SAVEPOINT {sp}")
                    self._tx_depth.depth = depth
                else:
                    s.execute("ROLLBACK")
                    self._tx_depth.depth = 0
                raise
            else:
                if sp:
                    s.execute(f"RELEASE SAVEPOINT {sp}")
                    self._tx_depth.depth = depth
                else:
                    s.execute("COMMIT")
                    self._tx_depth.depth = 0

    def _now(self) -> datetime:
        return self.clock.now()

    def _now_str(self) -> str:
        return format_instant(self._now())

    def _impact(self, subject_type, subject_id, kind, detail, actor_id=None):
        self.store.execute(
            "INSERT INTO impact_events(subject_type,subject_id,kind,detail,actor_id,at)"
            " VALUES(?,?,?,?,?,?)",
            (subject_type, subject_id, kind, json.dumps(detail, ensure_ascii=False),
             actor_id, self._now_str()),
        )

    def _notify(self, project_id, kind, payload, dedup_key, user_id=None):
        """Insert a notification; a repeated dedup_key is swallowed.

        Returns True when a new row was created — that is what keeps
        repeated imports / retried uploads from spamming the reviewers.
        """
        try:
            self.store.execute(
                "INSERT INTO notifications(id,project_id,user_id,kind,payload,created_at,dedup_key)"
                " VALUES(?,?,?,?,?,?,?)",
                (_id("ntf"), project_id, user_id, kind,
                 json.dumps(payload, ensure_ascii=False), self._now_str(), dedup_key),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # UNIQUE(dedup_key): duplicate notification

    def _get_or_404(self, table, oid, label):
        row = self.store.get(f"SELECT * FROM {table} WHERE id=?", (oid,))
        if row is None:
            raise NotFound(f"{label} {oid} not found")
        return row

    # ------------------------------------------------------------------ #
    # users, projects, permissions
    # ------------------------------------------------------------------ #
    def create_user(self, name, team, user_id=None):
        if team not in TEAMS:
            raise ValidationFailed(f"unknown team {team!r}")
        user_id = user_id or _id("usr")
        with self._tx():
            self.store.execute(
                "INSERT INTO users(id,name,team,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, name, team, "active", self._now_str()),
            )
        return self.get_user(user_id)

    def mark_user_departed(self, user_id):
        """Off-boarding: account disabled and every live grant revoked
        immediately; audit history is left intact."""
        self._get_or_404("users", user_id, "user")
        with self._tx():
            self.store.execute(
                "UPDATE users SET status='departed' WHERE id=?", (user_id,))
            grants = self.store.all(
                "SELECT project_id, role, max_clearance FROM permissions"
                " WHERE user_id=? AND revoked_at IS NULL", (user_id,))
            self.store.execute(
                "UPDATE permissions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (self._now_str(), user_id))
            for g in grants:
                self.store.execute(
                    "INSERT INTO permission_audit"
                    "(project_id,user_id,action,before_json,after_json,actor_id,at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (g["project_id"], user_id, "revoke",
                     json.dumps({"role": g["role"], "max_clearance": g["max_clearance"]}),
                     None, user_id, self._now_str()),
                )
        return self.get_user(user_id)

    def get_user(self, user_id):
        return self.store.row_to_dict(self._get_or_404("users", user_id, "user"))

    def create_project(self, name, country, timezone_name, creator_id=None):
        pid = _id("prj")
        with self._tx():
            self.store.execute(
                "INSERT INTO projects(id,name,country,timezone,created_at) VALUES(?,?,?,?,?)",
                (pid, name, country, timezone_name, self._now_str()),
            )
        return self.get_project(pid)

    def get_project(self, project_id):
        return self.store.row_to_dict(self._get_or_404("projects", project_id, "project"))

    def grant_permission(self, project_id, user_id, role, max_clearance, actor_id):
        """Create or adjust a grant.  The change is effective at once
        for content not yet downloaded; every version of the grant is
        kept in permission_audit."""
        if role not in ROLES:
            raise ValidationFailed(f"unknown role {role!r}")
        if not (1 <= int(max_clearance) <= 3):
            raise ValidationFailed("max_clearance must be 1..3")
        project = self._get_or_404("projects", project_id, "project")
        user = self._get_or_404("users", user_id, "user")
        if user["status"] != "active":
            raise ValidationFailed("cannot grant permissions to a departed user")
        with self._tx():
            row = self.store.get(
                "SELECT * FROM permissions WHERE project_id=? AND user_id=?",
                (project_id, user_id))
            before = None
            if row is None:
                self.store.execute(
                    "INSERT INTO permissions(id,project_id,user_id,role,max_clearance,"
                    "revoked_at,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?)",
                    (_id("perm"), project_id, user_id, role, int(max_clearance),
                     None, self._now_str(), actor_id),
                )
                action = "grant"
            else:
                before = {"role": row["role"], "max_clearance": row["max_clearance"],
                          "revoked": row["revoked_at"] is not None}
                self.store.execute(
                    "UPDATE permissions SET role=?, max_clearance=?, revoked_at=NULL,"
                    " updated_at=?, updated_by=? WHERE id=?",
                    (role, int(max_clearance), self._now_str(), actor_id, row["id"]),
                )
                action = "update"
            self.store.execute(
                "INSERT INTO permission_audit"
                "(project_id,user_id,action,before_json,after_json,actor_id,at)"
                " VALUES(?,?,?,?,?,?,?)",
                (project_id, user_id, action,
                 json.dumps(before) if before else None,
                 json.dumps({"role": role, "max_clearance": int(max_clearance)}),
                 actor_id, self._now_str()),
            )
            self._notify(project_id, "permission_changed",
                         {"user_id": user_id, "role": role,
                          "max_clearance": int(max_clearance), "action": action},
                         f"perm:{project_id}:{user_id}:{role}:{max_clearance}:{self._now_str()}",
                         user_id=user_id)
        return self.get_permission(project_id, user_id)

    def revoke_permission(self, project_id, user_id, actor_id):
        row = self.store.get(
            "SELECT * FROM permissions WHERE project_id=? AND user_id=?",
            (project_id, user_id))
        if row is None:
            raise NotFound("no permission to revoke")
        with self._tx():
            if row["revoked_at"] is None:
                self.store.execute(
                    "UPDATE permissions SET revoked_at=? WHERE id=?",
                    (self._now_str(), row["id"]))
                self.store.execute(
                    "INSERT INTO permission_audit"
                    "(project_id,user_id,action,before_json,after_json,actor_id,at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (project_id, user_id, "revoke",
                     json.dumps({"role": row["role"], "max_clearance": row["max_clearance"]}),
                     None, actor_id, self._now_str()),
                )
        return self.get_permission(project_id, user_id)

    def get_permission(self, project_id, user_id):
        row = self.store.get(
            "SELECT * FROM permissions WHERE project_id=? AND user_id=?",
            (project_id, user_id))
        return self.store.row_to_dict(row)

    def permission_audit(self, project_id=None, user_id=None):
        sql = "SELECT * FROM permission_audit WHERE 1=1"
        args = []
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if user_id:
            sql += " AND user_id=?"; args.append(user_id)
        sql += " ORDER BY id"
        return [dict(r) for r in self.store.all(sql, tuple(args))]

    def _live_grant(self, project_id, user_id):
        """The grant as of this instant, or None. Re-read on every call
        so an admin adjustment affects not-yet-downloaded content with
        no caching in between."""
        user = self.store.get("SELECT * FROM users WHERE id=?", (user_id,))
        if user is None or user["status"] != "active":
            return None, "denied_departed"
        row = self.store.get(
            "SELECT * FROM permissions WHERE project_id=? AND user_id=? AND revoked_at IS NULL",
            (project_id, user_id))
        if row is None:
            return None, "denied_membership"
        return row, None

    # ------------------------------------------------------------------ #
    # requests, deadlines, scope
    # ------------------------------------------------------------------ #
    def create_request(self, project_id, title, country, clearance_required,
                       deadline, created_by, request_id=None):
        project = self._get_or_404("projects", project_id, "project")
        user = self._get_or_404("users", created_by, "user")
        if not (1 <= int(clearance_required) <= 3):
            raise ValidationFailed("clearance_required must be 1..3")
        deadline_dt = parse_local_deadline(deadline, project["timezone"])
        if deadline_dt <= self._now():
            raise ValidationFailed("deadline must be in the future")
        rid = request_id or _id("req")
        with self._tx():
            self.store.execute(
                "INSERT INTO requests(id,project_id,title,country,clearance_required,"
                "status,deadline_at,deadline_tz,created_by,created_at,closed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rid, project_id, title, country, int(clearance_required), "open",
                 format_instant(deadline_dt), project["timezone"], created_by,
                 self._now_str(), None),
            )
        return self.get_request(rid)

    def get_request(self, request_id):
        return self.store.row_to_dict(self._get_or_404("requests", request_id, "request"))

    def list_requests(self, project_id):
        return [dict(r) for r in self.store.all(
            "SELECT * FROM requests WHERE project_id=? ORDER BY created_at,id",
            (project_id,))]

    def extend_deadline(self, request_id, new_deadline_local, actor_id, reason=""):
        """Push the deadline out. The old instant is preserved in a
        scope event, so the chain shows *who extended what, why*."""
        req = self._get_or_404("requests", request_id, "request")
        project = self.get_project(req["project_id"])
        new_dt = parse_local_deadline(new_deadline_local, project["timezone"])
        old_dt = parse_instant(req["deadline_at"])
        if new_dt <= old_dt:
            raise ValidationFailed("extension must be later than the current deadline")
        with self._tx():
            detail = {"old_deadline_at": req["deadline_at"],
                      "new_deadline_at": format_instant(new_dt),
                      "timezone": project["timezone"], "reason": reason}
            self.store.execute(
                "UPDATE requests SET deadline_at=? WHERE id=?",
                (format_instant(new_dt), request_id))
            self.store.execute(
                "INSERT INTO request_scope_events(request_id,kind,detail,actor_id,at)"
                " VALUES(?,?,?,?,?)",
                (request_id, "deadline_extension", json.dumps(detail, ensure_ascii=False),
                 actor_id, self._now_str()),
            )
            self._impact("request", request_id, "deadline_extension", detail, actor_id)
            self._notify(req["project_id"], "deadline_extension",
                         {"request_id": request_id, **detail},
                         f"deadline:{request_id}:{format_instant(new_dt)}")
        return self.get_request(request_id)

    def change_scope(self, request_id, actor_id, country=None, clearance_required=None,
                     reason=""):
        req = self._get_or_404("requests", request_id, "request")
        new_country = country or req["country"]
        new_clearance = int(clearance_required or req["clearance_required"])
        if not (1 <= new_clearance <= 3):
            raise ValidationFailed("clearance_required must be 1..3")
        if new_country == req["country"] and new_clearance == req["clearance_required"]:
            raise Conflict("scope is unchanged")
        with self._tx():
            detail = {"old_country": req["country"], "new_country": new_country,
                      "old_clearance": req["clearance_required"],
                      "new_clearance": new_clearance, "reason": reason}
            self.store.execute(
                "UPDATE requests SET country=?, clearance_required=?, status='scope_changed'"
                " WHERE id=?",
                (new_country, new_clearance, request_id))
            self.store.execute(
                "INSERT INTO request_scope_events(request_id,kind,detail,actor_id,at)"
                " VALUES(?,?,?,?,?)",
                (request_id, "scope_change", json.dumps(detail, ensure_ascii=False),
                 actor_id, self._now_str()),
            )
            self._impact("request", request_id, "scope_change", detail, actor_id)
            # Documents already uploaded keep their own clearance level;
            # reviewers must be able to see that the bar moved under them.
            self._notify(req["project_id"], "scope_change",
                         {"request_id": request_id, **detail},
                         f"scope:{request_id}:{new_country}:{new_clearance}:"
                         f"{self._now_str()}")
        return self.get_request(request_id)

    def close_request(self, request_id, actor_id):
        with self._tx():
            self.store.execute(
                "UPDATE requests SET status='closed', closed_at=? WHERE id=?",
                (self._now_str(), request_id))
            self.store.execute(
                "INSERT INTO request_scope_events(request_id,kind,detail,actor_id,at)"
                " VALUES(?,?,?,?,?)",
                (request_id, "closed", "{}", actor_id, self._now_str()),
            )
        return self.get_request(request_id)

    def scope_events(self, request_id):
        return [dict(r) for r in self.store.all(
            "SELECT * FROM request_scope_events WHERE request_id=? ORDER BY id",
            (request_id,))]

    # ------------------------------------------------------------------ #
    # documents & versions
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _blob_put(self, content: bytes, media_type="text/plain"):
        digest = self._hash(content)
        self.store.execute(
            "INSERT INTO document_blobs(sha256,content,media_type) VALUES(?,?,?)"
            " ON CONFLICT(sha256) DO NOTHING",
            (digest, content, media_type))
        return digest

    def _blob_get(self, digest):
        row = self.store.get("SELECT content, media_type FROM document_blobs WHERE sha256=?",
                             (digest,))
        return (row["content"], row["media_type"]) if row else (None, None)

    def create_document(self, request_id, filename, classification, clearance_level,
                        created_by, document_id=None):
        self._get_or_404("requests", request_id, "request")
        if classification not in CLASSIFICATIONS:
            raise ValidationFailed(f"unknown classification {classification!r}")
        if not (1 <= int(clearance_level) <= 3):
            raise ValidationFailed("clearance_level must be 1..3")
        doc_id = document_id or _id("doc")
        with self._tx():
            self.store.execute(
                "INSERT INTO documents(id,request_id,filename,classification,clearance_level,"
                "current_version,supplier_withdrawn,withdrawn_at,created_at)"
                " VALUES(?,?,?,?,?,0,0,NULL,?)",
                (doc_id, request_id, filename, classification, int(clearance_level),
                 self._now_str()),
            )
        return self.get_document(doc_id)

    def get_document(self, document_id):
        return self.store.row_to_dict(self._get_or_404("documents", document_id, "document"))

    def list_documents(self, request_id):
        return [dict(r) for r in self.store.all(
            "SELECT * FROM documents WHERE request_id=? ORDER BY created_at,id",
            (request_id,))]

    def upload_version(self, document_id, content, uploader_id, summary="",
                       media_type="text/plain"):
        """Append a version. Appending never mutates older versions or
        anything that cited them; reviewers following the old link keep
        seeing the old bytes (same sha256).

        The next version number is taken from ``MAX(version)`` *inside*
        the writer transaction, so concurrent uploads serialize into a
        gapless 1,2,3… sequence.
        """
        doc = self._get_or_404("documents", document_id, "document")
        if doc["supplier_withdrawn"]:
            raise Conflict("document is withdrawn; uploads are closed")
        self._get_or_404("users", uploader_id, "user")
        if isinstance(content, str):
            content = content.encode("utf-8")
        req = self.get_request(doc["request_id"])
        for attempt in range(3):
            try:
                with self._tx():
                    row = self.store.get(
                        "SELECT COALESCE(MAX(version),0) AS v FROM document_versions"
                        " WHERE document_id=?", (document_id,))
                    version_no = int(row["v"]) + 1
                    digest = self._blob_put(content, media_type)
                    vid = _id("ver")
                    self.store.execute(
                        "INSERT INTO document_versions(id,document_id,version,sha256,"
                        "size_bytes,uploader_id,summary,uploaded_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (vid, document_id, version_no, digest, len(content),
                         uploader_id, summary, self._now_str()),
                    )
                    self.store.execute(
                        "UPDATE documents SET current_version=? WHERE id=?",
                        (version_no, document_id))
                    detail = {"version": version_no, "sha256": digest,
                              "summary": summary, "size_bytes": len(content)}
                    self._impact("document", document_id, "upload_version", detail,
                                 uploader_id)
                    self._notify(req["project_id"], "new_version",
                                 {"document_id": document_id, **detail},
                                 f"newversion:{document_id}:{version_no}")
                return self.get_version(vid)
            except sqlite3.IntegrityError:
                if attempt == 2:
                    raise
                # lost the race for this version number — retry

    def get_version(self, version_id):
        return self.store.row_to_dict(
            self._get_or_404("document_versions", version_id, "version"))

    def get_version_number(self, document_id, number):
        row = self.store.get(
            "SELECT * FROM document_versions WHERE document_id=? AND version=?",
            (document_id, int(number)))
        if row is None:
            raise NotFound(f"version {number} not found")
        return dict(row)

    def list_versions(self, document_id):
        return [dict(r) for r in self.store.all(
            "SELECT * FROM document_versions WHERE document_id=? ORDER BY version",
            (document_id,))]

    # ------------------------------------------------------------------ #
    # reviews & frozen evidence
    # ------------------------------------------------------------------ #
    def open_review(self, document_id, version_number, reviewer_id, note=""):
        doc = self._get_or_404("documents", document_id, "document")
        ver = self.get_version_number(document_id, version_number)
        reviewer = self._get_or_404("users", reviewer_id, "user")
        rid = _id("rev")
        with self._tx():
            self.store.execute(
                "INSERT INTO reviews(id,document_id,version_id,reviewer_id,conclusion,"
                "note,created_at,decided_at) VALUES(?,?,?,?,'pending',?,?,?)",
                (rid, document_id, ver["id"], reviewer_id, note, self._now_str(), None),
            )
        return self.get_review(rid)

    def decide_review(self, review_id, conclusion, reviewer_id, note=""):
        if conclusion not in ("approved", "flagged"):
            raise ValidationFailed("conclusion must be approved or flagged")
        review = self._get_or_404("reviews", review_id, "review")
        with self._tx():
            self.store.execute(
                "UPDATE reviews SET conclusion=?, note=?, decided_at=? WHERE id=?",
                (conclusion, note, self._now_str(), review_id))
            self.store.execute(
                "INSERT INTO review_conclusions(review_id,conclusion,note,reviewer_id,at)"
                " VALUES(?,?,?,?,?)",
                (review_id, conclusion, note, reviewer_id, self._now_str()),
            )
        return self.get_review(review_id)

    def cite_evidence(self, review_id, document_id, version_number):
        """Freeze a citation to one exact version + hash. The row still
        resolves after newer uploads, withdrawal and project copies."""
        review = self._get_or_404("reviews", review_id, "review")
        ver = self.get_version_number(document_id, version_number)
        doc = self.get_document(document_id)
        with self._tx():
            cid = _id("cit")
            self.store.execute(
                "INSERT INTO evidence_citations(id,review_id,document_id,version_id,"
                "sha256,quoted_filename,quoted_version,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (cid, review_id, document_id, ver["id"], ver["sha256"],
                 doc["filename"], version_number, self._now_str()),
            )
        return self.get_citation(cid)

    def get_citation(self, citation_id):
        return self.store.row_to_dict(
            self._get_or_404("evidence_citations", citation_id, "citation"))

    def list_citations(self, review_id):
        return [dict(r) for r in self.store.all(
            "SELECT * FROM evidence_citations WHERE review_id=? ORDER BY id",
            (review_id,))]

    def resolve_citation(self, citation_id, reviewer_id, purpose="evidence review"):
        """Evidence path: bytes are delivered for an exact cited hash
        even when the document has a newer current version.

        Frozen evidence is sensitive, so the caller must hold a live
        reviewer/admin grant covering the document's clearance.  The
        retrieval is audited like any other document open.
        """
        c = self._get_or_404("evidence_citations", citation_id, "citation")
        doc = self.get_document(c["document_id"])
        req = self.get_request(doc["request_id"])
        project = self.get_project(req["project_id"])
        grant, problem = self._live_grant(req["project_id"], reviewer_id)
        if problem:
            self._record_access(
                actor_id=reviewer_id, link_id=None, document_id=doc["id"],
                version_id=c["version_id"], project_id=req["project_id"],
                purpose=purpose, decision=problem,
                reason="evidence access denied", watermark="")
            raise AuthorizationError("no live permission for this project",
                                     code=problem)
        if grant["role"] not in ("admin", "reviewer"):
            self._record_access(
                actor_id=reviewer_id, link_id=None, document_id=doc["id"],
                version_id=c["version_id"], project_id=req["project_id"],
                purpose=purpose, decision="denied_membership",
                reason="reviewer role required for cited evidence", watermark="")
            raise AuthorizationError("only reviewers may resolve cited evidence",
                                     code="forbidden")
        if int(grant["max_clearance"]) < int(doc["clearance_level"]):
            self._record_access(
                actor_id=reviewer_id, link_id=None, document_id=doc["id"],
                version_id=c["version_id"], project_id=req["project_id"],
                purpose=purpose, decision="denied_clearance",
                reason="insufficient clearance for cited evidence", watermark="")
            raise AuthorizationError("insufficient clearance for cited evidence",
                                     code="denied_clearance")
        ver = self.get_version(c["version_id"])
        if ver["sha256"] != c["sha256"]:
            raise Conflict("cited content hash does not match stored version")
        content, media = self._blob_get(c["sha256"])
        reviewer = self.store.get("SELECT name FROM users WHERE id=?",
                                  (reviewer_id,))
        wm = build_watermark("stamp",
                             reviewer["name"] if reviewer else reviewer_id,
                             purpose, self._now_str(), project["country"])
        rendered = self._render(content, wm)
        rec = self._record_access(
            actor_id=reviewer_id, link_id=None, document_id=doc["id"],
            version_id=ver["id"], project_id=req["project_id"], purpose=purpose,
            decision="watermarked", reason="frozen evidence", watermark=wm.text)
        return {"allowed": True, "decision": "watermarked", "document": doc,
                "version": ver, "content": rendered, "media_type": media,
                "watermark": wm.text, "citation": dict(c), "record": rec}

    def get_review(self, review_id):
        return self.store.row_to_dict(self._get_or_404("reviews", review_id, "review"))

    def review_history(self, review_id):
        """Full review packet: the pinned version, every conclusion
        recorded (incl. later supersession) and the frozen citations."""
        review = self.get_review(review_id)
        conclusions = [dict(r) for r in self.store.all(
            "SELECT * FROM review_conclusions WHERE review_id=? ORDER BY id",
            (review_id,))]
        citations = self.list_citations(review_id)
        return {"review": review, "conclusions": conclusions,
                "citations": citations}

    def pending_reviews(self, project_id=None, reviewer_id=None):
        sql = ("SELECT rv.* FROM reviews rv JOIN documents d ON d.id=rv.document_id "
               "JOIN requests q ON q.id=d.request_id WHERE rv.conclusion='pending'")
        args = []
        if project_id:
            sql += " AND q.project_id=?"; args.append(project_id)
        if reviewer_id:
            sql += " AND rv.reviewer_id=?"; args.append(reviewer_id)
        sql += " ORDER BY rv.created_at,rv.id"
        return [dict(r) for r in self.store.all(sql, tuple(args))]

    # ------------------------------------------------------------------ #
    # supplier withdrawal
    # ------------------------------------------------------------------ #
    def supplier_withdraw(self, document_id, actor_id, reason=""):
        doc = self._get_or_404("documents", document_id, "document")
        if doc["supplier_withdrawn"]:
            raise Conflict("document already withdrawn")
        with self._tx():
            self.store.execute(
                "UPDATE documents SET supplier_withdrawn=1, withdrawn_at=? WHERE id=?",
                (self._now_str(), document_id))
            self.store.execute(
                "INSERT INTO supplier_restrictions(id,document_id,status,reason,imposed_at,lifted_at)"
                " VALUES(?,?, 'active', ?, ?, NULL)",
                (_id("res"), document_id, reason, self._now_str()),
            )
            req = self.get_request(doc["request_id"])
            detail = {"reason": reason, "withdrawn_at": self._now_str()}
            self._impact("document", document_id, "withdraw", detail, actor_id)
            self._notify(req["project_id"], "supplier_withdrawn",
                         {"document_id": document_id, **detail},
                         f"withdraw:{document_id}")
        return self.get_document(document_id)

    def active_restriction(self, document_id):
        return self.store.row_to_dict(self.store.get(
            "SELECT * FROM supplier_restrictions WHERE document_id=? AND status='active'"
            " ORDER BY imposed_at DESC LIMIT 1", (document_id,)))

    def impact_chain(self, subject_type, subject_id):
        return [dict(r) for r in self.store.all(
            "SELECT * FROM impact_events WHERE subject_type=? AND subject_id=? ORDER BY id",
            (subject_type, subject_id))]

    # ------------------------------------------------------------------ #
    # share links
    # ------------------------------------------------------------------ #
    def create_link(self, document_id, version_number, granted_by, purpose,
                    ttl_hours, watermark_policy="stamp", max_accesses=None):
        if not purpose or not purpose.strip():
            raise ValidationFailed("purpose is required")
        if watermark_policy not in ("none", "stamp", "cover"):
            raise ValidationFailed("unknown watermark policy")
        doc = self._get_or_404("documents", document_id, "document")
        ver = self.get_version_number(document_id, version_number)
        self._get_or_404("users", granted_by, "user")
        ttl = float(ttl_hours)
        if ttl <= 0:
            raise ValidationFailed("ttl_hours must be positive")
        with self._tx():
            lid = _id("lnk")
            now = self._now()
            expires = now + timedelta(hours=ttl)
            self.store.execute(
                "INSERT INTO share_links(id,document_id,version_id,purpose,granted_by,"
                "watermark_policy,issued_at,expires_at,revoked_at,revoke_reason,"
                "max_accesses,access_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                (lid, document_id, ver["id"], purpose, granted_by, watermark_policy,
                 format_instant(now), format_instant(expires), None, "", max_accesses),
            )
        return self.get_link(lid)

    def get_link(self, link_id):
        return self.store.row_to_dict(
            self._get_or_404("share_links", link_id, "link"))

    def revoke_link(self, link_id, actor_id, reason=""):
        link = self._get_or_404("share_links", link_id, "link")
        with self._tx():
            if link["revoked_at"] is None:
                self.store.execute(
                    "UPDATE share_links SET revoked_at=?, revoke_reason=? WHERE id=?",
                    (self._now_str(), reason, link_id))
                doc = self.get_document(link["document_id"])
                req = self.get_request(doc["request_id"])
                self._impact("document", link["document_id"], "link_revoked",
                             {"link_id": link_id, "reason": reason}, actor_id)
                self._notify(req["project_id"], "link_revoked",
                             {"link_id": link_id, "reason": reason},
                             f"linkrevoke:{link_id}")
        return self.get_link(link_id)

    # ------------------------------------------------------------------ #
    # access decisions
    # ------------------------------------------------------------------ #
    def _record_access(self, **kw):
        kw.setdefault("at", self._now_str())
        # Hold the lock across insert + last-rowid read so a concurrent
        # thread cannot interleave its own access row.
        with self.store.lock:
            cur = self.store.execute(
                "INSERT INTO access_records(at,actor_id,link_id,document_id,version_id,"
                "project_id,purpose,decision,reason,watermark)"
                " VALUES(:at,:actor_id,:link_id,:document_id,:version_id,:project_id,"
                ":purpose,:decision,:reason,:watermark)",
                kw)
            row = self.store.get("SELECT * FROM access_records WHERE id=?",
                                 (cur.lastrowid,))
        return dict(row)

    def _render(self, content: bytes, watermark):
        if not watermark or not watermark.required:
            return content
        banner = f"[[ {watermark.text} ]]".encode("utf-8")
        if watermark.policy == "cover":
            return banner + b"\n\n" + content
        # banners on both edges; byte-safe for binary payloads too
        return banner + b"\n" + content + b"\n" + banner

    def access_document(self, user_id, document_id, purpose, version_number=None):
        """Open a document under least-privilege checks.

        Permission is re-read each call, so a downgrade/revocation hits
        immediately. Every outcome — allow, watermark or any flavour of
        denial — lands in access_records."""
        if not purpose or not purpose.strip():
            raise ValidationFailed("purpose is required")
        doc = self._get_or_404("documents", document_id, "document")
        req = self.get_request(doc["request_id"])
        project_id = req["project_id"]
        project = self.get_project(project_id)
        user = self.store.get("SELECT * FROM users WHERE id=?", (user_id,))
        user_name = user["name"] if user else user_id

        grant, problem = self._live_grant(project_id, user_id)
        if problem:
            return self._deny_and_raise(user_id, None, document_id, None, project_id,
                                        purpose, problem,
                                        "user has departed" if problem == "denied_departed"
                                        else "no live permission for this project")
        if int(grant["max_clearance"]) < int(doc["clearance_level"]):
            return self._deny_and_raise(
                user_id, None, document_id, None, project_id, purpose,
                "denied_clearance",
                f"clearance {grant['max_clearance']} < required {doc['clearance_level']}")
        if doc["supplier_withdrawn"]:
            return self._deny_and_raise(
                user_id, None, document_id, None, project_id, purpose,
                "denied_withdrawn", "supplier has withdrawn this document")
        restriction = self.active_restriction(document_id)
        if restriction is not None:
            return self._deny_and_raise(
                user_id, None, document_id, None, project_id, purpose,
                "denied_restriction", f"restriction: {restriction['reason']}")

        # Version selection: casual opens only ever get the current one.
        if version_number is None:
            version_number = int(doc["current_version"])
        if int(version_number) != int(doc["current_version"]) and grant["role"] == "viewer":
            return self._deny_and_raise(
                user_id, None, document_id, None, project_id, purpose,
                "deprecated_version",
                f"version {version_number} is superseded by {doc['current_version']}")
        ver = self.get_version_number(document_id, version_number)

        content, media = self._blob_get(ver["sha256"])
        wm = build_watermark("stamp", user_name, purpose, self._now_str(),
                             project["country"])
        rendered = self._render(content, wm)
        decision = "watermarked" if wm.required else "allowed"
        rec = self._record_access(
            actor_id=user_id, link_id=None, document_id=document_id,
            version_id=ver["id"], project_id=project_id, purpose=purpose,
            decision=decision, reason="", watermark=wm.text)
        return {"allowed": True, "decision": decision, "document": dict(doc),
                "version": ver, "content": rendered, "media_type": media,
                "watermark": wm.text, "record": rec}

    def _deny_and_raise(self, actor_id, link_id, document_id, version_id, project_id,
                        purpose, decision, reason):
        rec = self._record_access(
            actor_id=actor_id, link_id=link_id, document_id=document_id,
            version_id=version_id, project_id=project_id, purpose=purpose,
            decision=decision, reason=reason, watermark="")
        err = AuthorizationError(reason, code=decision)
        err.record = rec
        raise err

    def access_link(self, link_id, purpose=None):
        """Open through a share link. After expiry/revocation/quota the
        caller only ever receives the same controlled hint."""
        link = self.store.get("SELECT * FROM share_links WHERE id=?", (link_id,))
        now = self._now()
        if link is None:
            # Do not reveal whether the token existed; keep the attempt.
            self._record_access(
                actor_id=None, link_id=link_id, document_id=None, version_id=None,
                project_id=None, purpose=purpose or "", decision="expired",
                reason="unknown link token", watermark="")
            raise LinkExpired("this link is no longer available")
        doc = self.get_document(link["document_id"])
        req = self.get_request(doc["request_id"])
        project = self.get_project(req["project_id"])
        record_purpose = purpose or link["purpose"]

        def unavailable(decision, reason, exc):
            rec = self._record_access(
                actor_id=None, link_id=link_id, document_id=link["document_id"],
                version_id=link["version_id"], project_id=req["project_id"],
                purpose=record_purpose, decision=decision, reason=reason, watermark="")
            exc.record = rec
            raise exc

        if link["revoked_at"] is not None:
            unavailable("revoked", "link was revoked",
                        LinkRevoked("this link is no longer available"))
        if now >= parse_instant(link["expires_at"]):
            unavailable("expired", "past expiry",
                        LinkExpired("this link is no longer available"))
        if link["max_accesses"] is not None and \
                int(link["access_count"]) >= int(link["max_accesses"]):
            unavailable("expired", "access quota exhausted",
                        LinkExpired("this link is no longer available"))
        if doc["supplier_withdrawn"]:
            unavailable("denied_withdrawn", "supplier withdrew the document",
                        LinkRevoked("this link is no longer available"))

        ver = self.get_version(link["version_id"])
        content, media = self._blob_get(ver["sha256"])
        granter = self.store.get("SELECT name FROM users WHERE id=?",
                                 (link["granted_by"],))
        # The holder of an anonymous link is not the granter; label them
        # honestly so a leak traces back to the sharing channel.
        viewer_label = f"external linkholder (granted by " \
                       f"{granter['name'] if granter else 'unknown'})"
        wm = build_watermark(link["watermark_policy"], viewer_label,
                             record_purpose, self._now_str(), project["country"])
        rendered = self._render(content, wm)

        # Consume one slot atomically: the conditional UPDATE only fires
        # while the quota is genuinely unspent, so N racing callers on a
        # max_accesses=k link produce exactly k successes.  A denial
        # record is committed in the same step without bumping the count.
        rec = None
        denied = None
        with self._tx():
            fresh = self.store.get(
                "SELECT * FROM share_links WHERE id=?", (link_id,))
            if fresh["revoked_at"] is not None:
                denied = ("revoked", "link was revoked", LinkRevoked)
            elif self._now() >= parse_instant(fresh["expires_at"]):
                denied = ("expired", "past expiry", LinkExpired)
            else:
                fresh_doc = self.store.get(
                    "SELECT supplier_withdrawn FROM documents WHERE id=?",
                    (link["document_id"],))
                if fresh_doc is not None and fresh_doc["supplier_withdrawn"]:
                    denied = ("denied_withdrawn",
                              "supplier withdrew the document", LinkRevoked)
                else:
                    cur = self.store.execute(
                        "UPDATE share_links SET access_count=access_count+1 WHERE id=?"
                        " AND (max_accesses IS NULL OR access_count < max_accesses)",
                        (link_id,))
                    if cur.rowcount == 0:
                        denied = ("expired", "access quota exhausted", LinkExpired)
                    else:
                        rec = self._record_access(
                            actor_id=None, link_id=link_id,
                            document_id=link["document_id"], version_id=ver["id"],
                            project_id=req["project_id"], purpose=record_purpose,
                            decision="watermarked" if wm.required else "allowed",
                            reason="", watermark=wm.text)
            if denied:
                self._record_access(
                    actor_id=None, link_id=link_id,
                    document_id=link["document_id"], version_id=link["version_id"],
                    project_id=req["project_id"], purpose=record_purpose,
                    decision=denied[0], reason=denied[1], watermark="")
        if denied:
            raise denied[2]("this link is no longer available")
        return {"allowed": True, "decision": "watermarked" if wm.required else "allowed",
                "document": dict(doc), "version": ver, "content": rendered,
                "media_type": media, "watermark": wm.text,
                "expires_at": link["expires_at"], "record": rec}

    def access_records(self, project_id=None, document_id=None, decision=None, limit=500):
        sql = "SELECT * FROM access_records WHERE 1=1"
        args = []
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if document_id:
            sql += " AND document_id=?"; args.append(document_id)
        if decision:
            sql += " AND decision=?"; args.append(decision)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(int(limit))
        return [dict(r) for r in self.store.all(sql, tuple(args))]

    # ------------------------------------------------------------------ #
    # project copy
    # ------------------------------------------------------------------ #
    def clone_project(self, source_project_id, new_name, new_country, new_timezone,
                      actor_id):
        """Copy a project for a new workstream.

        Documents, versions (same bytes/sha256), citations and the
        review trail are duplicated with identical version numbers, but
        permissions are NOT copied: the target starts least-privilege,
        so every member must be re-granted before opening anything.
        """
        src = self._get_or_404("projects", source_project_id, "project")
        with self._tx():
            new_pid = _id("prj")
            self.store.execute(
                "INSERT INTO projects(id,name,country,timezone,created_at) VALUES(?,?,?,?,?)",
                (new_pid, new_name, new_country, new_timezone, self._now_str()))

            id_map = {"requests": {}, "documents": {}, "versions": {},
                      "reviews": {}, "citations": {}}
            for req in self.store.all(
                    "SELECT * FROM requests WHERE project_id=?", (source_project_id,)):
                new_rid = _id("req")
                id_map["requests"][req["id"]] = new_rid
                self.store.execute(
                    "INSERT INTO requests(id,project_id,title,country,clearance_required,"
                    "status,deadline_at,deadline_tz,created_by,created_at,closed_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (new_rid, new_pid, req["title"], req["country"],
                     req["clearance_required"], req["status"], req["deadline_at"],
                     req["deadline_tz"], req["created_by"], req["created_at"],
                     req["closed_at"]))
                for d in self.store.all(
                        "SELECT * FROM documents WHERE request_id=?", (req["id"],)):
                    new_doc = _id("doc")
                    id_map["documents"][d["id"]] = new_doc
                    self.store.execute(
                        "INSERT INTO documents(id,request_id,filename,classification,"
                        "clearance_level,current_version,supplier_withdrawn,withdrawn_at,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (new_doc, new_rid, d["filename"], d["classification"],
                         d["clearance_level"], d["current_version"],
                         d["supplier_withdrawn"], d["withdrawn_at"], d["created_at"]))
                    for v in self.store.all(
                            "SELECT * FROM document_versions WHERE document_id=?"
                            " ORDER BY version", (d["id"],)):
                        new_ver = _id("ver")
                        id_map["versions"][v["id"]] = new_ver
                        self.store.execute(
                            "INSERT INTO document_versions(id,document_id,version,sha256,"
                            "size_bytes,uploader_id,summary,uploaded_at)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (new_ver, new_doc, v["version"], v["sha256"],
                             v["size_bytes"], v["uploader_id"], v["summary"],
                             v["uploaded_at"]))
                    for r in self.store.all(
                            "SELECT * FROM reviews WHERE document_id=?", (d["id"],)):
                        new_rev = _id("rev")
                        id_map["reviews"][r["id"]] = new_rev
                        self.store.execute(
                            "INSERT INTO reviews(id,document_id,version_id,reviewer_id,"
                            "conclusion,note,created_at,decided_at)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (new_rev, new_doc, id_map["versions"][r["version_id"]],
                             r["reviewer_id"], r["conclusion"], r["note"],
                             r["created_at"], r["decided_at"]))
                        for c in self.store.all(
                                "SELECT * FROM evidence_citations WHERE review_id=?",
                                (r["id"],)):
                            new_cit = _id("cit")
                            self.store.execute(
                                "INSERT INTO evidence_citations(id,review_id,document_id,"
                                "version_id,sha256,quoted_filename,quoted_version,created_at)"
                                " VALUES(?,?,?,?,?,?,?,?)",
                                (new_cit, new_rev, id_map["documents"][c["document_id"]],
                                 id_map["versions"][c["version_id"]], c["sha256"],
                                 c["quoted_filename"], c["quoted_version"],
                                 c["created_at"]))
            self._impact("project", new_pid, "cloned_from",
                         {"source_project_id": source_project_id}, actor_id)
        return self.get_project(new_pid), id_map

    # ------------------------------------------------------------------ #
    # bulk import (idempotent)
    # ------------------------------------------------------------------ #
    def import_batch(self, project_id, actor_id, batch_key, items):
        """Idempotent bulk import.

        Each item: {ref, title, country, clearance_required, deadline,
        filename?, content?, classification?, doc_clearance?}.
        Re-running with the same batch_key+ref skips the item and emits
        no duplicate notifications.  A bad row is validated before any
        write and, defensively, runs inside one transaction whose inner
        steps use savepoints — so failure never leaves an orphan request.
        """
        project = self._get_or_404("projects", project_id, "project")
        summary = {"created": 0, "skipped": 0, "documents": 0, "errors": []}
        for pos, item in enumerate(items):
            ref = str(item.get("ref") or f"row-{pos}")
            seen = self.store.get(
                "SELECT request_id FROM import_keys WHERE batch_key=? AND item_ref=?",
                (batch_key, ref))
            if seen is not None:
                summary["skipped"] += 1
                continue
            try:
                self._validate_import_item(item, project)
                with self._tx():
                    req = self.create_request(
                        project_id, item["title"], item.get("country", "UNKNOWN"),
                        int(item.get("clearance_required", 1)), item["deadline"],
                        actor_id)
                    doc_id = None
                    if item.get("filename"):
                        doc = self.create_document(
                            req["id"], item["filename"],
                            item.get("classification", "confidential"),
                            int(item.get("doc_clearance",
                                        item.get("clearance_required", 1))),
                            actor_id)
                        self.upload_version(doc["id"], item.get("content", b""),
                                            actor_id, item.get("summary", ""))
                        doc_id = doc["id"]
                        summary["documents"] += 1
                    self.store.execute(
                        "INSERT INTO import_keys(batch_key,item_ref,request_id,"
                        "document_id,at) VALUES(?,?,?,?,?)",
                        (batch_key, ref, req["id"], doc_id, self._now_str()))
                    summary["created"] += 1
            except DataRoomError as exc:
                summary["errors"].append({"ref": ref, "error": str(exc),
                                          "code": exc.code})
        return summary

    def _validate_import_item(self, item, project):
        if not item.get("title"):
            raise ValidationFailed("title is required")
        if not (1 <= int(item.get("clearance_required", 1)) <= 3):
            raise ValidationFailed("clearance_required must be 1..3")
        if item.get("doc_clearance") is not None and \
                not (1 <= int(item["doc_clearance"]) <= 3):
            raise ValidationFailed("doc_clearance must be 1..3")
        if item.get("classification", "confidential") not in CLASSIFICATIONS:
            raise ValidationFailed("unknown classification")
        deadline = parse_local_deadline(item["deadline"], project["timezone"])
        if deadline <= self._now():
            raise ValidationFailed("deadline must be in the future")

    def list_notifications(self, project_id, user_id=None, kinds=None):
        sql = "SELECT * FROM notifications WHERE project_id=?"
        args = [project_id]
        if user_id is not None:
            sql += " AND (user_id=? OR user_id IS NULL)"; args.append(user_id)
        if kinds:
            sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
            args.extend(kinds)
        sql += " ORDER BY created_at,id"
        return [dict(r) for r in self.store.all(sql, tuple(args))]
