"""Watermark policy.

Every rendered copy of a document carries a watermark identifying the
viewer, the declared purpose and the retrieval time, so that leaked
salary schedules and contracts are traceable back to one access record.
"""
from __future__ import annotations

from dataclasses import dataclass

POLICIES = ("none", "stamp", "cover")


@dataclass
class WatermarkDecision:
    policy: str
    text: str
    required: bool  # access is denied when a required watermark cannot be applied


def build(policy: str, viewer_name: str, purpose: str, when: str, project_country: str) -> WatermarkDecision:
    if policy not in POLICIES:
        policy = "stamp"
    if policy == "none":
        return WatermarkDecision("none", "", False)
    text = (
        f"CONFIDENTIAL - {project_country} | viewer: {viewer_name} | "
        f"purpose: {purpose} | retrieved: {when}"
    )
    return WatermarkDecision(policy, text, True)
