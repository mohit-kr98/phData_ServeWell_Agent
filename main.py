import json
from pathlib import Path
from agent_core.llm_client import run_triage_agent, run_resolution_agent
import time

ROOT_DIR = Path(".")
TICKETS_DIR = ROOT_DIR / "tickets" / "train"

def load_sample_ticket(filename: str) -> str:
    ticket_path = TICKETS_DIR / filename
    with open(ticket_path, 'r') as f:
        return f.read()

def main():
    print("=========================================")
    print("ServeWell IT Support Agent POC")
    print("=========================================\n")
    
    # We load INC-00142.json as referenced in the README (POS terminal won't start)
    # If it doesn't exist, we will pick the first one from train_index.csv
    target_ticket_file = "INC-00142.json"
    
    # Let's verify it exists or fallback
    if not (TICKETS_DIR / target_ticket_file).exists():
        import csv
        with open(ROOT_DIR / "tickets" / "train_index.csv") as f:
            reader = csv.DictReader(f)
            first_row = next(reader)
            target_ticket_file = Path(first_row['filename']).name
            
    print(f"Loading incoming ticket: {target_ticket_file}...\n")
    ticket_json = load_sample_ticket(target_ticket_file)
    ticket_data = json.loads(ticket_json)
    
    print(f"Ticket ID: {ticket_data.get('ticket_id')}")
    print(f"Subject: {ticket_data.get('subject')}")
    print(f"Priority: {ticket_data.get('priority')}")
    print(f"Asset ID: {ticket_data.get('asset_id')}")
    print("-----------------------------------------\n")
    
    print(">>> Triggering Triage Agent...")
    triage_result = run_triage_agent(ticket_json)
    print(f"Routing Decision: {triage_result.get('routing')}")
    print(f"Reasoning: {triage_result.get('reasoning')}")
    print("-----------------------------------------\n")
    
    if triage_result.get('routing') == 'L1_GUIDED':
        print(">>> Triggering L1 Resolution Agent...")
        time.sleep(1) # Small pause for effect
        final_response = run_resolution_agent(ticket_json)
        print("\n--- Final Agent Output ---")
        print(final_response)
    else:
        print("Ticket escalated or routed to Non-IT. No further automated L1 action taken.")

    print("\n=========================================")
    print("End of Execution")
    print("=========================================")

if __name__ == "__main__":
    main()
