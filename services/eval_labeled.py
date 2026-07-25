"""
Evaluate the agent against the hand-labeled ground truth in labels/train_labels.json.

Unlike services/ragas_eval.py (which only checks a generic escalation_flag from
tickets/train_index.csv), this uses the richer per-ticket labels to compute the
two metrics the eval rubric calls out specifically:

  - routing_accuracy: did the Triage Agent's routing decision match correct_routing?
  - retrieval hit_rate / recall@k: for tickets routed to L1, did search_knowledge_base
    actually surface the runbook(s)/docs the label says are relevant?

Calls the already-running api_service (POST /triage, POST /resolve) so it exercises
the real orchestration + retrieval path, not a mocked one.
"""
import argparse
import datetime
import json
import re
from pathlib import Path

import pandas as pd
import requests

LABELS_PATH = Path("labels/train_labels.json")
TICKETS_DIR = Path("tickets/train")
API_URL = "http://localhost:8000"
SOURCE_RE = re.compile(r"--- Document Source: (.+?) ---")


def basename(doc_path: str) -> str:
    return doc_path.split("/")[-1]


def extract_retrieved_sources(trace: list) -> list[str]:
    sources = []
    for step in trace:
        if step.get("type") == "tool_result" and step.get("name") == "search_knowledge_base":
            sources.extend(SOURCE_RE.findall(step.get("result", "")))
    # de-dupe, preserve order
    seen = set()
    ordered = []
    for s in sources:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def evaluate(limit: int = 0, sleep_between: float = 0.0):
    labels = json.loads(LABELS_PATH.read_text())
    if limit > 0:
        labels = labels[:limit]

    routing_correct = 0
    routing_total = 0
    retrieval_hits = 0
    retrieval_recalls = []
    retrieval_total = 0
    errors = 0
    rows = []

    for label in labels:
        ticket_id = label["ticket_id"]
        ticket_file = TICKETS_DIR / f"{ticket_id}.json"
        if not ticket_file.exists():
            continue
        ticket_json = ticket_file.read_text()
        expected_routing = label["correct_routing"].strip().lower()
        relevant_docs = {basename(d) for d in label.get("relevant_kb_docs", [])}

        row = {"ticket_id": ticket_id, "expected_routing": expected_routing}
        try:
            triage_res = requests.post(f"{API_URL}/triage", json={"ticket_json": ticket_json}, timeout=300).json()
            actual_routing = triage_res.get("routing", "ERROR").strip().lower()
            routing_total += 1
            row["actual_routing"] = actual_routing
            row["routing_correct"] = actual_routing == expected_routing
            if row["routing_correct"]:
                routing_correct += 1

            if actual_routing == "l1_guided":
                resolve_res = requests.post(
                    f"{API_URL}/resolve", json={"ticket_json": ticket_json}, timeout=600
                ).json()
                trace = resolve_res.get("trace", [])
                retrieved = extract_retrieved_sources(trace)
                row["retrieved_docs"] = retrieved

                if relevant_docs:
                    retrieval_total += 1
                    hit = bool(relevant_docs & set(retrieved))
                    recall = len(relevant_docs & set(retrieved)) / len(relevant_docs)
                    row["retrieval_hit"] = hit
                    row["retrieval_recall"] = recall
                    retrieval_hits += int(hit)
                    retrieval_recalls.append(recall)

        except Exception as e:
            errors += 1
            row["error"] = str(e)

        rows.append(row)
        if sleep_between:
            import time

            time.sleep(sleep_between)

    metrics = {
        "total_tickets": len(labels),
        "routing_total": routing_total,
        "routing_correct": routing_correct,
        "routing_accuracy": routing_correct / routing_total if routing_total else 0,
        "retrieval_total": retrieval_total,
        "retrieval_hit_rate": retrieval_hits / retrieval_total if retrieval_total else 0,
        "retrieval_recall_avg": sum(retrieval_recalls) / len(retrieval_recalls) if retrieval_recalls else 0,
        "errors": errors,
    }

    eval_dir = Path("data/eval_runs")
    eval_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    (eval_dir / f"labeled_eval_{timestamp}_metrics.json").write_text(json.dumps(metrics, indent=4))
    pd.DataFrame(rows).to_csv(eval_dir / f"labeled_eval_{timestamp}_details.csv", index=False)

    return metrics, rows


def main():
    parser = argparse.ArgumentParser(description="Evaluate routing + retrieval accuracy against labels/train_labels.json")
    parser.add_argument("--limit", type=int, default=30, help="Number of labeled tickets to evaluate (0 = all 220).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between tickets (rate limiting).")
    args = parser.parse_args()

    metrics, _ = evaluate(limit=args.limit, sleep_between=args.sleep)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
