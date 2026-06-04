"""
LangGraph graph definition — used by run_agent() (non-streaming).
"""

from langgraph.graph import StateGraph, START, END
from .schemas import AgentState
from .nodes import (
    classify_node, search_node, change_node, cancel_node,
    qa_node, clarify_node, error_node, router_node,
)


def build_graph():
    g = StateGraph(AgentState)

    g.add_node("classify", classify_node)
    g.add_node("search", search_node)
    g.add_node("change", change_node)
    g.add_node("cancel", cancel_node)
    g.add_node("qa", qa_node)
    g.add_node("clarify", clarify_node)   # low-confidence path
    g.add_node("error", error_node)

    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", router_node, {
        "search": "search",
        "change": "change",
        "cancel": "cancel",
        "qa": "qa",
        "clarify": "clarify",
        "error": "error",
    })
    for node in ("search", "change", "cancel", "qa", "clarify", "error"):
        g.add_edge(node, END)

    return g.compile()


agent_graph = build_graph()
