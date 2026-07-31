import os
import re
import json
import time
import datetime
from pathlib import Path

import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv()

API_URL = os.environ.get("API_URL", "http://localhost:8000")
INGESTION_URL = os.environ.get("INGESTION_URL", "http://localhost:8001")
QUERY_URL = os.environ.get("QUERY_URL", "http://localhost:8002")
ADMIN_URL = os.environ.get("ADMIN_URL", "http://localhost:8003")

PROJECT_ROOT = Path(__file__).parent.absolute()
TICKETS_DIR = PROJECT_ROOT / "tickets" / "train"
EVAL_RUNS_DIR = PROJECT_ROOT / "data" / "eval_runs"
LATENCY_RESULT_CSV = PROJECT_ROOT / "RAG_Results" / "Latency_result.csv"
LATENCY_DETAILS_CSV = PROJECT_ROOT / "RAG_Results" / "Latency_details.csv"
ARCH_DIAGRAM_PATH = PROJECT_ROOT / "design_md" / "system_architecture.html"
LABELS_PATH = PROJECT_ROOT / "labels" / "train_labels.json"
TRAIN_INDEX_CSV = PROJECT_ROOT / "tickets" / "train_index.csv"

ROUTING_STYLE = {
    "L1_GUIDED": ("l1", "L1 — Guided Resolution"),
    "L2_ESCALATION": ("l2", "L2 — Escalation"),
    "NON_IT": ("nonit", "Non-IT — Routed Out"),
    "ERROR": ("l2", "Error"),
}


def to_container_path(host_path: str) -> str:
    """FastAPI services run in Docker with the repo mounted at /app."""
    p = str(Path(host_path).absolute())
    if p.startswith(str(PROJECT_ROOT)):
        return p.replace(str(PROJECT_ROOT), "/app", 1)
    return p


def check_health(url: str) -> bool:
    try:
        return requests.get(f"{url}/docs", timeout=2).status_code == 200
    except Exception:
        return False


def timed_post(url: str, payload: dict, timeout: int):
    """POST and return (response_json, elapsed_seconds). Raises on HTTP/network errors."""
    start = time.perf_counter()
    res = requests.post(url, json=payload, timeout=timeout)
    elapsed = time.perf_counter() - start
    res.raise_for_status()
    return res.json(), elapsed


def list_sample_tickets():
    if not TICKETS_DIR.exists():
        return []
    return sorted({f.stem for f in TICKETS_DIR.glob("*.json")})


@st.cache_data
def load_labels() -> dict:
    """The challenge answer key (correct_routing, relevant_kb_docs,
    should_escalate, evaluator_notes, ...). Shipped with the challenge data
    and used as ground truth for both routing and retrieval."""
    if not LABELS_PATH.exists():
        return {}
    return {x["ticket_id"]: x for x in json.loads(LABELS_PATH.read_text())}


@st.cache_data
def load_train_index() -> dict:
    """The escalation_flag each ticket ARRIVES carrying -- an input field, not
    the answer. Agrees with the answer key's correct_routing only 50.5% of the
    time; shown as a diagnostic so the divergence stays visible."""
    if not TRAIN_INDEX_CSV.exists():
        return {}
    df = pd.read_csv(TRAIN_INDEX_CSV)
    return {row["ticket_id"]: bool(row["escalation_flag"]) for _, row in df.iterrows()}


def extract_sources(trace: list) -> list[str]:
    sources = []
    for step in trace:
        if step.get("type") == "tool_result":
            sources.extend(re.findall(r"--- Document Source: (.+?) ---", step.get("result", "")))
    return list(dict.fromkeys(sources))


def read_ticket_json(ticket_id: str) -> str:
    return (TICKETS_DIR / f"{ticket_id}.json").read_text()


def build_latency_rows(state: dict) -> list[dict]:
    """Component-level timing: each retrieval call and each LLM call the
    backend measured, from agent_core/llm_client.py's per-step instrumentation."""
    rows = []
    for t in state.get("triage_timing", []):
        rows.append({"Step": t["step"], "Type": "LLM", "Duration (s)": round(t["duration_s"], 2), "Size (chars)": ""})
    for step in state.get("trace", []):
        if step.get("type") == "tool_result" and "duration_s" in step:
            rows.append({
                "Step": step["name"], "Type": "Retrieval/Tool",
                "Duration (s)": round(step["duration_s"], 2),
                "Size (chars)": step.get("result_chars", ""),
            })
        elif step.get("type") == "llm_call":
            rows.append({
                "Step": f"LLM reasoning (turn {step['loop']})", "Type": "LLM",
                "Duration (s)": round(step["duration_s"], 2),
                "Size (chars)": step.get("prompt_chars", ""),
            })
    return rows


def build_demo_ticket(subject, description, priority, category, subcategory, store_id, asset_id):
    ticket_id = "DEMO-" + datetime.datetime.now().strftime("%H%M%S")
    ticket = {
        "ticket_id": ticket_id,
        "store_id": store_id or "SW-DEMO",
        "priority": priority,
        "category": category or "General",
        "subcategory": subcategory or "",
        "subject": subject,
        "description": description,
        "asset_id": asset_id or "",
        "escalation_flag": False,
        "tags": [],
    }
    return ticket_id, json.dumps(ticket, indent=2)


def classify_status(final_resp: str) -> str:
    """Determine outcome from the agent's final response text, checked in
    priority order so a failure mode never gets mistaken for a success."""
    if final_resp == "Error" or final_resp.startswith("Error"):
        return "escalated"
    if "reached maximum iterations" in final_resp.lower():
        return "escalated"
    if "escalated" in final_resp.lower():
        return "escalated"
    if "?" in final_resp or "let me know" in final_resp.lower():
        return "awaiting_user"
    return "resolved"


def analyze_guardrails(trace):
    """Rule-based, transparent re-check of the guardrails the agent's system
    prompt claims to enforce — computed independently from the trace so the
    UI isn't just trusting the agent's own narration.

    Thin adapter over services.eval_quality.check_guardrails so the UI and the
    batch evaluation grade against one implementation instead of two that can
    silently drift apart."""
    from services.eval_quality import check_guardrails
    return [(c["passed"], c["message"]) for c in check_guardrails(trace)]


def render_trace(trace):
    for step in trace:
        step_type = step.get("type")
        if step_type == "tool_call":
            name = step.get("name")
            st.markdown(f"**`{name}`**")
            args = {k: v for k, v in step.get("args", {}).items() if k != "message"}
            if args:
                st.caption(", ".join(f"{k}={v}" for k, v in args.items()))
        elif step_type == "tool_result":
            name = step.get("name")
            result = step.get("result", "")
            if name in ("search_knowledge_base", "search_faq", "get_system_spec"):
                sources = list(dict.fromkeys(re.findall(r"--- Document Source: (.+?) ---", result)))
                if sources:
                    st.markdown("&nbsp;&nbsp;&nbsp;&nbsp;Retrieved: " + " · ".join(f"`{s}`" for s in sources))
                    with st.expander("View retrieved content", expanded=False):
                        st.text(result)
                else:
                    st.markdown("&nbsp;&nbsp;&nbsp;&nbsp;No relevant documents found")
            else:
                with st.expander(f"Result of `{name}`", expanded=False):
                    st.code(result)
        elif step_type == "reasoning":
            text = (step.get("text") or "").strip()
            if text:
                st.markdown(f"> {text}")


def build_escalation_context(state: dict) -> str:
    """Summarize how/why a ticket reached L2, for the copilot's context --
    covers both a direct triage escalation and one the L1 resolution agent
    made mid-conversation after attempting to help."""
    parts = [f"Triage reasoning: {state.get('reasoning', '')}"]
    if state.get("routing") == "L1_GUIDED" and state.get("chat_history"):
        parts.append("L1 attempted guided resolution before this was escalated. L1 conversation transcript:")
        for msg in state["chat_history"]:
            role = "Store employee" if msg["role"] == "user" else "L1 Agent"
            parts.append(f"{role}: {msg['content']}")
    return "\n".join(parts)


def render_approval_gate(state: dict, ticket_id: str):
    """Human-in-the-loop gate for a remediation action the agent proposed.

    Nothing has executed at this point. The agent can only propose; the tool
    that actually runs an action refuses without approved=True, and that check
    lives in agent_core/actions.py rather than in this screen — so declining
    here is not the only thing standing between a proposal and a device."""
    proposal = state.get("proposed_action")
    if not proposal:
        return

    outcome = state.get("action_outcome")
    st.markdown("##### Action proposed — human approval required")

    if outcome:
        if outcome.get("status") == "EXECUTED":
            st.success(f"Executed: {outcome.get('label')} on `{outcome.get('asset_id')}` "
                       f"(approved by {outcome.get('approved_by')})")
            st.caption(outcome.get("note", ""))
        elif outcome.get("status") == "DENIED":
            st.info("Denied by reviewer — nothing ran. The store still has the manual steps above.")
        else:
            st.warning(f"Refused: {outcome.get('reason', outcome)}")
        return

    st.warning(f"**{proposal.get('label')}** on `{proposal.get('asset_id')}`")
    st.caption(f"Agent's reason: {proposal.get('reason')}")
    st.caption("The agent proposed this; it has not run. Approving executes a simulated "
               "device-management call and writes an audit record.")

    col_a, col_d = st.columns([1, 1])
    with col_a:
        if st.button("Approve & run", type="primary", key=f"approve_{ticket_id}"):
            try:
                res, _ = timed_post(f"{API_URL}/execute_action", {
                    "action": proposal.get("action"), "asset_id": proposal.get("asset_id"),
                    "ticket_id": ticket_id, "approved": True, "approved_by": "demo-reviewer",
                }, timeout=30)
                state["action_outcome"] = res
            except Exception as e:
                st.error(str(e))
            st.rerun()
    with col_d:
        if st.button("Deny", key=f"deny_{ticket_id}"):
            state["action_outcome"] = {"status": "DENIED"}
            st.rerun()


def render_l2_copilot(state: dict, ticket_id: str, ticket_json: str):
    st.markdown("##### L2 Engineer Copilot")
    st.caption("Chat with an assistant sharing L1's knowledge-base/asset/store/SLA tools — for the "
               "human L2 engineer investigating this ticket, not the store.")

    if "l2_chat_history" not in state:
        state["l2_chat_history"] = []
    if "l2_trace" not in state:
        state["l2_trace"] = []

    for msg in state["l2_chat_history"]:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("elapsed") is not None:
                st.caption(f"{msg['elapsed']:.1f}s")

    engineer_msg = st.chat_input("Ask the copilot (as the L2 engineer)...", key=f"l2_chat_{ticket_id}")
    if engineer_msg:
        state["l2_chat_history"].append({"role": "user", "content": engineer_msg})
        with st.spinner("Copilot is investigating..."):
            try:
                payload = {
                    "ticket_json": ticket_json,
                    "escalation_context": build_escalation_context(state),
                    "chat_history": state["l2_chat_history"],
                }
                result, elapsed = timed_post(f"{API_URL}/l2_copilot", payload, timeout=180)
                final_resp = result.get("final_response", "Error")
                state["l2_chat_history"].append({"role": "assistant", "content": final_resp, "elapsed": elapsed})
                state["l2_trace"].extend(result.get("trace", []))
            except Exception as e:
                st.error(str(e))
        st.rerun()

    if state["l2_trace"]:
        st.markdown("##### Copilot trace")
        render_trace(state["l2_trace"])


st.set_page_config(page_title="ServeWell IT Support Agent", layout="wide")

st.markdown("""
<style>
.pill { display:inline-block; padding:4px 12px; border-radius:999px; font-weight:600; font-size:0.85rem; }
.pill-l1 { background:#E1F3E7; color:#1E8F5F; }
.pill-l2 { background:#F8E9DC; color:#C0632A; }
.pill-nonit { background:#E9E7F1; color:#6E6A86; }
.health-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }
.health-up { background:#1E8F5F; } .health-down { background:#B23B32; }
</style>
""", unsafe_allow_html=True)

st.title("ServeWell Agentic IT Support")
st.caption("Triage + Resolution agents over a reranked RAG pipeline, with grounding and escalation guardrails.")

health = {
    "API": check_health(API_URL),
    "Ingestion": check_health(INGESTION_URL),
    "Query": check_health(QUERY_URL),
    "Admin": check_health(ADMIN_URL),
}
st.markdown(" &nbsp;·&nbsp; ".join(
    f'<span class="health-dot {"health-up" if ok else "health-down"}"></span>{name}'
    for name, ok in health.items()
), unsafe_allow_html=True)

tab_demo, tab_eval, tab_latency, tab_arch, tab_admin = st.tabs(["Live Demo", "Evaluation", "Latency", "Architecture & KB", "Admin"])

# ============================================================ LIVE DEMO ====
with tab_demo:
    st.subheader("Run a ticket through the agent")
    source = st.radio("Ticket source", ["Sample ticket", "New ticket (type your own)"], horizontal=True, label_visibility="collapsed")

    ticket_id, ticket_json = None, None

    if source == "Sample ticket":
        sample_ids = list_sample_tickets()
        if not sample_ids:
            st.warning(f"No sample tickets found in `{TICKETS_DIR}`.")
        else:
            ticket_id = st.selectbox("Choose a ticket", sample_ids, key="demo_sample_ticket")
            if ticket_id:
                ticket_json = read_ticket_json(ticket_id)
                data = json.loads(ticket_json)
                st.info(f"**{data.get('subject', '')}**\n\n{data.get('description', '')}")
                with st.expander("Raw ticket JSON"):
                    st.json(data)
    else:
        with st.form("new_ticket_form"):
            c1, c2 = st.columns(2)
            with c1:
                subject = st.text_input("Subject", placeholder="e.g. Kiosk touchscreen unresponsive")
                priority = st.selectbox("Priority", ["P1", "P2", "P3", "P4"], index=2)
                category = st.text_input("Category", placeholder="e.g. Kiosks")
            with c2:
                store_id = st.text_input("Store ID", placeholder="SW-0023")
                asset_id = st.text_input("Asset ID (optional)")
                subcategory = st.text_input("Subcategory (optional)")
            description = st.text_area("Description", placeholder="Describe the issue as the store reported it...")
            if st.form_submit_button("Use this ticket", type="primary"):
                if not subject or not description:
                    st.error("Subject and description are required.")
                else:
                    new_id, new_json = build_demo_ticket(subject, description, priority, category, subcategory, store_id, asset_id)
                    st.session_state["demo_new_ticket_id"] = new_id
                    st.session_state["demo_new_ticket_json"] = new_json

        if st.session_state.get("demo_new_ticket_json"):
            ticket_id = st.session_state["demo_new_ticket_id"]
            ticket_json = st.session_state["demo_new_ticket_json"]
            st.success(f"Ready: **{ticket_id}**")
            with st.expander("Ticket JSON that will be sent"):
                st.code(ticket_json, language="json")

    if ticket_id and ticket_json:
        state_key = f"demo_state_{ticket_id}"
        if state_key not in st.session_state:
            st.session_state[state_key] = {"started": False, "chat_history": [], "trace": [], "routing": "", "reasoning": "", "status": "", "triage_time": None, "triage_timing": [], "l2_chat_history": [], "l2_trace": [], "proposed_action": None, "action_outcome": None}
        state = st.session_state[state_key]

        col_run, col_reset = st.columns([1, 5])
        with col_run:
            run_clicked = st.button("Run Agent", type="primary", disabled=state["started"], key=f"run_{ticket_id}")
        with col_reset:
            if state["started"] and st.button("Reset", key=f"reset_{ticket_id}"):
                del st.session_state[state_key]
                st.rerun()

        if run_clicked and not state["started"]:
            state["started"] = True
            with st.spinner("Triaging ticket..."):
                try:
                    triage_result, state["triage_time"] = timed_post(f"{API_URL}/triage", {"ticket_json": ticket_json}, timeout=120)
                except Exception as e:
                    triage_result, state["triage_time"] = {"routing": "ERROR", "reasoning": str(e)}, None
            state["routing"] = triage_result.get("routing", "ERROR")
            state["reasoning"] = triage_result.get("reasoning", "")
            state["triage_timing"] = triage_result.get("timing", [])

            if state["routing"] == "L1_GUIDED":
                with st.spinner("Resolving — searching the knowledge base, reasoning, deciding on an action..."):
                    try:
                        resolve_result, elapsed = timed_post(f"{API_URL}/resolve", {"ticket_json": ticket_json, "chat_history": []}, timeout=180)
                        final_resp = resolve_result.get("final_response", "Error")
                        state["chat_history"].append({"role": "assistant", "content": final_resp, "elapsed": elapsed})
                        state["trace"] = resolve_result.get("trace", [])
                        state["proposed_action"] = resolve_result.get("proposed_action")
                        state["status"] = classify_status(final_resp)
                    except Exception as e:
                        state["status"] = "escalated"
                        state["chat_history"].append({"role": "assistant", "content": f"Error: {e}"})
            else:
                state["status"] = "resolved"
            st.rerun()

        if state["started"]:
            st.divider()
            cls, label = ROUTING_STYLE.get(state["routing"], ("l2", state["routing"]))
            timing_bits = []
            if state.get("triage_time") is not None:
                timing_bits.append(f"triage {state['triage_time']:.1f}s")
            turn_elapsed = [m["elapsed"] for m in state["chat_history"] if m.get("role") == "assistant" and m.get("elapsed") is not None]
            if turn_elapsed:
                timing_bits.append(f"resolution {sum(turn_elapsed):.1f}s")
            timing_label = "  " + " · ".join(timing_bits) if timing_bits else ""
            st.markdown(f'<span class="pill pill-{cls}">{label}</span>{timing_label}', unsafe_allow_html=True)
            st.caption(state["reasoning"])

            official_escalate = load_train_index().get(ticket_id)
            if official_escalate is not None:
                agent_escalated = (state["routing"] or "").strip().upper() == "L2_ESCALATION"
                match = agent_escalated == official_escalate
                flag_label = "escalation_flag=true" if official_escalate else "escalation_flag=false"
                st.caption(f"Ticket arrived marked `{flag_label}` — an input field, correct only ~50% of the time. Not the score.")

            gt = load_labels().get(ticket_id)
            if gt:
                gt_routing = str(gt.get("correct_routing", ""))
                agent_routing = (state["routing"] or "").strip().lower()
                match = agent_routing == gt_routing.strip().lower()
                verdict = "Agent matched" if match else "Agent disagreed"
                st.markdown(f"**Answer key (labels/train_labels.json):** `{gt_routing}` — {verdict}")
                if gt.get("escalation_reason"):
                    st.caption(gt["escalation_reason"])

            latency_rows = build_latency_rows(state)
            if latency_rows:
                with st.expander("Component-level latency", expanded=False):
                    df_latency = pd.DataFrame(latency_rows)
                    st.dataframe(df_latency, use_container_width=True, hide_index=True)
                    llm_total = sum(r["Duration (s)"] for r in latency_rows if r["Type"] == "LLM")
                    tool_total = sum(r["Duration (s)"] for r in latency_rows if r["Type"] != "LLM")
                    st.caption(f"Total: {llm_total + tool_total:.2f}s — LLM calls {llm_total:.2f}s, retrieval/tools {tool_total:.2f}s")

            if state["routing"] == "L1_GUIDED":
                left, right = st.columns([3, 2])
                with left:
                    st.markdown("##### Conversation")
                    for msg in state["chat_history"]:
                        with st.chat_message(msg["role"]):
                            st.write(msg["content"])
                            if msg.get("elapsed") is not None:
                                st.caption(f"{msg['elapsed']:.1f}s")

                    if state["status"] == "awaiting_user":
                        user_reply = st.chat_input("Reply as the store employee...")
                        if user_reply:
                            state["chat_history"].append({"role": "user", "content": user_reply})
                            with st.spinner("Agent is reasoning..."):
                                try:
                                    resolve_result, elapsed = timed_post(f"{API_URL}/resolve", {"ticket_json": ticket_json, "chat_history": state["chat_history"]}, timeout=180)
                                    final_resp = resolve_result.get("final_response", "Error")
                                    state["chat_history"].append({"role": "assistant", "content": final_resp, "elapsed": elapsed})
                                    state["trace"].extend(resolve_result.get("trace", []))
                                    if resolve_result.get("proposed_action"):
                                        state["proposed_action"] = resolve_result["proposed_action"]
                                    state["status"] = classify_status(final_resp)
                                except Exception as e:
                                    st.error(str(e))
                            st.rerun()

                    render_approval_gate(state, ticket_id)

                    if state["status"] in ("resolved", "escalated"):
                        if state["status"] == "resolved":
                            st.success("Ticket resolved")
                        else:
                            st.warning("Escalated to L2")
                            st.divider()
                            render_l2_copilot(state, ticket_id, ticket_json)

                with right:
                    if gt and gt.get("relevant_kb_docs"):
                        relevant = {d.split("/")[-1] for d in gt["relevant_kb_docs"]}
                        retrieved = set(extract_sources(state["trace"]))
                        found = relevant & retrieved
                        st.markdown("##### Retrieval vs. ground truth (unverified)")
                        (st.success if found else st.error)(
                            f"{len(found)}/{len(relevant)} relevant docs retrieved",
                            icon=None,
                        )
                        st.caption("Expected: " + ", ".join(f"`{d}`" + (" (retrieved)" if d in retrieved else "") for d in sorted(relevant)))
                        st.caption("Source: `labels/train_labels.json:relevant_kb_docs` — the challenge answer key.")

                    st.markdown("##### Guardrails")
                    checks = analyze_guardrails(state["trace"])
                    if not checks:
                        st.caption("No guardrail-relevant actions yet.")
                    for ok, text in checks:
                        (st.success if ok else st.error)(text)

                    st.markdown("##### Agent trace")
                    render_trace(state["trace"])
            elif state["routing"] == "L2_ESCALATION":
                render_l2_copilot(state, ticket_id, ticket_json)
            else:
                st.info("Routed outside the L1 flow — no resolution agent run needed for this ticket.")

# ============================================================ EVALUATION ===
with tab_eval:
    st.subheader("Routing & retrieval accuracy")
    st.caption("Graded against `labels/train_labels.json` — the answer key shipped with the challenge: "
               "`correct_routing` for routing, `relevant_kb_docs` for retrieval. "
               "The ticket's own `escalation_flag` is an input field, not the answer (they agree only 50.5% of the time), "
               "so it is reported below as a diagnostic rather than a score.")

    metrics_files = sorted(EVAL_RUNS_DIR.glob("labeled_eval_*_metrics.json")) if EVAL_RUNS_DIR.exists() else []

    if not metrics_files:
        st.info("No evaluation runs yet. Use the panel below to run one.")
    else:
        def _format_run_label(f: Path) -> str:
            m = json.loads(f.read_text())
            stamp = f.stem.replace("labeled_eval_", "").replace("_metrics", "")
            try:
                dt = datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
                when = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                when = stamp
            mode = "flag-honored" if "flag_honored" in m.get("routing_mode", "") else "reasoning-only"
            return (f"{when} — routing {m.get('routing_accuracy', 0)*100:.1f}% "
                    f"({mode}), retrieval recall {m.get('retrieval_recall_avg', 0)*100:.1f}%")

        run_options = list(reversed(metrics_files))  # most recent first
        run_labels = {f: _format_run_label(f) for f in run_options}
        selected_file = st.selectbox(
            "Evaluation run", run_options,
            format_func=lambda f: run_labels[f],
            help="Every evaluation run is saved to data/eval_runs/ — pick any past run to inspect, not just the latest.",
        )
        is_latest = selected_file == metrics_files[-1]

        selected = json.loads(selected_file.read_text())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Routing accuracy", f"{selected.get('routing_accuracy', 0) * 100:.1f}%",
                  help=f"{selected.get('routing_correct', 0)}/{selected.get('routing_total', 0)} correct")
        c2.metric("Retrieval hit-rate", f"{selected.get('retrieval_hit_rate', 0) * 100:.1f}%",
                  help="Share of L1 tickets where at least one relevant runbook was retrieved")
        c3.metric("Retrieval recall@k", f"{selected.get('retrieval_recall_avg', 0) * 100:.1f}%",
                  help="Average share of all relevant docs found per ticket")
        c4.metric("Tickets evaluated", selected.get("total_tickets", 0), help=f"{selected.get('errors', 0)} errors")
        st.caption(f"{'Latest' if is_latest else 'Selected'} run: `{selected_file.stem}`")

        quality = selected.get("quality")
        if quality:
            st.markdown("###### Groundedness & guardrails")
            st.caption("Routing accuracy says which queue a ticket went to; these say whether the guidance the store "
                       "actually receives is grounded in a real runbook and whether the agent's own guardrails held.")
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Groundedness", f"{quality.get('groundedness_avg', 0) * 100:.1f}%",
                      help="Share of the agent's concrete, actionable specifics (menu paths, durations, error codes, PINs) "
                           "that actually appear in a retrieved runbook.")
            q2.metric("Fully-grounded replies", f"{quality.get('fully_grounded_rate', 0) * 100:.1f}%",
                      help="Replies where every checkable specific was supported by retrieved content.")
            q3.metric("Unsupported specifics",
                      f"{quality.get('specifics_unsupported', 0)}/{quality.get('specifics_total', 0)}",
                      help="Individual claims not found in any retrieved doc — these are the ones that would send a store "
                           "employee down a path that doesn't exist.")
            q4.metric("Guardrail pass rate", f"{quality.get('guardrail_pass_rate', 0) * 100:.1f}%",
                      help="Tickets where every guardrail check passed (searched before acting, search budget respected, "
                           "escalation justified).")
            fails = quality.get("guardrail_failures_by_check") or {}
            bits = [f"{quality.get('tickets_with_no_checkable_claims', 0)} reply(s) made no checkable claims (excluded — "
                    f"a cautious clarifying question shouldn't be scored as ungrounded)"]
            if fails:
                bits.append("guardrail failures: " + ", ".join(f"`{k}` ×{v}" for k, v in fails.items()))
            st.caption(" · ".join(bits))

        latency = selected.get("latency")
        if latency:
            st.markdown("###### Latency (measured in the same pass, no extra calls)")
            l1, l2, l3, l4 = st.columns(4)
            l1.metric("Avg total time", f"{latency.get('avg_total_time_s', 0):.2f}s")
            l2.metric("Median total time", f"{latency.get('median_total_time_s', 0):.2f}s")
            l3.metric("P95 total time", f"{latency.get('p95_total_time_s', 0):.2f}s")
            l4.metric("Max total time", f"{latency.get('max_total_time_s', 0):.2f}s")
            st.caption(f"Avg triage: {latency.get('avg_triage_time_s', 0):.2f}s · "
                       f"Avg resolve: {latency.get('avg_resolve_time_s', 0):.2f}s")
        else:
            st.caption("No latency data for this run — it predates this being captured automatically "
                       "(re-run to get it).")

        if len(metrics_files) > 1:
            hist = []
            for f in metrics_files:
                m = json.loads(f.read_text())
                hist.append({
                    "run": f.stem.replace("labeled_eval_", "").replace("_metrics", ""),
                    "routing_accuracy": m.get("routing_accuracy", 0),
                    "retrieval_hit_rate": m.get("retrieval_hit_rate", 0),
                })
            st.line_chart(pd.DataFrame(hist).set_index("run"))

        details_path = selected_file.with_name(selected_file.name.replace("_metrics.json", "_details.csv"))
        if details_path.exists():
            details_df = pd.read_csv(details_path)

            # Where the queue actually went. Routing *accuracy* above says how
            # often the agent agreed with the answer key; it does not say how
            # the work was split, and those answer different questions. A run
            # could be 90% accurate while sending far too much to L2 -- which
            # is what capacity planning and the ROI model actually depend on,
            # since only the L1 share is deflectable.
            if "actual_routing" in details_df.columns:
                st.markdown("###### How the queue split")
                routed = details_df["actual_routing"].fillna("(none)").astype(str)
                n_total = len(routed)
                n_l1 = int((routed == "l1_guided").sum())
                n_l2 = int((routed == "l2_escalation").sum())
                # Anything that is neither is a failed/odd run -- surfaced
                # rather than folded into a percentage, because a silent error
                # row would otherwise read as a routing decision.
                n_other = n_total - n_l1 - n_l2

                exp = details_df.get("expected_routing")
                exp_l1 = int((exp == "l1_guided").sum()) if exp is not None else None
                exp_l2 = int((exp == "l2_escalation").sum()) if exp is not None else None

                q1, q2, q3, q4 = st.columns(4)
                q1.metric(
                    "Handled at L1", f"{n_l1}",
                    delta=(None if exp_l1 is None else f"{n_l1 - exp_l1:+d} vs answer key"),
                    delta_color="off",
                    help="Tickets the agent kept at L1_GUIDED and walked the store through.",
                )
                q2.metric(
                    "Escalated to L2", f"{n_l2}",
                    delta=(None if exp_l2 is None else f"{n_l2 - exp_l2:+d} vs answer key"),
                    delta_color="off",
                    help="Tickets the agent handed to a human L2 engineer.",
                )
                q3.metric(
                    "L1 share", f"{(n_l1 / n_total * 100):.1f}%" if n_total else "—",
                    help="The deflectable share — the only portion the ROI model can claim against.",
                )
                q4.metric(
                    "Tickets routed", f"{n_total}",
                    delta=(f"{n_other} not routed" if n_other else None),
                    delta_color="off",
                    help="Rows in this run. 'Not routed' means the call errored, so no decision was made.",
                )
                split_note = (
                    f"Agent sent **{n_l1} to L1** and **{n_l2} to L2**"
                    + (f" ({n_other} errored)" if n_other else "")
                    + (f". The answer key says {exp_l1} / {exp_l2}." if exp_l1 is not None else ".")
                    + " Accuracy and split are different questions: the agent can send the right *number*"
                      " to each queue while still sending the wrong *tickets*."
                )
                st.caption(split_note)

            # Per-ticket retrieval quality, keyed by ticket ID. The headline
            # recall@k above is a single average over exactly these rows;
            # breaking it out by ticket is the actionable version, because the
            # same average can mean "uniformly mediocre everywhere" or "mostly
            # perfect with a bad tail", and those call for different fixes.
            if "retrieval_recall" in details_df.columns:
                scored = details_df[details_df["retrieval_recall"].notna()]
                if not scored.empty:
                    st.markdown("###### Retrieval accuracy per ticket")
                    n_missed = int((scored["retrieval_recall"] < 1).sum())
                    misses_only = st.checkbox(
                        f"Show only tickets with missed documents ({n_missed} of {len(scored)})",
                        value=False, key="retrieval_chart_misses_only",
                    )
                    chart_src = scored[scored["retrieval_recall"] < 1] if misses_only else scored
                    chart_src = chart_src[["ticket_id", "retrieval_recall"]].sort_values("retrieval_recall")
                    # Altair rather than st.bar_chart: st.bar_chart re-sorts the
                    # x-axis alphabetically by index, which silently discarded
                    # the sort_values above and left the caption claiming an
                    # ordering the chart did not have. Encoding sort explicitly
                    # is the only way to hold worst-first.
                    import altair as alt
                    chart = (
                        alt.Chart(chart_src)
                        .mark_bar()
                        .encode(
                            x=alt.X("ticket_id:N", sort=list(chart_src["ticket_id"]),
                                    title=None, axis=alt.Axis(labelAngle=-90, labelFontSize=9)),
                            y=alt.Y("retrieval_recall:Q", title="recall@k",
                                    scale=alt.Scale(domain=[0, 1])),
                            color=alt.condition(
                                alt.datum.retrieval_recall <= 0.5,
                                alt.value("#C0563F"), alt.value("#1F7A6B")),
                            tooltip=["ticket_id", "retrieval_recall"],
                        )
                        .properties(height=340)
                    )
                    st.altair_chart(chart, use_container_width=True)
                    st.caption(
                        f"recall@k per ticket, worst first (red = 50% or below) — the share of that ticket's labelled "
                        f"`relevant_kb_docs` that retrieval actually returned. Only {len(scored)} of "
                        f"{len(details_df)} tickets appear: a ticket is scoreable here only if it routed "
                        "to L1_GUIDED (so retrieval ran at all) and the answer key lists relevant docs "
                        "for it. 1.0 means every labelled document was retrieved."
                    )

            with st.expander("Per-ticket results", expanded=False):
                st.dataframe(details_df, use_container_width=True)

    st.divider()
    st.markdown("##### Run a new evaluation")
    st.caption("Calls the live agent for each ticket in train_index.csv — this is a real, not simulated, accuracy check, "
               "and also reports latency for the same run (same /triage + /resolve calls, timed -- no extra requests). "
               "Always runs reasoning-only: each ticket's escalation_flag is withheld so the triage LLM has to judge "
               "escalate-vs-not from the ticket text alone. (Honoring the flag is correct production behavior, but "
               "since routing accuracy is graded against that same flag, letting the agent see it would just be "
               "checking that a value equals itself -- trivially ~100%, and not a test of anything.)")

    col_lim, col_par = st.columns(2)
    limit = col_lim.slider("Number of tickets to evaluate", 5, 256, 20)
    workers = col_par.slider("Tickets processed in parallel", 1, 12, 1)

    # Concurrency is a throughput knob, not a latency one, and the difference
    # matters because this same run publishes the latency figures above.
    # Tickets run concurrently queue behind each other on the single-worker
    # api_service and the CPU-bound cross-encoder in query_pipeline (measured:
    # 1 call ~0.55s vs 4 concurrent ~1.19s), so per-ticket durations come out
    # inflated. eval_labeled flags those runs invalid and keeps them out of
    # the latency history; this warning is so nobody is surprised by it.
    if workers > 1:
        st.warning(
            f"**{workers}× parallel — accuracy only.** The batch finishes much faster, but each "
            "ticket queues behind the others, so this run's latency numbers are inflated and will "
            "be marked invalid (and excluded from the latency history). Use 1 for quotable latency."
        )

    # Rendered from session state rather than written inline after the run:
    # the st.rerun() below (needed to refresh the metrics above with the new
    # run) discards the current render pass, so anything written before it
    # never reaches the screen. Stash the timing, rerun, then show it here.
    last_timing = st.session_state.get("last_eval_timing")
    if last_timing:
        st.success(
            f"Last run: **{last_timing['limit']} tickets in {last_timing['elapsed']:.1f}s** "
            f"({last_timing['per_ticket']:.2f}s per ticket wall-clock, "
            f"{last_timing['workers']}× parallel)."
        )

    if st.button("Run evaluation now", type="primary"):
        mode = f"{limit} tickets, {workers}× parallel" if workers > 1 else f"{limit} tickets, serial"
        with st.spinner(f"Evaluating {mode} against ground truth..."):
            try:
                from services.eval_labeled import evaluate as run_labeled_eval
                t0 = time.perf_counter()
                new_metrics, _ = run_labeled_eval(limit=limit, workers=workers)
                elapsed = time.perf_counter() - t0
                st.session_state["last_eval_timing"] = {
                    "elapsed": elapsed,
                    "per_ticket": elapsed / limit if limit else 0,
                    "limit": limit,
                    "workers": workers,
                }
                st.rerun()
            except Exception as e:
                st.error(f"Evaluation failed: {e}")

# ============================================================== LATENCY ====
with tab_latency:
    st.subheader("Component-level latency across all labeled tickets")
    st.caption("Where each ticket's processing time actually goes — triage, retrieval calls, and LLM reasoning turns — measured on the live agent, not simulated.")

    if LATENCY_RESULT_CSV.exists():
        lat_df = pd.read_csv(LATENCY_RESULT_CSV)
        n = len(lat_df)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tickets measured", n)
        c2.metric("Avg total time", f"{lat_df['total_time_s'].mean():.2f}s")
        c3.metric("Median total time", f"{lat_df['total_time_s'].median():.2f}s")
        c4.metric("P95 total time", f"{lat_df['total_time_s'].quantile(0.95):.2f}s")

        st.markdown("##### Average time by component")
        component_cols = {
            "triage_time_s": "Triage LLM call",
            "search_knowledge_base_time_s": "search_knowledge_base",
            "get_system_spec_time_s": "get_system_spec",
            "search_faq_time_s": "search_faq",
        }
        avg_total = lat_df["total_time_s"].mean()
        rows = []
        for col, label in component_cols.items():
            if col in lat_df.columns:
                rows.append({"Component": label, "Avg (s)": lat_df[col].mean(), "Median (s)": lat_df[col].median(), "% of total": lat_df[col].mean() / avg_total * 100})
        llm_turns = (lat_df["llm_time_s"] - lat_df["triage_time_s"]).clip(lower=0)
        rows.append({"Component": "LLM reasoning turns", "Avg (s)": llm_turns.mean(), "Median (s)": llm_turns.median(), "% of total": llm_turns.mean() / avg_total * 100})
        component_table = pd.DataFrame(rows).round({"Avg (s)": 3, "Median (s)": 3, "% of total": 1})
        st.dataframe(component_table, use_container_width=True, hide_index=True)

        st.markdown("##### LLM time vs. retrieval/tool time")
        split_table = pd.DataFrame([
            {"Category": "LLM calls (triage + reasoning)", "Avg (s)": lat_df["llm_time_s"].mean(), "% of total": lat_df["llm_time_s"].mean() / avg_total * 100},
            {"Category": "Retrieval/tools", "Avg (s)": lat_df["tool_time_s"].mean(), "% of total": lat_df["tool_time_s"].mean() / avg_total * 100},
        ]).round({"Avg (s)": 3, "% of total": 1})
        st.dataframe(split_table, use_container_width=True, hide_index=True)

        with st.expander("Per-ticket results", expanded=False):
            st.dataframe(lat_df, use_container_width=True, hide_index=True)

        if LATENCY_DETAILS_CSV.exists():
            with st.expander("Per-component detail (every call, every ticket)", expanded=False):
                st.dataframe(pd.read_csv(LATENCY_DETAILS_CSV), use_container_width=True, hide_index=True)

        st.caption(f"Source: `RAG_Results/Latency_result.csv` and `RAG_Results/Latency_details.csv`")
    else:
        st.info("No latency measurement yet. Run one below.")

    st.divider()
    st.markdown("##### Run a new latency measurement")
    st.caption("Calls the live agent for every labeled ticket and records per-component timing. Takes a few minutes for the full set.")
    lat_limit = st.slider("Number of tickets to measure", 5, 220, 20, key="latency_limit_slider")
    if st.button("Run latency measurement now", type="primary"):
        with st.spinner(f"Measuring latency across {lat_limit} tickets..."):
            try:
                from services.eval_latency import run_latency_eval
                summary_df, _ = run_latency_eval(limit=lat_limit)
                st.success(f"Measured {len(summary_df)} tickets.")
                st.rerun()
            except Exception as e:
                st.error(f"Latency measurement failed: {e}")

# ================================================== ARCHITECTURE & KB ======
with tab_arch:
    st.subheader("System architecture")
    if ARCH_DIAGRAM_PATH.exists():
        components.html(ARCH_DIAGRAM_PATH.read_text(), height=950, scrolling=True)
    else:
        st.warning("Architecture diagram not found at design_md/system_architecture.html.")

    st.divider()
    st.subheader("Ask the knowledge base directly")
    st.caption("Bypasses the agent — runs the same retrieval + rerank pipeline the Resolution Agent uses, for technical Q&A.")
    kb_query = st.text_input("Query")
    if kb_query:
        with st.spinner("Searching..."):
            try:
                res = requests.post(f"{QUERY_URL}/query", json={"query": kb_query, "n_results": 5}, timeout=30)
                res.raise_for_status()
                st.text(res.json().get("results", "No results."))
            except Exception as e:
                st.error(str(e))

# ============================================================== ADMIN ======
with tab_admin:
    st.caption("Behind-the-scenes tooling — not part of the live demo flow.")

    with st.expander("Knowledge base ingestion"):
        kb_dir = st.text_input("KB directory", value="./kb", key="admin_kb_dir")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Preview parse"):
                try:
                    res = requests.post(f"{INGESTION_URL}/preview", json={"directory": to_container_path(kb_dir)}, timeout=60)
                    res.raise_for_status()
                    st.session_state["admin_preview"] = res.json()
                except Exception as e:
                    st.error(str(e))
        with c2:
            if st.button("Ingest → Delta index", type="primary"):
                try:
                    res = requests.post(f"{INGESTION_URL}/ingest", json={"directory": to_container_path(kb_dir)}, timeout=300)
                    res.raise_for_status()
                    st.success(res.json().get("message"))
                except Exception as e:
                    st.error(str(e))
        with c3:
            if st.button("Rebuild main index"):
                try:
                    res = requests.post(f"{INGESTION_URL}/rebuild_main_index", timeout=120)
                    res.raise_for_status()
                    st.success(res.json().get("message"))
                except Exception as e:
                    st.error(str(e))
        if "admin_preview" in st.session_state:
            p = st.session_state["admin_preview"]
            st.write(f"{p.get('total_chunks', 0)} chunks across categories: {', '.join(p.get('categories', []))}")

    with st.expander("Vector store"):
        try:
            stats = requests.get(f"{ADMIN_URL}/pgvector/stats", timeout=5).json()
            st.metric("Total vectors", stats.get("total_vectors", 0))
        except Exception:
            st.caption("Admin backend unreachable.")

        st.markdown("**Add a chunk manually**")
        with st.form("admin_add_chunk"):
            new_content = st.text_area("Content")
            new_file = st.text_input("File name", value="manual_entry")
            new_section = st.text_input("Section title", value="Manual insertion")
            if st.form_submit_button("Add"):
                try:
                    res = requests.post(f"{INGESTION_URL}/add_chunk",
                                         json={"content": new_content, "file_name": new_file, "section_title": new_section},
                                         timeout=30)
                    res.raise_for_status()
                    st.success("Added.")
                except Exception as e:
                    st.error(str(e))

    with st.expander("Processed ticket history"):
        try:
            hist = requests.get(f"{API_URL}/history", timeout=5).json().get("history", [])
            if hist:
                st.dataframe(pd.DataFrame(hist), use_container_width=True)
            else:
                st.caption("No tickets processed yet.")
        except Exception as e:
            st.error(str(e))

    with st.expander("Batch regression run (sample tickets)"):
        sample_ids = list_sample_tickets()
        if not sample_ids:
            st.caption(f"No sample tickets found in `{TICKETS_DIR}`.")
        else:
            n = st.number_input("Number of tickets", min_value=1, max_value=len(sample_ids), value=min(10, len(sample_ids)))
            if st.button("Run batch"):
                progress = st.progress(0)
                results = []
                for i, tid in enumerate(sample_ids[:n]):
                    tj = read_ticket_json(tid)
                    data = json.loads(tj)
                    total_time = 0.0
                    try:
                        triage, triage_time = timed_post(f"{API_URL}/triage", {"ticket_json": tj}, timeout=120)
                        routing = triage.get("routing", "ERROR")
                        total_time += triage_time
                    except Exception as e:
                        routing, triage = "ERROR", {"reasoning": str(e)}

                    final_response, trace = "N/A", []
                    if routing == "L1_GUIDED":
                        try:
                            resolve_result, resolve_time = timed_post(f"{API_URL}/resolve", {"ticket_json": tj}, timeout=180)
                            final_response = resolve_result.get("final_response", "Error")
                            trace = resolve_result.get("trace", [])
                            total_time += resolve_time
                        except Exception as e:
                            final_response = f"Error: {e}"

                    results.append({
                        "ID": tid, "Subject": data.get("subject", ""), "Routing": routing,
                        "Time (s)": round(total_time, 1), "Final Response": final_response, "_trace": trace,
                    })
                    progress.progress((i + 1) / n)

                df = pd.DataFrame(results)[["ID", "Subject", "Routing", "Time (s)", "Final Response"]]
                st.dataframe(df, use_container_width=True)
                st.caption(f"Average: {df['Time (s)'].mean():.1f}s per ticket")
                for r in results:
                    if r["_trace"]:
                        with st.expander(f"{r['ID']} — {r['Routing']}"):
                            render_trace(r["_trace"])
