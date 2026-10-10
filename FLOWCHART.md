# End-to-end flow — trigger to explanation

```
 OPERATOR (dashboard, tab "baseline vs injection")
   │ picks target + fault in dropdowns, clicks "run experiment"
   ▼
 chaos/dashboard/app.js
   │ POST /api/run-experiment  { target, fault }
   ▼
 chaos/dashboard_server.py
   │ concurrency guard: refuse if _pipeline_proc already running
   │ subprocess.Popen(["python","day1_pipeline.py",
   │                    "--target",target,"--fault",fault],
   │                   stdin=DEVNULL)
   ▼
 chaos/day1_pipeline.py  __main__
   │ sets MANUAL_OVERRIDE = {target, fault}
   │ builds LangGraph, invokes with callbacks=[LocalTracer()]
   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      LangGraph  StateGraph  (single pass)           │
│                                                                       │
│  [capture_baseline] ──▶ [plan_experiment] ──▶ [inject_and_monitor]  │
│         │                      │                       │            │
│         │                      │                       ▼            │
│         │                      │              [explain_impact] ──▶ END
│         │                      │                       │            │
└─────────┼──────────────────────┼───────────────────────┼────────────┘
          │                      │                       │
          ▼                      ▼                       ▼
   every node call is intercepted by LocalTracer (BaseCallbackHandler)
   on_chain_start/end, on_llm_start/end, on_tool_start/end
          │
          ▼
   chaos/data/agent_traces.jsonl  ──▶  dashboard tab "agent traces"
                                        (our local LangSmith-equivalent,
                                         sandbox-only, nothing leaves the box)


 ── NODE 1: capture_baseline ────────────────────────────────────────
   monitor.snapshot() + monitor.is_healthy()
   unhealthy? → retry (max 4x, 3s apart)   [debounce vs. one noisy read]
   store {"type":"baseline", ts, healthy, reasons, snapshot}
          │
          ▼  (row appended)
   chaos/data/live_log.jsonl   ──▶  dashboard tab "live log"


 ── NODE 2: plan_experiment ─────────────────────────────────────────
                 MANUAL_OVERRIDE["target"] set?
                 (true every time — dashboard always passes --target)
         ┌───────────────┴───────────────┐
        YES                              NO   (CLI-only, dormant today)
         │                                │
   look up that service,            filter to untested services,
   fault = given or                 sort by -criticality_score,
   SPOF-based default               pick top; fault = crash if SPOF
   reasoning: "manually                 else failed_dependency
   selected by operator                 reasoning: auto-ranked
   (not auto-ranked)"
         └───────────────┬───────────────┘
                          ▼
         FakeListChatModel(responses=[reasoning]).invoke(...)
         (real LangChain call path either way → identical trace shape,
          so swapping in a real model later is a drop-in change)


 ── NODE 3: inject_and_monitor  (the safety-critical node) ───────────

   before = monitor.snapshot()
   injection = injector.inject(fault, target)      ← docker kill / pause
          │
          ▼
   ┌─ ABORT LOOP — poll every 2s, hard cap 30s ───────────────────┐
   │  snap = monitor.snapshot()                                   │
   │  healthy, reasons = monitor.is_healthy(snap)                 │
   │                                                                │
   │  ANY single unhealthy read?        ──▶ reason set, BREAK NOW │
   │  5 consecutive blind polls                                   │
   │    (no Prometheus visibility)      ──▶ reason set, BREAK NOW │
   │                                                                │
   │  track peak_impact_snap = worst reading seen so far          │
   │  (used as "after", not a post-recovery snapshot)             │
   └────────────────────────────────────────────────────────────────┘
          │  FAIL FAST — one bad signal is enough to stop
          ▼
   injection.undo()        ← docker start / unpause, unconditional,
                              runs whether aborted or window just ended
          │
          ▼
   ┌─ RECOVERY LOOP — poll every 1s, timeout 90s ─────────────────┐
   │  healthy?  streak += 1   else   streak = 0                   │
   │  streak >= 3 consecutive healthy reads ──▶ recovered,        │
   │                                            record recovery_sec│
   │  never reaches 3-in-a-row in 90s ──▶ recovery_sec = None     │
   └────────────────────────────────────────────────────────────────┘
          │  CONFIRM SLOW — one bad signal resets the streak
          ▼
   recovered_snapshot = monitor.snapshot()

   every watchdog tick ──▶ chaos/data/live_log.jsonl ──▶ "live log" tab


 ── NODE 4: explain_impact ────────────────────────────────────────────
   builds explanation string from before / peak_impact_snap / recovery_sec
   delivered through FakeListChatModel (real LangChain call path)
          │
          ▼
   append rows to:
     chaos/data/injection_comparison.jsonl ─▶ "baseline vs injection" tab
     chaos/data/runs.jsonl                 ─▶ feeds past_targets for
                                               next plan_experiment call
          │
          ▼
                      END — pipeline exits, logs "pipeline finished"


 ── PARALLEL, ALWAYS RUNNING: dashboard polling (independent of runs) ─
   GET /api/pipeline-status     every 2s   → run/reset buttons enabled?
   GET /api/available-targets   every 6s   → target dropdown contents
   GET /api/dependency-map      every 4s   → tab 1 (mapper.py: compose
                                              files + live Jaeger v3 edges)
   GET /api/prometheus-insights every 5s   → tab 2 (direct Prometheus
                                              queries, ranked)
   GET /api/agent-traces        every 3s   → tab 3
   GET /api/injection-comparison every 2.5s→ tab 4
   GET /api/live-log            every 1.5s → tab 5


 ── SEPARATE, UNRELATED MECHANISM: "reset for clean run" button ──────
   POST /api/reset  (refused if a pipeline run is still in progress)
          │
          ▼
   chaos/reset.py  →  deletes injection_comparison.jsonl, runs.jsonl,
                       live_log.jsonl, chaos_state.json
   (clears HISTORY/LOGS only — never touches Docker. Restoring the
    target service already happened unconditionally back in NODE 3,
    regardless of whether this button is ever pressed.)
```
