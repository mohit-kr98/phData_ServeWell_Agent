import asyncio
from fastapi import FastAPI, Request
from pydantic import BaseModel
from agent_core.llm_client import run_triage_agent, run_resolution_agent, run_l2_copilot_agent
from agent_core.actions import execute_action
from agent_core.pii import mask_pii
import uvicorn
import json
import traceback

import os
import psycopg2
import psycopg2.extras

app = FastAPI(title="ServeWell IT Support API")

class TicketRequest(BaseModel):
    ticket_json: str
    chat_history: list = []

class L2CopilotRequest(BaseModel):
    ticket_json: str
    escalation_context: str = ""
    chat_history: list = []

class ExecuteActionRequest(BaseModel):
    action: str
    asset_id: str
    ticket_id: str = ""
    approved: bool = False
    approved_by: str = ""

class HistoryRequest(BaseModel):
    ticket_id: str
    subject: str
    routing: str
    reasoning: str
    resolution: str = ""

def get_db_connection():
    conn_str = os.environ.get("PGVECTOR_CONNECTION_STRING", "postgresql://postgres:password@localhost:5432/vectordb")
    if conn_str.startswith("postgresql+psycopg2://"):
        conn_str = conn_str.replace("postgresql+psycopg2://", "postgresql://")
    return psycopg2.connect(conn_str)

@app.post("/triage")
async def triage_ticket(req: TicketRequest):
    """
    Run the Triage Agent to determine routing.
    """
    try:
        # Mask PII before this reaches run_triage_agent: it's @traceable, and
        # LangSmith captures a traced function's arguments at call time --
        # masking inside the function body would be too late.
        masked_ticket_json = mask_pii(req.ticket_json)
        # run_triage_agent makes blocking boto3 calls. uvicorn runs this
        # service single-worker with no thread pool of its own, so calling
        # it directly here would block the whole event loop -- including
        # keep-alives and every other in-flight connection -- for the call's
        # full duration. to_thread hands it to a worker thread instead.
        triage_result = await asyncio.to_thread(run_triage_agent, masked_ticket_json)
        return triage_result
    except Exception as e:
        return {"error": str(e)}

@app.post("/resolve")
async def resolve_ticket(req: TicketRequest):
    """
    Run the Resolution Agent to interactively query KB and propose a resolution.
    """
    try:
        # Mask PII before this reaches run_resolution_agent -- see /triage.
        masked_ticket_json = mask_pii(req.ticket_json)
        # Same reasoning as /triage, and more pressing here: this call chains
        # multiple blocking LLM/tool round-trips and measured up to ~24s on a
        # single ticket, during which the event loop would otherwise be
        # completely unresponsive.
        final_response, trace = await asyncio.to_thread(
            run_resolution_agent, masked_ticket_json, chat_history=req.chat_history
        )
        # Surface any proposed remediation so the UI can render an approval
        # gate. Nothing has run at this point -- the agent can only propose,
        # and execution goes through /execute_action with approved=True.
        proposal = None
        for step in trace:
            if step.get("type") == "tool_result" and step.get("name") == "propose_action":
                try:
                    parsed = json.loads(step.get("result", "{}"))
                except json.JSONDecodeError:
                    continue
                if parsed.get("status") == "PROPOSED_AWAITING_APPROVAL":
                    proposal = parsed
        return {
            "final_response": final_response,
            "trace": trace,
            "proposed_action": proposal,
        }
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}

@app.post("/l2_copilot")
async def l2_copilot(req: L2CopilotRequest):
    """
    Conversational assistant for the human L2 engineer working an escalated ticket.
    """
    try:
        # Mask PII before this reaches run_l2_copilot_agent -- see /triage.
        masked_ticket_json = mask_pii(req.ticket_json)
        final_response, trace = await asyncio.to_thread(
            run_l2_copilot_agent, masked_ticket_json,
            escalation_context=req.escalation_context, chat_history=req.chat_history
        )
        return {
            "final_response": final_response,
            "trace": trace
        }
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}

@app.post("/execute_action")
async def execute_action_endpoint(req: ExecuteActionRequest):
    """Run a remediation action the agent proposed.

    execute_action refuses without approved=True, and that check lives in
    agent_core/actions.py rather than here -- so the gate holds for any caller,
    not just this endpoint.
    """
    try:
        result = await asyncio.to_thread(
            execute_action, req.action, req.asset_id,
            ticket_id=req.ticket_id, approved=req.approved, approved_by=req.approved_by,
        )
        return json.loads(result)
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}

def _save_history_sync(req: HistoryRequest):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO processed_tickets (ticket_id, subject, routing, reasoning, resolution)
        VALUES (%s, %s, %s, %s, %s)
    """, (req.ticket_id, req.subject, req.routing, req.reasoning, req.resolution))
    conn.commit()
    cur.close()
    conn.close()

@app.post("/save_history")
async def save_history(req: HistoryRequest):
    try:
        await asyncio.to_thread(_save_history_sync, req)
        return {"status": "success"}
    except Exception as e:
        return {"error": str(e)}

def _get_history_sync():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM processed_tickets ORDER BY processed_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    for row in rows:
        if 'processed_at' in row and row['processed_at']:
            row['processed_at'] = row['processed_at'].isoformat()
    return rows

@app.get("/history")
async def get_history():
    try:
        rows = await asyncio.to_thread(_get_history_sync)
        return {"history": rows}
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
