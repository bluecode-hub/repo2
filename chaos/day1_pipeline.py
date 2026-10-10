"""
day1_pipeline.py - the Day-1 agent pipeline: capture baseline -> plan one experiment ->
inject + watchdog -> observe impact -> thin explanation. Built as a real LangGraph
StateGraph with LangChain LLM nodes, traced by the same LocalTracer that already feeds
the dashboard's "agent traces" tab (tab 3) -- running this populates tab 3, tab 4
(baseline vs injection) and tab 5 (live log) automatically, nothing dashboard-side needs
to change per run.

Scope decision (2026-10-10): runs exactly ONE experiment and stops -- that's the Day-1
minimum ("one safe failure injection with a working kill switch"), not the Day-2 minimum
("three or more"). The graph structure already supports looping back to plan_experiment
for Day 2; today's entry point just doesn't take that edge.

Usage:
    python day1_pipeline.py
"""
import json
import time
from pathlib import Path
from typing import Optional, TypedDict

import docker
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END

from agent_tracer import LocalTracer
from fault_injector import DAY1_SAFE_FAULTS, FAULT_DESCRIPTIONS, FaultInjector
from monitor import Monitor
from safety_injector import (
    MAX_BLIND_POLLS, MAX_FRONTEND_DOWN_POLLS, POLL_INTERVAL_SEC,
    RECOVERY_HEALTHY_STREAK, RECOVERY_TIMEOUT_SEC,
)

DATA = Path("data")
DATA.mkdir(exist_ok=True)
COMPARISON_FILE = DATA / "injection_comparison.jsonl"
LOG_FILE = DATA / "live_log.jsonl"
DEP_MAP_FILE = DATA / "dependency_map.json"
RUNS_FILE = DATA / "runs.jsonl"

tracer = LocalTracer()
monitor = Monitor()
injector = FaultInjector()
docker_client = docker.from_env()


def log(message: str, level: str = "info"):
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "level": level, "message": message}) + "\n")
    print(f"[{level.upper()}] {message}")


def append_comparison_row(row: dict):
    with COMPARISON_FILE.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ==================================================================== state
class PipelineState(TypedDict):
    baseline_snapshot: dict
    services: list
    target: str
    fault: str
    reason: str
    injection: object       # fault_injector.Injection, not serialized, in-memory only
    aborted: bool
    abort_reason: Optional[str]
    recovery_sec: Optional[float]
    before: dict
    after: dict
    recovered_snapshot: dict
    explanation: str


# ==================================================================== node: baseline
def capture_baseline(state: PipelineState, attempts: int = 4, gap_sec: float = 3.0) -> PipelineState:
    """A single snapshot can be one transient blip -- confirmed live: a baseline capture
    once caught 'products'/'cart' probes failing and 0 Prometheus services for one instant,
    then the exact same live check a moment later came back fully healthy. Every other
    safety-relevant read in this system (safety_injector.py's pre-flight, the watchdog's
    down-poll counter) already requires more than one bad sample before acting on it --
    this node didn't, and it's the one that gets pinned at the top of the whole comparison
    page, so a single noisy read looked like a false "the system was already broken" story.
    Retries like the proven stable_preflight() pattern; if it's STILL unhealthy after every
    retry, that's real, and it gets stored and flagged rather than silently smoothed over."""
    log("capturing baseline conditions (pre-injection steady state)")
    snap, healthy, reasons = None, False, []
    for i in range(attempts):
        snap = monitor.snapshot()
        healthy, reasons = monitor.is_healthy(snap)
        if healthy:
            break
        log(f"baseline attempt {i + 1}/{attempts} not steady-state yet: {reasons}", "warn")
        if i < attempts - 1:
            time.sleep(gap_sec)
    if not healthy:
        log(f"baseline still NOT steady-state after {attempts} attempts: {reasons} -- "
            f"storing it anyway, flagged in the record as genuinely degraded", "warn")
    append_comparison_row({"type": "baseline", "ts": time.time(), "healthy": healthy,
                            "reasons": reasons, "snapshot": snap})
    log(f"baseline stored: probe_error_rate={snap['probe_error_rate']:.1%} "
        f"probe_max_ms={snap['probe_max_ms']:.0f}ms services_tracked={len(snap['services'])}")

    dep = json.loads(DEP_MAP_FILE.read_text())
    app_services = [s for s in dep["services"] if s["role"] == "app" and s["chaos_target_allowed"]]
    return {**state, "baseline_snapshot": snap, "services": app_services}


# ==================================================================== node: plan

# Set by --target/--fault on the CLI, or by the dashboard's "run experiment" dropdown
# (passed the same way, via CLI args to the subprocess it launches). None means "let the
# planner auto-rank," which is the default Day-1 behavior.
MANUAL_OVERRIDE = {"target": None, "fault": None}


def build_planner_llm(app_services, past_targets):
    if MANUAL_OVERRIDE["target"]:
        pick = next((s for s in app_services if s["name"] == MANUAL_OVERRIDE["target"]), None)
        if pick is None:
            raise SystemExit(f"'{MANUAL_OVERRIDE['target']}' is not a valid chaos target "
                              f"(not in dependency_map.json's app services, or chaos_target_allowed=false)")
        fault = MANUAL_OVERRIDE["fault"] or (DAY1_SAFE_FAULTS[0] if pick["single_point_of_failure"] else DAY1_SAFE_FAULTS[1])
        text = (
            f"TARGET: {pick['name']}\n"
            f"FAULT: {fault}\n"
            f"REASON: manually selected by the operator (not auto-ranked). "
            f"{pick['name']} has criticality_score={pick['criticality_score']}, "
            f"single_point_of_failure={pick['single_point_of_failure']}, "
            f"traffic_share={pick['traffic_share']}. {FAULT_DESCRIPTIONS[fault]}."
        )
        return FakeListChatModel(responses=[text]), pick["name"], fault

    untested = [s for s in app_services if s["name"] not in past_targets]
    pool = untested or app_services
    pick = sorted(pool, key=lambda s: -s["criticality_score"])[0]
    fault = DAY1_SAFE_FAULTS[0] if pick["single_point_of_failure"] else DAY1_SAFE_FAULTS[1]
    text = (
        f"TARGET: {pick['name']}\n"
        f"FAULT: {fault}\n"
        f"REASON: {pick['name']} has criticality_score={pick['criticality_score']} "
        f"(highest {'untested' if untested else 'known'} service), "
        f"single_point_of_failure={pick['single_point_of_failure']}, "
        f"traffic_share={pick['traffic_share']}. "
        f"{FAULT_DESCRIPTIONS[fault]}. Restricted to the Day-1 safe pool "
        f"({DAY1_SAFE_FAULTS}) -- network/disk/cpu/traffic-spike faults are implemented "
        f"but held out of today's run per the team's risk decision."
    )
    return FakeListChatModel(responses=[text]), pick["name"], fault


def plan_experiment(state: PipelineState, config: RunnableConfig) -> PipelineState:
    log("planner: selecting target and fault type from the dependency map"
        + (f" (manual override: {MANUAL_OVERRIDE['target']}/{MANUAL_OVERRIDE['fault']})"
           if MANUAL_OVERRIDE["target"] else ""))
    past_targets = []
    if RUNS_FILE.exists():
        past_targets = [json.loads(l)["target"] for l in RUNS_FILE.read_text().splitlines() if l.strip()]

    llm, target, fault = build_planner_llm(state["services"], past_targets)
    summary = "\n".join(f"- {s['name']}: score={s['criticality_score']}, spof={s['single_point_of_failure']}"
                         for s in state["services"])
    messages = [
        SystemMessage(content="You are the chaos-experiment planner for Day 1. Pick exactly "
                               "one service and one Day-1-safe fault (crash or failed_dependency)."),
        HumanMessage(content=f"Services:\n{summary}\n\nAlready tested: {past_targets}"),
    ]
    response = llm.invoke(messages, config={"callbacks": config.get("callbacks")})

    lines = dict(l.split(": ", 1) for l in response.content.splitlines() if ": " in l)
    log(f"planner decided: target={target} fault={fault}")
    log(f"planner reasoning: {lines.get('REASON', '')}")
    return {**state, "target": target, "fault": fault, "reason": lines.get("REASON", "")}


# ==================================================================== node: inject + watchdog
def inject_and_monitor(state: PipelineState, config: RunnableConfig) -> PipelineState:
    target, fault = state["target"], state["fault"]
    log(f"injector: applying '{fault}' to '{target}' ({FAULT_DESCRIPTIONS[fault]})")
    before = monitor.snapshot()

    injection = injector.inject(fault, target)
    log(f"injector: fault active -- {injection.meta}")

    duration_sec = 30
    blind_polls = down_polls = 0
    aborted, abort_reason = False, None
    peak_impact_snap = before  # falls back to pre-injection if somehow no poll ever runs
    t0 = time.time()

    while time.time() - t0 < duration_sec:
        time.sleep(POLL_INTERVAL_SEC)
        snap = monitor.snapshot()
        healthy, reasons = monitor.is_healthy(snap)
        t = round(time.time() - t0, 1)
        log(f"watchdog t={t}s: {'OK' if healthy else 'FAIL(' + '; '.join(reasons) + ')'}")

        # Keep the worst snapshot seen, not just the last one: "after" needs to show the
        # actual injected-state impact. A post-recovery snapshot (everything healthy again)
        # would show 0% error both before and after, erasing the exact thing Day-1 wants
        # visible -- confirmed live on the first real run (product-catalog/crash came back
        # 0.0% -> 0.0% because "after" was captured post-recovery, after the fix below it
        # correctly shows the failing-probes moment instead).
        if snap["probe_error_rate"] >= peak_impact_snap.get("probe_error_rate", 0) or not healthy:
            peak_impact_snap = snap

        blind_polls = blind_polls + 1 if not snap["services"] else 0
        down_polls = down_polls + 1 if snap["failed_probes"] else 0
        reason = None if healthy else "; ".join(reasons)
        if down_polls >= MAX_FRONTEND_DOWN_POLLS and reason is None:
            reason = f"probes failing for {down_polls} consecutive polls"
        if blind_polls >= MAX_BLIND_POLLS:
            reason = f"no service telemetry for {blind_polls} consecutive polls"
        if reason:
            aborted, abort_reason = True, reason
            log(f"KILL SWITCH: {reason} -- restoring {target} now", "warn")
            break

    injection.undo()
    log(f"injector: {target} restored ({injection.fault} undone)")

    # recovery timing: consecutive healthy snapshots post-restore
    rstart, streak, recovery_sec = time.time(), 0, None
    while time.time() - rstart < RECOVERY_TIMEOUT_SEC:
        snap = monitor.snapshot()
        healthy, _ = monitor.is_healthy(snap)
        streak = streak + 1 if healthy else 0
        if streak >= RECOVERY_HEALTHY_STREAK:
            recovery_sec = round(time.time() - rstart, 2)
            break
        time.sleep(1)
    log(f"recovery: {(str(recovery_sec) + 's') if recovery_sec is not None else 'NOT recovered within ' + str(RECOVERY_TIMEOUT_SEC) + 's'}")

    recovered_snapshot = monitor.snapshot()
    # "after" = the worst state actually observed during injection (what Day-1 wants shown),
    # not the post-recovery state (which would just echo the baseline).
    return {**state, "before": before, "after": peak_impact_snap,
            "recovered_snapshot": recovered_snapshot, "aborted": aborted,
            "abort_reason": abort_reason, "recovery_sec": recovery_sec}


# ==================================================================== node: thin analyst
def explain_impact(state: PipelineState, config: RunnableConfig) -> PipelineState:
    log("analyst: summarizing observed impact")
    before, after, target = state["before"], state["after"], state["target"]
    b_err, a_err = before["probe_error_rate"], after["probe_error_rate"]
    b_ms, a_ms = before["probe_max_ms"], after["probe_max_ms"]

    text = (
        f"Injecting {state['fault']} into {target} "
        f"{'was caught and rolled back by the safety watchdog' if state['aborted'] else 'ran for its full planned duration'}"
        f"{' (' + state['abort_reason'] + ')' if state['abort_reason'] else ''}. "
        f"Probe error rate moved {b_err:.1%} -> {a_err:.1%}, slowest probe latency moved "
        f"{b_ms:.0f}ms -> {a_ms:.0f}ms. "
        f"{'System recovered in ' + str(state['recovery_sec']) + 's after restore.' if state['recovery_sec'] is not None else 'System had not fully recovered within the timeout window.'}"
    )
    llm = FakeListChatModel(responses=[text])
    response = llm.invoke(
        [SystemMessage(content="Summarize this chaos experiment's impact in plain English, one paragraph."),
         HumanMessage(content=json.dumps({"target": target, "fault": state["fault"],
                                           "before": before, "after": after}, default=str))],
        config={"callbacks": config.get("callbacks")},
    )
    log(f"analyst: {response.content}")

    row = {
        "type": "injection", "ts": time.time(), "target": target, "fault": state["fault"],
        "reason": state["reason"], "aborted": state["aborted"], "abort_reason": state["abort_reason"],
        "recovery_sec": state["recovery_sec"],
        "before": {"probe_error_rate": before["probe_error_rate"], "probe_max_ms": before["probe_max_ms"],
                   "service": before["services"].get(target)},
        "after": {"probe_error_rate": after["probe_error_rate"], "probe_max_ms": after["probe_max_ms"],
                  "service": after["services"].get(target)},
        "explanation": response.content,
    }
    append_comparison_row(row)
    with RUNS_FILE.open("a") as f:
        f.write(json.dumps({"target": target, "fault": state["fault"], "ts": time.time()}) + "\n")
    return {**state, "explanation": response.content}


# ==================================================================== graph (Day 1: single pass)
graph = StateGraph(PipelineState)
graph.add_node("capture_baseline", capture_baseline)
graph.add_node("plan_experiment", plan_experiment)
graph.add_node("inject_and_monitor", inject_and_monitor)
graph.add_node("explain_impact", explain_impact)
graph.set_entry_point("capture_baseline")
graph.add_edge("capture_baseline", "plan_experiment")
graph.add_edge("plan_experiment", "inject_and_monitor")
graph.add_edge("inject_and_monitor", "explain_impact")
graph.add_edge("explain_impact", END)  # Day 2: loop back to plan_experiment instead
app = graph.compile()


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="override the planner: run against this service specifically")
    ap.add_argument("--fault", choices=DAY1_SAFE_FAULTS, help="override the fault type (default: "
                     "chosen from the Day-1 safe pool based on whether --target is a SPOF)")
    args = ap.parse_args()
    MANUAL_OVERRIDE["target"] = args.target
    MANUAL_OVERRIDE["fault"] = args.fault

    log("=== Day-1 pipeline starting ===")
    try:
        result = app.invoke(
            {"services": [], "target": "", "fault": "", "reason": "", "aborted": False,
             "abort_reason": None, "recovery_sec": None, "before": {}, "after": {},
             "recovered_snapshot": {}, "explanation": ""},
            config={"callbacks": [tracer], "run_name": "day1_experiment_run"},
        )
        log(f"=== Day-1 pipeline complete: {result['target']} / {result['fault']} / "
            f"{'ABORTED' if result['aborted'] else 'completed'} ===")
    except Exception as e:
        log(f"=== Day-1 pipeline FAILED: {e} ===", "error")
        raise

    # Only prompt when a human is actually watching a real terminal. Deliberately OUTSIDE
    # the try/except above: confirmed live that stdin=subprocess.DEVNULL from the
    # dashboard's "run experiment" button doesn't reliably make isatty() return False on
    # this machine, input() hit immediate EOF, and because this used to live inside that
    # same try/except, a completely successful experiment got logged as "FAILED: EOF when
    # reading a line" -- misleading and wrong. Now a crash here can never relabel a result
    # that already finished and was already logged above.
    if sys.stdin.isatty():
        try:
            answer = input("\nPress Enter to reset for the next clean run, or type anything "
                            "else to keep this result: ").strip()
            if answer == "":
                import reset as reset_module
                reset_module.reset()
                print("Cleared -- tab 4 will show a clean slate on the next run.")
        except (EOFError, OSError):
            pass  # no real interactive terminal after all -- just skip the prompt
