# ServeWell IT Support Agent POC

This repository contains the proof-of-concept (POC) for ServeWell's agentic IT Support triage system, built on the simulated phData Intelligence Platform.

## Architecture

The solution uses a two-agent orchestration system:

1.  **Triage Agent**: A lightweight router that analyzes incoming JSON tickets. It determines whether the issue is a standard L1 IT issue, a critical P1/complex L2 escalation, or a non-IT request.
2.  **Resolution Agent**: An L1 Support Agent equipped with native tool-calling capabilities. It uses Retrieval-Augmented Generation (RAG) to search the knowledge base and invokes data tools to gather context before formulating a response.

### Components

*   `agent_core/llm_client.py`: Contains the system prompts and orchestration logic for the Triage and Resolution agents.
*   `agent_core/tools.py`: Implements the structured data lookup tools (`get_asset_info`, `get_store_info`, `check_sla`) and simulated action tools (`escalate_to_l2`, `resolve_ticket`, `reply_to_user`).
*   `agent_core/rag_setup.py`: Script to parse the markdown runbooks in `kb/`, embed them using `sentence-transformers`, and index them in a local persistent `chromadb` instance.

## Guardrails

*   **Grounding**: The Resolution Agent is strictly instructed to query the RAG system (`search_knowledge_base` tool) *before* providing any troubleshooting steps.
*   **Confidence Checks**: If the retrieved documents do not match the issue or confidence is low, the agent is instructed to use the `escalate_to_l2` tool instead of hallucinating an answer.

## Setup & Running Locally

1.  **Install dependencies**:
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

2.  **Initialize the RAG Vector DB**:
    ```bash
    python -m agent_core.rag_setup
    ```

3.  **Run the POC pipeline via CLI**:
    *Ensure you have your API keys set (e.g., `OPENROUTER_API_KEY`) if your LLM SDK requires it.*
    ```bash
    python main.py
    ```

4.  **Run the Microservices (FastAPI + Streamlit)**:
    This architecture uses two separate services.
    
    *Terminal 1 (Backend API):*
    ```bash
    export OPENROUTER_API_KEY="your_api_key_here"
    source .venv/bin/activate
    python api.py
    ```
    
    *Terminal 2 (Frontend UI):*
    ```bash
    source .venv/bin/activate
    streamlit run app.py
    ```
    This will launch a web interface where you can select tickets from the queue and visualize the agent's thought process, tool calls, and final resolution.

The CLI script `main.py` will load a sample ticket, run the Triage Agent, and if routed to L1, trigger the Resolution Agent. The Streamlit UI provides a more interactive exploration.
