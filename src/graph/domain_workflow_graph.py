"""ENE-C2-116 — inner domain workflow graph (Cat 2).

Instantiated by ExtractQualityValidateWorkflowGraphNode.get_subgraph() in graph.py. Linear topology with
per-node skip guards (the portable Cat 2 form; conditional edges don't propagate across the subgraph
boundary):

    START → field_semantic_map → rule_check → exception_synthesise → human_gate → END

On rejected / 0-record input, field_semantic_map sets mapped_count=0 (+error_code); rule_check and human_gate
no-op and exception_synthesise emits the out-of-scope safe answer — no fabricated report.
"""

from __future__ import annotations
from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState

from src.nodes.exception_synthesise_node import ExceptionSynthesiseNode
from src.nodes.field_semantic_map_node import FieldSemanticMapNode
from src.nodes.human_gate_node import HumanGateNode
from src.nodes.rule_check_node import RuleCheckNode
from src.schemas.state import State


class ExtractQualityValidateWorkflow(BaseGraph):
    """Inner graph: field_semantic_map → rule_check → exception_synthesise → human_gate."""

    @property
    def name(self) -> str:
        return "ExtractQualityValidateWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract.
        self._nodes["field_semantic_map"] = FieldSemanticMapNode()
        self._nodes["rule_check"] = RuleCheckNode()
        self._nodes["exception_synthesise"] = ExceptionSynthesiseNode()
        self._nodes["human_gate"] = HumanGateNode()

    def add_edges(self) -> None:
        # Static linear backbone; the 0-record / rejected skip is handled by per-node guards.
        self._sg.add_edge(START, "field_semantic_map")
        self._sg.add_edge("field_semantic_map", "rule_check")
        self._sg.add_edge("rule_check", "exception_synthesise")
        self._sg.add_edge("exception_synthesise", "human_gate")
        self._sg.add_edge("human_gate", END)

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph ABC. Linear topology → not wired to a conditional edge."""
        if state.get("error_code") or state.get("mapped_count", 0) == 0:
            return "exception_synthesise"
        return "rule_check"

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "exception_count": state.get("exception_count", 0),
            "human_review_required": state.get("human_review_required", False),
            "error_code": state.get("error_code"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
