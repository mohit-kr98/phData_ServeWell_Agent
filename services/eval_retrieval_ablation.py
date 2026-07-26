"""
Ablation: does the cross-encoder reranking stage in query_pipeline.py actually
improve retrieval accuracy, or would raw embedding-similarity order do just as
well?

Retrieval-only (no LLM calls, no triage/resolve) -- for each labeled ticket,
queries the same knowledge base twice with an identical query: once with
reranking on (rerank=True) and once with it off (rerank=False), then scores
both against the ticket's relevant_kb_docs the same way eval_labeled.py does.

Records both runs to RAG_Results/ so the improvement is directly comparable.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import requests

# Allow `python3 services/eval_retrieval_ablation.py` as well as `-m` / package imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.rag_results import record_run

LABELS_PATH = Path("labels/train_labels.json")
TICKETS_DIR = Path("tickets/train")
QUERY_URL = "http://localhost:8002"
SOURCE_RE = re.compile(r"--- Document Source: (.+?) ---")


def basename(doc_path: str) -> str:
    return doc_path.split("/")[-1]


def build_query(ticket_id: str) -> str:
    """Same query the live agent's forced initial search uses -- subject +
    description from the raw ticket, not any ground-truth solution text."""
    ticket_file = TICKETS_DIR / f"{ticket_id}.json"
    if not ticket_file.exists():
        return ticket_id
    ticket = json.loads(ticket_file.read_text())
    return (ticket.get("subject", "") + " " + ticket.get("description", ""))[:500]


def query_once(query: str, n_results: int, rerank: bool) -> list[str]:
    res = requests.post(
        f"{QUERY_URL}/query",
        json={"query": query, "n_results": n_results, "rerank": rerank},
        timeout=60,
    )
    res.raise_for_status()
    results_text = res.json().get("results", "")
    return list(dict.fromkeys(SOURCE_RE.findall(results_text)))


def evaluate_condition(labels: list, n_results: int, rerank: bool):
    hits, recalls, rows = 0, [], []
    total = 0

    for label in labels:
        relevant_docs = label.get("relevant_kb_docs", [])
        relevant = {basename(d) for d in relevant_docs}
        if not relevant:
            continue

        query_text = build_query(label["ticket_id"])

        try:
            retrieved = query_once(query_text, n_results=n_results, rerank=rerank)
        except Exception as e:
            rows.append({"ticket_id": label["ticket_id"], "error": str(e)})
            continue

        total += 1
        hit = bool(relevant & set(retrieved))
        recall = len(relevant & set(retrieved)) / len(relevant)
        hits += int(hit)
        recalls.append(recall)

        rows.append({
            "ticket_id": label["ticket_id"],
            "relevant_docs": sorted(relevant),
            "retrieved_docs": retrieved,
            "hit": hit,
            "recall": recall,
        })

    metrics = {
        "total_tickets": total,
        "retrieval_total": total,
        "retrieval_hit_rate": hits / total if total else 0,
        "retrieval_recall_avg": sum(recalls) / len(recalls) if recalls else 0,
        "errors": sum(1 for r in rows if "error" in r),
    }
    return metrics, rows


def run_ablation(limit: int = 0, n_results: int = 5):
    labels = json.loads(LABELS_PATH.read_text())
    if limit > 0:
        labels = labels[:limit]

    reranked_metrics, reranked_rows = evaluate_condition(labels, n_results, rerank=True)
    baseline_metrics, baseline_rows = evaluate_condition(labels, n_results, rerank=False)

    record_run(eval_type="retrieval_ablation", metrics=reranked_metrics, details=reranked_rows, reranker_enabled=True)
    record_run(eval_type="retrieval_ablation", metrics=baseline_metrics, details=baseline_rows, reranker_enabled=False)

    return reranked_metrics, baseline_metrics


def main():
    parser = argparse.ArgumentParser(description="Compare retrieval accuracy with vs. without cross-encoder reranking.")
    parser.add_argument("--limit", type=int, default=0, help="Number of labeled tickets to test (0 = all).")
    parser.add_argument("--n-results", type=int, default=5, help="Top-k returned per query.")
    args = parser.parse_args()

    reranked, baseline = run_ablation(limit=args.limit, n_results=args.n_results)

    print("\nWith cross-encoder reranking:")
    print(json.dumps(reranked, indent=2))
    print("\nWithout reranking (raw embedding-similarity order):")
    print(json.dumps(baseline, indent=2))
    print(f"\nHit-rate delta:  {(reranked['retrieval_hit_rate'] - baseline['retrieval_hit_rate']) * 100:+.1f}pp")
    print(f"Recall@k delta:  {(reranked['retrieval_recall_avg'] - baseline['retrieval_recall_avg']) * 100:+.1f}pp")


if __name__ == "__main__":
    main()
