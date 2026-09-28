# Template Design Specification — ENE-C2-116

Electricity Market Extract Quality Validator (Cat 2, GraphNode-in-main).

## Position in AgentCore Architecture

- **Agent Class**: `ElectricityMarketExtractQualityValidatorAgent` (module-level alias of `Graph`)
- **L1 Base**: AgentBaseGraph (L1 direct — Cat 2 GraphNode-in-main; **not** AutonomousBaseGraph). The
  `DocGenerationAgent` L2 pattern is a design reference only; the workflow is implemented directly on
  AgentBaseGraph (2026-05-18 L2-deprecation ruling).
- **Category**: Cat 2 — orchestrates a fixed multi-step workflow to produce one job-to-be-done deliverable
  (an ExtractQualityReport for an already-approved, org-licensed electricity-market extract).
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible); complex fields are JSON strings (ADR-005)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only — no `config` param)
  - Graph: composition (`register_nodes()` for node substitution; domain complexity behind a `GraphNode`)
- **LLM**: none. The template is **fully deterministic** (bounded field/unit dictionary mapping + set
  membership + threshold/interval/duplicate rule checks + keyed clause composition against a seeded approved
  field/unit dictionary + organisation-owned quality profile). There is no model in `config/agent.yaml`, no
  LLM dependency in `pyproject.toml`, and no LLM call anywhere in `src/`. "pre-LLM" in the S-2 discussion
  below therefore means "before any downstream node reads the free-text metadata".

## Architecture Overview

### Node Configuration (outer 5-slot backbone)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema/session/trust setup | user_input | caller_trust_level, session_id | InitializeNode (default) |
| pre_process | `ExtractIngest` + `SensitiveDataDetectAndMinimise` — S-1 normalisation (NFKC, control-strip, size cap) + S-2 pre-LLM minimisation. **injection/oversize → degraded `SUCCESS + error_code`, offending body discarded (never `status=ERROR`)**. Field-level input hygiene (credential/My-Number/email/phone redaction); **PII display fields dropped**; **market-sensitive / counterparty-identifier fields DROPPED (not masked)** before the workflow; identifiers (`source_row_id`/`extract_id`) **UNCONDITIONALLY tokenized to opaque, non-reversible surrogates** (a bare name is opaque like any value, no surrogate→raw rejoin map kept); **provenance `source` resolved to a citation ONLY if it names an authorized market system of record (privacy-tokenized `src:<sha8>`), else dropped to `None`** — S-3 then blocks; `scope`/`period`/`extract_id` hygiened | user_input | validated_input, input_format, enriched_context, (error_code) | PreProcessNode (FunctionNode) |
| main | `ExtractQualityValidateWorkflowGraphNode` — wraps inner `ExtractQualityValidateWorkflow` (composition criterion #9) | validated_input | result, exception_count, human_review_required, (error_code), status | GraphNode (subgraph) |
| post_process | `QualityReportCompose` / `OutputSanitise` — S-3 output gate: **fail-closed per-exception citation completeness** (any exception without a matching top-level `{exception_id, source}` citation → `needs_review` degrade, exception body withheld, `error_code=CITATION_INCOMPLETE`) + name/company/phone/email/credential/My-Number re-redaction + DRAFT no-repair disclaimer, S-4 no-persist audit | result | formatted_output, disclaimer, audit_logged, (error_code) | PostProcessNode (FunctionNode) |
| finalize | build response envelope | formatted_output | output, status | FinalizeNode (default) |

### Inner workflow (`src/graph/domain_workflow_graph.py` — BaseGraph, linear + per-node skip guard)

```
START → field_semantic_map → rule_check → exception_synthesise → human_gate → END
```

| Inner Node | Responsibility | Skip guard |
|------|---------------|-----------|
| field_semantic_map | **Step 3 — bounded interpretation (Agent value)**: deterministic ingest + normalize of the supplied records; map naming/unit variance (e.g. `受渡日`/`delivery_dt` → `delivery_date`; `円/kWh` → `JPY/kWh`) to canonical market/delivery-date/time-block/price/unit/publication-status fields **within the approved dictionary vocabulary — never beyond it**; a column outside the dictionary is recorded as an `unmapped_columns` entry (over-interpretation guard, no inference); set `mapped_count`. **0 valid records → `error_code=NO_RECORDS` → out-of-scope safe answer**. Free-text metadata is never interpreted semantically | — (first node; emits `.skip` on rejected/no-record input) |
| rule_check | **Step 4 — deterministic (Tool separation recommended)**: evaluate required fields, unit conformance, delivery-date/time-block validity, duplicate detection, missing-interval detection, preliminary/final status separation, quality-profile threshold (value band) checks, and unmapped-field flags against the seeded quality profile → per-exception deviation set with matched drivers + evidence + severity; set `exception_count` (0 = clean extract) | no-op `return {}` (after `.skip` emit) on `error_code` / `mapped_count == 0` |
| exception_synthesise | **Step 5 — synthesis (Agent value)**: retrieve the cited dictionary/quality-profile/rule clauses (`<clause_id>@<version>`) per exception and compose the **ExtractQualityReport** deliverable: declared publication status, checks-run summary + type distribution, per-exception record-level entry (source-row refs + rationale + needs-review candidate mark + source citation, **no inferred/repaired values**), citations list; ordered by priority (value-band / missing-required first). On 0-record/rejected → out-of-scope safe answer | emits safe answer on `error_code` / no records |
| human_gate | **Step 6 gate — deterministic HumanApprovalGate**: mark `human_review_required=True` + `review_status="pending_human_approval"`, record material findings (each exception requires an authorized analyst's sign-off before the extract is used downstream). The agent proposes and **never** confirms/repairs | no-op `return {}` (after `.skip` emit) on `error_code` / `mapped_count == 0` (safe answer needs no human gate) |

`ExtractQualityValidateWorkflowGraphNode.get_subgraph()` caches the compiled inner workflow on the **class
attribute** (`ExtractQualityValidateWorkflowGraphNode._subgraph`, not `self` — avoids mutable node-instance
state per §9; built once; `BaseGraph.invoke()` `_ensure_compiled` is idempotent). `extract_input()` passes
`validated_input` into the inner graph; `merge_output()` surfaces `result / exception_count /
human_review_required / error_code / status` — with **`error_code` OUTER-first** (`state.get("error_code")
or sub_result.get("error_code")`) so a pre-stage rejection survives to the terminal S-4 audit (the inner
workflow runs on the discarded body and would otherwise overwrite it with `NO_RECORDS`).

## Security Model (S-1 … S-5)

- **S-1 (input normalisation + field hygiene)**: NFKC + control-char strip + size cap (no injection
  attribution here); every string written into `validated_input` is passed through
  credential/My-Number/email/phone redaction; identifiers are tokenized to opaque surrogates; provenance
  resolved exactly once here.
- **S-2 (pre-LLM sensitive-data detect & minimise, market-data)**: market-sensitive / counterparty-identifier
  fields (counterparty / member id / trader / participant / position / P&L / strategy / bid) are **dropped
  entirely — not masked** — before the workflow runs, so they are never used, inferred, or carried. Only the
  minimised working copy proceeds. Opaque IDs are non-reversible one-way hashes with **no surrogate→raw
  rejoin map in graph state**; the S-4 audit references only minimised counts. **Injection containment is
  pre-LLM**: prompt-injection markers or oversize input degrade to a safe out-of-scope answer *without any
  semantic execution of the offending text*.
  - **Degraded contract (SDK 1.0.0)**: an S-2 rejection is surfaced as **`status=SUCCESS` + `error_code`**
    (`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED`) with the offending body discarded — it is
    **never `status=ERROR`** (which would short-circuit `route()` straight to `finalize`, skipping
    `post_process` and thus the disclaimer / S-3 redaction / S-4 audit). `post_process` therefore always
    runs and always delivers the out-of-scope safe answer + disclaimer + audit. `_extra_security_gate_input`
    MUST NOT raise and MUST `return dict(state)`; `execute()` re-checks the same conditions because the
    local stub framework does not invoke the `@final` hook.
- **S-3 (output gate, fail-closed)**: enforce per-exception citation completeness — a grounded
  ExtractQualityReport in which any exception lacks a verifiable `source` citation (missing, or a top-level
  citation belonging to a different exception) is **never presented**; it degrades to `needs_review` with the
  exception body withheld (`error_code=CITATION_INCOMPLETE`, still SUCCESS). A clean extract (0 exceptions)
  is a valid grounded report (nothing ungrounded to withhold). Re-redact any leaked secret/contact/name
  pattern (defense-in-depth). Append the mandatory DRAFT no-repair advisory disclaimer.
  `_extra_security_gate_output` receives the `execute()` result delta and MAY raise to block an output
  missing the disclaimer.
- **S-4 (audit, no-persist)**: every node `execute()` path — including every skip/0-count/degraded branch —
  emits a count-only domain trace event via `src.utils.audit.emit_trace_event`; payloads carry counts /
  type distribution / rule keys / error codes only (no analyst name, free-text metadata, counterparty
  identifier, or raw/unmasked market value beyond aggregate check evidence). The raw/unmasked extract is not
  retained.
- **S-5 (rate limit / abuse)**: enforced at the platform entry point; the agent is read-only and performs
  no external write.

## Read-only / non-execution boundary

The agent **never** fetches restricted/unlicensed data, repairs / imputes / infers a value, forecasts price
or supply-demand, recommends a trade / procurement / bid, submits a bid, or treats the extract as an official
settlement record. All output is **candidate / needs-review**, and the final data-quality decision is always
an authorized human analyst's, gated by the HumanApprovalGate.

## Open Items (Stage ③ implementation plan)

The design MR ships `docs/02` + `src/schemas/state.py` only. The Stage ③ implementation MR adds: the six
node implementations (pre_process, the four inner nodes, post_process), the inner/outer graph wiring
(`get_subgraph` class-level caching + `merge_output` outer-first error_code), the deterministic
`MarketExtractQualityService` (seeded field/unit dictionary + quality profile + rule engine +
provenance/opaque-id helpers), `src/utils/audit.py` (S-4 shim), the
`ElectricityMarketExtractQualityValidatorAgent = Graph` registry alias, and the unit / integration /
real-invoke tests (including the forged-surrogate, missing-provenance, and market-sensitive-field
regressions). The seeded field/unit dictionary + quality profile are CoE-calibratable via a change-controlled
engineer MR + specialist review — they are not runtime-editable operational actions.

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | fixed multi-step deterministic workflow, no autonomous loop |
| Composition pattern | GraphNode (subgraph) | Standalone | **GraphNode (subgraph)** | Cat 2 encapsulates domain workflow in the `main` slot |
| Error propagation | propagate | handle | **propagate** | degraded paths surface `error_code`; outer route runs post_process |
