"""ENE-C2-116 — inner workflow step 4: rule_check.

Deterministically evaluates the seeded organisation-owned quality profile over the mapped records: required
fields, unit conformance, delivery-date / time-block validity, duplicate detection, missing-interval
detection, preliminary/final publication-status separation, value-band thresholds, and unmapped-field flags.
Produces the record-level quality-exception set (each with matched drivers + evidence + severity + cited
source rows + provenance citation). Sets ``exception_count`` (0 = clean extract — still a valid report, not a
skip). Skips (no-op) on rejected / 0-record input, after emitting a skip audit event. The free-text metadata
is never interpreted semantically, so prompt-like text in a supplied field cannot influence a check.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import MarketExtractQualityService
from src.utils.audit import emit_trace_event


class RuleCheckNode(FunctionNode):
    """Evaluate the seeded quality profile deterministically and produce record-level quality exceptions."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("mapped_count", 0) == 0:
            emit_trace_event("rule_check.skip", {"reason": state.get("error_code") or "no_records"}, state)
            return {}

        records = json.loads(state.get("mapped_records") or "[]")
        exceptions = MarketExtractQualityService.check(records)
        distribution: dict[str, int] = {}
        for e in exceptions:
            distribution[e["exception_type"]] = distribution.get(e["exception_type"], 0) + 1
        emit_trace_event(
            "rule_check.complete",
            {"records": len(records), "exceptions": len(exceptions), "type_distribution": distribution},
            state,
        )
        return {
            "quality_exceptions": json.dumps(exceptions, ensure_ascii=False),
            "exception_count": len(exceptions),
            "status": AgentStatus.SUCCESS.value,
        }
