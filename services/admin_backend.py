import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psycopg2
from typing import Optional

from langchain_community.vectorstores.pgvector import PGVector
from langchain_openai import OpenAIEmbeddings
from sentence_transformers import CrossEncoder

import uvicorn

app = FastAPI(title="Admin Backend API")

PGVECTOR_CONNECTION_STRING = os.environ.get("PGVECTOR_CONNECTION_STRING", "postgresql+psycopg2://postgres:password@localhost:5432/vectordb")
MAIN_COLLECTION = "main_index"
DELTA_COLLECTION = "delta_index"

# Load Reranker once globally
reranker = None
def get_reranker():
    global reranker
    if reranker is None:
        reranker = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
    return reranker

def get_embeddings():
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "dummy"
    base_url = "https://openrouter.ai/api/v1" if (not os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENROUTER_API_KEY")) else None
    
    return OpenAIEmbeddings(
        model="text-embedding-3-small",
        api_key=api_key,
        base_url=base_url,
        check_embedding_ctx_length=False
    )

class SearchRequest(BaseModel):
    query: str
    initial_k: int = 15

@app.get("/pgvector/stats")
def get_pgvector_stats():
    total_vectors = 0
    try:
        conn = psycopg2.connect(PGVECTOR_CONNECTION_STRING.replace("postgresql+psycopg2://", "postgresql://"))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM langchain_pg_embedding")
        total_vectors = cursor.fetchone()[0]
        cursor.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    return {"total_vectors": total_vectors}

@app.post("/pgvector/search")
def pgvector_search(req: SearchRequest):
    embeddings = get_embeddings()
    main_store = PGVector(
        connection_string=PGVECTOR_CONNECTION_STRING,
        embedding_function=embeddings,
        collection_name=MAIN_COLLECTION
    )
    delta_store = PGVector(
        connection_string=PGVECTOR_CONNECTION_STRING,
        embedding_function=embeddings,
        collection_name=DELTA_COLLECTION
    )
    
    candidates = []
    
    try:
        candidates.extend(delta_store.similarity_search(req.query, k=req.initial_k))
    except Exception as e:
        pass # Handle empty delta store
        
    try:
        candidates.extend(main_store.similarity_search(req.query, k=req.initial_k))
    except Exception as e:
        pass
        
    # Deduplicate
    unique_candidates = []
    seen = set()
    for doc in candidates:
        key = (doc.metadata.get("file_name"), doc.metadata.get("section_title", doc.page_content[:50]))
        if key not in seen:
            seen.add(key)
            unique_candidates.append(doc)
            
    # Cross-Encoder Reranking
    reranker = get_reranker()
    pairs = [[req.query, doc.page_content] for doc in unique_candidates]
    scores = reranker.predict(pairs)
    
    scored_results = list(zip(unique_candidates, scores))
    scored_results.sort(key=lambda x: x[1], reverse=True)
    
    top_results = []
    for doc, score in scored_results[:5]:
        top_results.append({
            "score": float(score),
            "metadata": doc.metadata,
            "page_content": doc.page_content
        })
        
    return {
        "unique_fetched": len(unique_candidates),
        "results": top_results
    }

@app.get("/pgvector/documents")
def get_pgvector_documents(collection_name: str, limit: int = 100):
    docs = []
    try:
        conn = psycopg2.connect(PGVECTOR_CONNECTION_STRING.replace("postgresql+psycopg2://", "postgresql://"))
        cursor = conn.cursor()
        cursor.execute("SELECT c.uuid, c.document, c.cmetadata FROM langchain_pg_embedding c JOIN langchain_pg_collection col ON c.collection_id = col.uuid WHERE col.name = %s LIMIT %s", (collection_name, limit))
        for row in cursor.fetchall():
            doc_id = row[0]
            page_content = row[1]
            metadata = row[2] or {}
            docs.append({
                "Doc ID": str(doc_id),
                "File": metadata.get("file_name", ""),
                "Section": metadata.get("section_title", ""),
                "Last Modified": metadata.get("last_modified", ""),
                "Content": page_content
            })
        cursor.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    return {"documents": docs}


# --- RAGAS EVALUATION ---
from services.ragas_eval import evaluate_tickets
from services.retrieval_eval import generate_synthetic_testset, evaluate_retrieval
import logging

logger = logging.getLogger(__name__)

class EvaluateTicketsRequest(BaseModel):
    csv_path: str
    folder_path: str
    k: int = 5
    search_type: str = "similarity"
    embedding_model: str = "text-embedding-3-small"
    limit: int = 0

@app.post("/ragas/evaluate_tickets")
async def api_evaluate_tickets(req: EvaluateTicketsRequest):
    """Evaluate agent performance on real tickets using a ground-truth CSV."""
    try:
        metrics, detailed_results = evaluate_tickets(
            csv_path=req.csv_path,
            folder_path=req.folder_path,
            k=req.k,
            search_type=req.search_type,
            embedding_model=req.embedding_model,
            limit=req.limit
        )
        return {
            "status": "success",
            "metrics": metrics,
            "detailed_results": detailed_results
        }
    except Exception as e:
        logger.error(f"Error during evaluation: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

class GenerateTestsetRequest(BaseModel):
    num_questions: int = 5
    embedding_model: str = "text-embedding-3-small"

@app.post("/ragas/generate_retrieval_testset")
async def api_generate_testset(req: GenerateTestsetRequest):
    try:
        csv_path, dataset = generate_synthetic_testset(num_questions=req.num_questions, embedding_model=req.embedding_model)
        return {
            "status": "success",
            "csv_path": csv_path,
            "dataset": dataset
        }
    except Exception as e:
        logger.error(f"Error generating testset: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

class EvaluateRetrievalRequest(BaseModel):
    csv_path: str
    embedding_model: str = "text-embedding-3-small"
    k: int = 5

@app.post("/ragas/evaluate_retrieval")
async def api_evaluate_retrieval(req: EvaluateRetrievalRequest):
    try:
        metrics, detailed_results = evaluate_retrieval(csv_path=req.csv_path, embedding_model=req.embedding_model, k=req.k)
        return {
            "status": "success",
            "metrics": metrics,
            "detailed_results": detailed_results
        }
    except Exception as e:
        logger.error(f"Error evaluating retrieval: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
