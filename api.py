from fastapi import FastAPI, Request
from pydantic import BaseModel
from agent_core.llm_client import run_triage_agent, run_resolution_agent
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
        triage_result = run_triage_agent(req.ticket_json)
        return triage_result
    except Exception as e:
        return {"error": str(e)}

@app.post("/resolve")
async def resolve_ticket(req: TicketRequest):
    """
    Run the Resolution Agent to interactively query KB and propose a resolution.
    """
    try:
        final_response, trace = run_resolution_agent(req.ticket_json, chat_history=req.chat_history)
        return {
            "final_response": final_response,
            "trace": trace
        }
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}

@app.post("/save_history")
async def save_history(req: HistoryRequest):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO processed_tickets (ticket_id, subject, routing, reasoning, resolution)
            VALUES (%s, %s, %s, %s, %s)
        """, (req.ticket_id, req.subject, req.routing, req.reasoning, req.resolution))
        conn.commit()
        cur.close()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/history")
async def get_history():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM processed_tickets ORDER BY processed_at DESC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for row in rows:
            if 'processed_at' in row and row['processed_at']:
                row['processed_at'] = row['processed_at'].isoformat()
        return {"history": rows}
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
