#!/usr/bin/env python3
"""Seed a small demo scenario so the API can be poked by hand.

    python3 demo.py --db data/demo.db
    python3 -m dataroom --db data/demo.db --port 8080
"""
from __future__ import annotations

import argparse

from dataroom import Clock, DataRoomService, Store


def seed(db_path: str):
    svc = DataRoomService(Store(db_path), Clock("2026-09-18T09:00:00Z"))

    admin = svc.create_user("Ada Admin", "admin", "usr_admin")
    fin = svc.create_user("Finn Finance", "finance", "usr_fin")
    legal = svc.create_user("Lia Legal", "legal", "usr_legal")
    tax = svc.create_user("Theo Tax", "tax", "usr_tax")

    project = svc.create_project("Project Merlion", "SG", "Asia/Singapore",
                                 admin["id"])
    for uid, role, cl in ((admin["id"], "admin", 3), (fin["id"], "reviewer", 2),
                          (legal["id"], "reviewer", 2), (tax["id"], "viewer", 1)):
        svc.grant_permission(project["id"], uid, role, cl, admin["id"])

    req = svc.create_request(project["id"], "Target payroll pack", "SG", 2,
                             "2026-10-01T17:00", fin["id"])
    doc = svc.create_document(req["id"], "payroll-2026.xlsx", "secret", 2,
                              fin["id"])
    svc.upload_version(doc["id"], b"salary schedule v1", fin["id"],
                       "initial upload")
    review = svc.open_review(doc["id"], 1, legal["id"], "change-of-control")
    svc.cite_evidence(review["id"], doc["id"], 1)
    link = svc.create_link(doc["id"], 1, fin["id"],
                           "external counsel contract review", 72, "stamp")

    print(f"project_id   {project['id']}")
    print(f"request_id   {req['id']}")
    print(f"document_id  {doc['id']}")
    print(f"review_id    {review['id']}")
    print(f"link_id      {link['id']}")
    print("users: usr_admin / usr_fin / usr_legal / usr_tax")
    print("\ntry:")
    print(f"  curl 'http://127.0.0.1:8080/documents/{doc['id']}"
          f"?actor=usr_fin&purpose=QoE'")
    print(f"  curl 'http://127.0.0.1:8080/documents/{doc['id']}"
          f"?actor=usr_tax&purpose=peek'   # 403 denied_clearance")
    print(f"  curl http://127.0.0.1:8080/links/{link['id']}")
    print(f"  curl http://127.0.0.1:8080/records?project_id={project['id']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/demo.db")
    args = parser.parse_args()
    seed(args.db)
