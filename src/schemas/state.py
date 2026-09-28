"""ENE-C2-116 — Agent state (Electricity Market Extract Quality Validator, Cat 2).

ADR-005: State is a flat TypedDict — never a validation/BaseModel instance. Complex fields are stored
as JSON strings (``NotRequired[str]`` + ``# JSON:``); nodes ``json.dumps`` on write / ``json.loads`` on read.

Read-only / advisory: the agent ingests an already-approved, org-licensed JEPX-derived electricity-market
extract (structured records: market / delivery-date / time-block / price / unit / publication-status +
free-text metadata) together with a seeded approved field/unit dictionary + organisation-owned quality
profile, and produces an **ExtractQualityReport** deliverable of cited, record-level quality exceptions
(needs-review candidates) — it never fetches restricted data, repairs / imputes / infers a value, forecasts
price or supply-demand, recommends a trade / procurement / bid, or treats the extract as an official
settlement record. The final data-quality decision is always an authorized human analyst's, and the report
output is candidate / needs-review only.

All agent-specific fields are NotRequired (populated progressively; absent at empty-start invoke).
"""

from __future__ import annotations


from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for the extract structural/semantic quality-validation + exception-package workflow."""

    # ── pre_process (ExtractIngest + SensitiveDataMinimise; S-1 + S-2 pre-LLM) ──
    validated_input: str  # JSON: {records[], extract_id, scope, period} (PII/market-sensitive minimised)
    input_format: str  # "json" | "text" | "empty" | "rejected"
    enriched_context: str  # JSON: {source, channel} (read-only caller context)

    # ── inner workflow (field_semantic_map → rule_check → exception_synthesise → human_gate) ─
    mapped_records: str  # JSON: [{source_row_id, canonical{...}, unmapped_columns[], source}]
    mapped_count: int  # records mapped to canonical fields (0 → out-of-scope safe answer)
    quality_exceptions: str  # JSON: [{exception_id, exception_type, cited_source_rows[], severity, citation}]
    exception_count: int  # record-level exceptions synthesised (0 = clean extract, still a valid report)
    result: str  # JSON: assembled ExtractQualityReport (incl. human_review)
    human_review_required: bool  # True once the HumanApprovalGate flags material findings
    review_status: str  # "pending_human_approval" | "not_required"

    # ── post_process (QualityReportCompose — S-3 gate + S-4 audit) ──────────────
    formatted_output: str  # JSON: final response envelope (report + disclaimer)
    disclaimer: str  # mandatory DRAFT / advisory-only (no-repair) disclaimer
    audit_logged: bool  # True once the terminal audit event is emitted

    # ── degraded-path signalling (SUCCESS + error_code, never status=ERROR) ─────
    # INPUT_REJECTED | INJECTION_REJECTED | INPUT_TOO_LONG | NO_RECORDS | CITATION_INCOMPLETE
    error_code: str
    error_message: str  # operator-facing detail
