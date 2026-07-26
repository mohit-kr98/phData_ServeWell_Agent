import os
import re
import pandas as pd
import json
import requests
from pathlib import Path
from typing import Dict, Any, Optional

def search_knowledge_base(query: str, n_results: int = 5) -> str:
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

def search_faq(query: str, n_results: int = 3) -> str:
    """Search only kb/faq for this query. A blended symptom-style query
    consistently ranks runbook prose above FAQ prose (both cover similar
    ground, in different genres), so FAQ content needs a search of its own
    rather than relying on search_knowledge_base to surface it."""
    try:
        query_url = os.environ.get("QUERY_URL", "http://query_pipeline:8002")
        response = requests.post(f"{query_url}/query", json={
            "query": query,
            "n_results": n_results,
            "metadata_filter": {"folder": "faq"},
        })
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
KB_SYSTEM_SPECS_DIR = Path("kb") / "system-specs"

# Maps a keyword found in a ticket's `system_version` field to its spec sheet.
# This is a deterministic lookup, not semantic search -- spec sheets read like
# reference tables, not troubleshooting prose, so they rank poorly against a
# symptom-style query even when they're exactly the doc a ticket needs.
SYSTEM_SPEC_KEYWORDS = {
    "foodtech": "foodtech-pos.md",
    "orbitpos": "orbitpos.md",
    "netlink": "netlink-router.md",
    "cisco": "netlink-router.md",
    "frostypro": "frostypro-soft-serve.md",
    "creamtech": "creamtech-soft-serve.md",
    "starmc": "star-printer.md",
    "star": "star-printer.md",
    "epson": "epson-printer.md",
}

# Spec sheets are full technical references (hardware specs, registry paths,
# backup/upgrade procedures) written for L2, with the actual L1-relevant
# content -- overview, error codes, troubleshooting steps -- interspersed
# among sections an L1 reply never needs. Keeping the whole file (some run
# 16K+ characters) bloats every subsequent LLM call in the conversation for
# no benefit, so only the L1-relevant sections are kept.
SPEC_SECTION_KEYWORDS = ["overview", "error", "known issue", "troubleshoot", "indicator", "quick reference", "self-test"]
MAX_SPEC_CHARS = 4000

def _extract_l1_sections(content: str, max_chars: int = MAX_SPEC_CHARS) -> str:
    parts = re.split(r'\n(?=## )', content)
    kept = [parts[0].strip()[:600]]  # document title + intro, capped
    for part in parts[1:]:
        heading = part.split("\n", 1)[0].lower()
        if any(kw in heading for kw in SPEC_SECTION_KEYWORDS):
            kept.append(part.strip())
    return "\n\n".join(kept)[:max_chars]

def get_system_spec(system_version: str) -> Optional[str]:
    """Look up the system-specs doc matching a ticket's system_version field.
    Returns formatted content in the same '--- Document Source: X ---' shape
    as search_knowledge_base results, or None if no keyword matches."""
    if not system_version:
        return None
    version_lower = system_version.lower()
    for keyword, filename in SYSTEM_SPEC_KEYWORDS.items():
        if keyword in version_lower:
            spec_path = KB_SYSTEM_SPECS_DIR / filename
            if spec_path.exists():
                content = _extract_l1_sections(spec_path.read_text())
                return f"--- Document Source: system-specs/{filename} ---\n{content}\n"
            return None
    return None

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
