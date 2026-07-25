# ServeWell Agentic IT Support Flow

The ServeWell IT support system is an agentic workflow that triages and resolves L1 support tickets using Large Language Models and Retrieval-Augmented Generation (RAG).

## Architecture & Interaction Flow

```mermaid
sequenceDiagram
    actor UI as Streamlit App (User)
    participant API as FastAPI Backend
    participant Triage as Triage Agent (LLM)
    participant Res as Resolution Agent (LLM)
    participant Tools as Agent Tools & ChromaDB
    participant LS as LangSmith (Observability)

    Note over UI, API: Ticket ingestion
    UI->>API: POST /triage (Ticket JSON)
    
    API->>Triage: run_triage_agent()
    Triage-->>LS: Automatically Logs Trace
    Triage-->>API: Returns Routing Decision & Reasoning
    API-->>UI: Display Decision (L1_GUIDED, L2_ESCALATION, NON_IT)

    alt If Routing == L1_GUIDED
        UI->>API: POST /resolve (Ticket JSON)
        API->>Res: run_resolution_agent()
        
        loop Reasoning & Tool Use (Max 5 Iterations)
            Res->>Res: Think / Analyze Ticket
            Res-->>LS: Logs reasoning steps
            
            alt Needs Knowledge Base
                Res->>Tools: search_knowledge_base(query)
                Tools-->>Res: Retrieve Runbooks from ChromaDB
            else Needs Context
                Res->>Tools: get_asset_info / get_store_info / check_sla
                Tools-->>Res: Return context data
            end
        end
        
        Note over Res: Agent decides on final action
        Res->>Tools: reply_to_user / resolve_ticket / escalate_to_l2
        Res-->>LS: Logs final tool action
        Res-->>API: Returns Final Response & Execution Trace
        API-->>UI: Displays Trace Steps & Final Output
    end
```

## Core Components

> [!TIP]
> **LangSmith Observability** is embedded directly into the OpenAI clients. Any call to `openai.OpenAI` (whether for the main agent loop or generating embeddings for ChromaDB) is logged seamlessly to your LangSmith project.

### 1. Triage Agent (`run_triage_agent`)
- **Role**: Initial dispatcher. Analyzes the raw incident ticket.
- **Possible Outcomes**:
  - `L1_GUIDED`: Issue can be solved using standard runbooks (e.g., POS offline, receipt printer).
  - `L2_ESCALATION`: Issue is highly complex, requires physical intervention, or is P1 critical.
  - `NON_IT`: Issue is HR/Facilities related.

### 2. Resolution Agent (`run_resolution_agent`)
- **Role**: L1 Support Specialist. Actively troubleshoots the issue iteratively.
- **Workflow**: 
  1. Analyzes symptoms.
  2. Actively searches the Knowledge Base (ChromaDB) for relevant standard operating procedures (SOPs).
  3. Looks up Asset/Store data if necessary.
  4. Decides to either reply to the user with steps, resolve the ticket if fixed, or escalate if runbooks don't cover it.

### 3. Knowledge Base / RAG (`rag_setup.py`)
- **Role**: Ingests markdown files from the `kb/` directory, chunks them, generates embeddings using `text-embedding-3-small`, and stores them locally in ChromaDB.
- **Retrieval**: When the Resolution Agent calls `search_knowledge_base`, the query is embedded and compared against the vector store to fetch relevant documents.
