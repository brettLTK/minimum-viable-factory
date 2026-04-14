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
    prototype_flow_entry,
    prototype_generator_fanout,
    prototype_eval_gate,
    prototype_selection_gate,
    graduation_trigger,
)


def should_block(state: FactoryState) -> str:
    if state.get("error") or state.get("current_state") == "Blocked":
        return "blocked_handler"
    return "continue"


def route_flow_type(state: FactoryState) -> str:
    """Deterministic routing based on PM agent classification. No LLM involved."""
    if state.get("error") or state.get("current_state") == "Blocked":
        return "blocked_handler"
    flow_type = state.get("flow_type", "prototype")
    if flow_type == "direct_sdlc":
        return "direct_sdlc"
    return "prototype"


def route_graduation(state: FactoryState) -> str:
    """Deterministic routing based on prototype winner. No LLM involved."""
    if state.get("error") or state.get("current_state") == "Blocked":
        return "blocked_handler"
    winner = state.get("prototype_winner", "")
    if not winner or winner == "Archived":
        return "archive"
    return "graduation"


def qa_fanout(state: FactoryState) -> list[str]:
    """After dev_parallel, fan out to both QA agents or block."""
    if state.get("error") or state.get("current_state") == "Blocked":
        return ["blocked_handler"]
    return ["review_agent", "test_agent"]


def build_graph() -> StateGraph:
    """Construct the pipeline graph (uncompiled — caller adds checkpointer)."""
    builder = StateGraph(FactoryState)

    # Prototype flow nodes (GH #362)
    builder.add_node("prototype_flow_entry", prototype_flow_entry)
    builder.add_node("prototype_generator_fanout", prototype_generator_fanout)
    builder.add_node("prototype_eval_gate", prototype_eval_gate)
    builder.add_node("prototype_selection_gate", prototype_selection_gate)
    builder.add_node("graduation_trigger", graduation_trigger)

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

    # Prototype flow edges (GH #362)
    builder.add_conditional_edges(
        "prototype_flow_entry",
        route_flow_type,
        {
            "direct_sdlc": "pm_agent",
            "prototype": "prototype_generator_fanout",
            "blocked_handler": "blocked_handler",
        },
    )
    builder.add_conditional_edges(
        "prototype_generator_fanout",
        should_block,
        {"blocked_handler": "blocked_handler", "continue": "prototype_eval_gate"},
    )
    builder.add_edge("prototype_eval_gate", "prototype_selection_gate")
    builder.add_conditional_edges(
        "prototype_selection_gate",
        route_graduation,
        {
            "graduation": "graduation_trigger",
            "archive": "done_handler",
            "blocked_handler": "blocked_handler",
        },
    )
    builder.add_edge("graduation_trigger", "done_handler")

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
