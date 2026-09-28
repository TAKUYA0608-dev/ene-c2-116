# ENE-C2-116 — Integration: full outer Graph().invoke() across a multi-record extract portfolio

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph

_SUCCESS = AgentStatus.SUCCESS.value


def _invoke(user_input: str):
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return Graph().invoke(user_input, ctx=ctx)


def test_multi_record_portfolio_prioritised_and_grounded():
    payload = {
        "extract_id": "ext-2026-07",
        "scope": "jepx-spot-2026-07",
        "records": [
            # value out of band (high priority) — grounded
            {"source_row_id": "r1", "market": "spot", "delivery_date": "2026-07-01", "time_block": 0,
             "price": 5000.0, "price_unit": "円/kWh", "status": "final", "source": "jepx:r1"},
            # unit non-conformance — grounded
            {"source_row_id": "r2", "market": "spot", "delivery_date": "2026-07-01", "time_block": 1,
             "price": 12.5, "price_unit": "barrels", "status": "final", "source": "jepx:r2"},
            # unmapped column — grounded
            {"source_row_id": "r3", "market": "intraday", "delivery_date": "2026-07-01", "time_block": 5,
             "price": 9.9, "price_unit": "円/kWh", "status": "final", "source": "jepx:r3", "extra": "x"},
        ],
    }
    out = _invoke(json.dumps(payload))
    assert out["status"] == _SUCCESS
    env = json.loads(out["output"])
    assert env["status_kind"] == "extract_quality_report"
    assert env["quality_exceptions"] and env["citations"]
    # every exception is cited (all rows have authorized provenance) → report is presented
    assert env["citation_complete"] is True
    # the value-band breach (highest priority rank) is ranked first
    assert env["quality_exceptions"][0]["exception_type"] == "value_out_of_band"
    assert env["quality_summary"]["records_validated"] == 3
    assert env["human_review"]["required"] is True
    assert "DRAFT" in env["disclaimer"]


def test_mixed_cited_and_uncited_blocks_whole_report():
    """Per-exception citation completeness: one uncited exception fails-closed the whole grounded report."""
    payload = {"records": [
        {"source_row_id": "r1", "market": "spot", "delivery_date": "2026-07-01", "time_block": 0,
         "price": 5000.0, "price_unit": "円/kWh", "status": "final", "source": "jepx:r1"},   # cited
        {"source_row_id": "r2", "market": "spot", "delivery_date": "2026-07-02", "time_block": 1,
         "price": 6000.0, "price_unit": "円/kWh", "status": "final"},                          # uncited
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "needs_review"
    assert env["quality_exceptions"] == [] and env["citations"] == []


def test_forged_source_row_id_surrogate_rehashed():
    # ★ a caller value SHAPED like an internal surrogate (row:deadbeef) is re-hashed at S-1 (no syntactic
    # passthrough), so it can never forge an internal join key / reference another record.
    payload = {"records": [
        {"source_row_id": "row:deadbeef", "market": "spot", "delivery_date": "2026-07-01", "time_block": 0,
         "price": 5000.0, "price_unit": "円/kWh", "status": "final", "source": "jepx:r1"},
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "extract_quality_report"
    cited = env["quality_exceptions"][0]["cited_source_rows"][0]
    assert cited.startswith("row:") and cited != "row:deadbeef"   # re-hashed, not passthrough
    assert "row:deadbeef" not in out["output"]


def test_empty_object_is_out_of_scope():
    out = _invoke(json.dumps({"records": []}))
    env = json.loads(out["output"])
    assert env["status_kind"] == "out_of_scope" and env["citations"] == []


def test_clean_extract_passes_end_to_end():
    payload = {"records": [
        {"source_row_id": "r1", "market": "spot", "delivery_date": "2026-07-01", "time_block": 0,
         "price": 12.5, "price_unit": "円/kWh", "status": "final", "source": "jepx:r1"},
        {"source_row_id": "r2", "market": "spot", "delivery_date": "2026-07-01", "time_block": 1,
         "price": 13.0, "price_unit": "円/kWh", "status": "final", "source": "jepx:r2"},
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "extract_quality_report"
    assert env["quality_exceptions"] == []
    assert env["quality_summary"]["completeness"] == 1.0
