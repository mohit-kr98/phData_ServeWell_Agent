import os
import asyncio
import warnings
import logging
import datetime
from collections import OrderedDict
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

# Embeddings client and store handles created once and reused across requests --
# each was previously re-created per request, paying fresh connection-pool setup
# every call, and each store re-embedded the query text independently (two
# Titan API calls for one query). Both are avoidable per-request costs.
embeddings = BedrockEmbeddings(
    model_id=BEDROCK_EMBEDDING_MODEL,
    region_name=os.environ.get("AWS_REGION", "ap-south-1")
)
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

# Embedding the query is the single largest cost in a retrieval call (~0.2-0.4s
# of network round-trip to Bedrock, vs ~0.01-0.05s for the pgvector lookup
# itself). The agent re-embeds identical text constantly: the forced
# search_knowledge_base and search_faq calls share the same query string, and
# the resolution loop often re-runs a query it already ran. Caching by exact
# text makes every repeat free.
EMBED_CACHE_MAX = 512
_embed_cache: "OrderedDict[str, list]" = OrderedDict()
_embed_cache_hits = 0
_embed_cache_misses = 0


def embed_query_cached(text: str) -> list:
    global _embed_cache_hits, _embed_cache_misses
    if text in _embed_cache:
        _embed_cache.move_to_end(text)
        _embed_cache_hits += 1
        return _embed_cache[text]
    _embed_cache_misses += 1
    vector = embeddings.embed_query(text)
    _embed_cache[text] = vector
    if len(_embed_cache) > EMBED_CACHE_MAX:
        _embed_cache.popitem(last=False)
    return vector


@app.get("/cache_stats")
def cache_stats():
    total = _embed_cache_hits + _embed_cache_misses
    return {
        "embed_cache_size": len(_embed_cache),
        "hits": _embed_cache_hits,
        "misses": _embed_cache_misses,
        "hit_rate": _embed_cache_hits / total if total else 0,
    }


class QueryRequest(BaseModel):
    query: str
    n_results: int = 5
    search_type: str = "similarity"
    rerank: bool = True
    metadata_filter: dict | None = None

@app.post("/query")
async def query_knowledge_base(req: QueryRequest):
    try:
        initial_k = max(10, req.n_results * 2)

        # Embed the query once (cached) and reuse the vector for both stores,
        # instead of letting each store's similarity_search() re-embed the same
        # text independently (was two Titan API calls for one query).
        query_vector = await asyncio.to_thread(embed_query_cached, req.query)

        def perform_search(store):
            if req.search_type == "mmr":
                return store.max_marginal_relevance_search_by_vector(query_vector, k=initial_k, fetch_k=initial_k * 3, filter=req.metadata_filter)
            return store.similarity_search_by_vector(query_vector, k=initial_k, filter=req.metadata_filter)

        # Delta and main are independent lookups -- run them concurrently
        # instead of blocking one on the other.
        delta_task = asyncio.to_thread(perform_search, delta_store)
        main_task = asyncio.to_thread(perform_search, main_store)
        delta_results, main_results = await asyncio.gather(
            delta_task, main_task, return_exceptions=True
        )

        candidates = []
        if isinstance(delta_results, Exception):
            logger.warning(f"Failed to query Delta index (maybe empty): {delta_results}")
        else:
            candidates.extend(delta_results)
        if isinstance(main_results, Exception):
            logger.warning(f"Failed to query Main index (maybe empty): {main_results}")
        else:
            candidates.extend(main_results)

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

        if req.rerank:
            # Stage 2: Cross-encoder reranking
            pairs = [[req.query, doc.page_content] for doc in initial_results]
            scores = reranker_model.predict(pairs)
            scored_results = list(zip(initial_results, scores))
            scored_results.sort(key=lambda x: x[1], reverse=True)
            final_results = [doc for doc, score in scored_results[:req.n_results]]
        else:
            # Baseline: raw embedding-similarity order, no reranking
            final_results = initial_results[:req.n_results]

        formatted_results = []
        logger.info(f"Query: '{req.query}' (rerank={req.rerank}, filter={req.metadata_filter})")
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
