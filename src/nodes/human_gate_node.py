"""ENE-C2-116 — inner workflow step 6 gate: human_gate (HumanApprovalGate).

Deterministic human-in-the-loop gate. It does **not** execute anything and it never repairs a value or makes
a data-quality decision — it marks the ExtractQualityReport as requiring an authorized human analyst's
sign-off before the extract is used downstream, records the material findings (each surfaced quality
exception, high-severity ones flagged) into the report, and sets ``human_review_required``. Skips (no-op) on
the rejected / 0-record out-of-scope safe-answer branch (no human gate needed) after emitting a skip audit
event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event


class HumanGateNode(FunctionNode):
    """Flag the report for authorized human analyst sign-off; record material findings; set the review flag."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report = json.loads(state.get("result") or "{}")
        if (
            state.get("error_code")
            or state.get("mapped_count", 0) == 0
            or report.get("status_kind") != "extract_quality_report"
        ):
            emit_trace_event("human_gate.skip", {"reason": state.get("error_code") or "no_report"}, state)
            return {
                "human_review_required": False,
                "review_status": "not_required",
                "status": AgentStatus.SUCCESS.value,
            }

        material: list[dict[str, Any]] = []
        for exc in report.get("quality_exceptions", []):
            material.append(
                {
                    "exception_id": exc["exception_id"],
                    "exception_type": exc["exception_type"],
                    "severity": exc["severity"],
                    "reason": "Candidate quality exception — requires authorized analyst sign-off before the "
                    "extract is used downstream (no value is repaired by this agent)",
                }
            )

        # A validated extract (clean or with exceptions) requires analyst confirmation before downstream use.
        review = {
            "required": True,
            "status": "pending_human_approval",
            "note": "Data-quality acceptance and any downstream use must be confirmed by an authorized human "
            "analyst. This agent produces a candidate ExtractQualityReport only and never repairs a "
            "value.",
            "material_findings": material,
        }
        report["human_review"] = review
        emit_trace_event(
            "human_gate.complete", {"review_required": True, "material_finding_count": len(material)}, state
        )
        return {
            "result": json.dumps(report, ensure_ascii=False),
            "human_review_required": True,
            "review_status": review["status"],
            "status": AgentStatus.SUCCESS.value,
        }
