import os
import warnings
import logging
import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

# Langchain imports
from langchain_aws import BedrockEmbeddings
from langchain_community.vectorstores.pgvector import PGVector

# Reranker import
from sentence_transformers import CrossEncoder

load_dotenv()
warnings.filterwarnings("ignore")

# --- Logging Configuration ---
LOG_DIR = Path("logs/query_pipeline")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("query_pipeline")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(LOG_DIR / f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

app = FastAPI(title="Query Pipeline Service")

PGVECTOR_CONNECTION_STRING = os.environ.get("PGVECTOR_CONNECTION_STRING", "postgresql+psycopg2://postgres:password@localhost:5432/vectordb")
MAIN_COLLECTION = "main_index"
DELTA_COLLECTION = "delta_index"

# Initialize CrossEncoder globally
print("Loading CrossEncoder model...")
reranker_model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
print("CrossEncoder model loaded.")

BEDROCK_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"

class QueryRequest(BaseModel):
    query: str
    n_results: int = 3
    search_type: str = "similarity"
    embedding_model: str = BEDROCK_EMBEDDING_MODEL

@app.post("/query")
async def query_knowledge_base(req: QueryRequest):
    try:
        embeddings = BedrockEmbeddings(
            model_id=req.embedding_model,
            region_name=os.environ.get("AWS_REGION", "us-east-1")
        )

        candidates = []
        initial_k = max(10, req.n_results * 2)
        
        def perform_search(store, q, k, stype):
            if stype == "mmr":
                return store.max_marginal_relevance_search(q, k=k, fetch_k=k*3)
            return store.similarity_search(q, k=k)
            
        # Query Delta Index
        try:
            delta_store = PGVector(
                connection_string=PGVECTOR_CONNECTION_STRING,
                embedding_function=embeddings,
                collection_name=DELTA_COLLECTION
            )
            candidates.extend(perform_search(delta_store, req.query, initial_k, req.search_type))
        except Exception as e:
            logger.warning(f"Failed to query Delta index (maybe empty): {e}")
            
        # Query Main Index
        try:
            main_store = PGVector(
                connection_string=PGVECTOR_CONNECTION_STRING,
                embedding_function=embeddings,
                collection_name=MAIN_COLLECTION
            )
            candidates.extend(perform_search(main_store, req.query, initial_k, req.search_type))
        except Exception as e:
            logger.warning(f"Failed to query Main index (maybe empty): {e}")
        
        if not candidates:
            return {"status": "success", "results": "No relevant documents found."}
            
        # Deduplicate candidates: Delta chunks were added to the list first, so they take precedence
        unique_candidates = []
        seen = set()
        for doc in candidates:
            # fallback to content snippet if section_title is missing
            key = (doc.metadata.get("file_name"), doc.metadata.get("section_title", doc.page_content[:50]))
            if key not in seen:
                seen.add(key)
                unique_candidates.append(doc)
                
        initial_results = unique_candidates
            
        # Stage 2: Reranking
        pairs = [[req.query, doc.page_content] for doc in initial_results]
        scores = reranker_model.predict(pairs)
        
        # Zip, sort, and select top n
        scored_results = list(zip(initial_results, scores))
        scored_results.sort(key=lambda x: x[1], reverse=True)
        final_results = [doc for doc, score in scored_results[:req.n_results]]
            
        formatted_results = []
        logger.info(f"Query: '{req.query}'")
        for idx, doc in enumerate(final_results):
            source = doc.metadata.get('file_name', doc.metadata.get('source', 'Unknown'))
            logger.info(f"Result {idx+1} [Source: {source}]: {doc.page_content[:100]}...")
            formatted_results.append(f"--- Document Source: {source} ---\n{doc.page_content}\n")
            
        return {"status": "success", "results": "\n".join(formatted_results)}
        
    except Exception as e:
        logger.error(f"Error during query: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
