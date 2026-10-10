"""
demo_agent.py - minimal LangGraph "Experiment Planner" agent, wired to the local tracer.

This is a real head start on the actual Day-2 "AI experiment planner" deliverable
(rank/select the next failure experiment from the dependency map, with a reason), not a
throwaway demo. It uses a FAKE chat model so it runs with zero API key, proving the
LangGraph -> LocalTracer -> dashboard pipeline end to end. Swap FakeListChatModel for
ChatOpenAI/ChatAnthropic/ChatGoogleGenerativeAI (one line, see build_llm()) once a real
key is available -- the graph, state, and tracer don't change.

Usage:
    python demo_agent.py
"""
import json
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END

from agent_tracer import LocalTracer

DATA = Path("data")


def build_llm():
    """FAKE model -- deterministic, zero API key, exists only so this runs standalone
    right now. Replace with e.g. ChatOpenAI(model="gpt-4o-mini") once a key exists;
    everything downstream (graph, state, tracer) is unchanged."""
    return FakeListChatModel(responses=[
        "TARGET: product-catalog\n"
        "FAULT: pause\n"
        "REASON: highest criticality_score and traffic_share in the dependency map, "
        "and no experiment has been run against it yet -- most informative first test."
    ])


class PlannerState(TypedDict):
    services: list
    past_runs: list
    decision: str


def gather_context(state: PlannerState) -> PlannerState:
    dep = json.loads((DATA / "dependency_map.json").read_text())
    services = [s for s in dep["services"] if s["role"] == "app"]
    runs_path = DATA / "runs.jsonl"
    past_runs = []
    if runs_path.exists():
        past_runs = [json.loads(line)["target"] for line in runs_path.read_text().splitlines() if line.strip()]
    return {**state, "services": services, "past_runs": past_runs}


def plan_experiment(state: PlannerState, config: RunnableConfig) -> PlannerState:
    llm = build_llm()
    summary = "\n".join(
        f"- {s['name']}: criticality={s['criticality_score']}, "
        f"traffic_share={s['traffic_share']}, already_tested={s['name'] in state['past_runs']}"
        for s in state["services"]
    )
    messages = [
        SystemMessage(content="You are the chaos-experiment planner. Pick ONE service to "
                               "target next and say why, in the format "
                               "TARGET/FAULT/REASON."),
        HumanMessage(content=f"Services:\n{summary}\n\nPast experiments: {state['past_runs']}"),
    ]
    # Forward only the callbacks, not the whole node config: config also carries an
    # explicit run_id for plan_experiment's OWN span, and passing that wholesale makes the
    # LLM call reuse that exact run_id instead of getting a fresh child one (confirmed live
    # -- every event came back with the same run_id until this was narrowed to callbacks
    # only). Callbacks alone preserve correct parent-run nesting without that collision.
    response = llm.invoke(messages, config={"callbacks": config.get("callbacks")})
    return {**state, "decision": response.content}


tracer = LocalTracer()

graph = StateGraph(PlannerState)
graph.add_node("gather_context", gather_context)
graph.add_node("plan_experiment", plan_experiment)
graph.set_entry_point("gather_context")
graph.add_edge("gather_context", "plan_experiment")
graph.add_edge("plan_experiment", END)
app = graph.compile()


if __name__ == "__main__":
    result = app.invoke(
        {"services": [], "past_runs": [], "decision": ""},
        config={"callbacks": [tracer], "run_name": "experiment_planner_run"},
    )
    print("Planner decision:\n" + result["decision"])
    print(f"\nTrace written to {tracer.out_path}")
