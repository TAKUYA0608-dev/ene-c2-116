"""ENE-C2-116 — pre_process node: ExtractIngest + SensitiveDataDetectAndMinimise (S-1 + S-2, pre-LLM).

Accepts a structured JSON request (a ``records[]`` array of already-approved, org-licensed JEPX-derived
market-extract rows, plus optional ``extract_id`` / ``scope`` / ``period``) or NL text, normalizes it (NFKC),
enforces S-1/S-2, and extracts the analysis slots. The agent is read-only: it never fetches restricted data,
repairs a value, forecasts, or sends a transaction.

Degraded contract (SDK 1.0.0): injection markers / oversize / empty never set ``status=ERROR``. They return
``status=SUCCESS + error_code`` (``INJECTION_REJECTED`` / ``INPUT_TOO_LONG`` / ``INPUT_REJECTED``) and
**discard the offending body** so ``main``/``post_process`` still run (disclaimer + S-3 + S-4). The ``@final``
framework hook is not invoked by the local stub framework, so ``execute()`` re-checks the same S-2 conditions
itself. Injection containment is pre-LLM: prompt-like input degrades to a safe out-of-scope answer *without
any semantic execution* of the offending text.

Field-level input hygiene (S-1) + sensitive-data minimisation (S-2, pre-LLM, market-data):
- every string written into ``validated_input`` is passed through ``_hygiene()`` (redacts credential /
  My-Number / email / phone patterns);
- **PII display fields** (analyst / owner / contact name / email / phone) are DROPPED — never masked;
- **market-sensitive / counterparty-identifier fields** (counterparty / member id / trader / participant /
  position / P&L / strategy / bid) are DROPPED entirely before the workflow — never used, inferred, or
  carried;
- identifier fields (``source_row_id`` / ``extract_id``) are UNCONDITIONALLY tokenized to an opaque,
  non-reversible surrogate (a bare name is opaque like any value), with no surrogate→raw rejoin map kept;
- caller-supplied provenance (``source``) is constrained to a grounded citation only if it resolves to an
  authorized market system of record; anything else is dropped so untrusted text can never reach a citation;
- the extract's free-text metadata is treated as quoted data / schema-constrained input (never interpreted
  semantically downstream). ``extract_id`` / ``scope`` / ``period`` are hygiened too.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import opaque_id, resolve_provenance
from src.utils.audit import emit_trace_event

_MAX_INPUT = 200_000  # market extracts carry many 30-min source rows → larger cap than a chat prompt
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "system prompt",
    "you are now",
    "###system",
    "<|im_start|>",
)
_REJECT_CODES = frozenset({"INJECTION_REJECTED", "INPUT_TOO_LONG"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# ── input hygiene: redact secrets a caller may inadvertently include before persisting to State ──
_CREDENTIAL = re.compile(r"\b(sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})\b")
_MY_NUMBER = re.compile(r"\b\d{12}\b")  # Japanese My-Number / 個人番号
_EMAIL = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}\b")
_PHONE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
_REDACTED = "[REDACTED]"

# PII display fields dropped entirely — rows are keyed by opaque IDs, never by a person's name/contact.
_PII_DROP_FIELDS = frozenset(
    {
        "analyst_name",
        "analyst",
        "reviewer",
        "reporter",
        "reporter_name",
        "owner",
        "owner_name",
        "operator",
        "operator_name",
        "author",
        "submitted_by",
        "contact_name",
        "contact_email",
        "contact_phone",
        "name",
        "full_name",
        "email",
        "phone",
        "tel",
    }
)
# Market-sensitive / counterparty-identifier fields DROPPED before the workflow (S-2, market-data): they are
# not needed for structural quality validation and are never used, inferred, or carried.
_SENSITIVE_DROP_FIELDS = frozenset(
    {
        "counterparty",
        "counterparty_id",
        "counterparty_name",
        "member_id",
        "member",
        "participant_id",
        "participant",
        "trader",
        "trader_id",
        "trader_name",
        "desk",
        "book",
        "portfolio",
        "position",
        "position_mwh",
        "net_position",
        "pnl",
        "p_and_l",
        "strategy",
        "bid",
        "bid_price",
        "offer",
        "offer_price",
    }
)
# Identifier keys are unconditionally tokenized to an opaque surrogate — no syntactic passthrough — so a PII /
# free-text identifier can never leak into State, a citation, or output. prefix chosen per key.
_ID_PREFIX = {"source_row_id": "row", "row_id": "row", "id": "row", "extract_id": "ext"}


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _hygiene(text: str) -> str:
    """Redact credential / My-Number / email / phone patterns from a free-text value."""
    out = _CREDENTIAL.sub(_REDACTED, text)
    out = _MY_NUMBER.sub(_REDACTED, out)
    out = _EMAIL.sub(_REDACTED, out)
    out = _PHONE.sub(_REDACTED, out)
    return out


def _tokenize_id(value: Any, prefix: str) -> str | None:
    """Tokenize a top-level identifier (e.g. ``extract_id``) to an opaque surrogate, or ``None`` when absent."""
    text = str(value).strip() if value is not None else ""
    return opaque_id(text, prefix) if text else None


def _hygiene_obj(obj: Any) -> Any:
    """Recursively drop PII / market-sensitive fields, tokenize identifiers, constrain provenance to an
    authorized citation, and redact secrets in every string value."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = k.lower()
            if key in _PII_DROP_FIELDS or key in _SENSITIVE_DROP_FIELDS:
                continue  # dropped, not masked
            if key in _ID_PREFIX:
                text = str(v).strip() if v is not None else ""
                out[k] = opaque_id(text, _ID_PREFIX[key]) if text else None
                continue
            if key == "source":
                # Provenance → grounded citation only if it resolves to an authorized system of record
                # (privacy-tokenized); unverifiable / free-text source → None → S-3 blocks (needs_review).
                out[k] = resolve_provenance(v)
                continue
            out[k] = _hygiene_obj(v)
        return out
    if isinstance(obj, list):
        return [_hygiene_obj(v) for v in obj]
    if isinstance(obj, str):
        return _hygiene(obj)
    return obj


class PreProcessNode(FunctionNode):
    """Validate + minimise the extract request and extract its records / extract_id / scope slots."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject_code(self, raw: str) -> str | None:
        if len(raw) > _MAX_INPUT:
            return "INPUT_TOO_LONG"
        if any(marker in _nfkc(raw).lower() for marker in _INJECTION_MARKERS):
            return "INJECTION_REJECTED"
        return None

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2 domain checks: size cap + prompt-injection markers (pre-LLM containment).

        SDK 1.0.0 contract: MUST NOT raise, and MUST NOT set status=ERROR (that would short-circuit the
        pipeline past post_process). A rejection is surfaced as a degraded ``SUCCESS + error_code``; the
        offending body is discarded by execute() with no semantic execution of the prompt-like text.
        """
        raw = str(state.get("user_input") or "")  # coerce non-string caller input (S-2 never raises)
        code = self._reject_code(raw)
        if code:
            out = dict(state)
            out["error_code"] = code
            return out
        return dict(state)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = str(state.get("user_input") or "")  # coerce non-string caller input (S-2 never raises)
        input_context = state.get("input_context", {})  # read-only caller context [C1]
        enriched = json.dumps(
            {
                "source": "ElectricityMarketExtractQualityValidatorAgent",
                "channel": input_context.get("channel", "unknown"),
            },
            ensure_ascii=False,
        )

        # Degrade on rejection: the S-2 hook may already have set error_code (real SDK); re-detect here because
        # the local stub framework does not invoke the hook. Discard the offending body entirely.
        prior = state.get("error_code")
        code = prior if prior in _REJECT_CODES else self._reject_code(raw)
        if code:
            emit_trace_event("extract_ingest.rejected", {"reason": code}, state)
            return {
                "validated_input": "{}",
                "input_format": "rejected",
                "enriched_context": enriched,
                "user_input": "",
                "error_code": code,
                "status": AgentStatus.SUCCESS.value,
            }

        if not raw.strip():
            emit_trace_event("extract_ingest.rejected", {"reason": "empty_input"}, state)
            return {
                "validated_input": "{}",
                "input_format": "empty",
                "enriched_context": enriched,
                "error_code": "INPUT_REJECTED",
                "status": AgentStatus.SUCCESS.value,
            }

        slots, fmt = self._parse(_CONTROL.sub("", _nfkc(raw)))
        emit_trace_event(
            "extract_ingest.validated",
            {
                "input_format": fmt,
                "record_count": len(slots["records"]),
                "has_extract_id": slots.get("extract_id") is not None,
            },
            state,
        )
        return {
            "validated_input": json.dumps(slots, ensure_ascii=False),
            "input_format": fmt,
            "enriched_context": enriched,
            "status": AgentStatus.SUCCESS.value,
        }

    def _parse(self, text: str) -> tuple[dict[str, Any], str]:
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return {"records": [], "extract_id": None, "scope": None, "period": None}, "text"
        if isinstance(obj, dict):
            records = (
                obj.get("records")
                if isinstance(obj.get("records"), list)
                else (obj.get("rows") if isinstance(obj.get("rows"), list) else [])
            )
            # every free-text field is untrusted → hygiene it (S-3 re-redacts at output).
            return {
                "records": _hygiene_obj(records),
                "extract_id": _tokenize_id(obj.get("extract_id"), "ext"),
                "scope": _hygiene_obj(obj.get("scope")),
                "period": _hygiene_obj(obj.get("period")),
            }, "json"
        if isinstance(obj, list):  # bare records array
            return {"records": _hygiene_obj(obj), "extract_id": None, "scope": None, "period": None}, "json"
        return {"records": [], "extract_id": None, "scope": None, "period": None}, "text"
