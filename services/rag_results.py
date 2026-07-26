"""Shared writer for RAG_Results/ -- the consolidated evaluation record.

Every eval run (full agent eval, or a retrieval-only ablation) appends one
row to RAG_Results/Rag_result.csv and writes a full JSON dump (metrics +
per-ticket details) to RAG_Results/rag_json/<run_id>.json.
"""
import csv
import datetime
import json
from pathlib import Path

RESULTS_DIR = Path("RAG_Results")
CSV_PATH = RESULTS_DIR / "Rag_result.csv"
JSON_DIR = RESULTS_DIR / "rag_json"

CSV_COLUMNS = [
    "run_id", "timestamp", "eval_type", "reranker_enabled",
    "tickets_evaluated", "routing_total", "routing_correct", "routing_accuracy",
    "retrieval_total", "retrieval_hit_rate", "retrieval_recall_avg", "errors",
]


def record_run(eval_type: str, metrics: dict, details: list, reranker_enabled: bool = True) -> str:
    """Append a summary row to Rag_result.csv and dump the full result to
    rag_json/. Returns the run_id used for both."""
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{eval_type}_{'rerank' if reranker_enabled else 'norerank'}_{timestamp}"

    row = {
        "run_id": run_id,
        "timestamp": timestamp,
        "eval_type": eval_type,
        "reranker_enabled": reranker_enabled,
        "tickets_evaluated": metrics.get("total_tickets", metrics.get("tickets_evaluated", "")),
        "routing_total": metrics.get("routing_total", ""),
        "routing_correct": metrics.get("routing_correct", ""),
        "routing_accuracy": metrics.get("routing_accuracy", ""),
        "retrieval_total": metrics.get("retrieval_total", ""),
        "retrieval_hit_rate": metrics.get("retrieval_hit_rate", ""),
        "retrieval_recall_avg": metrics.get("retrieval_recall_avg", ""),
        "errors": metrics.get("errors", ""),
    }

    write_header = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    (JSON_DIR / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id,
        "eval_type": eval_type,
        "reranker_enabled": reranker_enabled,
        "metrics": metrics,
        "details": details,
    }, indent=2, default=str))

    return run_id
