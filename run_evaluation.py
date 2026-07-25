import argparse
import requests
import sys
import json

ADMIN_URL = "http://localhost:8003"

def print_metrics(title, metrics):
    print(f"\n{'='*40}")
    print(f"{title}")
    print(f"{'='*40}")
    for k, v in metrics.items():
        if isinstance(v, float):
            print(f"{k.ljust(25)}: {v:.4f}")
        else:
            print(f"{k.ljust(25)}: {v}")
    print(f"{'='*40}\n")

def run_agent_eval(args):
    print(f"Running Agent Evaluation (Tickets: {args.tickets_folder}, Index: {args.tickets_csv})...")
    payload = {
        "csv_path": args.tickets_csv,
        "folder_path": args.tickets_folder,
        "k": args.k,
        "search_type": args.search_type,
        "embedding_model": args.embedding_model,
        "limit": args.limit
    }
    print(f"Payload: {json.dumps(payload, indent=2)}")
    print("Sending request to admin backend... (This may take several minutes depending on your LLM)")
    
    try:
        # Note: Depending on LLM, this could take minutes
        res = requests.post(f"{ADMIN_URL}/ragas/evaluate_tickets", json=payload, timeout=1200)
        res.raise_for_status()
        data = res.json()
        metrics = data.get("metrics", {})
        
        print_metrics("Agent Evaluation Metrics", metrics)
        print("Detailed results and metrics saved to: data/eval_runs/")
        
    except requests.exceptions.RequestException as e:
        print(f"Error during agent evaluation: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        sys.exit(1)

def run_retrieval_gen(args):
    print(f"Generating {args.num_questions} synthetic questions...")
    payload = {
        "num_questions": args.num_questions,
        "embedding_model": args.embedding_model
    }
    print(f"Payload: {json.dumps(payload, indent=2)}")
    print("Sending request to admin backend... (This may take a few minutes)")
    try:
        res = requests.post(f"{ADMIN_URL}/ragas/generate_retrieval_testset", json=payload, timeout=600)
        res.raise_for_status()
        data = res.json()
        print(f"Success! Testset saved to: {data.get('csv_path')}")
    except requests.exceptions.RequestException as e:
        print(f"Error during testset generation: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        sys.exit(1)

def run_retrieval_eval(args):
    print(f"Running Retrieval Evaluation on {args.testset_csv} with K={args.k}...")
    payload = {
        "csv_path": args.testset_csv,
        "embedding_model": args.embedding_model,
        "k": args.k
    }
    print(f"Payload: {json.dumps(payload, indent=2)}")
    print("Sending request to admin backend... (This may take a few minutes)")
    try:
        res = requests.post(f"{ADMIN_URL}/ragas/evaluate_retrieval", json=payload, timeout=600)
        res.raise_for_status()
        data = res.json()
        metrics = data.get("metrics", {})
        
        print_metrics("Retrieval Evaluation Metrics", metrics)
        print("Detailed results and metrics saved to: data/eval_runs/")
    except requests.exceptions.RequestException as e:
        print(f"Error during retrieval evaluation: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Response: {e.response.text}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Standalone CLI to run Agent and Retrieval Evaluations.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # --- Agent Evaluation Subparser ---
    parser_agent = subparsers.add_parser("agent", help="Run End-to-End Agent Evaluation on historical tickets.")
    parser_agent.add_argument("--tickets-csv", type=str, default="/app/tickets/train_index.csv", help="Path to ground truth CSV (in container).")
    parser_agent.add_argument("--tickets-folder", type=str, default="/app/tickets/train", help="Path to ticket JSON folder (in container).")
    parser_agent.add_argument("--k", type=int, default=5, help="Top K results for Resolution agent to fetch.")
    parser_agent.add_argument("--search-type", type=str, choices=["similarity", "mmr"], default="similarity", help="Search type for pgvector.")
    parser_agent.add_argument("--embedding-model", type=str, default="text-embedding-3-small", help="Embedding model.")
    parser_agent.add_argument("--limit", type=int, default=10, help="Batch limit (0 for all).")
    
    # --- Retrieval Testset Gen Subparser ---
    parser_gen = subparsers.add_parser("generate", help="Generate synthetic testset from the Knowledge Base.")
    parser_gen.add_argument("--num-questions", type=int, default=5, help="Number of questions to generate.")
    parser_gen.add_argument("--embedding-model", type=str, default="text-embedding-3-small", help="Embedding model.")
    
    # --- Retrieval Eval Subparser ---
    parser_retrieval = subparsers.add_parser("retrieval", help="Run Standalone Retrieval Evaluation.")
    parser_retrieval.add_argument("--testset-csv", type=str, default="data/testsets/synthetic_retrieval_testset.csv", help="Path to generated testset CSV.")
    parser_retrieval.add_argument("--embedding-model", type=str, default="text-embedding-3-small", help="Embedding model.")
    parser_retrieval.add_argument("--k", type=int, default=5, help="Top K chunks to fetch.")
    
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
        
    args = parser.parse_args()
    
    if args.command == "agent":
        run_agent_eval(args)
    elif args.command == "generate":
        run_retrieval_gen(args)
    elif args.command == "retrieval":
        run_retrieval_eval(args)

if __name__ == "__main__":
    main()
