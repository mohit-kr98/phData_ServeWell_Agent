"""
Component-level latency measurement across all 220 labeled tickets.

Runs each ticket through the live agent (/triage, then /resolve if
L1_GUIDED) and extracts the per-component timing already instrumented in
agent_core/llm_client.py's trace (duration_s on each tool_result and
llm_call step). Writes a per-ticket summary CSV and a per-component detail
CSV to RAG_Results/.
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

import pandas as pd
import requests

# Allow `python3 services/eval_latency.py` as well as `-m` / package imports
sys.path.insert(0, str(Path(__file__).parent.parent))

LABELS_PATH = Path("labels/train_labels.json")
TICKETS_DIR = Path("tickets/train")
API_URL = "http://localhost:8000"
RESULTS_DIR = Path("RAG_Results")
# Timestamped per-run history, mirroring data/eval_runs/ for accuracy. The
# fixed-name CSVs in RESULTS_DIR are still written (they're the "latest"
# pointer other code reads), but every run is also archived here so a
# latency regression can be traced back over time rather than being
# overwritten and lost on the next run.
LATENCY_RUNS_DIR = Path("data/latency_runs")

KNOWN_TOOL_COMPONENTS = ["search_knowledge_base", "get_system_spec", "search_faq"]


def extract_timing(triage_res: dict, resolve_res: dict | None) -> tuple[dict, list[dict]]:
    """Pure extraction of per-component timing from already-fetched /triage
    and /resolve responses -- no API calls, no ticket identity.

    Split out so a caller that already has these responses for another
    reason (eval_labeled.py's accuracy loop calls both endpoints anyway) can
    get latency data for free, instead of re-triaging/re-resolving every
    ticket a second time just to time it.
    """
    detail_rows = []
    triage_time = 0.0
    for t in triage_res.get("timing", []):
        triage_time += t["duration_s"]
        detail_rows.append({"step": t["step"], "type": "LLM", "duration_s": round(t["duration_s"], 3), "size_chars": None})

    resolve_time, llm_time, tool_time = 0.0, 0.0, 0.0
    num_llm_calls, num_tool_calls = 0, 0
    component_times = {}

    if resolve_res is not None:
        for step in resolve_res.get("trace", []):
            if "duration_s" not in step:
                continue
            dur = step["duration_s"]
            resolve_time += dur
            if step["type"] == "llm_call":
                llm_time += dur
                num_llm_calls += 1
                name = f"llm_reasoning_turn{step.get('loop')}"
                size = step.get("prompt_chars")
            else:
                tool_time += dur
                num_tool_calls += 1
                name = step.get("name", "unknown")
                size = step.get("result_chars")
                component_times[name] = component_times.get(name, 0.0) + dur
            detail_rows.append({"step": name, "type": step["type"], "duration_s": round(dur, 3), "size_chars": size})

    # Prefer the agent's own wall-clock measurement over the sum of step
    # durations. Retrieval and enrichment run concurrently in a thread pool, so
    # summing their individual timings over-counts real elapsed time -- measured
    # at +25% to +65% per ticket. The summed figure is retained as
    # resolve_step_time_s because it is still the right basis for "which
    # component costs most", just not for "how long did this take".
    wall = next((s["duration_s"] for s in (resolve_res or {}).get("trace", [])
                 if s.get("type") == "wall_clock"), None)
    resolve_elapsed = wall if wall is not None else resolve_time

    fields = {
        "triage_time_s": round(triage_time, 3),
        "resolve_time_s": round(resolve_elapsed, 3),
        "resolve_step_time_s": round(resolve_time, 3),
        "llm_time_s": round(triage_time + llm_time, 3),
        "tool_time_s": round(tool_time, 3),
        "total_time_s": round(triage_time + resolve_elapsed, 3),
        "num_llm_calls": num_llm_calls + 1,  # +1 for triage
        "num_tool_calls": num_tool_calls,
    }
    for comp in KNOWN_TOOL_COMPONENTS:
        fields[f"{comp}_time_s"] = round(component_times.get(comp, 0.0), 3)

    return fields, detail_rows


def run_ticket(ticket_id: str, ticket_json: str):
    """Returns (summary_row: dict, detail_rows: list[dict]) for one ticket.
    Calls /triage and /resolve itself -- for the standalone latency CLI/tab.
    eval_labeled.py calls extract_timing() directly since it already has
    these responses from its own accuracy pass."""
    detail_rows = []
    summary = {"ticket_id": ticket_id, "routing": "ERROR", "error": ""}

    try:
        res = requests.post(f"{API_URL}/triage", json={"ticket_json": ticket_json}, timeout=120)
        res.raise_for_status()
        triage_res = res.json()
    except Exception as e:
        summary["error"] = str(e)
        return summary, detail_rows

    summary["routing"] = triage_res.get("routing", "ERROR")
    resolve_res = None
    if summary["routing"] == "L1_GUIDED":
        try:
            res = requests.post(f"{API_URL}/resolve", json={"ticket_json": ticket_json}, timeout=180)
            res.raise_for_status()
            resolve_res = res.json()
        except Exception as e:
            summary["error"] = str(e)
            resolve_res = {"trace": []}

    timing_fields, timing_detail_rows = extract_timing(triage_res, resolve_res)
    summary.update(timing_fields)
    for d in timing_detail_rows:
        detail_rows.append({"ticket_id": ticket_id, **d})

    return summary, detail_rows


def summarize_latency(summary_df: pd.DataFrame) -> dict:
    """Headline latency stats from a per-ticket summary frame."""
    if summary_df.empty or "total_time_s" not in summary_df.columns:
        return {}
    totals = summary_df["total_time_s"].dropna()
    if totals.empty:
        return {}
    stats = {
        "tickets_measured": int(len(totals)),
        "avg_total_time_s": round(totals.mean(), 3),
        "median_total_time_s": round(totals.median(), 3),
        "p95_total_time_s": round(totals.quantile(0.95), 3),
        "max_total_time_s": round(totals.max(), 3),
    }
    for col, key in [("triage_time_s", "avg_triage_time_s"), ("resolve_time_s", "avg_resolve_time_s"),
                     ("llm_time_s", "avg_llm_time_s"), ("tool_time_s", "avg_tool_time_s")]:
        if col in summary_df.columns:
            stats[key] = round(summary_df[col].dropna().mean(), 3)
    return stats


def run_latency_eval(limit: int = 0):
    labels = json.loads(LABELS_PATH.read_text())
    if limit > 0:
        labels = labels[:limit]

    summaries, details = [], []
    for label in labels:
        tid = label["ticket_id"]
        ticket_file = TICKETS_DIR / f"{tid}.json"
        if not ticket_file.exists():
            continue
        tj = ticket_file.read_text()
        summary, detail_rows = run_ticket(tid, tj)
        summaries.append(summary)
        details.extend(detail_rows)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_df = pd.DataFrame(summaries)
    detail_df = pd.DataFrame(details)
    # "Latest" pointer -- other code and the Latency tab read these.
    summary_df.to_csv(RESULTS_DIR / "Latency_result.csv", index=False)
    detail_df.to_csv(RESULTS_DIR / "Latency_details.csv", index=False)

    # Archive this run so it isn't lost when the next one overwrites the above.
    LATENCY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    metrics = {"source": "eval_latency", **summarize_latency(summary_df)}
    (LATENCY_RUNS_DIR / f"latency_{timestamp}_metrics.json").write_text(json.dumps(metrics, indent=4))
    summary_df.to_csv(LATENCY_RUNS_DIR / f"latency_{timestamp}_result.csv", index=False)
    detail_df.to_csv(LATENCY_RUNS_DIR / f"latency_{timestamp}_details.csv", index=False)

    return summary_df, detail_df


def main():
    parser = argparse.ArgumentParser(description="Measure component-level latency across labeled tickets.")
    parser.add_argument("--limit", type=int, default=0, help="Number of tickets to test (0 = all).")
    args = parser.parse_args()

    summary_df, _ = run_latency_eval(limit=args.limit)
    print(f"Processed {len(summary_df)} tickets. Saved to RAG_Results/Latency_result.csv and Latency_details.csv\n")
    print(summary_df[["total_time_s", "triage_time_s", "resolve_time_s", "llm_time_s", "tool_time_s"]].describe().round(2))


if __name__ == "__main__":
    main()
