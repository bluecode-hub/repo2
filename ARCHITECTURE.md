# Architecture — AI-Driven Chaos Engineering for Cloud-Native Resilience

BGSW AI Innovation Hackathon · Problem Statement: inject controlled failures into a
cloud-native app and have an AI system analyze the response.

## 1. Target system

**OpenTelemetry Demo** ("astronomy-shop") — an open-source, intentionally realistic
microservices app, ~18 services wired by real gRPC/HTTP calls, backed by Kafka,
instrumented end-to-end with OpenTelemetry. Chosen because it gives us genuine
cross-service dependencies and genuine telemetry to react to, instead of a toy app.

Observability stack already shipped with it and reused as-is: **Prometheus** (metrics,
native OTLP ingestion — not pull-based scraping), **Jaeger** (traces, v3 API /
OTLP-shaped JSON), **OpenSearch** (logs), **Grafana** (not used in the final UI — see
§5).

Location: `sandbox-app/opentelemetry-demo/` (vendored directly, not a git submodule —
see §7). Compose files patched for stability under repeated fault injection:
`compose.yaml` (checkout/product-catalog memory limits raised + matching `GOMEMLIMIT`,
payment memory raised), `compose.full.yaml` (kafka memory raised),
`compose.observability.yaml` (grafana/opensearch memory raised).

## 2. Why "probabilistic + deterministic"

Bosch's own hackathon material frames good AI systems as probabilistic reasoning
wrapped in deterministic guardrails. We took that literally as a design split:

- **Deterministic, no LLM involved at all:** the safety watchdog that decides
  abort/recover during a live fault. Safety-critical logic must be predictable and
  auditable, not subject to a model's judgment call.
- **Probabilistic / LLM-driven:** picking *what* to test next and *explaining* what the
  data means afterward — reasoning tasks, not safety-critical ones.

This split is the main thing worth defending in front of judges: we didn't bolt an LLM
onto everything indiscriminately, we put it only where reasoning actually adds value.

## 3. The pipeline (`chaos/day1_pipeline.py`)

A LangGraph `StateGraph` with four nodes, single pass:

```
capture_baseline → plan_experiment → inject_and_monitor → explain_impact → END
```

- **`capture_baseline`** — snapshots system health before touching anything. Retries
  up to 4x (3s apart) if the first read looks unhealthy, so a single noisy reading
  doesn't poison the whole run's "before" picture.
- **`plan_experiment`** — decides *target service* + *fault type*. Two modes (see §4):
  manual (operator picks via the dashboard dropdown — used for every real run so far)
  or auto-rank (criticality-score based, built and working, just not yet exercised
  live). Delivered through a real LangChain call path (`FakeListChatModel` today,
  swappable for a real model later — see §6) so every decision is traced identically
  regardless of which model is actually behind it.
- **`inject_and_monitor`** — applies the fault, runs a deterministic watchdog loop,
  undoes the fault, then measures recovery. This is the safety-critical node; full
  mechanics in §4.
- **`explain_impact`** — turns the before/after snapshots into a plain-English causal
  explanation (also delivered through the same real LangChain call path).

Day 2 would loop `explain_impact → plan_experiment` instead of `END`, so the system
runs a self-directed sequence of experiments instead of one.

Every node emits structured events through `chaos/agent_tracer.py`
(`LocalTracer(BaseCallbackHandler)`), which writes JSON lines to
`chaos/data/agent_traces.jsonl` — this is our own local, sandbox-only equivalent of
LangSmith, built because sending agent traces to an external SaaS would break the
"local sandbox only" constraint of the challenge. The dashboard's "agent traces" tab
reads this file and renders it as a nested waterfall.

## 4. Fault injection and the safety watchdog

**Fault primitives** (`chaos/fault_injector.py`, `FaultInjector` class):

| Fault | Mechanism | Used live on Day 1 |
|---|---|---|
| `crash` | `docker kill` target, undo = `docker start` | yes |
| `failed_dependency` | `docker pause` / `unpause` | yes |
| `slow_network` | netem sidecar joining target's network namespace | built, not run live |
| `full_disk` | `dd` fill via `exec_run` | built, not run live |
| `overloaded_db` | `docker update --cpus` | built, not run live |
| `traffic_spike` | Locust swarm API | built, not run live |

`DAY1_SAFE_FAULTS = [crash, failed_dependency]` — the other four exist in code but were
deliberately not exercised against the live stack within the hackathon's time budget.
That's a stated risk decision, not an oversight.

**Detection** (`chaos/monitor.py`, `Monitor` class): HTTP probes against
home/products/cart/recommendations/ads, Docker container state, and per-service
Prometheus metrics (error rate, latency), filtered to this compose project only (a
cross-project container-name collision was a real bug caught and fixed). Prometheus
query windows use 2 minutes, not 30 seconds — the collector's flush interval is ~60s,
so anything shorter silently returns empty data.

**Abort decision — fail fast.** Inside `inject_and_monitor`, every 2 seconds
(`POLL_INTERVAL_SEC`) while the fault is active (max 30s):

```python
healthy, reasons = monitor.is_healthy(snap)
reason = None if healthy else "; ".join(reasons)
if reason: aborted, abort_reason = True, reason; break
```

A single unhealthy reading ends the fault immediately — no multi-strike confirmation.
There is also a `blind_polls` counter: 5 consecutive polls with *no* Prometheus
service-level data fail-closes even if nothing else looks wrong, because "I can't
verify" has to be treated as itself unsafe, not as "probably fine."

**Recovery decision — confirm slowly.** After `injection.undo()`, a *separate* loop
polls every 1 second (not 2 — recovery confirmation is cheap to check often) for up to
90 seconds (`RECOVERY_TIMEOUT_SEC`), and requires **3 consecutive healthy reads in a
row** (`RECOVERY_HEALTHY_STREAK`) before declaring recovery and recording
`recovery_sec`. A single bad read resets the streak to zero.

This asymmetry — abort on the first bad sign, but require sustained good signs before
declaring "all clear" — is the core safety design and the most defensible thing to
walk a judge through: fast to distrust, slow to trust.

Because `injection.undo()` runs unconditionally right after the watchdog loop (success
or abort, no branching), the target service is already restored before
`explain_impact` even runs — there is no window where the pipeline reports "finished"
while the target is still broken.

`peak_impact_snap` — the worst snapshot observed during the fault window — is tracked
and used as the "after" picture, instead of a post-recovery snapshot (which would show
a useless 0%→0% comparison once everything's already healed).

## 5. Dashboard (`chaos/dashboard_server.py` + `chaos/dashboard/`)

FastAPI backend, static frontend (Cytoscape.js + dagre, no framework), 5 tabs:

1. **Dependency map** — live-rendered from `chaos/mapper.py`, which cross-references
   the compose files (static topology) against live Jaeger traces (real call counts,
   last 20 min). Arrow numbers on the graph = actual observed call volume, not a
   guess. SPOF services get a dashed border; criticality is color-coded.
2. **Prometheus insights** — direct Prometheus queries for latency/error/traffic/memory
   rankings. A CPU-utilization panel was built and then deliberately removed once its
   numbers didn't match `docker stats` reality — we chose not to ship a metric we
   couldn't verify, rather than guess at its formula.
3. **Agent traces** — reads `agent_traces.jsonl`, renders the LangGraph/LangChain call
   tree as a waterfall (our local LangSmith equivalent, see §3).
4. **Baseline vs injection** — the controls bar (target dropdown, fault dropdown, run
   button, reset button) plus the before/after/explanation output for the last run.
5. **Live log** — tail of `chaos/data/live_log.jsonl`, the human-readable run narration
   (same events that drive the watchdog decisions in §4).

Design notes worth having answers for: Grafana's own UI was tried and dropped (slow,
dark-background, looked out of place next to judge-facing material) in favor of
querying Prometheus directly and presenting ranked numbers; a `no-store` cache-control
middleware was added after a real stale-browser-cache bug cost debugging time; the
dependency graph's layout bug turned out to be a stale-data-skip optimization
preventing any retry of a bad first paint, fixed properly with a `ResizeObserver`
rather than timing guesses.

## 6. Manual control, not auto-ranking — current state of `plan_experiment`

`plan_experiment` always runs as a graph node. What's dormant is only the auto-ranking
*branch* inside `build_planner_llm()`:

- If `MANUAL_OVERRIDE["target"]` is set (true for every run so far — the dashboard's
  "run experiment" button always passes `--target`), it skips ranking and builds its
  reasoning around the operator's choice, explicitly labeled "manually selected by the
  operator (not auto-ranked)".
- If not set (only reachable by invoking `day1_pipeline.py` from a terminal with no
  `--target`), it filters to untested services, sorts by `-criticality_score`, and
  picks the top one — crash if it's a SPOF, failed_dependency otherwise.

Both paths return their decision through the same `FakeListChatModel` call path, so
the trace format is identical either way — which is what makes swapping in a real
model later (see §8) a drop-in change rather than a rewrite.

A reset mechanism (`chaos/reset.py`) clears only the four JSONL history files
(`injection_comparison.jsonl`, `runs.jsonl`, `live_log.jsonl`, `chaos_state.json`) for a
clean demo run. It does **not** touch Docker — it has nothing to do with restoring the
target service, which is handled unconditionally inside `inject_and_monitor` itself
(see §4). These are two unrelated mechanisms; conflating them was a real point of
confusion during testing.

## 7. A deliberate repo decision

`sandbox-app/opentelemetry-demo/` was originally a separate git clone (its own `.git`,
tracked by the outer repo only as a commit pointer). That meant our actual compose
edits — the memory-limit and `GOMEMLIMIT` fixes that keep the stack stable under
repeated fault injection — lived only in an untracked working tree and would not have
survived a push. Fixed by stripping the nested `.git` and committing the sample app's
files directly into this repo, so the patched compose files travel with the rest of
the submission instead of silently being lost.

## 8. What's intentionally not done yet

- **Real LLM wiring.** `plan_experiment` and `explain_impact` currently deliver their
  (already fully real, Python-computed) decisions through `FakeListChatModel` rather
  than an actual model call — proven out via `chaos/demo_agent.py` and paused by
  explicit team decision pending a Gemini free-tier swap (`langchain-google-genai`'s
  `ChatGoogleGenerativeAI`). The decision logic itself does not change when this swaps
  in; only how the text is "delivered" changes.
- **Day-2 loop.** The graph is single-pass; looping `explain_impact` back into
  `plan_experiment` for a multi-experiment ranked run is scoped but not built.
- **4 of 6 fault types** (`slow_network`, `full_disk`, `overloaded_db`,
  `traffic_spike`) are implemented but not yet exercised against the live stack.

## 9. End-to-end flow

See `FLOWCHART.md` (or the ASCII diagram the team was given alongside this document)
for the full operator-trigger-to-explanation sequence across the dashboard, pipeline,
watchdog, and fault layer.
