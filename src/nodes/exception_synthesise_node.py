"""ENE-C2-116 — inner workflow step 5: exception_synthesise.

Composes the **ExtractQualityReport** deliverable: a quality summary (records validated, declared publication
status, completeness, type distribution, checks run), and a per-exception record-level entry (policy-defined
exception type, cited source rows, cited dictionary / quality-profile clauses, rationale, needs-review mark),
each cited to its authorized provenance. Exceptions are ordered by priority (value-band / missing-required
first). The report is candidate / advisory only — it never repairs, imputes, or infers a value, and it never
treats the extract as an official settlement record. A clean extract (0 exceptions) yields a valid report
with declared_status="passed". On the 0-record / rejected branch it emits the out-of-scope safe answer.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import MarketExtractQualityService
from src.utils.audit import emit_trace_event

_OUT_OF_SCOPE = (
    "検証可能な電力市場 extract レコードが入力に見つかりませんでした。"
    "records 配列に source_row_id・market・delivery_date(YYYY-MM-DD)・time_block(0-47)・price・price_unit・"
    "status・source を含む JSON をご指定いただくか、対象範囲・期間を明確にしてください。"
)


class ExceptionSynthesiseNode(FunctionNode):
    """Compose the ExtractQualityReport deliverable with citations (or safe answer on 0 records)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        records = json.loads(state.get("mapped_records") or "[]")
        if state.get("error_code") or state.get("mapped_count", 0) == 0 or not records:
            emit_trace_event("exception_synthesise.safe", {"reason": state.get("error_code") or "no_records"}, state)
            report: dict[str, Any] = {
                "status_kind": "out_of_scope",
                "message": _OUT_OF_SCOPE,
                "quality_summary": {},
                "quality_exceptions": [],
                "citations": [],
            }
            return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

        exceptions = json.loads(state.get("quality_exceptions") or "[]")
        entries: list[dict[str, Any]] = []
        citations: list[dict[str, str]] = []
        for e in exceptions:
            refs = MarketExtractQualityService.retrieve_references(e)
            entries.append(MarketExtractQualityService.compose_exception(e, refs))
            citations.append({"exception_id": e["exception_id"], "source": e["citation"]})
        entries.sort(key=lambda x: (-x["priority_rank"], -x["severity_score"], x["exception_id"]))

        summary = MarketExtractQualityService.quality_summary(records, exceptions)
        report = {
            "status_kind": "extract_quality_report",
            "extract_id": self._extract_id(state),
            "scope": self._scope(state),
            "declared_status": summary["declared_status"],
            "quality_summary": summary,
            "quality_exceptions": entries,
            "citations": citations,
        }
        emit_trace_event(
            "exception_synthesise.complete",
            {"exception_count": len(entries), "citation_count": len(citations), "records": len(records)},
            state,
        )
        return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _slots(state: dict[str, Any]) -> dict[str, Any]:
        slots = json.loads(state.get("validated_input") or "{}")
        return slots if isinstance(slots, dict) else {}

    @classmethod
    def _extract_id(cls, state: dict[str, Any]) -> Any:
        return cls._slots(state).get("extract_id")

    @classmethod
    def _scope(cls, state: dict[str, Any]) -> dict[str, Any]:
        slots = cls._slots(state)
        return {"scope": slots.get("scope"), "period": slots.get("period")}
