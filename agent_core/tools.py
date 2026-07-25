import os
import pandas as pd
import json
import requests
from pathlib import Path
from typing import Dict, Any, Optional

def search_knowledge_base(query: str, n_results: int = 3) -> str:
    """Search for runbooks and SOPs in the knowledge base."""
    try:
        query_url = os.environ.get("QUERY_URL", "http://query_pipeline:8002")
        response = requests.post(f"{query_url}/query", json={"query": query, "n_results": n_results})
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "success":
            return data.get("results", "No results returned.")
        return f"Error from query service: {data}"
    except Exception as e:
        return f"Error contacting query service: {e}"

# Assuming this runs from the root of the project
DATA_DIR = Path("data")
ASSETS_FILE = DATA_DIR / "assets.csv"
STORES_FILE = DATA_DIR / "stores.csv"
SLA_FILE = DATA_DIR / "sla_matrix.csv"

def get_asset_info(asset_id: str) -> str:
    """Retrieve details about a specific IT asset by its ID."""
    try:
        df = pd.read_csv(ASSETS_FILE)
        asset = df[df['asset_id'] == asset_id]
        if asset.empty:
            return f"No asset found with ID: {asset_id}"
        return asset.iloc[0].to_json()
    except Exception as e:
        return f"Error retrieving asset info: {str(e)}"

def get_store_info(store_id: str) -> str:
    """Retrieve details about a specific store by its ID."""
    try:
        df = pd.read_csv(STORES_FILE)
        store = df[df['store_id'] == store_id]
        if store.empty:
            return f"No store found with ID: {store_id}"
        return store.iloc[0].to_json()
    except Exception as e:
        return f"Error retrieving store info: {str(e)}"

def check_sla(priority: str, category: str) -> str:
    """Check the SLA targets for a given priority and category."""
    try:
        df = pd.read_csv(SLA_FILE)
        sla = df[(df['priority'] == priority) & (df['category'] == category)]
        if sla.empty:
            return f"No SLA found for Priority: {priority}, Category: {category}"
        return sla.iloc[0].to_json()
    except Exception as e:
        return f"Error retrieving SLA info: {str(e)}"

# Simulated Actions
def escalate_to_l2(ticket_id: str, reason: str) -> str:
    """Escalate the ticket to Level 2 human support."""
    # In a real system, this would call an ITSM API
    action_result = {
        "status": "Escalated",
        "ticket_id": ticket_id,
        "reason": reason,
        "message": "Ticket successfully escalated to L2 queue."
    }
    return json.dumps(action_result)

def resolve_ticket(ticket_id: str, resolution_summary: str) -> str:
    """Resolve the ticket with a summary of the fix."""
    # In a real system, this would call an ITSM API
    action_result = {
        "status": "Resolved",
        "ticket_id": ticket_id,
        "resolution_summary": resolution_summary,
        "message": "Ticket successfully marked as resolved."
    }
    return json.dumps(action_result)

def reply_to_user(ticket_id: str, message: str) -> str:
    """Send a reply to the user requesting more information or proposing a fix."""
    action_result = {
        "status": "Awaiting User",
        "ticket_id": ticket_id,
        "message_sent": message
    }
    return json.dumps(action_result)
