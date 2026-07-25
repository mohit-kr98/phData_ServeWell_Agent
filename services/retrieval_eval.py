import os
import sys
import types
try:
    module = types.ModuleType('langchain_community.chat_models.vertexai')
    sys.modules['langchain_community.chat_models.vertexai'] = module
    module.ChatVertexAI = None
except Exception:
    pass

import json
from pathlib import Path
import pandas as pd
import requests

from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from ragas.testset.generator import TestsetGenerator
from ragas.testset.evolutions import simple, reasoning, multi_context
from ragas import evaluate
from ragas.metrics import context_precision, context_recall
from datasets import Dataset

TESTSETS_DIR = Path("data/testsets")
TESTSETS_DIR.mkdir(parents=True, exist_ok=True)
QUERY_URL = os.environ.get("QUERY_URL", "http://query_pipeline_service:8002")
KB_DIR = "kb"

def get_ragas_llms(embedding_model="text-embedding-3-small"):
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "lm-studio"
    base_url = "https://openrouter.ai/api/v1" if (not os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENROUTER_API_KEY")) else None
    
    llm = ChatOpenAI(
        model="openai/gpt-4o",
        api_key=api_key or "dummy",
        base_url=base_url
    )
    
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

def generate_synthetic_testset(num_questions: int = 5, embedding_model: str = "text-embedding-3-small"):
    """Generate a synthetic testset using documents from the kb/ directory."""
    print(f"Loading documents from {KB_DIR}...")
    loader = DirectoryLoader(KB_DIR, glob="**/*.md", loader_cls=TextLoader, show_progress=False)
    documents = loader.load()
    print(f"Loaded {len(documents)} documents.")
    
    if not documents:
        raise ValueError(f"No markdown documents found in {KB_DIR}/")
        
    generator_llm, generator_embeddings = get_ragas_llms(embedding_model=embedding_model)
    
    generator = TestsetGenerator.from_langchain(
        generator_llm,
        generator_llm,
        generator_embeddings
    )
    
    # Generate testset
    print(f"Generating {num_questions} synthetic test questions...")
    testset = generator.generate_with_langchain_docs(
        documents,
        test_size=num_questions,
        distributions={
            simple: 0.5,
            reasoning: 0.25,
            multi_context: 0.25
        }
    )
    
    df = testset.to_pandas()
    output_path = TESTSETS_DIR / "synthetic_retrieval_testset.csv"
    df.to_csv(output_path, index=False)
    print(f"Testset saved to {output_path}")
    return str(output_path), df.to_dict(orient="records")


def evaluate_retrieval(csv_path: str, embedding_model: str = "text-embedding-3-small", k: int = 5):
    """Run evaluation for context_precision and context_recall."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Testset not found at {csv_path}")
        
    df = pd.read_csv(csv_path)
    
    questions = []
    ground_truths = []
    contexts = []
    answers = []
    
    p_at_k_list = []
    r_at_k_list = []
    
    for _, row in df.iterrows():
        question = row.get("question")
        gt = row.get("ground_truth", "")
        # Ragas evaluate expects a list of strings for ground_truths.
        if isinstance(gt, str):
            gt = [gt]
            
        # Hit our retrieval endpoint
        payload = {
            "query": question,
            "n_results": k,
            "rerank": True
        }
        try:
            # use query_pipeline_service:8002 when running inside docker
            res = requests.post(f"{QUERY_URL}/query", json=payload, timeout=300)
            res.raise_for_status()
            retrieved_chunks = res.json().get("results", [])
            retrieved_text = [chunk.get("page_content", "") for chunk in retrieved_chunks]
        except Exception as e:
            print(f"Error querying {QUERY_URL}: {e}")
            retrieved_text = []
            
        questions.append(question)
        ground_truths.append(gt)
        contexts.append(retrieved_text)
        answers.append(gt[0] if gt else "")
        
        # Calculate Precision@K and Recall@K manually
        # A retrieved chunk is relevant if any ground truth string is a substring of it (or vice-versa)
        hits = 0
        for ctx in retrieved_text[:k]:
            is_hit = False
            for g in gt:
                if (g.lower().strip() in ctx.lower().strip()) or (ctx.lower().strip() in g.lower().strip()):
                    is_hit = True
                    break
            if is_hit:
                hits += 1
                
        p_at_k = hits / k if k > 0 else 0
        r_at_k = hits / len(gt) if len(gt) > 0 else 0
        p_at_k_list.append(p_at_k)
        r_at_k_list.append(r_at_k)
        
    eval_data = {
        "question": questions,
        "contexts": contexts,
        "ground_truths": ground_truths,
        "answer": answers
    }
    
    avg_p_at_k = sum(p_at_k_list) / len(p_at_k_list) if p_at_k_list else 0
    avg_r_at_k = sum(r_at_k_list) / len(r_at_k_list) if r_at_k_list else 0
    
    eval_dataset = Dataset.from_dict(eval_data)
    eval_llm, eval_embeddings = get_ragas_llms(embedding_model=embedding_model)
    
    print("Evaluating metrics with Ragas...")
    ragas_result = evaluate(
        dataset=eval_dataset,
        metrics=[context_precision, context_recall],
        llm=eval_llm,
        embeddings=eval_embeddings
    )
    
    ragas_df = ragas_result.to_pandas()
    
    # Inject P@k and R@k into detailed results
    ragas_df[f"precision@{k}"] = p_at_k_list
    ragas_df[f"recall@{k}"] = r_at_k_list
    
    metrics = {
        "context_precision": ragas_result.get("context_precision", 0),
        "context_recall": ragas_result.get("context_recall", 0),
        f"precision@{k}": avg_p_at_k,
        f"recall@{k}": avg_r_at_k
    }
    
    # Save results to disk
    import datetime
    eval_dir = Path("data/eval_runs")
    eval_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    metrics_path = eval_dir / f"retrieval_eval_{timestamp}_metrics.json"
    results_path = eval_dir / f"retrieval_eval_{timestamp}_details.csv"
    
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=4)
        
    ragas_df.to_csv(results_path, index=False)
    
    return metrics, ragas_df.to_dict(orient="records")
