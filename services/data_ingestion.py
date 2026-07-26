import os
import shutil
import warnings
import logging
import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

# Langchain imports
from langchain_core.documents import Document
from pydantic import BaseModel

from typing import List, Dict, Any
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
import tiktoken

load_dotenv()
warnings.filterwarnings("ignore")

# --- Logging Configuration ---
LOG_DIR = Path("logs/data_ingestion")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "ingestion.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("DataIngestionPipeline")

app = FastAPI(title="Knowledge Base Ingestion API")

PGVECTOR_CONNECTION_STRING = os.environ.get("PGVECTOR_CONNECTION_STRING", "postgresql+psycopg2://postgres:password@localhost:5432/vectordb")
MAIN_COLLECTION = "main_index"
DELTA_COLLECTION = "delta_index"
BEDROCK_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"


def get_embeddings():
    from langchain_aws import BedrockEmbeddings
    return BedrockEmbeddings(
        model_id=BEDROCK_EMBEDDING_MODEL,
        region_name=os.environ.get("AWS_REGION", "ap-south-1")
    )

class IngestRequest(BaseModel):
    directory: str = "kb"

class AddChunkRequest(BaseModel):
    content: str
    file_name: str = "manual_entry"
    section_title: str = ""

class DeleteChunkRequest(BaseModel):
    doc_id: str
    index_type: str = "delta"

class MarkdownParser:
    def __init__(self):
        self.md = MarkdownIt("commonmark")
        
    def _format_table_text(self, text: str) -> str:
        if "|" not in text or "---" not in text:
            return text
            
        lines = text.strip().split('\n')
        output_lines = []
        in_table = False
        headers = []
        
        for i, line in enumerate(lines):
            # Check if this line is a table header by looking at the next line
            if "|" in line and i + 1 < len(lines) and "---" in lines[i+1] and "|" in lines[i+1]:
                in_table = True
                headers = [h.strip() for h in line.strip().strip('|').split('|')]
                continue
                
            if in_table:
                if "---" in line and "|" in line:
                    continue # Skip separator
                
                if "|" not in line:
                    in_table = False
                    output_lines.append(line)
                    continue
                    
                # Process table row
                values = [v.strip() for v in line.strip().strip('|').split('|')]
                row_parts = []
                for j in range(min(len(headers), len(values))):
                    # Ignore empty headers/values or pure dash lines
                    if headers[j] and values[j] and not all(c == '-' for c in headers[j]) and not all(c == '-' for c in values[j]):
                        row_parts.append(f"{headers[j]}: {values[j]}")
                if row_parts:
                    output_lines.append(", ".join(row_parts) + ".")
            else:
                output_lines.append(line)
                
        return "\n".join(output_lines)

    def parse(self, content: str, base_metadata: Dict[str, Any]) -> List[Document]:
        tokens = self.md.parse(content)
        node = SyntaxTreeNode(tokens)
        
        sections = []
        
        # A simple state machine to group nodes under headings
        current_heading = "General"
        current_content = []
        current_metadata = base_metadata.copy()
        
        # We will iterate through block-level nodes
        for child in node.children:
            if child.type == "heading":
                # Flush current section if it has content
                text_content = "\n\n".join(current_content).strip()
                if text_content:
                    meta = current_metadata.copy()
                    
                    breadcrumb = " > ".join([meta[f"Header {i}"] for i in range(1, 7) if f"Header {i}" in meta])
                    if not breadcrumb:
                        breadcrumb = current_heading
                        
                    meta["section_type"] = self._determine_section_type(text_content, current_heading, meta.get("folder", ""))
                    meta["section_title"] = breadcrumb
                    
                    full_content = f"{breadcrumb}\n\n{text_content}"
                    
                    sections.append(Document(
                        page_content=full_content,
                        metadata=meta
                    ))
                    current_content.clear()
                
                # Get heading text
                h_text = ""
                for inline_child in child.children:
                    if inline_child.type == "inline":
                        h_text += inline_child.content
                current_heading = h_text.strip()
                
                # Add heading to metadata
                level = child.tag
                header_level = int(level[1])
                current_metadata[f"Header {header_level}"] = current_heading
                
                # Clear lower level headers
                for i in range(header_level + 1, 7):
                    current_metadata.pop(f"Header {i}", None)
                
            elif child.type == "paragraph":
                p_text = ""
                for inline_child in child.children:
                    if inline_child.type == "inline":
                        p_text += inline_child.content
                        
                p_text = self._format_table_text(p_text)
                current_content.append(p_text)
                
            elif child.type in ["bullet_list", "ordered_list"]:
                # Very basic list extraction
                list_text = self._extract_list_text(child)
                current_content.append(list_text)
                
            elif child.type == "table":
                # A crude way to extract table text
                # Ideally you'd render it or parse deeply
                current_content.append("[Table Data]")
                
            elif child.type in ["fence", "code_block"]:
                lang = child.info if hasattr(child, "info") and child.info else ""
                # markdown-it tokens store the content in .content
                code_content = f"```{lang}\n{child.content}```"
                current_content.append(code_content)
                
        # Flush last section
        text_content = "\n\n".join(current_content).strip()
        if text_content:
            meta = current_metadata.copy()
            
            breadcrumb = " > ".join([meta[f"Header {i}"] for i in range(1, 7) if f"Header {i}" in meta])
            if not breadcrumb:
                breadcrumb = current_heading
                
            meta["section_type"] = self._determine_section_type(text_content, current_heading, meta.get("folder", ""))
            meta["section_title"] = breadcrumb
            
            full_content = f"{breadcrumb}\n\n{text_content}"
            
            sections.append(Document(
                page_content=full_content,
                metadata=meta
            ))
            
        return sections
        
    def _extract_list_text(self, node: SyntaxTreeNode, indent: int = 0) -> str:
        lines = []
        prefix = " " * indent + "- "
        for item in node.children:
            if item.type == "list_item":
                for child in item.children:
                    if child.type == "paragraph":
                        for inline in child.children:
                            if inline.type == "inline":
                                lines.append(prefix + inline.content)
                    elif child.type in ["bullet_list", "ordered_list"]:
                        lines.append(self._extract_list_text(child, indent + 2))
        return "\n".join(lines)
        
    def _determine_section_type(self, content: str, title: str, folder: str = "") -> str:
        # Table supersedes folder name
        if "[Table Data]" in content or ("|" in content and "---" in content):
            return "table"
            
        folder_lower = folder.lower()
        if "faq" in folder_lower:
            return "faq"
        elif "runbooks" in folder_lower or "procedure" in folder_lower:
            return "procedure"
            
        # Fallbacks
        title_lower = title.lower()
        if "**Q:" in content and "A:" in content:
            return "faq"
        if "step" in title_lower or "procedure" in title_lower:
            return "procedure"
            
        return "general"

class SectionChunker:
    # The reranker (cross-encoder/ms-marco-MiniLM-L-6-v2, loaded in
    # query_pipeline.py) has a hard 512 WordPiece-token limit on the *pair*
    # (query, chunk) it scores -- not the chunk alone. Sizing chunks only
    # against cl100k_base (a different tokenizer, used for the embedding
    # model's much larger context window) let chunks through that silently
    # truncate inside the reranker: BPE and WordPiece don't produce the same
    # token count for the same text, especially for hyphenated asset IDs and
    # technical jargon, and cl100k's 1000-token budget doesn't leave any room
    # for the query itself once converted.
    RERANKER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    RERANKER_MAX_LENGTH = 512
    RERANKER_QUERY_RESERVE = 200  # room for the query (up to ~500 chars) + [CLS]/[SEP]/[SEP]

    def __init__(self, max_tokens: int = 1000):
        self.max_tokens = max_tokens
        self.encoder = tiktoken.get_encoding("cl100k_base")

        from transformers import AutoTokenizer
        self.reranker_tokenizer = AutoTokenizer.from_pretrained(self.RERANKER_MODEL_NAME)
        self.reranker_safe_tokens = self.RERANKER_MAX_LENGTH - self.RERANKER_QUERY_RESERVE
        # Scales a WordPiece count onto the same numeric scale as max_tokens,
        # so the existing cl100k-based split logic below also enforces the
        # reranker's real limit without duplicating every chunking method.
        self._reranker_scale = self.max_tokens / self.reranker_safe_tokens

    def _count_tokens(self, text: str) -> int:
        if not text:
            return 0
        cl100k_count = len(self.encoder.encode(text))
        wordpiece_count = len(self.reranker_tokenizer.encode(text, add_special_tokens=False))
        return max(cl100k_count, int(wordpiece_count * self._reranker_scale))
        
    def chunk(self, documents: List[Document]) -> List[Document]:
        final_chunks = []
        for doc in documents:
            if self._count_tokens(doc.page_content) <= self.max_tokens:
                final_chunks.extend(self._enforce_reranker_limit(doc))
                continue
                
            sec_type = doc.metadata.get("section_type", "general")
            section_title = doc.metadata.get("section_title", "")

            # The section title gets prepended to every resulting chunk below,
            # so reserve its token cost first -- otherwise a chunk sized right
            # up against max_tokens can end up over budget once the title is
            # added back on, silently escaping the cap this method exists to
            # enforce.
            original_max_tokens = self.max_tokens
            if section_title:
                self.max_tokens = max(1, self.max_tokens - self._count_tokens(f"{section_title}\n\n"))
            try:
                if sec_type == "faq":
                    doc_chunks = self._chunk_faq(doc)
                elif sec_type == "procedure":
                    doc_chunks = self._chunk_procedure(doc)
                elif sec_type == "table":
                    doc_chunks = self._chunk_table(doc)
                else:
                    doc_chunks = self._chunk_generic(doc)
            finally:
                self.max_tokens = original_max_tokens

            if section_title:
                for c in doc_chunks:
                    if not c.page_content.startswith(section_title):
                        c.page_content = f"{section_title}\n\n{c.page_content}"

            for c in doc_chunks:
                final_chunks.extend(self._enforce_reranker_limit(c))

        logger.info(f"Chunked {len(documents)} logic sections into {len(final_chunks)} chunks using semantic overlap policies.")
        return final_chunks

    def _enforce_reranker_limit(self, doc: Document) -> List[Document]:
        """Hard backstop for the reranker's WordPiece budget.

        The per-section-type chunkers above split at unit boundaries (a
        table row, an FAQ answer, a procedure paragraph) and can't go finer
        than one whole unit -- so a single unusually long unit (a dense
        error-code table row, a long FAQ answer) can still come out over
        budget even though the surrounding logic is sized correctly. This
        catches that residual case by hard-slicing at the token level,
        which token-budget sizing upstream can't guarantee against.
        """
        ids = self.reranker_tokenizer.encode(doc.page_content, add_special_tokens=False)
        if len(ids) <= self.reranker_safe_tokens:
            return [doc]

        pieces = []
        for start in range(0, len(ids), self.reranker_safe_tokens):
            piece_ids = ids[start:start + self.reranker_safe_tokens]
            piece_text = self.reranker_tokenizer.decode(piece_ids)
            pieces.append(Document(page_content=piece_text, metadata=doc.metadata.copy()))
        return pieces

    def _chunk_generic(self, doc: Document) -> List[Document]:
        sentences = doc.page_content.replace('\n\n', ' ').split('. ')
        chunks = []
        current_chunk = ""
        last_sentence = ""
        
        for s in sentences:
            if not s.strip():
                continue
            
            sentence_text = s + ". "
            if self._count_tokens(current_chunk) + self._count_tokens(sentence_text) > self.max_tokens and current_chunk:
                chunks.append(Document(page_content=current_chunk.strip(), metadata=doc.metadata.copy()))
                # Overlap: approx last sentence
                current_chunk = f"{last_sentence}{sentence_text}" if last_sentence else sentence_text
            else:
                current_chunk += sentence_text
                
            last_sentence = sentence_text
            
        if current_chunk:
            chunks.append(Document(page_content=current_chunk.strip(), metadata=doc.metadata.copy()))
        return chunks

    def _chunk_faq(self, doc: Document) -> List[Document]:
        paragraphs = doc.page_content.split('\n\n')
        chunks = []
        current_chunk = ""
        current_question = ""
        last_paragraph = ""

        for p in paragraphs:
            if "**Q:" in p or "Q:" in p:
                current_question = p

            if self._count_tokens(current_chunk) + self._count_tokens(p) > self.max_tokens and current_chunk:
                chunks.append(Document(page_content=current_chunk, metadata=doc.metadata.copy()))
                # Overlap: Question + last paragraph
                overlap = f"{current_question}\n\n{last_paragraph}" if current_question else last_paragraph
                current_chunk = f"{overlap}\n\n{p}" if overlap else p
            else:
                if current_chunk:
                    current_chunk += f"\n\n{p}"
                else:
                    current_chunk = p
            last_paragraph = p
            
        if current_chunk:
            chunks.append(Document(page_content=current_chunk, metadata=doc.metadata.copy()))
        return chunks

    def _chunk_procedure(self, doc: Document) -> List[Document]:
        paragraphs = doc.page_content.split('\n')
        chunks = []
        current_chunk = ""
        last_step = ""

        for p in paragraphs:
            if not p.strip():
                continue

            if self._count_tokens(current_chunk) + self._count_tokens(p) > self.max_tokens and current_chunk:
                chunks.append(Document(page_content=current_chunk.strip(), metadata=doc.metadata.copy()))
                # Overlap: previous step
                current_chunk = f"{last_step}\n{p}" if last_step else p
            else:
                if current_chunk:
                    current_chunk += f"\n{p}"
                else:
                    current_chunk = p
            last_step = p
            
        if current_chunk:
            chunks.append(Document(page_content=current_chunk.strip(), metadata=doc.metadata.copy()))
        return chunks

    def _chunk_table(self, doc: Document) -> List[Document]:
        lines = doc.page_content.split('\n')
        table_header = []
        chunks = []
        current_chunk = ""

        for line in lines:
            if "|" in line and len(table_header) < 2:
                table_header.append(line)

            if self._count_tokens(current_chunk) + self._count_tokens(line) > self.max_tokens and current_chunk:
                chunks.append(Document(page_content=current_chunk.strip(), metadata=doc.metadata.copy()))
                # Overlap: table header
                header_str = "\n".join(table_header)
                current_chunk = f"{header_str}\n{line}" if header_str else line
            else:
                if current_chunk:
                    current_chunk += f"\n{line}"
                else:
                    current_chunk = line
                    
        if current_chunk:
            chunks.append(Document(page_content=current_chunk.strip(), metadata=doc.metadata.copy()))
        return chunks

def load_documents(kb_dir: Path, on_docs_yielded=None) -> dict[str, list[Document]]:
    """Load all markdown documents from the KB directory, grouped by folder."""
    documents_dict = {}

    logger.info(f"Starting document load from directory: {kb_dir}")

    print("Markdown Parser Starting")
    parser = MarkdownParser()
    print("Starting Section Chunker")
    chunker = SectionChunker(max_tokens=1000)

    # Collect all markdown and json files
    md_files = []
    for root, _, files in os.walk(kb_dir):
        for file in files:
            if file.endswith('.md'):
                md_files.append(Path(root) / file)

    def process_file(file_path: Path):
        file = file_path.name
        print(f"\\n➡️ [INGESTION] Processing file: {file_path}")
        logger.info(f"Found file: {file_path}")
        try:
            import time
            for _ in range(5):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    break
                except OSError as e:
                    if e.errno == 35:
                        time.sleep(0.2)
                        continue
                    else:
                        raise e
            else:
                raise Exception(f"Failed to read file after retries due to EDEADLK: {file_path}")
            
            if file.endswith('.json'):
                try:
                    import json
                    ticket_data = json.loads(content)
                    md_content = f"# Ticket {ticket_data.get('ticket_id', 'Unknown')}\n\n"
                    md_content += f"**Subject:** {ticket_data.get('subject', 'No Subject')}\n\n"
                    md_content += f"**Category:** {ticket_data.get('category', 'None')}\n\n"
                    md_content += f"**Priority:** {ticket_data.get('priority', 'None')}\n\n"
                    md_content += f"**Description:**\n{ticket_data.get('description', '')}\n\n"
                    
                    if 'comments' in ticket_data and isinstance(ticket_data['comments'], list):
                        md_content += "## Comments\n\n"
                        for comment in ticket_data['comments']:
                            md_content += f"**{comment.get('author', 'User')}** ({comment.get('date', '')}):\n"
                            md_content += f"{comment.get('text', '')}\n\n"
                    
                    if 'resolution' in ticket_data and ticket_data['resolution']:
                        md_content += f"## Resolution\n\n{ticket_data['resolution']}\n\n"
                    
                    content = md_content
                except Exception as json_e:
                    logger.error(f"Failed to parse JSON file {file_path}: {json_e}")
                    return None, []

            rel_path = file_path.relative_to(kb_dir)

            if len(rel_path.parts) > 1:
                category = rel_path.parts[0]
            else:
                category = "general"

            mtime = file_path.stat().st_mtime
            last_modified_str = datetime.datetime.fromtimestamp(mtime).isoformat()

            base_metadata = {
                "file_name": file,
                "folder": category,
                "path": str(file_path),
                "document_type": category,
                "source": "knowledge_base",
                "last_modified": last_modified_str
            }

            sections = parser.parse(content, base_metadata)
            docs = chunker.chunk(sections)

            print(f"✅ [INGESTION] Successfully processed {file} into {len(docs)} chunks for category '{category}'")
            logger.info(f"Successfully processed {file}: generated {len(sections)} sections and {len(docs)} chunks.")
            
            return category, docs
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            return None, []

    # Process files sequentially to avoid Mac Docker volume deadlocks (errno 35)
    for path in md_files:
        category, docs = process_file(path)
        if category:
            if on_docs_yielded:
                on_docs_yielded(category, docs)
                
            if category not in documents_dict:
                documents_dict[category] = []
            documents_dict[category].extend(docs)

    total_chunks = sum(len(docs) for docs in documents_dict.values())
    logger.info(f"Total chunks successfully loaded into memory: {total_chunks} across {len(documents_dict)} categories")
    return documents_dict

@app.post("/preview")
async def preview_data(req: IngestRequest):
    logger.info(f"--- Starting preview run for directory: {req.directory} ---")

    kb_dir = Path(req.directory)
    if not kb_dir.exists() or not kb_dir.is_dir():
        logger.error(f"Directory '{req.directory}' does not exist or is not a directory.")
        raise HTTPException(status_code=400, detail=f"Directory '{req.directory}' does not exist.")

    try:
        documents_dict = load_documents(kb_dir)

        # Compile stats and samples
        samples = {}
        for category, docs in documents_dict.items():
            samples[category] = []
            for d in docs:
                samples[category].append({
                    "metadata": d.metadata,
                    "content": d.page_content
                })

        total_chunks = sum(len(docs) for docs in documents_dict.values())

        return {
            "categories": list(documents_dict.keys()),
            "total_chunks": total_chunks,
            "samples": samples,
            "message": f"Successfully parsed {total_chunks} chunks across {len(documents_dict)} categories."
        }

    except Exception as e:
        logger.error(f"Error during preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ingest")
async def ingest_data(req: IngestRequest):
    logger.info(f"--- Starting new data ingestion pipeline run for directory: {req.directory} ---")

    kb_dir = Path(req.directory)
    if not kb_dir.exists() or not kb_dir.is_dir():
        logger.error(f"Directory '{req.directory}' does not exist or is not a directory.")
        raise HTTPException(status_code=400, detail=f"Directory '{req.directory}' does not exist.")

    original_project = os.environ.get("LANGCHAIN_PROJECT")
    ingestion_project = os.environ.get("LANGCHAIN_PROJECT_INGESTION", "servewell-data-ingestion")
    os.environ["LANGCHAIN_PROJECT"] = ingestion_project

    try:
        from langchain_community.vectorstores.pgvector import PGVector

        logger.info(f"Initializing embedding generation client ({BEDROCK_EMBEDDING_MODEL})...")
        embeddings = get_embeddings()

        vectorstore = PGVector(
            connection_string=PGVECTOR_CONNECTION_STRING,
            embedding_function=embeddings,
            collection_name=DELTA_COLLECTION
        )
        logger.info("Initialized PGVector Delta index.")

        total_ingested = 0
        
        def handle_docs(category, docs):
            nonlocal vectorstore, total_ingested
            if not docs:
                return
            vectorstore.add_documents(docs)
            total_ingested += len(docs)
            print(f"Added {len(docs)} chunks to PGVector Delta Index (Total Appended: {total_ingested})")

        print("Loading Documents and streaming to PGVector Delta")
        documents_dict = load_documents(kb_dir, on_docs_yielded=handle_docs)

        if total_ingested == 0:
            logger.warning("No documents found to index. Aborting.")
            raise HTTPException(status_code=400, detail="No documents found to index.")

        logger.info(f"Successfully generated embeddings and persisted {total_ingested} documents to PGVector Delta collection.")

        logger.info("--- Data ingestion pipeline run completed successfully ---")
        return {"status": "success", "message": f"Successfully ingested {total_ingested} documents into the Delta Index."}

    except Exception as e:
        logger.error(f"Critical error during ingestion pipeline: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Revert back to original project so we don't affect other components
        if original_project is not None:
            os.environ["LANGCHAIN_PROJECT"] = original_project
        else:
            if "LANGCHAIN_PROJECT" in os.environ:
                del os.environ["LANGCHAIN_PROJECT"]

@app.post("/add_chunk")
async def add_chunk(req: AddChunkRequest):
    try:
        from langchain_community.vectorstores.pgvector import PGVector
        from langchain_core.documents import Document

        embeddings = get_embeddings()

        doc = Document(
            page_content=req.content,
            metadata={
                "file_name": req.file_name,
                "section_title": req.section_title,
                "document_type": "manual",
                "last_modified": datetime.datetime.now().isoformat()
            }
        )
        
        store = PGVector(
            connection_string=PGVECTOR_CONNECTION_STRING,
            embedding_function=embeddings,
            collection_name=DELTA_COLLECTION
        )
        store.add_documents([doc])
        return {"status": "success", "message": "Chunk added to Delta index successfully."}
    except Exception as e:
        logger.error(f"Error adding chunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/delete_chunk")
async def delete_chunk(req: DeleteChunkRequest):
    try:
        from langchain_community.vectorstores.pgvector import PGVector

        embeddings = get_embeddings()

        collection = MAIN_COLLECTION if req.index_type.lower() == "main" else DELTA_COLLECTION
        
        store = PGVector(
            connection_string=PGVECTOR_CONNECTION_STRING,
            embedding_function=embeddings,
            collection_name=collection
        )
            
        store.delete(ids=[req.doc_id])
        return {"status": "success", "message": f"Successfully deleted document '{req.doc_id}' from {req.index_type} index."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting chunk: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rebuild_main_index")
async def rebuild_main_index():
    """Merges the Delta index into the Main HNSW index, then wipes the Delta index."""
    try:
        from langchain_community.vectorstores.pgvector import PGVector
        import psycopg2

        embeddings = get_embeddings()

        # Connect to Postgres directly to move chunks from delta to main
        conn_str = PGVECTOR_CONNECTION_STRING.replace("+psycopg2", "")
        conn = psycopg2.connect(conn_str)
        cursor = conn.cursor()
        
        # PGVector tables are usually langchain_pg_embedding and langchain_pg_collection
        # Let's get the collection IDs
        cursor.execute("SELECT uuid, name FROM langchain_pg_collection WHERE name IN (%s, %s)", (DELTA_COLLECTION, MAIN_COLLECTION))
        collections = cursor.fetchall()
        
        delta_uuid = None
        main_uuid = None
        for cid, name in collections:
            if name == DELTA_COLLECTION:
                delta_uuid = cid
            elif name == MAIN_COLLECTION:
                main_uuid = cid
                
        if not delta_uuid:
            return {"status": "success", "message": "Delta index is empty. Nothing to merge."}
            
        if not main_uuid:
            # Main collection doesn't exist yet, just create it by initializing PGVector
            main_store = PGVector(
                connection_string=PGVECTOR_CONNECTION_STRING,
                embedding_function=embeddings,
                collection_name=MAIN_COLLECTION
            )
            cursor.execute("SELECT uuid FROM langchain_pg_collection WHERE name = %s", (MAIN_COLLECTION,))
            main_uuid = cursor.fetchone()[0]
            
        # Move all embeddings from delta to main
        cursor.execute(
            "UPDATE langchain_pg_embedding SET collection_id = %s WHERE collection_id = %s",
            (main_uuid, delta_uuid)
        )
        moved_count = cursor.rowcount
        conn.commit()
        
        cursor.close()
        conn.close()
        
        return {"status": "success", "message": f"Successfully merged {moved_count} delta chunks into the Main HNSW Index."}

    except Exception as e:
        logger.error(f"Error rebuilding main index: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Data Ingestion Service on port 8001...")
    uvicorn.run(app, host="0.0.0.0", port=8001)