"""ENE-C2-116 — S-4 audit trace helper (platform logger + stderr fallback).

Prefers the platform audit logger (`shared.utils.audit_logger.emit_trace_event`); falls back to a
best-effort stderr record in the local stub framework. Never raises and never logs raw extract content —
counts / exception-type distribution / rule-clause keys / error codes only (no analyst name, no free-text
metadata, no counterparty identifier, no raw/unmasked market value beyond aggregate check evidence). S-4
no-persist: the raw/unmasked extract and any PII are not retained; the audit holds minimised rule + reference
data only.
"""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from shared.utils.audit_logger import emit_trace_event as _platform_emit
except Exception:  # pragma: no cover - import-time environment branch
    _platform_emit = None

_TEMPLATE_ID = "ENE-C2-116"


def emit_trace_event(event_type: str, payload: dict[str, Any], state: dict[str, Any] | None = None) -> None:
    """Emit a domain audit event (best-effort, never raises)."""
    if _platform_emit is not None:
        try:
            _platform_emit(event_type, payload, state)
            return
        except Exception:  # pragma: no cover - defensive
            pass
    record = {
        "template_id": _TEMPLATE_ID,
        "event_type": event_type,
        "payload": payload,
        "session_id": (state or {}).get("session_id"),
    }
    try:
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
    except Exception:  # pragma: no cover - defensive
        pass
