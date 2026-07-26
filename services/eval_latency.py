"""
Component-level latency measurement across all 220 labeled tickets.

Runs each ticket through the live agent (/triage, then /resolve if
L1_GUIDED) and extracts the per-component timing already instrumented in
agent_core/llm_client.py's trace (duration_s on each tool_result and
llm_call step). Writes a per-ticket summary CSV and a per-component detail
CSV to RAG_Results/.
"""
import argparse
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

KNOWN_TOOL_COMPONENTS = ["search_knowledge_base", "get_system_spec", "search_faq"]


def run_ticket(ticket_id: str, ticket_json: str):
    """Returns (summary_row: dict, detail_rows: list[dict]) for one ticket."""
    detail_rows = []
    summary = {"ticket_id": ticket_id, "routing": "ERROR", "error": ""}

    try:
        res = requests.post(f"{API_URL}/triage", json={"ticket_json": ticket_json}, timeout=120)
        res.raise_for_status()
        triage = res.json()
    except Exception as e:
        summary["error"] = str(e)
        return summary, detail_rows

    summary["routing"] = triage.get("routing", "ERROR")
    triage_time = 0.0
    for t in triage.get("timing", []):
        triage_time += t["duration_s"]
        detail_rows.append({"ticket_id": ticket_id, "step": t["step"], "type": "LLM", "duration_s": round(t["duration_s"], 3), "size_chars": None})
    summary["triage_time_s"] = round(triage_time, 3)

    resolve_time, llm_time, tool_time = 0.0, 0.0, 0.0
    num_llm_calls, num_tool_calls = 0, 0
    component_times = {}

    if summary["routing"] == "L1_GUIDED":
        try:
            res = requests.post(f"{API_URL}/resolve", json={"ticket_json": ticket_json}, timeout=180)
            res.raise_for_status()
            resolve_result = res.json()
        except Exception as e:
            summary["error"] = str(e)
            resolve_result = {"trace": []}

        for step in resolve_result.get("trace", []):
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
            detail_rows.append({"ticket_id": ticket_id, "step": name, "type": step["type"], "duration_s": round(dur, 3), "size_chars": size})

    summary["resolve_time_s"] = round(resolve_time, 3)
    summary["llm_time_s"] = round(triage_time + llm_time, 3)
    summary["tool_time_s"] = round(tool_time, 3)
    summary["total_time_s"] = round(triage_time + resolve_time, 3)
    summary["num_llm_calls"] = num_llm_calls + 1  # +1 for triage
    summary["num_tool_calls"] = num_tool_calls
    for comp in KNOWN_TOOL_COMPONENTS:
        summary[f"{comp}_time_s"] = round(component_times.get(comp, 0.0), 3)

    return summary, detail_rows


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
    summary_df.to_csv(RESULTS_DIR / "Latency_result.csv", index=False)
    detail_df.to_csv(RESULTS_DIR / "Latency_details.csv", index=False)

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
