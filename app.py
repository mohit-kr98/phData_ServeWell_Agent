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
RAG_RESULTS_CSV = PROJECT_ROOT / "RAG_Results" / "Rag_result.csv"
LATENCY_RESULT_CSV = PROJECT_ROOT / "RAG_Results" / "Latency_result.csv"
LATENCY_DETAILS_CSV = PROJECT_ROOT / "RAG_Results" / "Latency_details.csv"
ARCH_DIAGRAM_PATH = PROJECT_ROOT / "design_md" / "system_architecture.html"
LABELS_PATH = PROJECT_ROOT / "labels" / "train_labels.json"
TRAIN_INDEX_CSV = PROJECT_ROOT / "tickets" / "train_index.csv"

ROUTING_STYLE = {
    "L1_GUIDED": ("l1", "🟢 L1 — Guided Resolution"),
    "L2_ESCALATION": ("l2", "🟠 L2 — Escalation"),
    "NON_IT": ("nonit", "⚪ Non-IT — Routed Out"),
    "ERROR": ("l2", "🔴 Error"),
}

TOOL_ICONS = {
    "search_knowledge_base": "🔍",
    "search_faq": "❓",
    "get_system_spec": "📋",
    "get_asset_info": "🖥️",
    "get_store_info": "🏬",
    "check_sla": "⏱️",
    "reply_to_user": "💬",
    "resolve_ticket": "✅",
    "escalate_to_l2": "🚨",
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
    """LLM-generated labels (relevant_kb_docs, correct_routing, ...) -- a
    labeling aid, not officially verified. Only relevant_kb_docs (used for
    retrieval eval) should be treated as load-bearing; correct_routing
    disagrees with the official escalation_flag on ~48% of tickets."""
    if not LABELS_PATH.exists():
        return {}
    return {x["ticket_id"]: x for x in json.loads(LABELS_PATH.read_text())}


@st.cache_data
def load_train_index() -> dict:
    """Official ground truth provided with the challenge."""
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
    UI isn't just trusting the agent's own narration."""
    checks = []
    search_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") == "search_knowledge_base"]
    search_results = [s for s in trace if s.get("type") == "tool_result" and s.get("name") == "search_knowledge_base"]
    action_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") in ("reply_to_user", "resolve_ticket", "escalate_to_l2")]
    reply_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") == "reply_to_user"]
    escalate_calls = [s for s in trace if s.get("type") == "tool_call" and s.get("name") == "escalate_to_l2"]

    if action_calls and search_calls:
        grounded = trace.index(search_calls[0]) < trace.index(action_calls[0])
    else:
        grounded = bool(search_calls)
    checks.append((grounded, "Searched the knowledge base before taking any action" if grounded
                   else "No knowledge-base search occurred before acting — grounding guardrail bypassed"))

    if len(search_calls) > 2:
        checks.append((False, f"Called search_knowledge_base {len(search_calls)} times — exceeds the 2-attempt cap"))

    if escalate_calls:
        empty_searches = sum(1 for s in search_results if "No relevant documents found" in s.get("result", ""))
        if empty_searches >= 2:
            checks.append((True, f"Escalated after {empty_searches} empty knowledge-base search(es)"))
        elif len(reply_calls) >= 2:
            checks.append((True, f"Escalated after {len(reply_calls)} troubleshooting attempt(s) with the user"))
        else:
            checks.append((False, "Escalated without 2 empty searches or 2 troubleshooting replies — escalation guardrail may have been bypassed"))

    return checks


def render_trace(trace):
    for step in trace:
        step_type = step.get("type")
        if step_type == "tool_call":
            name = step.get("name")
            icon = TOOL_ICONS.get(name, "🛠️")
            st.markdown(f"**{icon} `{name}`**")
            args = {k: v for k, v in step.get("args", {}).items() if k != "message"}
            if args:
                st.caption(", ".join(f"{k}={v}" for k, v in args.items()))
        elif step_type == "tool_result":
            name = step.get("name")
            result = step.get("result", "")
            if name in ("search_knowledge_base", "search_faq", "get_system_spec"):
                sources = list(dict.fromkeys(re.findall(r"--- Document Source: (.+?) ---", result)))
                if sources:
                    st.markdown("&nbsp;&nbsp;&nbsp;&nbsp;📄 Retrieved: " + " · ".join(f"`{s}`" for s in sources))
                    with st.expander("View retrieved content", expanded=False):
                        st.text(result)
                else:
                    st.markdown("&nbsp;&nbsp;&nbsp;&nbsp;⚠️ No relevant documents found")
            else:
                with st.expander(f"Result of `{name}`", expanded=False):
                    st.code(result)
        elif step_type == "reasoning":
            text = (step.get("text") or "").strip()
            if text:
                st.markdown(f"> 🧠 {text}")


st.set_page_config(page_title="ServeWell IT Support Agent", layout="wide", page_icon="🛠️")

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

st.title("🛠️ ServeWell Agentic IT Support")
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

tab_demo, tab_eval, tab_latency, tab_arch, tab_admin = st.tabs(["🚀 Live Demo", "📊 Evaluation", "⏱️ Latency", "🏗️ Architecture & KB", "⚙️ Admin"])

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
            st.session_state[state_key] = {"started": False, "chat_history": [], "trace": [], "routing": "", "reasoning": "", "status": "", "triage_time": None, "triage_timing": []}
        state = st.session_state[state_key]

        col_run, col_reset = st.columns([1, 5])
        with col_run:
            run_clicked = st.button("▶️ Run Agent", type="primary", disabled=state["started"], key=f"run_{ticket_id}")
        with col_reset:
            if state["started"] and st.button("↺ Reset", key=f"reset_{ticket_id}"):
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
            timing_label = " ⏱️ " + " · ".join(timing_bits) if timing_bits else ""
            st.markdown(f'<span class="pill pill-{cls}">{label}</span>{timing_label}', unsafe_allow_html=True)
            st.caption(state["reasoning"])

            official_escalate = load_train_index().get(ticket_id)
            if official_escalate is not None:
                agent_escalated = (state["routing"] or "").strip().upper() == "L2_ESCALATION"
                match = agent_escalated == official_escalate
                verdict = "✅ Agent matched" if match else "❌ Agent disagreed"
                expected_label = "should escalate (L2)" if official_escalate else "should NOT escalate (L1)"
                st.markdown(f"**Official ground truth (tickets/train_index.csv):** `{expected_label}` — {verdict}")

            gt = load_labels().get(ticket_id)
            if gt:
                gt_routing = gt.get("correct_routing", "")
                st.caption(f"LLM-generated label (unverified, disagrees with official ~48% of the time): `{gt_routing}`" + (f" — {gt['escalation_reason']}" if gt.get("escalation_reason") else ""))

            latency_rows = build_latency_rows(state)
            if latency_rows:
                with st.expander("⏱️ Component-level latency", expanded=False):
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
                                st.caption(f"⏱️ {msg['elapsed']:.1f}s")

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
                                    state["status"] = classify_status(final_resp)
                                except Exception as e:
                                    st.error(str(e))
                            st.rerun()

                    if state["status"] in ("resolved", "escalated"):
                        if state["status"] == "resolved":
                            st.success("✅ Ticket resolved")
                        else:
                            st.warning("🚨 Escalated to L2")

                with right:
                    if gt and gt.get("relevant_kb_docs"):
                        relevant = {d.split("/")[-1] for d in gt["relevant_kb_docs"]}
                        retrieved = set(extract_sources(state["trace"]))
                        found = relevant & retrieved
                        st.markdown("##### Retrieval vs. ground truth (unverified)")
                        (st.success if found else st.error)(
                            f"{'✅' if found else '❌'} {len(found)}/{len(relevant)} relevant docs retrieved",
                            icon=None,
                        )
                        st.caption("Expected: " + ", ".join(f"`{d}`" + (" ✓" if d in retrieved else "") for d in sorted(relevant)))
                        st.caption("Note: relevant_kb_docs is LLM-generated, not officially verified — treat as directional.")

                    st.markdown("##### Guardrails")
                    checks = analyze_guardrails(state["trace"])
                    if not checks:
                        st.caption("No guardrail-relevant actions yet.")
                    for ok, text in checks:
                        (st.success if ok else st.error)(text, icon="✅" if ok else "⚠️")

                    st.markdown("##### Agent trace")
                    render_trace(state["trace"])
            else:
                st.info("Routed outside the L1 flow — no resolution agent run needed for this ticket.")

# ============================================================ EVALUATION ===
with tab_eval:
    st.subheader("Routing & retrieval accuracy")
    st.caption("Routing accuracy: vs. `tickets/train_index.csv` escalation_flag — official ground truth from the challenge. "
               "Retrieval hit-rate/recall: vs. `labels/train_labels.json` relevant_kb_docs — LLM-generated, not officially verified; treat as directional.")

    metrics_files = sorted(EVAL_RUNS_DIR.glob("labeled_eval_*_metrics.json")) if EVAL_RUNS_DIR.exists() else []

    if not metrics_files:
        st.info("No evaluation runs yet. Use the panel below to run one.")
    else:
        latest = json.loads(metrics_files[-1].read_text())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Routing accuracy", f"{latest.get('routing_accuracy', 0) * 100:.1f}%",
                  help=f"{latest.get('routing_correct', 0)}/{latest.get('routing_total', 0)} correct")
        c2.metric("Retrieval hit-rate", f"{latest.get('retrieval_hit_rate', 0) * 100:.1f}%",
                  help="Share of L1 tickets where at least one relevant runbook was retrieved")
        c3.metric("Retrieval recall@k", f"{latest.get('retrieval_recall_avg', 0) * 100:.1f}%",
                  help="Average share of all relevant docs found per ticket")
        c4.metric("Tickets evaluated", latest.get("total_tickets", 0), help=f"{latest.get('errors', 0)} errors")
        st.caption(f"Latest run: `{metrics_files[-1].stem}`")

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

        details_path = metrics_files[-1].with_name(metrics_files[-1].name.replace("_metrics.json", "_details.csv"))
        if details_path.exists():
            with st.expander("Per-ticket results"):
                st.dataframe(pd.read_csv(details_path), use_container_width=True)

    st.divider()
    st.markdown("##### Run a new evaluation")
    st.caption("Calls the live agent for each ticket in train_index.csv — this is a real, not simulated, accuracy check.")
    limit = st.slider("Number of tickets to evaluate", 5, 256, 20)
    if st.button("▶️ Run evaluation now", type="primary"):
        with st.spinner(f"Evaluating {limit} tickets against ground truth..."):
            try:
                from services.eval_labeled import evaluate as run_labeled_eval
                new_metrics, _ = run_labeled_eval(limit=limit)
                st.success("Evaluation complete.")
                st.json(new_metrics)
                st.rerun()
            except Exception as e:
                st.error(f"Evaluation failed: {e}")

    st.divider()
    st.markdown("##### Does the cross-encoder reranker actually help?")
    st.caption("Retrieval-only ablation — same query sent twice per ticket, once reranked and once left in raw embedding-similarity order. No LLM calls, so it's fast to re-run.")

    if RAG_RESULTS_CSV.exists():
        rag_df = pd.read_csv(RAG_RESULTS_CSV)
        ablation_df = rag_df[rag_df["eval_type"] == "retrieval_ablation"]
        if not ablation_df.empty:
            latest_ts = ablation_df["timestamp"].max()
            latest = ablation_df[ablation_df["timestamp"] == latest_ts]
            reranked = latest[latest["reranker_enabled"] == True]
            baseline = latest[latest["reranker_enabled"] == False]
            if not reranked.empty and not baseline.empty:
                r, b = reranked.iloc[0], baseline.iloc[0]
                c1, c2, c3 = st.columns(3)
                c1.metric("Hit-rate — with reranker", f"{r['retrieval_hit_rate'] * 100:.1f}%",
                          delta=f"{(r['retrieval_hit_rate'] - b['retrieval_hit_rate']) * 100:+.1f}pp vs. no rerank")
                c2.metric("Recall@k — with reranker", f"{r['retrieval_recall_avg'] * 100:.1f}%",
                          delta=f"{(r['retrieval_recall_avg'] - b['retrieval_recall_avg']) * 100:+.1f}pp vs. no rerank")
                c3.metric("Tickets tested", int(r["tickets_evaluated"]))
                st.caption(f"Baseline without reranking: {b['retrieval_hit_rate'] * 100:.1f}% hit-rate, {b['retrieval_recall_avg'] * 100:.1f}% recall@k. Run: `{latest_ts}`")
        else:
            st.info("No ablation run yet — use the button below.")
    else:
        st.info("No ablation run yet — use the button below.")

    if st.button("▶️ Run reranker ablation now"):
        with st.spinner("Querying the knowledge base with and without reranking for every labeled ticket..."):
            try:
                from services.eval_retrieval_ablation import run_ablation
                reranked_m, baseline_m = run_ablation(limit=0)
                st.success("Ablation complete.")
                st.json({"with_reranker": reranked_m, "without_reranker": baseline_m})
                st.rerun()
            except Exception as e:
                st.error(f"Ablation failed: {e}")

    if RAG_RESULTS_CSV.exists():
        with st.expander("All RAG_Results runs"):
            st.dataframe(pd.read_csv(RAG_RESULTS_CSV), use_container_width=True)

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
    if st.button("▶️ Run latency measurement now", type="primary"):
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

    with st.expander("📚 Knowledge base ingestion"):
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

    with st.expander("🧮 Vector store"):
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

    with st.expander("📜 Processed ticket history"):
        try:
            hist = requests.get(f"{API_URL}/history", timeout=5).json().get("history", [])
            if hist:
                st.dataframe(pd.DataFrame(hist), use_container_width=True)
            else:
                st.caption("No tickets processed yet.")
        except Exception as e:
            st.error(str(e))

    with st.expander("📦 Batch regression run (sample tickets)"):
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
