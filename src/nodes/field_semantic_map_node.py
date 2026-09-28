"""ENE-C2-116 — inner workflow step 3: field_semantic_map.

Deterministic ingest + normalization of the supplied already-approved market-extract records, mapping each
record's naming/unit variance to canonical market / delivery-date / time-block / price / unit /
publication-status fields **within the seeded approved field/unit dictionary — never beyond it** (bounded
interpretation, the Agent value). A source column outside the dictionary is recorded as an ``unmapped_columns``
entry (over-interpretation guard; flagged downstream, never inferred). Sets ``mapped_count``. **0 valid records
(rejected input, non-JSON text, or all rows missing source_row_id) routes to the out-of-scope safe answer** —
the agent never fabricates a validation for data it did not receive. The free-text metadata is never
interpreted semantically, so prompt-like text in a supplied field cannot influence the mapping.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import MarketExtractQualityService
from src.utils.audit import emit_trace_event


class FieldSemanticMapNode(FunctionNode):
    """Ingest + map supplied records to canonical fields (bounded interpretation) and flag unmapped columns."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # Records arrive already validated + provenance-resolved by pre_process (S-1): each `source` is a
        # grounded citation `src:<sha8>` or None (a forged surrogate was dropped at S-1). We do not re-run
        # provenance here — mapping trusts that single upstream resolution.
        slots = json.loads(state.get("validated_input") or state.get("user_input") or "{}")
        if not isinstance(slots, dict):
            slots = {}
        canonical = json.dumps(slots, ensure_ascii=False)
        records = slots.get("records") if isinstance(slots.get("records"), list) else []

        if state.get("error_code") or not records:
            emit_trace_event("field_semantic_map.skip", {"reason": state.get("error_code") or "no_records"}, state)
            return {
                "validated_input": canonical,
                "mapped_records": "[]",
                "mapped_count": 0,
                "error_code": state.get("error_code") or "NO_RECORDS",
                "status": AgentStatus.SUCCESS.value,
            }

        mapped = MarketExtractQualityService.map_records(records)
        if not mapped:
            emit_trace_event("field_semantic_map.skip", {"reason": "all_malformed"}, state)
            return {
                "validated_input": canonical,
                "mapped_records": "[]",
                "mapped_count": 0,
                "error_code": "NO_RECORDS",
                "status": AgentStatus.SUCCESS.value,
            }

        unmapped_total = sum(len(r["unmapped_columns"]) for r in mapped)
        emit_trace_event(
            "field_semantic_map.complete",
            {"supplied": len(records), "mapped": len(mapped), "unmapped_column_count": unmapped_total},
            state,
        )
        return {
            "validated_input": canonical,
            "mapped_records": json.dumps(mapped, ensure_ascii=False),
            "mapped_count": len(mapped),
            "status": AgentStatus.SUCCESS.value,
        }
