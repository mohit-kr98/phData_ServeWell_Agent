"""
Groundedness and guardrail metrics -- the two things that DO carry signal.

Motivation: routing accuracy answers "did it go to the right queue", which
says nothing about whether the guidance the store actually receives is
correct. These metrics measure the part a store employee has to act on.

(Historical note: an earlier version of this file claimed routing was
statistically unlearnable at a ~74.2% ceiling. That analysis graded against
tickets/train_index.csv:escalation_flag, which is an INPUT field carried
inside each ticket rather than the answer key. Against the real key,
labels/train_labels.json:correct_routing, routing is very much learnable --
ROC-AUC 0.914 -- and measured accuracy moved from 31% to ~79% once the target
was corrected. Retained here as a caution: check what a metric is graded
against before concluding anything is impossible.)

Deliberately deterministic -- no LLM-as-judge. The only model this account
can reach (nvidia.nemotron-nano-3-30b) was shown during routing work to be
unreliable at exactly this kind of nuanced "is X supported by Y" judgment,
so grading with it would add noise, cost, and latency while being less
trustworthy than a rule you can read. Everything here is inspectable and
reproducible.

The headline signal is UNSUPPORTED SPECIFICS: concrete, actionable details
in the agent's reply (menu paths, durations, error codes, PINs) that do not
appear anywhere in the retrieved context. A vague-but-wrong sentence wastes
a little time; a confidently wrong "Settings > Admin, PIN 7890" sends a
store down a path that does not exist. Those are the failures worth
catching.
"""
import re

SOURCE_RE = re.compile(r"--- Document Source: (.+?) ---")
RETRIEVAL_TOOLS = ("search_knowledge_base", "search_faq", "get_system_spec")

# A UI/navigation path: "Settings > Admin", "Integration > Connected Stores".
# Segments are Title-Case UI labels, which keeps surrounding prose ("go to
# X > Y and verify") out of the captured claim -- otherwise the filler words
# get treated as part of the path and it can never match the source doc.
_SEG = r"[A-Z][\w\-]*(?:[ ][A-Z0-9][\w\-]*){0,3}"
NAV_PATH_RE = re.compile(rf"{_SEG}(?:\s*[>→]\s*{_SEG})+")
# Durations / quantities that a person will act on: "30 s", "2 minutes", "90-120 seconds"
QUANTITY_RE = re.compile(r"\b(\d{1,4})\s*(?:-|–|to)?\s*(\d{1,4})?\s*(seconds?|secs?|s|minutes?|mins?|hours?|hrs?|%)\b", re.I)
# Error / status codes: ERR-P502, F01, E04, SC-500
CODE_RE = re.compile(r"\b(?:[A-Z]{1,4}-?\d{2,4}[A-Z]?|[A-Z]{2,4}-[A-Z0-9]{2,6})\b")
# Explicit PIN / passcode values
PIN_RE = re.compile(r"\b(?:PIN|passcode|code)\b[^.\n]{0,20}?(\d{3,8})", re.I)


def extract_context(trace: list, ticket_json: str = "") -> str:
    """Everything the agent legitimately had in front of it: retrieved
    document text PLUS the ticket itself.

    The ticket must count as grounding. Asset IDs, store IDs and system
    versions come from the ticket, not the runbooks -- an agent saying
    "restart PRN-0096" for a ticket whose asset_id is PRN-0096-P1 is
    perfectly grounded. Checking only retrieved docs flagged four such
    echoes as hallucinations in a 60-ticket run (RTR-0044, PRN-0096,
    RTR-0031, POS-0066), i.e. 4 of 5 reported failures were false alarms.
    """
    parts = []
    for step in trace or []:
        if step.get("type") == "tool_result" and step.get("name") in RETRIEVAL_TOOLS:
            parts.append(str(step.get("result", "")))
    if ticket_json:
        parts.append(str(ticket_json))
    return "\n".join(parts)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def extract_specifics(text: str) -> list[dict]:
    """Concrete, checkable claims in the reply. Each is something a store
    employee would literally act on, so each is worth verifying."""
    found = []
    for m in NAV_PATH_RE.finditer(text or ""):
        raw = m.group(0)
        if ">" in raw or "→" in raw:
            found.append({"kind": "nav_path", "value": raw.strip()})
    for m in QUANTITY_RE.finditer(text or ""):
        found.append({"kind": "quantity", "value": m.group(0).strip()})
    for m in CODE_RE.finditer(text or ""):
        found.append({"kind": "code", "value": m.group(0).strip()})
    for m in PIN_RE.finditer(text or ""):
        found.append({"kind": "pin", "value": m.group(1).strip()})
    # de-dupe on normalized value, keep first occurrence
    seen, out = set(), []
    for f in found:
        key = (f["kind"], _norm(f["value"]))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _is_supported(spec: dict, context_norm: str) -> bool:
    """Is this specific claim present in the retrieved context?"""
    val = _norm(spec["value"])
    if spec["kind"] == "nav_path":
        # Compare segment-wise: the agent may reformat separators/casing, but
        # every segment of the path must appear, in order, in the context.
        segs = [s.strip() for s in re.split(r"[>→]", val) if s.strip()]
        if not segs:
            return False
        # A sentence-leading imperative verb can get absorbed into the first
        # segment ("Enter Settings > Admin"), so allow it to match on a
        # suffix. Only the first segment -- later ones must match in full or
        # we would start accepting genuinely wrong paths.
        first_variants = [segs[0]]
        words = segs[0].split()
        first_variants += [" ".join(words[i:]) for i in range(1, len(words))]

        for first in first_variants:
            pos = context_norm.find(first)
            if pos == -1:
                continue
            pos += len(first)
            matched_rest = True
            for seg in segs[1:]:
                i = context_norm.find(seg, pos)
                if i == -1:
                    matched_rest = False
                    break
                pos = i + len(seg)
            if matched_rest:
                return True
        return False
    if spec["kind"] == "quantity":
        # The number is what matters; unit wording varies ("30 s" vs "30 seconds").
        nums = re.findall(r"\d{1,4}", val)
        return all(re.search(rf"\b{n}\b", context_norm) for n in nums) if nums else False
    return val in context_norm


def score_groundedness(final_response: str, trace: list, ticket_json: str = "") -> dict:
    """Fraction of the reply's concrete specifics that appear in the context
    the agent actually had (retrieved chunks + the ticket).

    Note on interpretation: a flagged specific means "not present in what the
    agent retrieved or was given". It may still exist elsewhere in the KB in a
    chunk that wasn't retrieved -- that is still worth flagging, since the
    agent asserted it without support in front of it, but it is a softer
    finding than a value that appears nowhere in the corpus.
    """
    context = extract_context(trace, ticket_json)
    context_norm = _norm(context)
    specifics = extract_specifics(final_response or "")

    supported, unsupported = [], []
    for s in specifics:
        (supported if _is_supported(s, context_norm) else unsupported).append(s)

    total = len(specifics)
    return {
        "retrieved_sources": sorted(set(SOURCE_RE.findall(context))),
        "n_specifics": total,
        "n_specifics_grounded": len(supported),
        # None (not 0.0) when the reply made no checkable claims -- averaging a
        # 0 there would punish a correctly-cautious reply that only asked a
        # clarifying question.
        "groundedness": round(len(supported) / total, 3) if total else None,
        "unsupported_specifics": [f"{s['kind']}:{s['value']}" for s in unsupported],
        "had_context": bool(context.strip()),
    }


def check_guardrails(trace: list) -> list[dict]:
    """Independent re-check of the guardrails the system prompt claims to
    enforce, computed from the trace rather than trusting the agent's
    narration. Returns structured results so both the UI and the batch eval
    can consume one implementation."""
    trace = trace or []
    search_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") == "search_knowledge_base"]
    search_results = [s for s in trace if s.get("type") == "tool_result" and s.get("name") == "search_knowledge_base"]
    action_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") in ("reply_to_user", "resolve_ticket", "escalate_to_l2")]
    reply_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") == "reply_to_user"]
    escalate_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") == "escalate_to_l2"]

    checks = []

    if action_calls and search_calls:
        ok = trace.index(search_calls[0]) < trace.index(action_calls[0])
    else:
        ok = bool(search_calls)
    checks.append({
        "id": "searched_before_acting", "passed": ok,
        "message": "Searched the knowledge base before taking any action" if ok
                   else "No knowledge-base search occurred before acting — grounding guardrail bypassed",
    })

    # Budget is 2 forced searches (main + subcategory) plus at most 1 of the
    # agent's own. Count only searches that actually EXECUTED: when the agent
    # exceeds the budget the runtime refuses the call but the attempt is still
    # recorded in the trace as a tool_call, so counting raw calls would score a
    # correctly-enforced cap as a violation -- exactly backwards.
    blocked = sum(1 for s in search_results if str(s.get("result", "")).startswith("Search limit reached"))
    executed = len(search_results) - blocked
    # Read the budget the agent actually enforced rather than keeping a second
    # copy here. The duplicated constant drifted twice and both times graded
    # correct behaviour as a violation; the trace is the single source of truth.
    budget = next((s.get("max_kb_calls") for s in trace
                   if s.get("type") == "policy" and s.get("name") == "search_budget"), None)
    if budget is None:
        budget = 3  # traces predating the policy entry
    cap_ok = executed <= budget
    detail = f"{executed} executed" + (f", {blocked} blocked by the cap" if blocked else "")
    checks.append({
        "id": "search_cap_respected", "passed": cap_ok,
        "message": f"search_knowledge_base: {detail} (budget {budget})" if cap_ok
                   else f"search_knowledge_base executed {executed} times — exceeds the budget of {budget}",
    })

    if escalate_calls:
        empty = sum(1 for s in search_results if "No relevant documents found" in s.get("result", ""))
        ok = empty >= 2 or len(reply_calls) >= 2
        checks.append({
            "id": "escalation_justified", "passed": ok,
            "message": (f"Escalated after {empty} empty search(es)" if empty >= 2
                        else f"Escalated after {len(reply_calls)} troubleshooting attempt(s)") if ok
                       else "Escalated without 2 empty searches or 2 troubleshooting replies — escalation guardrail may have been bypassed",
        })

    return checks


def score_ticket_quality(final_response: str, trace: list, ticket_json: str = "") -> dict:
    """Flat, CSV-friendly per-ticket quality row."""
    g = score_groundedness(final_response, trace, ticket_json)
    checks = check_guardrails(trace)
    failed = [c["id"] for c in checks if not c["passed"]]
    return {
        **{k: v for k, v in g.items() if k != "retrieved_sources"},
        "unsupported_specifics": "; ".join(g["unsupported_specifics"]),
        "guardrails_checked": len(checks),
        "guardrails_failed": len(failed),
        "guardrails_failed_ids": "; ".join(failed),
        "guardrails_all_passed": not failed,
    }


def summarize_quality(rows: list[dict]) -> dict:
    """Aggregate per-ticket quality rows into headline metrics."""
    scored = [r for r in rows if r.get("groundedness") is not None]
    with_gr = [r for r in rows if r.get("guardrails_checked")]
    if not rows:
        return {}
    out = {
        "tickets_with_checkable_claims": len(scored),
        "tickets_with_no_checkable_claims": len(rows) - len(scored),
    }
    if scored:
        out["groundedness_avg"] = round(sum(r["groundedness"] for r in scored) / len(scored), 3)
        out["fully_grounded_rate"] = round(sum(1 for r in scored if r["groundedness"] == 1.0) / len(scored), 3)
        tot = sum(r.get("n_specifics", 0) for r in scored)
        ok = sum(r.get("n_specifics_grounded", 0) for r in scored)
        out["specifics_total"] = tot
        out["specifics_unsupported"] = tot - ok
        out["unsupported_specific_rate"] = round((tot - ok) / tot, 3) if tot else 0.0
    if with_gr:
        out["guardrail_pass_rate"] = round(sum(1 for r in with_gr if r["guardrails_all_passed"]) / len(with_gr), 3)
        failed = {}
        for r in with_gr:
            for fid in (r.get("guardrails_failed_ids") or "").split("; "):
                if fid:
                    failed[fid] = failed.get(fid, 0) + 1
        out["guardrail_failures_by_check"] = failed
    return out
