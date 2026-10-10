"""
mapper.py - dependency map for the OpenTelemetry Demo (works for any Docker Compose app).

It reads the five sources that already exist in the repo / running app:

  SOURCE                         WHERE                         WHAT WE TAKE
  1. Compose files               compose.yaml (+ full layer)   which services exist, env vars per service
  2. .env (+ .env.override)      repo root                     real addresses, e.g. CART_ADDR=cart:7070
  3. depends_on                  compose files                 declared startup dependencies
  4. Jaeger traces               running app                   who REALLY called whom, how often
  5. Jaeger dependency graph     running app (/api/dependencies) same graph Jaeger's UI draws, as data

...and adds what the repo does NOT have: one machine-readable map, config-vs-traffic
comparison (hidden dependencies), infra filtering, criticality + single-point-of-failure
scoring, chaos-target flags and the resilience settings the AI needs for fixes.

Usage:
    pip install pyyaml requests

    # Static only - point at the cloned repo folder (no Docker needed):
    python mapper.py --repo ../opentelemetry-demo

    # Static + live traffic (app running, load generator on for ~10 min):
    python mapper.py --repo ../opentelemetry-demo --jaeger http://localhost:8080/jaeger/ui

    # Minimal mode (no Kafka services):
    python mapper.py --repo ../opentelemetry-demo --files compose.yaml

    # Alternative: an already-merged file from `docker compose ... config > merged.yaml`
    python mapper.py --compose merged.yaml

    # Manual fixes (allowed by the rules):
    python mapper.py --repo ../opentelemetry-demo --overrides manual_links.json
    manual_links.json -> {"add": [["checkout", "payment"]], "remove": [["ad", "flagd"]]}

Outputs (./data/):
    dependency_map.json   read by monitor, safety guard and the AI planner
    dependency_map.mmd    Mermaid diagram -> paste into mermaid.live for the architecture slide
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

import requests
import yaml

# Platform / observability services: never chaos targets, and their edges are noise
# (every service exports telemetry to otel-collector and reads flags from flagd).
INFRA = {
    "otel-collector", "otelcol", "jaeger", "grafana", "prometheus", "opensearch",
    "flagd", "flagd-ui", "load-generator", "loadgenerator", "frontend-proxy",
    "image-provider", "react-native-app", "telemetry-docs", "opamp", "opamp-server",
    "llm", "agent", "chatbot", "mcp",
}


# =============================================================== SOURCE 2: .env files
def load_env_files(paths):
    """Parse .env files (later files override earlier ones) and expand ${VAR} references."""
    env = {}
    for p in paths:
        if not os.path.exists(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for _ in range(5):  # resolve nested refs like CART_ADDR=cart:${CART_PORT}
        env = {k: interpolate(v, env) for k, v in env.items()}
    return env


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def interpolate(value, env):
    """Docker-style ${VAR}, ${VAR:-default}, $VAR substitution."""
    if not isinstance(value, str):
        return value

    def rep(m):
        name = m.group(1) or m.group(3)
        default = m.group(2) or ""
        return env.get(name, default)

    return _VAR.sub(rep, value)


# =============================================================== SOURCE 1: compose files
def env_to_dict(environment, env):
    """Compose 'environment' can be a list or a dict, and a bare name (- CART_ADDR)
    means 'take the value from .env'. Returns {NAME: resolved value}."""
    out = {}
    if not environment:
        return out
    items = environment.items() if isinstance(environment, dict) else \
        [(e.split("=", 1) + [None])[:2] for e in environment]
    for k, v in items:
        k = str(k).strip()
        v = env.get(k, "") if v is None else interpolate(str(v), env)
        out[k] = v
    return out


def deep_merge_service(base, extra):
    """Merge a later compose layer into an earlier one (environment and depends_on merge by key)."""
    merged = dict(base)
    for key, val in extra.items():
        if key == "environment":
            merged[key] = {**base.get(key, {}), **val}
        elif key == "depends_on":
            a = base.get(key, {})
            b = val
            a = a if isinstance(a, dict) else {d: {} for d in a}
            b = b if isinstance(b, dict) else {d: {} for d in b}
            merged[key] = {**a, **b}
        elif isinstance(val, dict) and isinstance(base.get(key), dict):
            merged[key] = {**base[key], **val}
        else:
            merged[key] = val
    return merged


def load_compose_files(paths, env):
    services = {}
    for p in paths:
        with open(p) as f:
            doc = yaml.safe_load(f) or {}
        for name, cfg in (doc.get("services") or {}).items():
            cfg = dict(cfg or {})
            cfg["environment"] = env_to_dict(cfg.get("environment"), env)
            services[name] = deep_merge_service(services.get(name, {}), cfg)
    return services


# =============================================================== SOURCES 1-3: static edges
def static_edges(services):
    """Edges from depends_on (source 3), links, and resolved env addresses (sources 1+2)."""
    names = set(services)
    edges = {}
    for name, cfg in services.items():
        deps = cfg.get("depends_on") or {}
        for dep in (deps.keys() if isinstance(deps, dict) else deps):
            edges[(name, dep)] = "depends_on"
        for link in cfg.get("links", []) or []:
            edges[(name, link.split(":")[0])] = "links"
        for var, val in (cfg.get("environment") or {}).items():
            for other in names - {name}:
                # match host part of an address: "cart:7070", "http://email:6060", "Host=postgresql;"
                if re.search(rf"(^|[/=@;\s,]){re.escape(other)}([:/;\s,]|$)", str(val)):
                    edges.setdefault((name, other), f"env:{var}")
    return edges


# =============================================================== SOURCES 4+5: Jaeger
def jaeger_edges(base, lookback_min=30):
    """{(caller, callee): calls}. Tries Jaeger v3 first (the API recent Jaeger/jaeger-v2
    all-in-one distributions expose, confirmed live on 2026-10-10 against this deployment --
    it has no dependency-graph endpoint, so this always walks traces); falls back to the
    older v1 API (dependency graph, then raw trace walk) for older Jaeger deployments, so
    this still works against whatever Jaeger version the next app/judge's sandbox runs."""
    base = base.rstrip("/")

    try:
        r = requests.get(f"{base}/api/v3/services", timeout=15)
        r.raise_for_status()
        services = r.json().get("services") or []
        if services:
            return _jaeger_edges_v3(base, services, lookback_min)
    except Exception as e:
        print(f"  v3 API unavailable ({e}); trying v1 API")

    return _jaeger_edges_v1(base, lookback_min)


def _jaeger_edges_v3(base, services, lookback_min):
    """v3 returns OTLP-shaped JSON (resourceSpans/scopeSpans/spans), structurally
    different from v1's Jaeger-native format -- NOT the same parsing as _jaeger_edges_v1.
    Keyed by (traceId, spanId), not bare spanId, since v3 flattens spans from potentially
    many traces into one response with no per-trace grouping."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=lookback_min)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

    span_service, span_parent = {}, {}

    def ingest(resource_spans):
        for rs in resource_spans or []:
            attrs = {a["key"]: a.get("value", {}).get("stringValue")
                     for a in (rs.get("resource", {}).get("attributes") or [])}
            svc = attrs.get("service.name")
            if not svc:
                continue
            for ss in rs.get("scopeSpans") or []:
                for sp in ss.get("spans") or []:
                    key = (sp["traceId"], sp["spanId"])
                    span_service[key] = svc
                    parent_id = sp.get("parentSpanId")
                    if parent_id:
                        span_parent[key] = (sp["traceId"], parent_id)

    for svc in services:
        try:
            r = requests.get(f"{base}/api/v3/traces", timeout=30, params={
                "query.service_name": svc,
                "query.start_time_min": fmt(start),
                "query.start_time_max": fmt(now),
                "query.num_traces": 50,
            })
            r.raise_for_status()
            text = r.text.strip()
            # Normally one JSON object (confirmed live); defensively also accept the
            # gRPC-gateway's streaming NDJSON form in case a different Jaeger build uses it.
            for line in (text.splitlines() if "\n" in text else [text]):
                if line.strip():
                    ingest(json.loads(line).get("result", {}).get("resourceSpans"))
        except Exception:
            continue

    counts = Counter()
    for child_key, parent_key in span_parent.items():
        child_svc, parent_svc = span_service.get(child_key), span_service.get(parent_key)
        if parent_svc and child_svc and parent_svc != child_svc:
            counts[(parent_svc, child_svc)] += 1

    print(f"  walked v3 traces for {len(services)} services -> {len(counts)} caller/callee pairs")
    return dict(counts)


def _jaeger_edges_v1(base, lookback_min):
    end_ms = int(time.time() * 1000)
    try:
        r = requests.get(f"{base}/api/dependencies",
                         params={"endTs": end_ms, "lookback": lookback_min * 60_000}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data") or []
        if data:
            print("  using Jaeger dependency graph")
            return {(d["parent"], d["child"]): d.get("callCount", 1)
                    for d in data if d["parent"] != d["child"]}
    except Exception as e:
        print(f"  dependency graph unavailable ({e}); walking traces instead")

    counts = Counter()
    services = requests.get(f"{base}/api/services", timeout=15).json().get("data") or []
    for svc in services:
        try:
            r = requests.get(f"{base}/api/traces", timeout=30,
                             params={"service": svc, "limit": 50, "lookback": f"{lookback_min}m"})
            traces = r.json().get("data") or []
        except Exception:
            continue
        for tr in traces:
            procs = {pid: p["serviceName"] for pid, p in tr.get("processes", {}).items()}
            span_svc = {s["spanID"]: procs.get(s["processID"]) for s in tr.get("spans", [])}
            for s in tr.get("spans", []):
                child = span_svc.get(s["spanID"])
                for ref in s.get("references", []):
                    if ref.get("refType") == "CHILD_OF":
                        parent = span_svc.get(ref.get("spanID"))
                        if parent and child and parent != child:
                            counts[(parent, child)] += 1
    print(f"  walked traces for {len(services)} services")
    return dict(counts)


# =============================================================== merge + score
def norm(name):
    return re.sub(r"(-?service)$", "", name.lower().replace("_", "-"))


def merge(services, s_edges, d_edges, aliases):
    by_norm = {norm(n): n for n in services}
    to_compose = lambda j: aliases.get(j) or by_norm.get(norm(j), j)

    merged = {(a, b): {"from": a, "to": b, "source": "config", "found_via": how, "calls": 0}
              for (a, b), how in s_edges.items()}
    for (ja, jb), calls in d_edges.items():
        a, b = to_compose(ja), to_compose(jb)
        if a == b:
            continue
        if (a, b) in merged:
            merged[(a, b)]["source"] = "both"
            merged[(a, b)]["calls"] += calls
        else:
            merged[(a, b)] = {"from": a, "to": b, "source": "traffic",
                              "found_via": "jaeger", "calls": calls}
    return merged


def apply_overrides(edges, path):
    with open(path) as f:
        ov = json.load(f)
    for a, b in ov.get("add", []):
        edges[(a, b)] = {"from": a, "to": b, "source": "manual", "found_via": "manual", "calls": 0}
    for a, b in ov.get("remove", []):
        edges.pop((a, b), None)
    return edges


def container_states():
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format",
             '{{.Label "com.docker.compose.service"}}|{{.State}}'],
            capture_output=True, text=True, timeout=10, check=True).stdout
    except Exception:
        return {}
    return dict(l.split("|", 1) for l in out.splitlines() if "|" in l and l.split("|")[0])


def declared_criticality(cfg):
    """The OTel demo tags services with service.criticality=critical/high/medium/low."""
    m = re.search(r"service\.criticality=(\w+)",
                  (cfg.get("environment") or {}).get("OTEL_RESOURCE_ATTRIBUTES", ""))
    return m.group(1) if m else None


def build(services, edges, states, have_traffic):
    app_edges = [e for e in edges.values() if e["from"] not in INFRA and e["to"] not in INFRA]
    total_calls = sum(e["calls"] for e in app_edges) or 1

    nodes = []
    for name, cfg in services.items():
        is_infra = name in INFRA
        deps = sorted({e["to"] for e in app_edges if e["from"] == name})
        dependents = sorted({e["from"] for e in app_edges if e["to"] == name})
        inbound = sum(e["calls"] for e in app_edges if e["to"] == name)
        replicas = (cfg.get("deploy") or {}).get("replicas", 1)
        share = round(inbound / total_calls, 3) if have_traffic else None
        limits = ((cfg.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        nodes.append({
            "name": name,
            "role": "infra" if is_infra else "app",
            "chaos_target_allowed": not is_infra,
            "declared_criticality": declared_criticality(cfg),
            "replicas": replicas,
            "restart_policy": cfg.get("restart", "no"),
            "has_healthcheck": "healthcheck" in cfg,
            "memory_limit": limits.get("memory"),
            "status": states.get(name, "unknown"),
            "depends_on": deps,
            "dependents": dependents,
            "inbound_calls": inbound,
            "traffic_share": share,
            "criticality_score": round(len(dependents) + (share * 5 if share else 0), 2),
            "single_point_of_failure": (not is_infra) and replicas == 1 and len(dependents) >= 1,
        })
    nodes.sort(key=lambda n: (n["role"] != "app", -n["criticality_score"], n["name"]))

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": ["compose", ".env", "depends_on"] + (["jaeger"] if have_traffic else []),
        "services": nodes,
        "edges": sorted(edges.values(), key=lambda e: (e["from"], e["to"])),
        "hidden_dependencies": [e for e in app_edges if e["source"] == "traffic"],
        "declared_but_unused": [e for e in app_edges if have_traffic and e["source"] == "config"],
    }


def to_mermaid(dep_map):
    lines = ["graph LR"]
    for e in dep_map["edges"]:
        if e["from"] in INFRA or e["to"] in INFRA:
            continue
        a, b = e["from"].replace("-", "_"), e["to"].replace("-", "_")
        arrow = "-.->" if e["source"] == "traffic" else "-->"
        label = f"|{e['calls']}|" if e["calls"] else ""
        lines.append(f"    {a} {arrow}{label} {b}")
    for n in dep_map["services"]:
        if n["single_point_of_failure"]:
            lines.append(f"    style {n['name'].replace('-', '_')} stroke:#e00,stroke-width:3px")
    return "\n".join(lines)


# =============================================================== main
def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="path to the cloned opentelemetry-demo folder")
    src.add_argument("--compose", help="a single (already merged) compose file")
    ap.add_argument("--files", nargs="+", default=["compose.yaml", "compose.full.yaml"],
                    help="compose files inside --repo, in layer order")
    ap.add_argument("--jaeger", help="Jaeger UI base URL, e.g. http://localhost:8080/jaeger/ui")
    ap.add_argument("--lookback", type=int, default=30, help="minutes of traces to read")
    ap.add_argument("--overrides", help="JSON with manual add/remove links")
    ap.add_argument("--aliases", help='JSON map jaeger-name -> compose-name')
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    if args.repo:
        env = load_env_files([os.path.join(args.repo, ".env"),
                              os.path.join(args.repo, ".env.override")])
        files = [os.path.join(args.repo, f) for f in args.files
                 if os.path.exists(os.path.join(args.repo, f))]
        print(f"Loaded {len(env)} variables from .env; compose layers: "
              f"{', '.join(os.path.basename(f) for f in files)}")
    else:
        env, files = dict(os.environ), [args.compose]

    services = load_compose_files(files, env)
    if not services:
        sys.exit("No services found.")
    print(f"Found {len(services)} services")

    s_edges = static_edges(services)
    d_edges = {}
    if args.jaeger:
        print(f"Reading live traffic from {args.jaeger} (last {args.lookback} min)...")
        d_edges = jaeger_edges(args.jaeger, args.lookback)
        print(f"  {len(d_edges)} caller -> callee pairs")

    aliases = json.load(open(args.aliases)) if args.aliases else {}
    edges = merge(services, s_edges, d_edges, aliases)
    if args.overrides:
        edges = apply_overrides(edges, args.overrides)

    dep_map = build(services, edges, container_states(), bool(d_edges))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "dependency_map.json"), "w") as f:
        json.dump(dep_map, f, indent=2)
    with open(os.path.join(args.out, "dependency_map.mmd"), "w") as f:
        f.write(to_mermaid(dep_map))

    print(f"\n{'service':<18}{'declared':<10}{'score':>6} {'traffic':>8}  SPOF  depends_on")
    print("-" * 96)
    for n in dep_map["services"]:
        if n["role"] != "app":
            continue
        ts = f"{n['traffic_share']:.0%}" if n["traffic_share"] is not None else "-"
        print(f"{n['name']:<18}{(n['declared_criticality'] or '-'):<10}{n['criticality_score']:>6} "
              f"{ts:>8}  {'YES ' if n['single_point_of_failure'] else '    '}  "
              f"{', '.join(n['depends_on']) or '-'}")

    if dep_map["hidden_dependencies"]:
        print("\nHidden dependencies (seen in traffic, not in config):")
        for e in dep_map["hidden_dependencies"]:
            print(f"  {e['from']} -> {e['to']}  ({e['calls']} calls)")
    print(f"\nSaved {args.out}/dependency_map.json and {args.out}/dependency_map.mmd")


if __name__ == "__main__":
    main()
