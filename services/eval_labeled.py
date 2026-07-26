"""
Evaluate the agent's routing decisions and retrieval quality against the
challenge's answer key, labels/train_labels.json.

GROUND TRUTH: labels/train_labels.json. It ships in the challenge data
(present in the initial commit), lives in a directory named labels/, and
carries evaluator-facing fields -- correct_routing, should_escalate,
escalation_reason, correct_resolution_summary, expected_agent_action,
chaos_correct_behavior, evaluator_notes. That is a grading rubric.

  - routing_accuracy is measured against correct_routing.
  - retrieval hit_rate / recall@k are measured against relevant_kb_docs.

NOT ground truth: tickets/train_index.csv's escalation_flag. That column is a
copy of a field carried INSIDE each ticket JSON -- it is an input the ticket
arrives with (what the store or an upstream automation marked), not a
statement of what the agent should do. It agrees with correct_routing only
50.5% of the time, which is exactly what you would expect from "what was
marked" versus "what was correct".

An earlier version of this file graded routing against escalation_flag and
described train_labels.json as an unverified LLM-generated aid. That was
wrong, and it was costly: grading against an input feature made routing look
statistically unlearnable (it is not -- correct_routing is predictable from
ticket text at ROC-AUC 0.914), and a deterministic override was then added to
suppress escalations, driving the escalation rate to 0% against a key that
says ~69% of tickets should escalate. escalation_flag is still reported as a
secondary diagnostic, clearly labelled, because the gap between "what was
marked" and "what was correct" is itself interesting -- but it is not the
score.

The flag is withheld from the agent before triage (see strip_escalation_flag)
so the triage step must reason from the ticket text rather than short-circuit
on an input field.

Calls the already-running api_service (POST /triage, POST /resolve) so it
exercises the real orchestration + retrieval path, not a mocked one.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import pandas as pd
import requests

# Allow `python3 services/eval_labeled.py` as well as `-m` / package imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.eval_latency import LATENCY_RUNS_DIR, extract_timing, summarize_latency
from services.eval_quality import score_ticket_quality, summarize_quality

TRAIN_INDEX_CSV = Path("tickets/train_index.csv")
LABELS_PATH = Path("labels/train_labels.json")
TICKETS_DIR = Path("tickets/train")
API_URL = "http://localhost:8000"
SOURCE_RE = re.compile(r"--- Document Source: (.+?) ---")


def basename(doc_path: str) -> str:
    return doc_path.split("/")[-1]


def extract_retrieved_sources(trace: list) -> list[str]:
    sources = []
    for step in trace:
        if step.get("type") == "tool_result":
            sources.extend(SOURCE_RE.findall(step.get("result", "")))
    # Basename immediately: get_system_spec labels its source
    # "system-specs/X.md" while regular KB search results label theirs just
    # "X.md" for the same file. Comparing raw strings against the (already
    # basenamed) expected list silently counted every correctly-retrieved
    # spec sheet as a miss -- this alone was suppressing recall@k by ~15
    # points with no actual change in retrieval quality.
    sources = [basename(s) for s in sources]
    # de-dupe, preserve order
    seen = set()
    ordered = []
    for s in sources:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


def strip_escalation_flag(ticket_json: str) -> str:
    """Remove the ITSM escalation_flag before sending the ticket to /triage.

    escalation_flag IS the routing ground truth and it ships inside the
    ticket. If the agent were allowed to see it, "routing accuracy" would
    just be checking that a value equals itself -- run_triage_agent reads
    the flag and echoes back the matching label with no reasoning involved,
    so honoring it (correct production behavior) trivially scores ~100% and
    tells you nothing about the agent's judgment. Always stripping it here
    measures the agent's *independent* triage judgment on the ticket text
    alone -- the only version of this number that can ever be wrong, and so
    the only one worth tracking. Baseline for context: always-predict-L1
    scores 74.2% on this set.
    """
    try:
        t = json.loads(ticket_json)
        t.pop("escalation_flag", None)
        return json.dumps(t)
    except Exception:
        return ticket_json


def evaluate(limit: int = 0, sleep_between: float = 0.0):
    index_df = pd.read_csv(TRAIN_INDEX_CSV)
    if limit > 0:
        index_df = index_df.head(limit)

    # Retrieval ground truth (relevant_kb_docs) is only available from the
    # LLM-generated labels -- used for retrieval eval only, never routing.
    labels_by_id = {x["ticket_id"]: x for x in json.loads(LABELS_PATH.read_text())}

    routing_correct = 0
    routing_total = 0
    retrieval_hits = 0
    retrieval_recalls = []
    retrieval_total = 0
    errors = 0
    rows = []

    for _, index_row in index_df.iterrows():
        ticket_id = index_row["ticket_id"]
        ticket_file = TICKETS_DIR / f"{ticket_id}.json"
        if not ticket_file.exists():
            continue
        label = labels_by_id.get(ticket_id)
        if not label:
            # No answer key for this ticket -- it cannot be scored, so skip it
            # rather than silently grading against a default.
            continue

        ticket_json = ticket_file.read_text()
        triage_input = strip_escalation_flag(ticket_json)
        expected_escalate = str(label.get("correct_routing", "")).strip().lower() == "l2_escalation"
        # Secondary diagnostic only -- what the ticket arrived marked as.
        marked_escalate = bool(index_row["escalation_flag"])
        relevant_docs = {basename(d) for d in label.get("relevant_kb_docs", [])}

        row = {"ticket_id": ticket_id, "expected_escalate": expected_escalate,
               "expected_routing": label.get("correct_routing"),
               "ticket_escalation_flag": marked_escalate}
        try:
            triage_res = requests.post(f"{API_URL}/triage", json={"ticket_json": triage_input}, timeout=300).json()
            actual_routing = triage_res.get("routing", "ERROR").strip().lower()
            actual_escalate = actual_routing == "l2_escalation"
            routing_total += 1
            row["actual_routing"] = actual_routing
            row["routing_correct"] = actual_escalate == expected_escalate
            row["matches_ticket_flag"] = actual_escalate == marked_escalate
            if row["routing_correct"]:
                routing_correct += 1

            resolve_res = None
            if actual_routing == "l1_guided":
                resolve_res = requests.post(
                    f"{API_URL}/resolve", json={"ticket_json": ticket_json}, timeout=600
                ).json()
                trace = resolve_res.get("trace", [])
                retrieved = extract_retrieved_sources(trace)
                row["retrieved_docs"] = retrieved

                # Groundedness + guardrails: unlike routing accuracy (whose
                # label was measured to be statistically unlearnable), these
                # grade what the agent actually controls -- whether the
                # specifics it tells a store to act on came from a retrieved
                # runbook, and whether its own guardrails held.
                row.update(score_ticket_quality(resolve_res.get("final_response", ""), trace, ticket_json))

                if relevant_docs:
                    retrieval_total += 1
                    hit = bool(relevant_docs & set(retrieved))
                    recall = len(relevant_docs & set(retrieved)) / len(relevant_docs)
                    row["retrieval_hit"] = hit
                    row["retrieval_recall"] = recall
                    retrieval_hits += int(hit)
                    retrieval_recalls.append(recall)

            # Free latency data: /triage and (when L1_GUIDED) /resolve were
            # just called for accuracy anyway, and both already carry
            # per-step duration_s -- extract_timing reads that instead of
            # re-triaging/re-resolving every ticket a second time just to
            # time it, which is what running the separate latency eval
            # after this one would otherwise do.
            timing_fields, _ = extract_timing(triage_res, resolve_res)
            row.update(timing_fields)

        except Exception as e:
            errors += 1
            row["error"] = str(e)

        rows.append(row)
        if sleep_between:
            import time

            time.sleep(sleep_between)

    scored = [r for r in rows if "routing_correct" in r]
    predicted_escalate = sum(1 for r in scored if r.get("actual_routing") == "l2_escalation")
    expected_escalate_n = sum(1 for r in scored if r.get("expected_escalate"))
    matches_flag = sum(1 for r in scored if r.get("matches_ticket_flag"))

    metrics = {
        "total_tickets": len(index_df),
        "routing_mode": "reasoning_only (escalation_flag withheld from agent)",
        "routing_ground_truth": "labels/train_labels.json:correct_routing (challenge answer key)",
        "retrieval_ground_truth": "labels/train_labels.json:relevant_kb_docs (challenge answer key)",
        "routing_total": routing_total,
        "routing_correct": routing_correct,
        "routing_accuracy": routing_correct / routing_total if routing_total else 0,
        # Escalation behaviour is the thing that actually goes wrong, so surface
        # it rather than leaving it buried in the per-ticket CSV.
        "escalation_rate_predicted": predicted_escalate / len(scored) if scored else 0,
        "escalation_rate_expected": expected_escalate_n / len(scored) if scored else 0,
        # Diagnostic, NOT a score: how often the agent's decision coincides with
        # the flag the ticket arrived carrying. Reported because the divergence
        # between "what was marked" and "what was correct" is informative.
        "agreement_with_ticket_escalation_flag": matches_flag / len(scored) if scored else 0,
        "retrieval_total": retrieval_total,
        "retrieval_hit_rate": retrieval_hits / retrieval_total if retrieval_total else 0,
        "retrieval_recall_avg": sum(retrieval_recalls) / len(retrieval_recalls) if retrieval_recalls else 0,
        "errors": errors,
    }

    # Latency comes free from the same /triage + /resolve calls already made
    # above for accuracy -- no extra requests. Summarized here so "run
    # evaluation" answers both "is it right" and "is it fast" in one pass.
    rows_df = pd.DataFrame(rows)
    latency_stats = summarize_latency(rows_df)
    if latency_stats:
        metrics["latency"] = latency_stats

    quality_stats = summarize_quality([r for r in rows if "groundedness" in r])
    if quality_stats:
        metrics["quality"] = quality_stats

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    eval_dir = Path("data/eval_runs")
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / f"labeled_eval_{timestamp}_metrics.json").write_text(json.dumps(metrics, indent=4))
    rows_df.to_csv(eval_dir / f"labeled_eval_{timestamp}_details.csv", index=False)

    # Also archive into the shared latency history, so latency trends over
    # time include runs measured here -- otherwise the Latency tab's history
    # would only ever show standalone eval_latency runs and silently miss
    # every measurement taken during an accuracy run.
    if latency_stats:
        LATENCY_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        (LATENCY_RUNS_DIR / f"latency_{timestamp}_metrics.json").write_text(
            json.dumps({"source": "eval_labeled", **latency_stats}, indent=4)
        )
        rows_df.to_csv(LATENCY_RUNS_DIR / f"latency_{timestamp}_result.csv", index=False)

    from services.rag_results import record_run
    record_run(
        eval_type="agent_eval_reasoning_only",
        metrics=metrics, details=rows, reranker_enabled=True,
    )

    return metrics, rows


def main():
    parser = argparse.ArgumentParser(description="Evaluate routing accuracy (vs. official escalation_flag, escalation_flag withheld) and retrieval quality (vs. LLM-generated labels).")
    parser.add_argument("--limit", type=int, default=30, help="Number of tickets to evaluate from train_index.csv (0 = all 256).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between tickets (rate limiting).")
    args = parser.parse_args()

    metrics, _ = evaluate(limit=args.limit, sleep_between=args.sleep)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
