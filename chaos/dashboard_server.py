"""
dashboard_server.py - unified observability page: app-layer (dependency map, baseline,
live health, chaos experiment history) + agent-layer (local LangChain/LangGraph traces,
the self-hosted LangSmith replacement).

Usage:
    python dashboard_server.py
    open http://localhost:8800/
"""
import json
import time
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from monitor import Monitor

DATA = Path("data")
PROMETHEUS_URL = "http://localhost:9090"
RATE_WINDOW = "2m"  # must exceed the collector's ~60s metrics flush interval, see monitor.py
app = FastAPI()
monitor = Monitor()


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """The browser silently served a stale index.html after an edit during this build --
    confirmed live: the DOM was missing a change that was definitely on disk, and a
    cache-busting query string fixed it instantly. This is a dev dashboard being actively
    edited while judges might be refreshing it; no response here should ever be cached."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


def _read_json(name: str, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _read_jsonl(name: str):
    p = DATA / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "dashboard" / "index.html")


@app.get("/api/dependency-map")
def dependency_map():
    return _read_json("dependency_map.json", {"services": [], "edges": []})


@app.get("/api/dependency-mermaid")
def dependency_mermaid():
    p = DATA / "dependency_map.mmd"
    return PlainTextResponse(p.read_text() if p.exists() else "graph LR\n  nodata[no dependency_map.mmd yet]")


@app.get("/api/baseline")
def baseline():
    return _read_json("baseline.json", None)


@app.get("/api/live")
def live():
    snap = monitor.snapshot()
    healthy, reasons = monitor.is_healthy(snap)
    return {"healthy": healthy, "reasons": reasons, "snapshot": snap}


@app.get("/api/runs")
def runs():
    return list(reversed(_read_jsonl("runs.jsonl")))  # newest first


@app.get("/api/agent-traces")
def agent_traces(limit: int = 200):
    events = _read_jsonl("agent_traces.jsonl")
    return list(reversed(events))[:limit]  # newest first


@app.get("/api/injection-comparison")
def injection_comparison():
    """Baseline (written once, at pipeline start) followed by one row per completed
    injection, in the order they happened -- this is tab 4's data source."""
    return _read_jsonl("injection_comparison.jsonl")


@app.get("/api/available-targets")
def available_targets():
    """Powers the manual target/fault dropdown. chaos_target_allowed comes from
    mapper.py's own classification (the single source of truth, see safety_injector.py's
    protected_services() for why) -- this endpoint doesn't duplicate that logic."""
    dep = _read_json("dependency_map.json", {"services": []})
    runs_path = DATA / "runs.jsonl"
    tested = {json.loads(l)["target"] for l in runs_path.read_text().splitlines() if l.strip()} \
        if runs_path.exists() else set()
    targets = [
        {"name": s["name"], "criticality_score": s["criticality_score"],
         "single_point_of_failure": s["single_point_of_failure"], "already_tested": s["name"] in tested}
        for s in dep["services"] if s["role"] == "app" and s["chaos_target_allowed"]
    ]
    targets.sort(key=lambda t: -t["criticality_score"])
    return {"targets": targets, "faults": ["crash", "failed_dependency"]}


_pipeline_proc = {"proc": None}


@app.post("/api/run-experiment")
def run_experiment(body: dict):
    """Launches day1_pipeline.py as a subprocess with the operator's manual target/fault
    choice. Non-blocking: returns immediately, and the already-running tab 3/4/5 polling
    picks up the result as it happens -- no new frontend polling needed for this."""
    import subprocess
    proc = _pipeline_proc["proc"]
    if proc is not None and proc.poll() is None:
        return {"status": "error", "message": "an experiment is already running -- wait for it to finish"}
    target, fault = body.get("target"), body.get("fault")
    if not target:
        return {"status": "error", "message": "target is required"}
    cmd = ["python", "day1_pipeline.py", "--target", target]
    if fault:
        cmd += ["--fault", fault]
    _pipeline_proc["proc"] = subprocess.Popen(cmd, cwd=str(Path(__file__).parent),
                                               stdin=subprocess.DEVNULL)
    return {"status": "started", "target": target, "fault": fault}


@app.get("/api/pipeline-status")
def pipeline_status():
    proc = _pipeline_proc["proc"]
    running = proc is not None and proc.poll() is None
    return {"running": running}


@app.post("/api/reset")
def reset_data():
    import reset as reset_module
    proc = _pipeline_proc["proc"]
    if proc is not None and proc.poll() is None:
        return {"status": "error", "message": "an experiment is still running -- wait for it to finish first"}
    reset_module.reset()
    return {"status": "cleared"}


@app.get("/api/live-log")
def live_log(limit: int = 1000):
    """Every pipeline node's log line, oldest first, for tab 5's scrolling terminal view."""
    return _read_jsonl("live_log.jsonl")[-limit:]


def _prom_query(query: str):
    r = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5)
    r.raise_for_status()
    return r.json()["data"]["result"]


@app.get("/api/prometheus-insights")
def prometheus_insights():
    """Structured EDA over the live Prometheus data -- ranked tables, not time-series charts
    (deliberately: the point is 'what's the highest-latency service right now', not a graph
    to eyeball). Falls back to empty lists if Prometheus isn't reachable."""
    srv = 'span_kind="SPAN_KIND_SERVER"'
    try:
        rps_r = _prom_query(f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{srv}}}[{RATE_WINDOW}]))")
        err_r = _prom_query(f'sum by (service_name) (rate(traces_span_metrics_calls_total{{{srv},status_code="STATUS_CODE_ERROR"}}[{RATE_WINDOW}]))')
        p95_r = _prom_query(f"histogram_quantile(0.95, sum by (le, service_name) "
                             f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{srv}}}[{RATE_WINDOW}])))")
        mem_r = _prom_query("container_memory_usage_total_bytes")
    except Exception as e:
        return {"error": str(e), "summary": {}, "by_latency": [], "by_error_rate": [],
                "by_traffic": [], "by_memory": [], "baseline_deviation": []}

    rps_map = {m["metric"].get("service_name"): float(m["value"][1]) for m in rps_r}
    err_map = {m["metric"].get("service_name"): float(m["value"][1]) for m in err_r}
    p95_map = {m["metric"].get("service_name"): float(m["value"][1]) for m in p95_r}
    mem_map = {m["metric"].get("container_name"): float(m["value"][1]) for m in mem_r}

    services = sorted(set(rps_map) | set(p95_map))
    service_rows = []
    for s in services:
        calls = rps_map.get(s, 0.0)
        service_rows.append({
            "service": s, "rps": round(calls, 3),
            "error_rate": round(err_map.get(s, 0.0) / calls, 4) if calls else 0.0,
            "p95_ms": round(p95_map[s], 1) if s in p95_map else None,
        })

    # Prometheus's container metrics aren't scoped to this compose project -- they include
    # every container cAdvisor sees on the host. Confirmed live: an unrelated container from
    # a different project ("astra-scan-ai-db-1") leaked into the memory ranking. Filter to
    # the names mapper.py actually knows about (same fix as monitor.py's container scoping,
    # applied here since Prometheus has no compose-project label to filter on directly).
    known = {n["name"] for n in _read_json("dependency_map.json", {"services": []})["services"]}
    containers = sorted(set(mem_map) & known) if known else sorted(set(mem_map))
    container_rows = [{
        "container": c,
        "memory_mb": round(mem_map.get(c, 0.0) / (1024 * 1024), 1),
    } for c in containers]

    valid_p95 = [r["p95_ms"] for r in service_rows if r["p95_ms"] is not None]
    summary = {
        "services": len(service_rows),
        "total_rps": round(sum(r["rps"] for r in service_rows), 2),
        "avg_error_rate": round(sum(r["error_rate"] for r in service_rows) / len(service_rows), 4) if service_rows else 0,
        "avg_p95_ms": round(sum(valid_p95) / len(valid_p95), 1) if valid_p95 else None,
        "max_p95_ms": round(max(valid_p95), 1) if valid_p95 else None,
    }

    # EDA touch: how far is each service's live p95 from its own recorded baseline?
    baseline = _read_json("baseline.json", None)
    deviations = []
    if baseline:
        base_services = baseline.get("services", {})
        for r in service_rows:
            b = base_services.get(r["service"])
            if b and b.get("p95_ms") and r["p95_ms"] is not None:
                dev_pct = (r["p95_ms"] - b["p95_ms"]) / b["p95_ms"] * 100
                deviations.append({
                    "service": r["service"], "baseline_p95_ms": b["p95_ms"],
                    "current_p95_ms": r["p95_ms"], "deviation_pct": round(dev_pct, 1),
                })
        deviations.sort(key=lambda d: -abs(d["deviation_pct"]))

    return {
        "generated_at": time.time(),
        "summary": summary,
        "by_latency": sorted(service_rows, key=lambda r: -(r["p95_ms"] or 0)),
        "by_error_rate": sorted(service_rows, key=lambda r: -r["error_rate"]),
        "by_traffic": sorted(service_rows, key=lambda r: -r["rps"]),
        "by_memory": sorted(container_rows, key=lambda r: -r["memory_mb"]),
        "baseline_deviation": deviations,
    }


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "dashboard"), name="static")


if __name__ == "__main__":
    import uvicorn
    print("Dashboard: http://localhost:8800/")
    uvicorn.run(app, host="0.0.0.0", port=8800)
