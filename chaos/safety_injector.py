"""
Controlled failure injector with a fail-closed safety kill switch.

Usage:
    python safety_injector.py <container> [pause|stop] [duration_sec]

Each run is appended to runs.jsonl for the Day-2 AI analysis.
"""
import json
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import docker
import requests

from monitor import Monitor

# ---------------- Configuration ----------------
PROMETHEUS_URL = "http://localhost:9090"
HEALTH_PROBE_URL = "http://localhost:8080/"   # demo frontend via frontend-proxy

ERROR_THRESHOLD = 0.05        # abort if AGGREGATE error ratio > 5% (coarse backstop; the real
P95_LATENCY_THRESHOLD_MS = 2000  # primary signal is monitor.is_healthy(), see breach_reason())
POLL_INTERVAL_SEC = 2
# The demo flushes span metrics to Prometheus about every 60s by default, so a short
# window returns nothing. Keep "2m" unless you set metrics_flush_interval: 5s
# (see README note), then "30s" works and detection is much faster.
RATE_WINDOW = "2m"
MAX_BLIND_POLLS = 5           # fail closed after this many consecutive failed queries
MAX_FRONTEND_DOWN_POLLS = 2   # real-time abort: frontend unreachable this many polls in a row
RECOVERY_TIMEOUT_SEC = 90
RECOVERY_HEALTHY_STREAK = 3   # consecutive healthy probes to count as recovered

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "chaos_state.json"
RUNS_FILE = DATA_DIR / "runs.jsonl"
DEP_MAP_FILE = DATA_DIR / "dependency_map.json"

# Fallback only, used if dependency_map.json isn't available yet. When the map exists, the
# real protected set is read from it (chaos_target_allowed per service) instead of this
# hardcoded list, so mapper.py's classification is the single source of truth and the two
# scripts can't silently drift apart (this fallback list, unlike mapper.py's INFRA set, used
# to miss image-provider/telemetry-docs/opamp-server).
PROTECTED_FALLBACK = {
    "prometheus", "otel-collector", "otelcol", "jaeger", "grafana",
    "opensearch", "load-generator", "flagd", "flagd-ui", "frontend-proxy",
}

# Verify these metric names in the Prometheus UI for your demo version
# (older versions: calls_total / duration_milliseconds_bucket).
# Confirmed live against this deployment (2026-10-10): names are correct as written.
ERROR_RATE_QUERY = (
    f'sum(rate(traces_span_metrics_calls_total{{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR"}}[{RATE_WINDOW}]))'
    f' / sum(rate(traces_span_metrics_calls_total{{span_kind="SPAN_KIND_SERVER"}}[{RATE_WINDOW}]))'
)
P95_LATENCY_QUERY = (
    f'histogram_quantile(0.95, sum by (le) (rate('
    f'traces_span_metrics_duration_milliseconds_bucket{{span_kind="SPAN_KIND_SERVER"}}[{RATE_WINDOW}])))'
)

client = docker.from_env()
# Reuses monitor.py's richer, baseline-aware, per-service/per-probe detection instead of this
# script running its own parallel copy. The aggregate Prometheus queries above are kept too,
# as a second, independent coarse check -- cheap to keep, catches a total system-wide collapse
# even if the probe/baseline layer has a problem of its own.
monitor = Monitor(prom=PROMETHEUS_URL, window=RATE_WINDOW)


def protected_services() -> set:
    try:
        dep = json.loads(DEP_MAP_FILE.read_text())
        return {n["name"] for n in dep["services"] if not n["chaos_target_allowed"]}
    except Exception:
        print(f"[WARN] {DEP_MAP_FILE} not found/unreadable - using the fallback protected list. "
              f"Run mapper.py first to get the full, maintained list.")
        return PROTECTED_FALLBACK


# ---------------- Telemetry ----------------
def query_prometheus(promql: str) -> Optional[float]:
    """Returns the value, or None if the query failed or returned no usable data."""
    try:
        r = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": promql}, timeout=2)
        r.raise_for_status()
        result = r.json()["data"]["result"]
        if not result:
            return None
        val = float(result[0]["value"][1])
        return None if val != val else val   # NaN -> None
    except Exception:
        return None


def probe_health() -> bool:
    try:
        return requests.get(HEALTH_PROBE_URL, timeout=3).status_code < 500
    except Exception:
        return False


def read_metrics() -> dict:
    """Aggregate (coarse, whole-system) view plus monitor.py's richer per-probe/per-service
    snapshot, so samples logged to runs.jsonl have both signals for the Day-2 Analyst."""
    snap = monitor.snapshot()
    healthy, reasons = monitor.is_healthy(snap)
    return {
        "error_rate": query_prometheus(ERROR_RATE_QUERY),
        "p95_ms": query_prometheus(P95_LATENCY_QUERY),
        "healthy": probe_health(),
        "monitor_healthy": healthy,
        "monitor_reasons": reasons,
        "failed_probes": snap["failed_probes"],
        "suspect_services": snap["suspect_services"],
    }


def breach_reason(m: dict) -> Optional[str]:
    # Primary signal: monitor.py's baseline-aware, per-probe/per-service check. This is what
    # actually notices a failure in cart/product-catalog/recommendation/ad specifically --
    # the aggregate queries below only see a whole-system-wide swing and can miss a single
    # service degrading (confirmed live: the bare "/" probe stayed 200 the entire time
    # product-catalog was down, since the frontend's SSR shell doesn't depend on it).
    if not m["monitor_healthy"]:
        return "; ".join(m["monitor_reasons"]) or "monitor flagged unhealthy"
    # Secondary, independent backstop: aggregate whole-system thresholds.
    if m["error_rate"] is not None and m["error_rate"] > ERROR_THRESHOLD:
        return f"aggregate error rate {m['error_rate']:.1%} > {ERROR_THRESHOLD:.0%}"
    if m["p95_ms"] is not None and m["p95_ms"] > P95_LATENCY_THRESHOLD_MS:
        return f"aggregate p95 {m['p95_ms']:.0f}ms > {P95_LATENCY_THRESHOLD_MS}ms"
    return None


def fmt(m: dict) -> str:
    er = "n/a" if m["error_rate"] is None else f"{m['error_rate']:.2%}"
    p95 = "n/a" if m["p95_ms"] is None else f"{m['p95_ms']:.0f}ms"
    mon = "OK" if m["monitor_healthy"] else f"FAIL({','.join(m['monitor_reasons'][:2]) or '?'})"
    return f"errors={er} p95={p95} frontend={'UP' if m['healthy'] else 'DOWN'} monitor={mon}"


# ---------------- Fault state (dead-man's switch) ----------------
def save_state(container: str, mode: str):
    STATE_FILE.write_text(json.dumps({"container": container, "mode": mode, "since": time.time()}))


def clear_state():
    STATE_FILE.unlink(missing_ok=True)


def restore(container_name: str, mode: str):
    c = client.containers.get(container_name)
    c.reload()
    if mode == "pause" and c.status == "paused":
        c.unpause()
    elif mode == "stop" and c.status != "running":
        c.start()
    clear_state()


def recover_orphans():
    """If a previous run died mid-experiment, undo its fault before doing anything else."""
    if not STATE_FILE.exists():
        return
    state = json.loads(STATE_FILE.read_text())
    print(f"[SAFETY] Found unfinished experiment on '{state['container']}' - restoring it.")
    try:
        restore(state["container"], state["mode"])
    except docker.errors.NotFound:
        clear_state()


# ---------------- Experiment ----------------
def measure_recovery() -> Optional[float]:
    """Seconds until the frontend answers healthily N times in a row; None if it never does."""
    start, streak = time.time(), 0
    while time.time() - start < RECOVERY_TIMEOUT_SEC:
        streak = streak + 1 if probe_health() else 0
        if streak >= RECOVERY_HEALTHY_STREAK:
            return round(time.time() - start, 2)
        time.sleep(1)
    return None


def stable_preflight(attempts: int = 3, gap_sec: float = 2.0) -> dict:
    """A single snapshot can be one noisy sample (confirmed live: a probe briefly read
    385ms then settled back under 20ms two seconds later). Everywhere else in this script
    requires a streak before acting (MAX_FRONTEND_DOWN_POLLS, RECOVERY_HEALTHY_STREAK) --
    the pre-flight gate should too, so one transient blip doesn't refuse a healthy system."""
    last = None
    for i in range(attempts):
        last = read_metrics()
        print(f"[PRE-FLIGHT {i + 1}/{attempts}] {fmt(last)}")
        if last["error_rate"] is not None and last["healthy"] and not breach_reason(last):
            return last
        if i < attempts - 1:
            time.sleep(gap_sec)
    return last  # still bad after every retry -- let the caller refuse on this one


def run_experiment(container_name: str, mode: str = "pause", duration_sec: int = 30) -> dict:
    recover_orphans()

    protected = protected_services()
    if any(p in container_name for p in protected):
        raise SystemExit(f"[REFUSED] '{container_name}' is protected (observability/load stack, "
                          f"or chaos_target_allowed=false in dependency_map.json).")
    try:
        target = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise SystemExit(f"[ERROR] '{container_name}' not found. Check names with 'docker ps'.")

    # Pre-flight: only inject into a system that is currently at steady state
    baseline = stable_preflight()
    if baseline["error_rate"] is None or not baseline["healthy"]:
        raise SystemExit("[REFUSED] Can't see the system (no metrics or frontend down). Is Locust running?")
    if breach_reason(baseline):
        raise SystemExit(f"[REFUSED] System still unhealthy after retries: {breach_reason(baseline)}")

    result = {
        "target": container_name, "mode": mode, "planned_duration_sec": duration_sec,
        "thresholds": {"error_rate": ERROR_THRESHOLD, "p95_ms": P95_LATENCY_THRESHOLD_MS},
        "baseline": baseline, "samples": [], "aborted": False, "abort_reason": None,
        "started_at": time.time(),
    }

    print(f"\n[*] Injecting '{mode}' into {container_name} for up to {duration_sec}s")
    save_state(container_name, mode)          # written BEFORE the fault, so a crash is recoverable
    target.pause() if mode == "pause" else target.stop(timeout=0)
    inject_time = time.time()
    blind_polls = 0
    down_polls = 0

    try:
        while time.time() - inject_time < duration_sec:
            time.sleep(POLL_INTERVAL_SEC)
            m = read_metrics()
            t = round(time.time() - inject_time, 1)
            result["samples"].append({"t": t, **m})
            print(f"[{t:>5}s] {fmt(m)}")

            # Fail closed: losing visibility is itself a reason to stop
            blind_polls = blind_polls + 1 if m["error_rate"] is None else 0
            down_polls = down_polls + 1 if not m["healthy"] else 0
            reason = breach_reason(m)
            if down_polls >= MAX_FRONTEND_DOWN_POLLS:
                reason = f"frontend unreachable for {down_polls} consecutive polls"
            if blind_polls >= MAX_BLIND_POLLS:
                reason = f"no telemetry for {blind_polls} consecutive polls"
            if reason:
                result.update(aborted=True, abort_reason=reason, abort_at_sec=t)
                print(f"\n[KILL SWITCH] {reason} - restoring {container_name}")
                break
    except KeyboardInterrupt:
        result.update(aborted=True, abort_reason="manual operator abort")
        print("\n[MANUAL ABORT]")
    finally:
        restore(container_name, mode)
        result["fault_duration_sec"] = round(time.time() - inject_time, 2)
        print(f"[SAFETY] {container_name} restored.")

    result["recovery_sec"] = measure_recovery()
    result["recovered"] = result["recovery_sec"] is not None
    result["ended_at"] = time.time()

    with RUNS_FILE.open("a") as f:
        f.write(json.dumps(result) + "\n")

    status = f"ABORTED ({result['abort_reason']})" if result["aborted"] else "completed full duration"
    rec = f"{result['recovery_sec']}s" if result["recovered"] else f"NOT recovered within {RECOVERY_TIMEOUT_SEC}s"
    print(f"\n[SUMMARY] {status} | recovery: {rec} | logged to {RUNS_FILE}")
    return result


def _handle_term(signum, frame):
    raise KeyboardInterrupt   # route SIGTERM through the same restore path


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_term)
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python safety_injector.py <container> [pause|stop] [duration_sec]")
    name = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "pause"
    duration = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    run_experiment(name, mode, duration)
