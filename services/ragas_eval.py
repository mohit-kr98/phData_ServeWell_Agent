import os
import sys
import types
try:
    # Fix for Ragas missing vertexai dependency in new langchain_community versions
    module = types.ModuleType('langchain_community.chat_models.vertexai')
    sys.modules['langchain_community.chat_models.vertexai'] = module
    module.ChatVertexAI = None
except Exception:
    pass
import json
import datetime
from pathlib import Path
import pandas as pd
import requests

# Ragas and Langchain imports
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
import psycopg2

from ragas.testset.generator import TestsetGenerator
from ragas.testset.evolutions import simple, reasoning, multi_context
from ragas import evaluate
from ragas.metrics import context_precision, context_recall
from datasets import Dataset

PGVECTOR_CONNECTION_STRING = os.environ.get("PGVECTOR_CONNECTION_STRING", "postgresql+psycopg2://postgres:password@localhost:5432/vectordb")
MAIN_COLLECTION = "main_index"
TESTSETS_DIR = Path("data/testsets")
QUERY_URL = os.environ.get("QUERY_URL", "http://localhost:8002")

def get_ragas_llms(embedding_model="text-embedding-3-small"):
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "lm-studio"
    base_url = "https://openrouter.ai/api/v1" if (not os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENROUTER_API_KEY")) else None
    
    llm = ChatOpenAI(
        model="openai/gpt-4o",
        api_key=api_key or "dummy",
        base_url=base_url
    )

    # Initialize embeddings
    if embedding_model == "LM Studio (Local)":
        embed_base_url = "http://host.docker.internal:1234/v1"
        embed_model = "nomic-embed-text-v1.5" # typical default
    else:
        embed_base_url = None
        embed_model = embedding_model
        
    embeddings = OpenAIEmbeddings(
        model=embed_model,
        api_key=api_key,
        base_url=embed_base_url,
        check_embedding_ctx_length=False
    )
    return llm, embeddings

from ragas.metrics import answer_relevancy, faithfulness
from datasets import Dataset
TICKET_URL = os.environ.get("TICKET_URL", "http://api_service:8000")

def evaluate_tickets(csv_path: str, folder_path: str, k: int = 5, search_type: str = "similarity", embedding_model: str = "text-embedding-3-small", limit: int = 0):
    df = pd.read_csv(csv_path)
    if 'escalation_flag' not in df.columns or 'ticket_id' not in df.columns:
        raise Exception("CSV must contain 'ticket_id' and 'escalation_flag' columns.")
        
    if limit > 0:
        df = df.head(limit)
        
    folder = Path(folder_path)
    
    results = []
    questions = []
    answers = []
    contexts = []
    
    correct_escalations = 0
    total_evaluated = 0
    errors = 0
    
    for _, row in df.iterrows():
        ticket_id = row['ticket_id']
        expected_escalation = str(row['escalation_flag']).lower() == 'true'
        
        ticket_file = folder / f"{ticket_id}.json"
        if not ticket_file.exists():
            continue
            
        with open(ticket_file, 'r') as f:
            ticket_json = f.read()
            ticket_data = json.loads(ticket_json)
            
        total_evaluated += 1
        
        try:
            # 1. Triage
            res = requests.post(f"{TICKET_URL}/triage", json={"ticket_json": ticket_json}, timeout=300)
            res.raise_for_status()
            triage_res = res.json()
            routing = triage_res.get('routing', 'ERROR')
            
            actual_escalation = False
            final_answer = ""
            retrieved_ctx = []
            
            if routing != "L1_GUIDED":
                actual_escalation = True
                final_answer = triage_res.get('reasoning', '')
            else:
                # 2. Resolve
                res = requests.post(f"{TICKET_URL}/resolve", json={"ticket_json": ticket_json}, timeout=600)
                res.raise_for_status()
                resolve_res = res.json()
                final_answer = resolve_res.get('final_response', '')
                trace = resolve_res.get('trace', [])
                
                # Check if it was escalated by resolution agent
                for step in trace:
                    if step.get('type') == 'tool_call' and step.get('name') == 'escalate_to_l2':
                        actual_escalation = True
                    if step.get('type') == 'tool_result':
                        retrieved_ctx.append(step.get('result', ''))
                        
            if actual_escalation == expected_escalation:
                correct_escalations += 1
                
            questions.append(ticket_data.get('description', ticket_data.get('subject', '')))
            answers.append(final_answer)
            contexts.append(retrieved_ctx)
            
            results.append({
                "ticket_id": ticket_id,
                "expected_escalation": expected_escalation,
                "actual_escalation": actual_escalation,
                "correct": actual_escalation == expected_escalation,
                "routing": routing,
                "answer": final_answer
            })
            
        except Exception as e:
            errors += 1
            results.append({
                "ticket_id": ticket_id,
                "error": str(e)
            })
            
    metrics = {
        "total_evaluated": total_evaluated,
        "correct_predictions": correct_escalations,
        "errors": errors,
        "escalation_accuracy": correct_escalations / total_evaluated if total_evaluated > 0 else 0
    }
    
    # Run Ragas metrics if we have valid non-empty contexts and answers
    try:
        if len(questions) > 0:
            eval_data = {
                "question": questions,
                "answer": answers,
                "contexts": contexts
            }
            eval_dataset = Dataset.from_dict(eval_data)
            llm, embeddings_model = get_ragas_llms(embedding_model=embedding_model)
            
            ragas_result = evaluate(
                dataset=eval_dataset,
                metrics=[answer_relevancy, faithfulness],
                llm=llm,
                embeddings=embeddings_model
            )
            ragas_dict = ragas_result.to_pandas().to_dict(orient="records")
            
            metrics["ragas_metrics"] = {
                "answer_relevancy": ragas_result.get("answer_relevancy", 0),
                "faithfulness": ragas_result.get("faithfulness", 0)
            }
            
            # merge ragas dict with results
            for i in range(len(results)):
                if "error" not in results[i] and i < len(ragas_dict):
                    results[i]["answer_relevancy"] = ragas_dict[i].get("answer_relevancy", 0)
                    results[i]["faithfulness"] = ragas_dict[i].get("faithfulness", 0)
    except Exception as e:
        print(f"Ragas eval skipped/failed: {e}")
        
    # Save results to disk
    eval_dir = Path("data/eval_runs")
    eval_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    metrics_path = eval_dir / f"agent_eval_{timestamp}_metrics.json"
    results_path = eval_dir / f"agent_eval_{timestamp}_details.csv"
    
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=4)
        
    pd.DataFrame(results).to_csv(results_path, index=False)
        
    return metrics, results
