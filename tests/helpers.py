"""Shared fixtures for the data-room test suite."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataroom import Clock, DataRoomService, Store  # noqa: E402


class World:
    """A seeded world: one Singapore project with finance/legal/tax
    members holding different clearance grants."""

    def __init__(self, db=":memory:", now="2026-09-18T09:00:00Z"):
        self.clock = Clock(now)
        self.store = Store(db)
        self.svc = DataRoomService(self.store, self.clock)

        self.admin = self.svc.create_user("Ada Admin", "admin", "usr_admin")
        self.fin = self.svc.create_user("Finn Finance", "finance", "usr_fin")
        self.legal = self.svc.create_user("Lia Legal", "legal", "usr_legal")
        self.tax = self.svc.create_user("Theo Tax", "tax", "usr_tax")

        self.project = self.svc.create_project(
            "Project Merlion", "SG", "Asia/Singapore", self.admin["id"])
        self.pid = self.project["id"]

        for uid, role, cl in (
            (self.admin["id"], "admin", 3),
            (self.fin["id"], "reviewer", 2),
            (self.legal["id"], "reviewer", 2),
            (self.tax["id"], "viewer", 1),
        ):
            self.svc.grant_permission(self.pid, uid, role, cl, self.admin["id"])

    def request_with_document(self, title="Target payroll pack",
                              country="SG", clearance=2, filename="payroll.xlsx",
                              content=b"salary schedule v1"):
        req = self.svc.create_request(
            self.pid, title, country, clearance, "2026-10-01T17:00",
            self.fin["id"])
        doc = self.svc.create_document(
            req["id"], filename, "secret", clearance, self.fin["id"])
        self.svc.upload_version(doc["id"], content, self.fin["id"],
                                "initial upload")
        return req, doc

    def close(self):
        self.store.close()


def temp_db():
    fd, path = tempfile.mkstemp(prefix="dataroom-", suffix=".db")
    os.close(fd)
    return path
