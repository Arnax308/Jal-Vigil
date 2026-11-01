"""The agent's state graph.

interpret_request -> load_area -> (fetch_rainfall || fetch_climatology) -> assess_risk
  assess_risk --alert-->     find_facilities -> plan_routes -> draft_alert -> translate_alert -> END
  assess_risk --all_clear--> all_clear -> END
"""

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import FloodState


def build_graph():
    g = StateGraph(FloodState)
    for fn in (
        nodes.interpret_request, nodes.load_area, nodes.fetch_rainfall, nodes.fetch_climatology,
        nodes.assess_risk, nodes.find_facilities, nodes.plan_routes, nodes.draft_alert,
        nodes.translate_alert, nodes.all_clear,
    ):
        g.add_node(fn.__name__, fn)

    g.add_edge(START, "interpret_request")
    g.add_edge("interpret_request", "load_area")
    # Rainfall and climatology are independent, so they run in the same superstep.
    g.add_edge("load_area", "fetch_rainfall")
    g.add_edge("load_area", "fetch_climatology")
    g.add_edge(["fetch_rainfall", "fetch_climatology"], "assess_risk")
    g.add_conditional_edges(
        "assess_risk", nodes.route_after_assessment,
        {"alert": "find_facilities", "all_clear": "all_clear"},
    )
    g.add_edge("find_facilities", "plan_routes")
    g.add_edge("plan_routes", "draft_alert")
    g.add_edge("draft_alert", "translate_alert")
    g.add_edge("translate_alert", END)
    g.add_edge("all_clear", END)
    return g.compile()


graph = build_graph()
