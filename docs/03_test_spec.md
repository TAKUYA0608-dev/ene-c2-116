# ENE-C2-116 — Test Specification

## Strategy

Three layers, all deterministic (**no LLM, no network** — dictionary mapping + rule engine + keyed clause
composition):

- **unit** — `tests/unit/test_nodes.py`: the deterministic `MarketExtractQualityService` (privacy tokenize vs
  provenance resolution, field-semantic mapping incl. naming/unit/status normalisation + unmapped-column
  flags, every quality-check type, clause retrieval, exception composition, quality summary) and each node in
  isolation (S-1/S-2 hygiene + minimise + degrade, inner-node skip guards with S-4 emit, S-3 fail-closed +
  disclaimer gate).
- **unit (graph + real invoke)** — `tests/unit/test_graph.py`: outer GraphNode wiring (alias, class-level
  subgraph cache, extract/merge, outer-first error_code), inner-workflow route/registration, and **end-to-end
  through the real `Graph().invoke()`** for grounded / clean / out-of-scope / injection-degrade /
  oversize-degrade / missing-provenance / unsafe-source / PII-tokenised / market-sensitive-dropped /
  forged-surrogate paths.
- **integration** — `tests/integration/test_end_to_end.py`: multi-record portfolio prioritisation +
  grounding, mixed cited/uncited fail-closed, forged surrogate re-hash, empty → out-of-scope, clean pass.

## Local result (local SDK stub)

- Core suites (`tests/unit/test_graph.py`, `tests/unit/test_nodes.py`, `tests/integration/`): **109 passed,
  1 skipped** (server import skipped when the platform module is unavailable in a local stub env),
  **coverage = 95%** (`--cov=src`, target ≥ 80%).
- Full `tests/` run: **111 passed, 3 skipped, 3 known env-diff failures** (`test_pb_invoke_order`,
  `test_framework_compliance_tc06_tc07::tc06/tc07`). These three assert framework-level `@final` enforcement
  that the local SDK stub shim does not implement; they **pass under the real SDK in CI** and are the unchanged
  scaffold conditional-stub / compliance files (byte-identical to the shipped scaffold and to the reference
  sibling templates).

## Degraded contract (SDK 1.0.0)

Injection markers / oversize / empty input are **never** `status=ERROR`. `_extra_security_gate_input` (S-2)
never raises and returns `dict(state)`; `execute()` re-checks and degrades to `status=SUCCESS` + `error_code`
(`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED`) with the offending body discarded, so
`post_process` always runs and delivers the out-of-scope safe answer + disclaimer + terminal S-4 audit
carrying the `error_code`.

## Key test cases (real `Graph().invoke()`)

| # | Case | Expectation |
|---|------|-------------|
| TC-01 | Grounded exception (authorized source) | `status=SUCCESS`, `status_kind=extract_quality_report`, `value_out_of_band` first, cited dictionary/profile refs, `human_review.required=True`, DRAFT disclaimer |
| TC-02 | Clean extract (all checks pass) | `extract_quality_report`, `quality_exceptions=[]`, `completeness=1.0`, disclaimer |
| TC-03 | NL text / empty | out-of-scope safe answer, `citations=[]`, disclaimer present |
| TC-04 | Injection payload | degraded `SUCCESS` (never ERROR), post ran (`PostProcessNode` in `node_history`), out-of-scope envelope, marker body absent, terminal S-4 audit carries `error_code=INJECTION_REJECTED` |
| TC-05 | Oversize (> 200 000 chars) | degraded `SUCCESS`, S-4 audit carries `error_code=INPUT_TOO_LONG` |
| TC-06 | Missing provenance (grounded exception, no source) | S-3 fail-closed → `needs_review`, exception body withheld, S-4 `error_code=CITATION_INCOMPLETE` |
| TC-07 | Unverifiable / unsafe source (name, phone) | `needs_review`, raw source never in output |
| TC-08 | Forged surrogate source (`src:1a2b3c4d` / `row:deadbeef` / `acct:deadbeef`) | `needs_review`, `citations=[]`, forged value never in output |
| TC-09 | PII / no-space-name `source_row_id` (`Alice` / `TaroYamada`) | tokenized `row:<sha8>`, name never in output, referential integrity exception ↔ citation |
| TC-10 | Market-sensitive fields (counterparty / position / P&L / strategy) | dropped pre-LLM (S-2) — none appear in output |
| TC-11 | Unknown caller field carrying PII | whitelist-by-construction — surfaced only as an unmapped-column count, never verbatim |
| TC-12 | Mixed cited + uncited portfolio | whole grounded report fails-closed to `needs_review` |
| TC-13 | Naming/unit variance (`受渡日`/`コマ`/`約定価格`/`単位`/`確報区分`) | mapped to canonical fields within the approved dictionary |

## Framework Compliance / Proof-of-Boundary

| ID | Boundary | Result |
|----|----------|--------|
| TC-06/07 (`@final` gates) | `_security_gate_input/output` not overridden; only `_extra_*` hooks used | verified under real SDK (env-diff local) |
| PB-4 | Import isolation — no Level-0 `agenticstar` import in `src/` | AST scan: 0 violations |
| PB-6 | Invoke execution order (S-1 → node_start → S-2 → execute → S-3 → node_complete) | verified under real SDK |
| PB-7 | HITL interrupt propagation *(conditional)* | **Auto-waived — non-HITL** (`hitl.enabled` not set) |
| S-4 | ≥1 domain `emit_trace_event` on every `execute()` path (incl. skip/degraded) | verified in unit tests |

## Reproduce

```bash
source .venv/bin/activate
python -m pytest tests/unit/test_graph.py tests/unit/test_nodes.py tests/integration/ -q --cov=src --cov-report=term
ruff check src
python scripts/check_trust_level.py src/
python scripts/check_cat_consistency.py
python scripts/check_dep_pinning.py
```
