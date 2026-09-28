"""ENE-C2-116 — deterministic domain services (no framework imports, no LLM).

MarketExtractQualityService: validates the structural + semantic quality of an already-approved, org-licensed
JEPX-derived electricity-market extract (structured records: market / delivery-date / time-block / price /
unit / publication-status) before analyst use. It maps naming/unit variance to canonical fields **within a
seeded approved field/unit dictionary — never beyond it** (bounded interpretation), evaluates the seeded
organisation-owned quality profile deterministically (required fields, unit conformance, date/time-block
validity, duplicates, missing intervals, preliminary/final status separation, value-band thresholds, and
unmapped-column flags), retrieves the cited dictionary/quality-profile/rule clauses, and composes a
record-level exception package (needs-review candidates).

Everything here is deterministic and auditable (dictionary lookup + set membership + interval-sequence /
duplicate / threshold rules + keyed clause composition) — there is **no LLM** (no model in config/agent.yaml,
no LLM dependency in pyproject, no LLM call anywhere in src/). Records are keyed by an opaque, non-reversible
``source_row_id`` surrogate; the raw row reference and any free-text metadata are never carried into the
report, and the S-3 output gate re-redacts anything that leaks. **No value is ever repaired, imputed, or
inferred** — exceptions are surfaced, not corrected. Seeded field/unit dictionary + quality profile are
overridable by CoE (a change-controlled engineer MR + specialist review) without touching node logic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, cast

# Two SEPARATE concerns — do not conflate them:
#   (1) PRIVACY (opaque_id): every caller identifier (source_row_id / extract_id) is UNCONDITIONALLY
#       tokenized to a deterministic, non-reversible opaque surrogate so a free-text / PII value (even a bare
#       name like ``Alice`` / ``Taro.Yamada`` / ``TaroYamada``, no spaces/symbols) can never reach a citation
#       or the report. Tokenizing is a privacy measure — it does NOT assert the value is authorized/verifiable.
#       Surrogates are one-way hashes; graph state never stores a surrogate→raw rejoin map, so the opaque ID is
#       non-linkable back to the source system.
#   (2) PROVENANCE (resolve_provenance): a caller ``source`` becomes a grounded CITATION only when it is
#       resolvable against the authorized provenance registry (names a trusted market system of record). Any
#       other free text (a name, ``unknown``, a fabricated value, or a caller value merely SHAPED like a
#       surrogate ``src:1a2b3c4d``) is NOT verifiable provenance → it yields NO citation → S-3 blocks the
#       report as CITATION_INCOMPLETE (fail-closed). "Tokenized" is never sufficient for a citation.
# Tokenization is UNCONDITIONAL (no syntactic passthrough): a caller value merely *shaped* like a surrogate
# (``row:deadbeef``) is re-hashed, never trusted, so it can never forge an internal join key. Identifiers /
# provenance are resolved exactly once at S-1 (pre_process); downstream trusts that resolution verbatim.
_SAFE_TOKEN = re.compile(r"^[a-z0-9_\-]{1,48}$")

# Authorized provenance registry: the market / data systems of record a service operator trusts as verifiable
# data sources. A caller ``source`` is accepted as a grounded citation ONLY when its leading namespace names
# one of these (the "trusted context"). This is the deploying org's / CoE's registry — overridable without
# touching node logic; it is a SEMANTIC allowlist of authorized systems, not a syntactic character class.
AUTHORIZED_PROVENANCE_SYSTEMS = frozenset(
    {
        "jepx",
        "jepx_feed",
        "jepx_spot",
        "spot_market",
        "intraday_market",
        "power_exchange",
        "occto",
        "escj",
        "tso",
        "bg",
        "balancing_group",
        "market_data_feed",
        "market_feed",
        "extract_feed",
        "quality_validated_feed",
        "data_platform",
        "market_data_platform",
        "mdp",
        "etrm",
        "trading_system_of_record",
        "system_of_record",
        "sor",
        "authorized_feed",
        "curated_extract",
    }
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def opaque_id(value: Any, prefix: str) -> str:
    """PRIVACY tokenize a caller identifier to a deterministic, non-reversible opaque surrogate
    ``<prefix>:<sha8>``.

    Caller identifiers are **always** tokenized — no syntactic passthrough — so a name (with or without
    spaces) can never survive into a citation or the report, and a caller value merely *shaped* like a
    surrogate (``row:deadbeef``) is re-hashed rather than trusted (it can never forge an internal join key).
    Same input → same surrogate (report / citations / summary stay joinable within one invocation). This is a
    privacy measure only; it makes no claim that the identifier is authorized, and no surrogate→raw rejoin map
    is ever kept.
    """
    return f"{prefix}:{_sha8(str(value or '').strip())}"


def resolve_provenance(value: Any) -> str | None:
    """Resolve a **raw** caller ``source`` to a grounded, privacy-tokenized CITATION — or ``None``.

    Provenance validation (separate from privacy) and the **single** resolution point (S-1 / pre_process).
    A citation is emitted **only** when the source names an authorized market system of record
    (``<authorized-namespace>[:<ref>]``). Any other value — a name, ``unknown``, a fabricated value, **or a
    value that merely looks like a surrogate (``src:1a2b3c4d``)** — is not verifiable provenance and returns
    ``None`` so the S-3 gate blocks the report as CITATION_INCOMPLETE (fail-closed). When authorized, the raw
    label is never used verbatim: the citation is a privacy hash (``src:<sha8>``) of the authorized reference.
    No synthetic provenance is fabricated.

    ★ Forged-surrogate defence: there is **no format-based passthrough**. A caller-supplied ``src:<hex>`` has
    namespace ``src`` (not an authorized system of record), so it resolves to ``None`` — it is dropped here at
    S-1 and can never reach a citation. Because provenance is resolved exactly once (here), the produced
    ``src:<sha8>`` is the trusted citation downstream and is **never** fed back through this function (which
    would, correctly, reject it), so no forged value can imitate an internal surrogate.
    """
    text = str(value or "").strip()
    if not text:
        return None
    namespace = text.split(":", 1)[0].strip().lower()
    if namespace not in AUTHORIZED_PROVENANCE_SYSTEMS:
        return None  # unverifiable / forged-surrogate provenance → fail-closed (no citation → needs_review)
    return "src:" + _sha8(text)


# ── seeded approved field/unit dictionary (bounded interpretation vocabulary, CoE-calibratable) ──
# Canonical field ← recognised source-column synonyms (NFKC-normalised; lower-cased at match time). Mapping is
# STRICTLY within this dictionary — a column outside it is an ``unmapped_columns`` flag, never inferred.
FIELD_DICTIONARY: dict[str, list[str]] = {
    "market": ["market", "市場", "board", "product", "commodity"],
    "delivery_date": ["delivery_date", "受渡日", "delivery_dt", "receipt_date", "date", "deliverydate"],
    "time_block": ["time_block", "コマ", "時間帯", "slot", "block", "period_block", "timeblock"],
    "price": ["price", "約定価格", "clearing_price", "contract_price", "system_price"],
    "price_unit": ["price_unit", "unit", "単位", "価格単位", "priceunit"],
    "volume": ["volume", "約定量", "contract_qty", "quantity", "qty"],
    "status": ["status", "publication_status", "確報区分", "区分", "kind", "publicationstatus"],
}
# reverse index: synonym(lower) → canonical field
_COLUMN_TO_CANONICAL: dict[str, str] = {
    syn.lower(): canonical for canonical, syns in FIELD_DICTIONARY.items() for syn in syns
}
# columns handled elsewhere (identifier / provenance) — never counted as unmapped.
_IGNORED_COLUMNS = frozenset({"source_row_id", "row_id", "id", "extract_id", "source"})

# ── seeded approved unit allow-list: canonical unit ← recognised notations (lower-cased) ──
UNIT_ALLOWLIST: dict[str, str] = {
    "jpy/kwh": "JPY/kWh",
    "円/kwh": "JPY/kWh",
    "yen/kwh": "JPY/kWh",
    "mwh": "MWh",
    "kwh": "kWh",
}
# ── seeded approved publication-status vocabulary: canonical ← recognised notations (lower-cased) ──
STATUS_VOCAB: dict[str, str] = {
    "preliminary": "preliminary",
    "prelim": "preliminary",
    "速報": "preliminary",
    "暫定": "preliminary",
    "provisional": "preliminary",
    "final": "final",
    "確報": "final",
    "確定": "final",
    "confirmed": "final",
}

# ── seeded organisation-owned quality profile (thresholds, CoE-calibratable) ──
REQUIRED_FIELDS: tuple[str, ...] = ("market", "delivery_date", "time_block", "price", "price_unit", "status")
_TIME_BLOCK_MIN, _TIME_BLOCK_MAX = 0, 47  # 30-min blocks per delivery day (0..47)
_PRICE_BAND = {"min": 0.01, "max": 999.0}  # approved JPY/kWh value band
_MISSING_INTERVAL_HIGH = 3  # >= this many missing intervals in a group = high severity
_DATE_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}$")

# ── seeded quality-profile clause registry (authorized, citable clause per rule) ──
# clause_id@version is a stable, citable reference to the approved quality-profile clause for each check.
QUALITY_PROFILE_CLAUSES: dict[str, dict[str, Any]] = {
    "required_fields": {"clause_id": "QP-REQ", "version": "v2"},
    "unit_conformance": {"clause_id": "QP-UNIT", "version": "v2"},
    "interval_validity": {"clause_id": "QP-INTERVAL", "version": "v2"},
    "duplicate": {"clause_id": "QP-DUP", "version": "v2"},
    "missing_interval": {"clause_id": "QP-GAP", "version": "v2"},
    "status_separation": {"clause_id": "QP-STATUS", "version": "v2"},
    "value_band": {"clause_id": "QP-BAND", "version": "v3"},
    "dictionary_coverage": {"clause_id": "QP-DICT", "version": "v2"},
}
# The dictionary clause a mapping cites (bounded-interpretation vocabulary version).
_DICTIONARY_REF = "FD-JEPX@v3"

# ── seeded quality-exception taxonomy: policy-defined type → description + clause + priority rank ──
EXCEPTION_TAXONOMY: dict[str, dict[str, Any]] = {
    "value_out_of_band": {
        "description": "A metric value falls outside the approved quality-profile band",
        "clause": "value_band",
        "rank": 6,
    },
    "missing_required_field": {
        "description": "A required canonical field is missing or empty for a record",
        "clause": "required_fields",
        "rank": 5,
    },
    "invalid_interval": {
        "description": "The delivery-date or 30-min time-block is malformed / out of range",
        "clause": "interval_validity",
        "rank": 4,
    },
    "duplicate_record": {
        "description": "The same (market, delivery_date, time_block) appears more than once",
        "clause": "duplicate",
        "rank": 3,
    },
    "status_mixing": {
        "description": "Preliminary and final publication status are mixed for the same group",
        "clause": "status_separation",
        "rank": 3,
    },
    "interval_gap": {
        "description": "A 30-min interval is missing within the observed contiguous range",
        "clause": "missing_interval",
        "rank": 2,
    },
    "unit_nonconformance": {
        "description": "A price unit is not in the approved unit allow-list",
        "clause": "unit_conformance",
        "rank": 2,
    },
    "unmapped_field": {
        "description": "A source column is outside the approved dictionary (over-interpretation "
        "guard — flagged, never inferred)",
        "clause": "dictionary_coverage",
        "rank": 1,
    },
    "unclassified": {
        "description": "No policy-defined exception type matched; routed for manual analyst review",
        "clause": "dictionary_coverage",
        "rank": 0,
    },
}
_SEVERITY_WEIGHT = {"high": 2, "med": 1}


def _num(value: Any) -> float | None:
    """Coerce to float; non-numeric / empty → None (never a repaired default)."""
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


class MarketExtractQualityService:
    """Deterministic field-semantic mapping, quality rule checking, clause retrieval, and report composition."""

    # ── field-semantic mapping (bounded interpretation) ─────────────────────────
    @staticmethod
    def map_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Map each supplied record's columns to canonical fields **within the approved dictionary**; normalize
        unit + status notation to the approved vocabulary; flag out-of-dictionary columns as
        ``unmapped_columns`` (over-interpretation guard, no inference). Rows without a ``source_row_id`` are
        dropped.

        ``source_row_id`` is always privacy-tokenized. ``source`` was already resolved to a grounded citation
        (``src:<sha8>``) or ``None`` by pre_process (S-1), the single provenance-resolution point — a forged
        surrogate was dropped there. Mapping trusts that value verbatim; it never re-resolves or fabricates
        provenance, and it never repairs a value.
        """
        out: list[dict[str, Any]] = []
        for raw in records or []:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("source_row_id") or raw.get("row_id") or raw.get("id") or "").strip()
            if not raw_id:
                continue
            source_row_id = opaque_id(raw_id, "row")  # tokenize unconditionally (no forgeable passthrough)
            source = raw.get("source")  # already resolved (src:<sha8> or None) at S-1

            canonical: dict[str, Any] = {}
            unmapped: list[str] = []
            for key, value in raw.items():
                low = str(key).lower()
                if low in _IGNORED_COLUMNS:
                    continue
                cf = _COLUMN_TO_CANONICAL.get(low)
                if cf is None:
                    unmapped.append(low)  # outside the approved dictionary → flagged, not inferred
                    continue
                canonical.setdefault(cf, value)  # first synonym wins (deterministic)

            out.append(
                {
                    "source_row_id": source_row_id,
                    "market": MarketExtractQualityService._norm_token(canonical.get("market")),
                    "delivery_date": (
                        str(canonical["delivery_date"]).strip() if _present(canonical.get("delivery_date")) else None
                    ),
                    "time_block": _int(canonical.get("time_block")),
                    "time_block_present": _present(canonical.get("time_block")),
                    "price": _num(canonical.get("price")),
                    "price_present": _present(canonical.get("price")),
                    "price_unit_raw": (
                        str(canonical.get("price_unit")).strip() if _present(canonical.get("price_unit")) else None
                    ),
                    "price_unit": MarketExtractQualityService._map_unit(canonical.get("price_unit")),
                    "volume": _num(canonical.get("volume")),
                    "status_raw": (str(canonical.get("status")).strip() if _present(canonical.get("status")) else None),
                    "status": MarketExtractQualityService._map_status(canonical.get("status")),
                    "unmapped_columns": sorted(set(unmapped)),
                    "source": source,
                }
            )
        return out

    @staticmethod
    def _norm_token(value: Any) -> str | None:
        text = str(value or "").strip().lower()
        if not text:
            return None
        return text if _SAFE_TOKEN.match(text) else "unknown"

    @staticmethod
    def _map_unit(value: Any) -> str | None:
        if not _present(value):
            return None
        return UNIT_ALLOWLIST.get(str(value).strip().lower())  # None → not in approved allow-list

    @staticmethod
    def _map_status(value: Any) -> str | None:
        if not _present(value):
            return None
        return STATUS_VOCAB.get(str(value).strip().lower())  # None → outside approved status vocabulary

    # ── deterministic quality rule checks ───────────────────────────────────────
    @staticmethod
    def check(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Evaluate the seeded quality profile over the mapped records and return record-level quality
        exceptions. Deterministic set-membership / threshold / interval rules only — the free-text metadata is
        never interpreted semantically, so prompt-like text in a supplied field cannot influence a check."""
        exceptions: list[dict[str, Any]] = []
        for rec in records:
            exceptions.extend(MarketExtractQualityService._check_record(rec))
        exceptions.extend(MarketExtractQualityService._check_cross(records))
        return exceptions

    @staticmethod
    def _check_record(rec: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        missing = [f for f in REQUIRED_FIELDS if not _present_canonical(rec, f)]
        if missing:
            sev = "high" if any(f in ("delivery_date", "time_block", "price") for f in missing) else "med"
            found.append(
                MarketExtractQualityService._mk("missing_required_field", [rec], sev, f"missing_required={missing}")
            )

        # unit conformance: a supplied unit that does not map to the approved allow-list.
        if rec["price_unit_raw"] is not None and rec["price_unit"] is None:
            found.append(
                MarketExtractQualityService._mk(
                    "unit_nonconformance", [rec], "med", f"unit={rec['price_unit_raw']!r} not in approved allow-list"
                )
            )

        # delivery-date / time-block validity.
        reasons: list[str] = []
        if rec["delivery_date"] is not None and not _DATE_RE.match(rec["delivery_date"]):
            reasons.append(f"delivery_date={rec['delivery_date']!r} malformed (want YYYY-MM-DD)")
        if rec["time_block_present"]:
            tb = rec["time_block"]
            if tb is None or tb < _TIME_BLOCK_MIN or tb > _TIME_BLOCK_MAX:
                reasons.append(f"time_block={tb} out of range [{_TIME_BLOCK_MIN},{_TIME_BLOCK_MAX}]")
        if reasons:
            found.append(MarketExtractQualityService._mk("invalid_interval", [rec], "high", "; ".join(reasons)))

        # value-band threshold (only when a numeric price is present).
        if rec["price_present"]:
            v = rec["price"]
            lo, hi = _PRICE_BAND["min"], _PRICE_BAND["max"]
            if v is None:
                found.append(
                    MarketExtractQualityService._mk("value_out_of_band", [rec], "high", "price present but non-numeric")
                )
            elif v < lo or v > hi:
                over = (v - hi) if v > hi else (lo - v)
                band = max(hi - lo, 1e-9)
                sev = "high" if over >= 0.5 * band else "med"
                found.append(
                    MarketExtractQualityService._mk(
                        "value_out_of_band", [rec], sev, f"price={round(v, 3)} outside band [{lo}, {hi}]"
                    )
                )

        # over-interpretation guard: columns outside the approved dictionary.
        if rec["unmapped_columns"]:
            found.append(
                MarketExtractQualityService._mk(
                    "unmapped_field", [rec], "med", f"unmapped_columns={rec['unmapped_columns']}"
                )
            )
        return found

    @staticmethod
    def _check_cross(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in records:
            key = (r["market"] or "unknown", r["delivery_date"] or "unknown")
            groups.setdefault(key, []).append(r)

        for (market, date), grp in sorted(groups.items()):
            # duplicate: same (market, delivery_date, time_block) appearing more than once.
            by_block: dict[int, list[dict[str, Any]]] = {}
            for r in grp:
                if r["time_block"] is not None:
                    by_block.setdefault(r["time_block"], []).append(r)
            dup_rows = [r for rows_ in by_block.values() if len(rows_) > 1 for r in rows_]
            if dup_rows:
                dup_blocks = sorted(b for b, rows_ in by_block.items() if len(rows_) > 1)
                sev = "high" if len(dup_blocks) >= 2 else "med"
                found.append(
                    MarketExtractQualityService._mk(
                        "duplicate_record", dup_rows, sev, f"duplicate blocks={dup_blocks} for {market}/{date}"
                    )
                )

            # missing_interval: a gap within the observed [min_block, max_block] contiguous range.
            present = sorted({r["time_block"] for r in grp if r["time_block"] is not None})
            if len(present) >= 2:
                expected = set(range(present[0], present[-1] + 1))
                gap = sorted(expected - set(present))
                if gap:
                    sev = "high" if len(gap) >= _MISSING_INTERVAL_HIGH else "med"
                    found.append(
                        MarketExtractQualityService._mk(
                            "interval_gap", grp, sev, f"missing_blocks={gap[:8]} (count={len(gap)}) for {market}/{date}"
                        )
                    )

            # status_mixing: both preliminary and final publication status present for the same group.
            statuses = {r["status"] for r in grp if r["status"] is not None}
            if "preliminary" in statuses and "final" in statuses:
                found.append(
                    MarketExtractQualityService._mk(
                        "status_mixing", grp, "high", f"preliminary and final mixed for {market}/{date}"
                    )
                )
        return found

    @staticmethod
    def _mk(exception_type: str, cited_rows: list[dict[str, Any]], severity: str, evidence: str) -> dict[str, Any]:
        """Build one quality exception. ``cited_source_rows`` are the tokenized ids; ``citation`` is the shared
        authorized provenance — or ``None`` if any cited row is ungrounded (→ S-3 fail-closed)."""
        cited_ids = sorted({r["source_row_id"] for r in cited_rows})
        sources = [r["source"] for r in cited_rows]
        grounded = bool(sources) and all(s for s in sources)  # every cited row resolved at S-1
        citation = sources[0] if grounded else None
        meta = EXCEPTION_TAXONOMY[exception_type]
        exception_id = "exc:" + _sha8(f"{exception_type}|{'|'.join(cited_ids)}|{evidence}")
        return {
            "exception_id": exception_id,
            "exception_type": exception_type,
            "exception_description": meta["description"],
            "severity": severity,
            "severity_score": _SEVERITY_WEIGHT[severity],
            "priority_rank": meta["rank"],
            "matched_drivers": [{"exception_type": exception_type, "severity": severity, "evidence": evidence}],
            "cited_source_rows": cited_ids,
            "citation": citation,
        }

    # ── clause retrieval ────────────────────────────────────────────────────────
    @staticmethod
    def retrieve_references(exception: dict[str, Any]) -> dict[str, Any]:
        """Deterministically retrieve the cited dictionary + quality-profile clauses for one exception. Clause
        refs are ``<clause_id>@<version>`` from the seeded authorized quality profile."""
        clause_key = EXCEPTION_TAXONOMY[exception["exception_type"]]["clause"]
        clause = QUALITY_PROFILE_CLAUSES[clause_key]
        return {
            "dictionary_ref": _DICTIONARY_REF,
            "profile_refs": [f"{clause['clause_id']}@{clause['version']}"],
        }

    # ── report composition ───────────────────────────────────────────────────────
    @staticmethod
    def compose_exception(exception: dict[str, Any], refs: dict[str, Any]) -> dict[str, Any]:
        """Compose the per-exception report entry (needs-review, cited). Candidate only — the final
        data-quality decision defers to a human analyst; no value is repaired."""
        return {
            "exception_id": exception["exception_id"],
            "exception_type": exception["exception_type"],
            "exception_description": exception["exception_description"],
            "severity": exception["severity"],
            "severity_score": exception["severity_score"],
            "priority_rank": exception["priority_rank"],
            "matched_drivers": exception["matched_drivers"],
            "cited_source_rows": exception["cited_source_rows"],
            "cited_dictionary_ref": refs["dictionary_ref"],
            "cited_profile_refs": refs["profile_refs"],
            "status_kind": "needs_review",
            "note": "Candidate quality exception only — the final data-quality decision is an authorized "
            "analyst's; this agent surfaces cited exceptions and never repairs, imputes, or infers a "
            "value.",
            "citation": exception["citation"],
        }

    @staticmethod
    def quality_summary(records: list[dict[str, Any]], exceptions: list[dict[str, Any]]) -> dict[str, Any]:
        """Portfolio-level rollup: record + exception count, declared publication status, type distribution,
        completeness, exceptions needing urgent review."""
        distribution: dict[str, int] = {}
        for e in exceptions:
            distribution[e["exception_type"]] = distribution.get(e["exception_type"], 0) + 1
        statuses = sorted({r["status"] for r in records if r["status"] is not None})
        declared = statuses[0] if len(statuses) == 1 else ("mixed" if len(statuses) > 1 else "undeclared")
        total_required = max(1, len(records) * len(REQUIRED_FIELDS))
        present = sum(1 for r in records for f in REQUIRED_FIELDS if _present_canonical(r, f))
        completeness = round(present / total_required, 4)
        urgent = [e["exception_id"] for e in exceptions if e["severity"] == "high"]
        return {
            "records_validated": len(records),
            "total_exceptions": len(exceptions),
            "declared_status": declared,
            "completeness": completeness,
            "type_distribution": distribution,
            "checks_run": sorted(QUALITY_PROFILE_CLAUSES.keys()),
            "exceptions_needing_urgent_review": urgent,
        }


def _present_canonical(rec: dict[str, Any], field: str) -> bool:
    """Presence of a required canonical field on a mapped record (unit/status count as present only when they
    mapped to the approved vocabulary; a supplied-but-nonconforming unit/status is caught by its own check)."""
    if field == "time_block":
        return cast(bool, rec.get("time_block_present", False))
    if field == "price":
        return cast(bool, rec.get("price_present", False))
    if field == "price_unit":
        return rec.get("price_unit_raw") is not None
    if field == "status":
        return rec.get("status_raw") is not None
    return _present(rec.get(field))
