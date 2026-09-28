"""ENE-C2-116 — post_process node: QualityReportCompose / OutputSanitise (S-3 output gate + S-4 audit).

S-3 (fail-closed): **enforce** per-exception citation completeness — a grounded ExtractQualityReport whose
exceptions are not all cited to a verifiable source is never presented; it degrades to a safe ``needs_review``
answer with the exception bodies withheld (``error_code=CITATION_INCOMPLETE``, still SUCCESS so post/S-4/
disclaimer run). A clean extract (0 exceptions) is a valid grounded report — there is nothing ungrounded to
withhold. Re-redact any credential / My-Number / email / phone / analyst- or company-name leakage
(defense-in-depth), and append the mandatory DRAFT no-repair advisory disclaimer — the report is a decision
aid, not an authoritative data-quality decision or an official settlement record; the final call is a human
analyst's, gated by the HumanApprovalGate, and **no value is ever repaired**. S-4 (no-persist): emit an audit
event (counts / type distribution / review flag / error_code only — never an analyst name, free-text
metadata, counterparty identifier, or raw/unmasked market value); the raw record is not retained. Runs on the
full report, the citation-blocked branch, and the out-of-scope safe branch.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, cast

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event

_DISCLAIMER = (
    "本レポートは提供された認可済み・ライセンス済みの電力市場 extract に対する参考用の DRAFT（候補・要レビュー）"
    "品質検証結果であり、値の修正・補完・推定は一切行いません。extract を確報・確定の公式決済記録として扱うもの"
    "でも、価格・需給の予測、取引・調達・入札の推奨を行うものでもありません。最終的なデータ品質判断および"
    "downstream 利用の可否は、必ず認可された人手のアナリストによる確認（HumanApprovalGate）を経てください。"
    "本エージェントは cited な品質例外の候補を surface するのみで、修正・実行は行いません。"
)

_CITATION_INCOMPLETE_MSG = (
    "品質例外の一部に検証可能な出典（provenance）が確認できなかったため、根拠不十分な例外レポートの提示を"
    "差し控えました。各 source_row に認可済みシステムの参照ID（source）を付与のうえ再実行してください。"
)
_NEEDS_REVIEW_NOTE = (
    "Grounding could not be verified for every quality exception; the draft exception report is withheld "
    "pending valid provenance and authorized human analyst review."
)

# S-3 defense-in-depth: re-redact secrets / contact info / analyst-or-company names that could leak into any
# free-text field of the report (applied to the whole serialized report before it becomes the output envelope).
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}|\d{12})\b")
_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}")
_PHONE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
# Person / company names: an English name run ending in a corporate suffix, or a Japanese company form.
_NAME = re.compile(
    r"(?:[A-Z][A-Za-z0-9&.\-]*\s){1,4}(?:Inc|Corp|Corporation|Ltd|LLC|LLP|GmbH|PLC|K\.?K|KK)\b\.?"
    r"|[^\s\"',]{1,24}(?:株式会社|有限会社|合同会社)"
    r"|(?:株式会社|有限会社|合同会社)[^\s\"',]{1,24}"
)
_REDACTORS = (_SECRET, _EMAIL, _PHONE, _NAME)


def _redact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Serialize → redact secret / contact / name patterns → deserialize (whole-report defense)."""
    text = json.dumps(report, ensure_ascii=False)
    for pattern in _REDACTORS:
        text = pattern.sub("[REDACTED]", text)
    return cast(dict[str, Any], json.loads(text))


class PostProcessNode(FunctionNode):
    """Verify citations, redact leakage, append the DRAFT no-repair disclaimer, emit S-4 audit."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """S-3 preservation check: the DRAFT advisory disclaimer must be present in the output envelope.

        SDK 1.0.0 contract: receives the **result dict from ``execute()``**; returns the (possibly filtered)
        result. MAY raise to block an output missing the mandatory disclaimer.
        """
        out = result.get("formatted_output", "")
        if out and "参考" not in out and "DRAFT" not in out:
            raise ValueError("S-3: DRAFT advisory disclaimer missing from output")
        return dict(result)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = _redact_report(json.loads(state.get("result", "{}") or "{}"))

        grounded = report.get("status_kind") == "extract_quality_report"
        citations = report.get("citations", [])
        exceptions = report.get("quality_exceptions", [])
        # S-3 per-entry authoritative correspondence: every exception must carry BOTH its own local citation AND
        # an exact top-level {exception_id, source} citation for the same exception (not merely a non-empty
        # citation list — a partially ungrounded exception, or a top-level citation belonging to a different
        # exception, must fail closed). A clean 0-exception report is trivially complete (nothing to ground).
        cited_sources = {c.get("exception_id"): c.get("source") for c in citations if c.get("source")}
        citation_complete = (not grounded) or (
            all(e.get("citation") for e in exceptions)
            and all(cited_sources.get(e.get("exception_id")) == e.get("citation") for e in exceptions)
        )

        # S-3 fail-closed: an ungrounded report (any exception missing a verifiable citation) is never
        # presented. Degrade to a safe needs-review answer (SUCCESS + error_code), withhold the exception
        # bodies, and still run the disclaimer + terminal S-4 audit.
        if grounded and not citation_complete:
            error_code = state.get("error_code") or "CITATION_INCOMPLETE"
            blocked: dict[str, Any] = {
                "status_kind": "needs_review",
                "extract_id": report.get("extract_id"),
                "scope": report.get("scope"),
                "quality_summary": {},
                "quality_exceptions": [],  # incomplete exception bodies withheld
                "human_review": {"required": True, "status": "pending_human_approval", "note": _NEEDS_REVIEW_NOTE},
                "citations": [],
                "citation_complete": False,
                "message": _CITATION_INCOMPLETE_MSG,
                "disclaimer": _DISCLAIMER,
            }
            emit_trace_event(
                "quality_report_compose.citation_blocked",
                {"exception_count": len(exceptions), "error_code": error_code},
                state,
            )
            return {
                "formatted_output": json.dumps(blocked, ensure_ascii=False),
                "disclaimer": _DISCLAIMER,
                "audit_logged": True,
                "error_code": error_code,
                "status": AgentStatus.SUCCESS.value,
            }

        human_review = report.get("human_review", {"required": False, "status": "not_required"})

        formatted = {
            "status_kind": report.get("status_kind"),
            "extract_id": report.get("extract_id"),
            "scope": report.get("scope"),
            "declared_status": report.get("declared_status"),
            "quality_summary": report.get("quality_summary", {}),
            "quality_exceptions": exceptions,
            "human_review": human_review,
            "citations": citations,
            "citation_complete": citation_complete,
            "message": report.get("message"),
            "disclaimer": _DISCLAIMER,
        }
        emit_trace_event(
            "quality_report_compose.complete",
            {
                "status_kind": report.get("status_kind"),
                "exception_count": len(exceptions),
                "review_required": human_review.get("required", False),
                "citation_complete": citation_complete,
                "error_code": state.get("error_code"),
            },
            state,
        )
        return {
            "formatted_output": json.dumps(formatted, ensure_ascii=False),
            "disclaimer": _DISCLAIMER,
            "audit_logged": True,
            "status": AgentStatus.SUCCESS.value,
        }
