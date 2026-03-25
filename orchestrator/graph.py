"""LangGraph state machine — defines the pipeline DAG."""

from langgraph.graph import StateGraph, END

from orchestrator.state import FactoryState
from orchestrator.nodes import (
    pm_agent,
    architect_agent,
    decompose,
    dev_parallel,
    review_agent,
    test_agent,
    gate_1,
    gate_2,
    gate_3,
    deploy_agent,
    done_handler,
    blocked_handler,
)


def should_block(state: FactoryState) -> str:
    if state.get("error") or state.get("current_state") == "Blocked":
        return "blocked_handler"
    return "continue"


def qa_fanout(state: FactoryState) -> list[str]:
    """After dev_parallel, fan out to both QA agents or block."""
    if state.get("error") or state.get("current_state") == "Blocked":
        return ["blocked_handler"]
    return ["review_agent", "test_agent"]


def build_graph() -> StateGraph:
    """Construct the pipeline graph (uncompiled — caller adds checkpointer)."""
    builder = StateGraph(FactoryState)

    # Nodes
    builder.add_node("pm_agent", pm_agent)
    builder.add_node("gate_1", gate_1)
    builder.add_node("architect_agent", architect_agent)
    builder.add_node("gate_2", gate_2)
    builder.add_node("decompose", decompose)
    builder.add_node("dev_parallel", dev_parallel)
    builder.add_node("review_agent", review_agent)
    builder.add_node("test_agent", test_agent)
    builder.add_node("gate_3", gate_3)
    builder.add_node("deploy_agent", deploy_agent)
    builder.add_node("done_handler", done_handler)
    builder.add_node("blocked_handler", blocked_handler)

    # Edges: Spec → Gate 1 → Arch → Gate 2 → Decompose → Parallel Dev → QA → Gate 3 → Deploy
    builder.set_entry_point("pm_agent")
    builder.add_conditional_edges(
        "pm_agent", should_block, {"blocked_handler": "blocked_handler", "continue": "gate_1"}
    )
    builder.add_edge("gate_1", "architect_agent")
    builder.add_conditional_edges(
        "architect_agent", should_block, {"blocked_handler": "blocked_handler", "continue": "gate_2"}
    )
    builder.add_edge("gate_2", "decompose")
    builder.add_edge("decompose", "dev_parallel")

    # Fan-out: dev_parallel -> review + test in parallel (or block)
    builder.add_conditional_edges("dev_parallel", qa_fanout)

    # Fan-in: both QA agents -> gate_3
    builder.add_edge("review_agent", "gate_3")
    builder.add_edge("test_agent", "gate_3")

    builder.add_edge("gate_3", "deploy_agent")
    builder.add_conditional_edges(
        "deploy_agent", should_block, {"blocked_handler": "blocked_handler", "continue": "done_handler"}
    )
    builder.add_edge("done_handler", END)
    builder.add_edge("blocked_handler", END)

    return builder
