"""
monitor.py - reads the app's health every second and builds the baseline ("what normal looks like").

It REUSES what the OpenTelemetry Demo already has and only adds the automation:
  1. HTTP probes   -> calls the store's own API routes through the proxy (like a shopper would)
  2. Docker        -> container state, health check status and restart count per service
  3. Prometheus    -> per-service request rate, error rate and p95 latency
                      (traces_span_metrics_* : the same metrics Grafana's dashboards use)

Usage:
    pip install requests

    # watch live (Ctrl+C to stop) - writes data/metrics_live.jsonl
    python monitor.py live

    # record the baseline: 5 minutes with NOTHING broken and the load generator running
    python monitor.py baseline --duration 300

    # check "is the app back to normal?" right now
    python monitor.py check

    # app on a teammate's laptop:
    python monitor.py live --base http://192.168.1.20:8080 --prom http://192.168.1.20:9090

Used by other modules:
    from monitor import Monitor
    m = Monitor(); snap = m.snapshot(); ok, reasons = m.is_healthy(snap)

Outputs (./data/):
    metrics_live.jsonl   one JSON line per second (read by the safety guard)
    baseline.json        normal values + suggested safety thresholds
"""
import argparse
import ipaddress
import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

DATA = "data"
SESSION = "chaos-monitor-probe"

# Each probe exercises one path through the app. "touches" = backend services on that path,
# so a failing probe tells the AI which part of the system broke.
PROBES = {
    "home":            {"path": "/",                                                    "touches": ["frontend"]},
    "products":        {"path": "/api/products?currencyCode=USD",                      "touches": ["frontend", "product-catalog", "currency"]},
    "cart":            {"path": f"/api/cart?sessionId={SESSION}&currencyCode=USD",     "touches": ["frontend", "cart", "valkey-cart"]},
    "recommendations": {"path": f"/api/recommendations?productIds=&sessionId={SESSION}&currencyCode=USD",
                        "touches": ["frontend", "recommendation", "product-catalog"]},
    "ads":             {"path": "/api/data?contextKeys=",                              "touches": ["frontend", "ad"]},
}


# ------------------------------------------------------------------ safety: local targets only
def assert_local(url):
    """Refuse to monitor anything outside the sandbox (localhost or a private LAN address)."""
    host = urlparse(url).hostname or ""
    if host in ("localhost",):
        return
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private:
            return
    except ValueError:
        pass
    sys.exit(f"REFUSED: {url} is not a local/sandbox address. Only localhost or private IPs allowed.")


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return round(s[k], 1)


class Monitor:
    def __init__(self, base="http://localhost:8080", prom="http://localhost:9090",
                 map_path=os.path.join(DATA, "dependency_map.json"), timeout=3.0, window="2m",
                 project="opentelemetry-demo"):
        # window default is 2m, not a shorter value: the demo's span-metrics connector only
        # flushes to Prometheus every ~60s, so a shorter rate() window frequently sees 0-1
        # data points and silently returns no data (confirmed live: 30s -> empty, 2m -> real
        # per-service values for every service).
        assert_local(base)
        if prom:
            assert_local(prom)
        self.base, self.prom, self.timeout, self.window = base.rstrip("/"), prom, timeout, window
        self.project = project
        self.http = requests.Session()
        self.services = self._load_services(map_path)
        self.baseline = self._load_json(os.path.join(DATA, "baseline.json"))
        self._prom_ok = None

    # -------------------------------------------------------------- setup helpers
    @staticmethod
    def _load_json(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def _load_services(self, map_path):
        dep = self._load_json(map_path)
        if not dep:
            print("  (no dependency_map.json found - watching every compose container)")
            return None
        return sorted(n["name"] for n in dep["services"] if n["role"] == "app")

    # -------------------------------------------------------------- source 1: HTTP probes
    def _probe(self, name):
        url = self.base + PROBES[name]["path"]
        t0 = time.perf_counter()
        try:
            r = self.http.get(url, timeout=self.timeout)
            ms = (time.perf_counter() - t0) * 1000
            return name, {"ok": 200 <= r.status_code < 400, "status": r.status_code, "ms": round(ms, 1)}
        except requests.RequestException as e:
            ms = (time.perf_counter() - t0) * 1000
            return name, {"ok": False, "status": type(e).__name__, "ms": round(ms, 1)}

    def probes(self):
        with ThreadPoolExecutor(max_workers=len(PROBES)) as ex:
            return dict(ex.map(self._probe, PROBES))

    # -------------------------------------------------------------- source 2: Docker
    def containers(self):
        fmt = ('{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Status}}|'
               '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}')
        # Scoped to this compose project, not just "has a compose service label": without this,
        # a stopped container from an unrelated project on the same machine that happens to share
        # a common service name (e.g. "frontend") collides in the result dict and silently reports
        # a running service as down. Confirmed live on this machine (carbonsense-frontend-1,
        # docubrain_frontend both label com.docker.compose.service=frontend).
        ps_filters = ["--filter", "label=com.docker.compose.service"]
        if self.project:
            ps_filters += ["--filter", f"label=com.docker.compose.project={self.project}"]
        try:
            ids = subprocess.run(["docker", "ps", "-aq", *ps_filters],
                                 capture_output=True, text=True, timeout=5, check=True).stdout.split()
            if not ids:
                return {}
            out = subprocess.run(["docker", "inspect", "--format", fmt, *ids],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
        except Exception:
            return {}
        result = {}
        for line in out.splitlines():
            svc, state, health, restarts = (line.split("|") + ["", "", "", "0"])[:4]
            if svc and (self.services is None or svc in self.services):
                result[svc] = {"state": state, "health": health, "restarts": int(restarts or 0)}
        return result

    # -------------------------------------------------------------- source 3: Prometheus
    def _q(self, query):
        r = self.http.get(f"{self.prom}/api/v1/query", params={"query": query}, timeout=self.timeout)
        r.raise_for_status()
        return {s["metric"].get("service_name", "?"): float(s["value"][1])
                for s in r.json()["data"]["result"] if s["value"][1] not in ("NaN", "+Inf")}

    def service_metrics(self):
        if not self.prom or self._prom_ok is False:
            return {}
        w, srv = self.window, 'span_kind="SPAN_KIND_SERVER"'
        try:
            rps = self._q(f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{srv}}}[{w}]))")
            err = self._q(f'sum by (service_name) (rate(traces_span_metrics_calls_total{{{srv},status_code="STATUS_CODE_ERROR"}}[{w}]))')
            p95 = self._q(f"histogram_quantile(0.95, sum by (le, service_name) "
                          f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{srv}}}[{w}])))")
            self._prom_ok = True
        except Exception as e:
            if self._prom_ok is None:
                print(f"  (Prometheus not reachable at {self.prom}: {e} - continuing without it)")
            self._prom_ok = False
            return {}
        out = {}
        for svc in set(rps) | set(p95):
            if self.services and svc not in self.services:
                continue
            calls = rps.get(svc, 0.0)
            out[svc] = {"rps": round(calls, 2),
                        "error_rate": round(err.get(svc, 0.0) / calls, 4) if calls else 0.0,
                        "p95_ms": round(p95[svc], 1) if svc in p95 else None}
        return out

    # -------------------------------------------------------------- one reading
    def snapshot(self):
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_p, f_c, f_s = ex.submit(self.probes), ex.submit(self.containers), ex.submit(self.service_metrics)
            probes, conts, svcs = f_p.result(), f_c.result(), f_s.result()
        lat = [p["ms"] for p in probes.values()]
        failed = [n for n, p in probes.items() if not p["ok"]]
        return {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "t": round(time.time(), 2),
            "probe_error_rate": round(len(failed) / len(probes), 3),
            "probe_max_ms": max(lat),
            "failed_probes": failed,
            "suspect_services": sorted({s for n in failed for s in PROBES[n]["touches"]}),
            "containers_down": sorted(s for s, c in conts.items() if c["state"] != "running"),
            "probes": probes,
            "containers": conts,
            "services": svcs,
        }

    # -------------------------------------------------------------- "is it normal?"
    def is_healthy(self, snap, baseline=None):
        """Compare a snapshot to the baseline. Returns (healthy, [reasons])."""
        b = baseline or self.baseline
        reasons = []
        if snap["failed_probes"]:
            reasons.append(f"failing probes: {', '.join(snap['failed_probes'])}")
        if snap["containers_down"]:
            reasons.append(f"containers not running: {', '.join(snap['containers_down'])}")
        if b:
            for name, p in snap["probes"].items():
                limit = b["probes"].get(name, {}).get("healthy_max_ms")
                if limit and p["ok"] and p["ms"] > limit:
                    reasons.append(f"{name} slow: {p['ms']}ms > {limit}ms")
            for svc, m in snap["services"].items():
                lim = b["services"].get(svc, {}).get("healthy_max_error_rate")
                if lim is not None and m["error_rate"] > lim:
                    reasons.append(f"{svc} error rate {m['error_rate']:.1%} > {lim:.1%}")
        return (not reasons), reasons

    # -------------------------------------------------------------- loop
    def run(self, duration=None, quiet=False, out_path=os.path.join(DATA, "metrics_live.jsonl")):
        os.makedirs(DATA, exist_ok=True)
        samples, start = [], time.time()
        try:
            with open(out_path, "a") as f:
                while duration is None or time.time() - start < duration:
                    tick = time.time()
                    snap = self.snapshot()
                    f.write(json.dumps(snap) + "\n")
                    f.flush()
                    samples.append(snap)
                    if not quiet:
                        self._print(snap, len(samples), duration)
                    time.sleep(max(0.0, 1.0 - (time.time() - tick)))
        except KeyboardInterrupt:
            print("\nstopped.")
        return samples

    @staticmethod
    def _print(s, n, duration):
        ok = "OK  " if not s["failed_probes"] and not s["containers_down"] else "FAIL"
        prog = f"[{n}/{duration}s]" if duration else f"[{n}s]"
        extra = ""
        if s["failed_probes"]:
            extra += f" failing={','.join(s['failed_probes'])}"
        if s["containers_down"]:
            extra += f" down={','.join(s['containers_down'])}"
        print(f"{prog:>12} {ok} errors={s['probe_error_rate']:.0%} slowest={s['probe_max_ms']:.0f}ms "
              f"containers={len(s['containers'])} services={len(s['services'])}{extra}")


# ------------------------------------------------------------------ baseline
def build_baseline(samples, window):
    if len(samples) < 10:
        sys.exit("Too few samples for a baseline - run for at least 60 seconds.")

    probes = {}
    for name in PROBES:
        vals = [s["probes"][name] for s in samples if name in s["probes"]]
        oks = [v["ms"] for v in vals if v["ok"]]
        p95 = pct(oks, 95)
        probes[name] = {
            "success_rate": round(len(oks) / len(vals), 4) if vals else 0,
            "avg_ms": round(statistics.mean(oks), 1) if oks else None,
            "p50_ms": pct(oks, 50),
            "p95_ms": p95,
            # "healthy" = within 2x normal p95 + 200ms of noise
            "healthy_max_ms": round(p95 * 2 + 200) if p95 else None,
        }

    services = {}
    names = {svc for s in samples for svc in s["services"]}
    for svc in sorted(names):
        rows = [s["services"][svc] for s in samples if svc in s["services"]]
        errs = [r["error_rate"] for r in rows]
        p95s = [r["p95_ms"] for r in rows if r["p95_ms"] is not None]
        avg_err = statistics.mean(errs) if errs else 0
        services[svc] = {
            "avg_rps": round(statistics.mean(r["rps"] for r in rows), 2),
            "avg_error_rate": round(avg_err, 4),
            "p95_ms": pct(p95s, 50),
            "healthy_max_error_rate": round(max(avg_err * 2, avg_err + 0.05), 4),
        }

    containers = {}
    for svc in sorted({c for s in samples for c in s["containers"]}):
        states = [s["containers"][svc]["state"] for s in samples if svc in s["containers"]]
        containers[svc] = {"running_ratio": round(states.count("running") / len(states), 3),
                           "restarts_at_end": samples[-1]["containers"].get(svc, {}).get("restarts", 0)}

    err = statistics.mean(s["probe_error_rate"] for s in samples)
    worst_p95 = max((p["p95_ms"] or 0) for p in probes.values())
    unstable = [n for n, p in probes.items() if p["success_rate"] < 0.95]

    return {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_s": round(samples[-1]["t"] - samples[0]["t"]),
        "samples": len(samples),
        "prometheus_window": window,
        "overall": {"probe_error_rate": round(err, 4), "worst_probe_p95_ms": worst_p95},
        "probes": probes,
        "services": services,
        "containers": containers,
        # starting values for the safety guard - tune after the first test run
        "suggested_thresholds": {
            "max_probe_error_rate": round(max(0.5, err + 0.3), 2),
            "max_probe_latency_ms": max(3000, round(worst_p95 * 5)),
            "max_duration_s": 60,
            "recovery_timeout_s": 120,
        },
        "warnings": [f"probe '{n}' already unstable at baseline ({probes[n]['success_rate']:.0%} success)"
                     for n in unstable],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "baseline", "check"])
    ap.add_argument("--duration", type=int, default=300, help="seconds (baseline mode)")
    ap.add_argument("--base", default="http://localhost:8080", help="store URL (frontend proxy)")
    ap.add_argument("--prom", default="http://localhost:9090", help="Prometheus URL ('' to skip)")
    ap.add_argument("--window", default="2m", help="Prometheus rate window (must exceed the "
                    "collector's metrics flush interval, ~60s by default, or rate() sees no data)")
    ap.add_argument("--project", default="opentelemetry-demo",
                    help="docker compose project name, to avoid colliding with other projects' "
                         "containers that share a service name ('' to disable scoping)")
    args = ap.parse_args()

    m = Monitor(args.base, args.prom or None, window=args.window, project=args.project or None)
    print(f"Monitoring {args.base} | prometheus: {args.prom or 'off'} | "
          f"services: {', '.join(m.services) if m.services else 'all'}")

    if args.mode == "live":
        m.run()

    elif args.mode == "check":
        snap = m.snapshot()
        ok, reasons = m.is_healthy(snap)
        print("HEALTHY" if ok else "NOT HEALTHY")
        for r in reasons:
            print("  -", r)
        sys.exit(0 if ok else 1)

    else:
        print(f"Recording baseline for {args.duration}s. Don't break anything; keep the load generator on.")
        samples = m.run(duration=args.duration)
        b = build_baseline(samples, args.window)
        path = os.path.join(DATA, "baseline.json")
        with open(path, "w") as f:
            json.dump(b, f, indent=2)
        print(f"\nBaseline saved to {path}  ({b['samples']} samples over {b['duration_s']}s)")
        print(f"  probe error rate: {b['overall']['probe_error_rate']:.1%}")
        for n, p in b["probes"].items():
            print(f"  {n:<16} success {p['success_rate']:.0%}  p50 {p['p50_ms']}ms  p95 {p['p95_ms']}ms")
        if b["services"]:
            print(f"  per-service metrics from Prometheus: {len(b['services'])} services")
        print(f"  suggested thresholds: {b['suggested_thresholds']}")
        for w in b["warnings"]:
            print("  WARNING:", w)


if __name__ == "__main__":
    main()
