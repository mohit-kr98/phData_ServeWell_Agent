ServeWell IT Support Agent Suite
================================
POC for the phData Intelligence Platform Senior Applied AI Engineer challenge.
An agentic triage -> grounded resolution -> human-approved action system for
ServeWell's L1 IT support load.


MODELS IN USE
-------------
Three models, one per job. All version-pinned in code, not left to whatever
"latest" resolves to at run time.

  Reasoning / chat     nvidia.nemotron-nano-3-30b   (AWS Bedrock)
                        Set in agent_core/llm_client.py:BEDROCK_CHAT_MODEL.
                        Used by all three agents: Triage, Resolution, L2
                        Copilot. Overridable via the LLM_MODEL env var --
                        nothing in .env or docker-compose.yml overrides it, so
                        the hardcoded default is what actually runs.

                        This is a constraint, not a preference: it is the only
                        model this AWS account could reach. Every Claude/Nova/
                        Llama candidate returned AccessDenied when tried.

  Embedding            amazon.titan-embed-text-v2:0   (AWS Bedrock)
                        Set in services/query_pipeline.py:BEDROCK_EMBEDDING_MODEL.
                        Embeds ticket text and knowledge-base chunks for
                        PGVector similarity search. Cached by exact text
                        (embed_query_cached) so a repeated query is free.

  Reranker             cross-encoder/ms-marco-MiniLM-L-6-v2
                        Set in services/query_pipeline.py, loaded once at
                        query_pipeline_service startup via sentence-transformers.
                        NOT a Bedrock call -- runs locally, CPU-bound. Re-scores
                        PGVector's top candidates against the literal query
                        text. Measured +2.7pp hit-rate over raw embedding-
                        similarity order.

  AWS region: ap-south-1 (see .env: AWS_REGION)


ARCHITECTURE, BRIEFLY
----------------------
  Triage Agent        Routes each ticket to L1_GUIDED, L2_ESCALATION, or
                       NON_IT. Deterministic rules first (repeated L1 history,
                       fault-class subcategory); the LLM only decides genuine
                       judgement calls. No tools, no retrieval -- ticket text
                       only. agent_core/llm_client.py:run_triage_agent

  Resolution Agent     Guides an L1_GUIDED ticket to a fix. Retrieves runbooks
                       (3 concurrent queries + a deterministic spec-sheet
                       lookup), reads structured CMDB/store/SLA data, and may
                       propose one of a closed set of remediation actions.
                       Bounded loop: <=5 turns, <=2 KB searches, 1 action
                       proposal. agent_core/llm_client.py:run_resolution_agent

  L2 Copilot           Assists the human engineer on an escalated ticket.
                       Same tools as Resolution, open-ended chat, no further
                       escalation. agent_core/llm_client.py:run_l2_copilot_agent

  Safety chain         LLM proposes (closed vocabulary, agent_core/actions.py:
                       ACTION_CATALOG) -> policy_check() vetoes deterministically
                       (payment/security/multi-store/etc. never reach a human)
                       -> a human approves or denies -> execute_action() refuses
                       without approved=True, enforced in the tool itself, not
                       just the UI.

  Prompts              prompts/*.md -- every model-facing instruction lives as
                       a versioned file, loaded via prompts.load()/render(),
                       not as string literals buried in orchestration code.


REPO LAYOUT
-----------
  agent_core/       Agents, tools, actions/guardrails, prompt loader
  prompts/          The four system prompts, as files
  services/         Query pipeline (retrieval+rerank), evaluation harnesses
  api.py            FastAPI service: /triage /resolve /l2_copilot /execute_action
  app.py            Streamlit UI: live demo, evaluation, latency, admin
  kb/               Runbooks, FAQ, SOP, system-specs (the knowledge base)
  tickets/          256 synthetic tickets + train_index.csv
  labels/           train_labels.json -- the answer key tickets are graded against
  data/             Eval run history (JSON metrics + per-ticket CSVs)
  logs/             ticket_actions.jsonl -- append-only action audit trail
  presentation/      Interview deck (.pptx/.pdf) + the script that builds it


RUNNING IT
----------
1. Environment (.env, not committed):
     OPENROUTER_API_KEY=            (unused by the current agent; harmless if blank)
     AWS_ACCESS_KEY_ID=
     AWS_SECRET_ACCESS_KEY=
     AWS_REGION=ap-south-1
     LANGCHAIN_API_KEY=             (optional -- LangSmith tracing)
     LANGCHAIN_PROJECT=

2. Start everything:
     docker compose up -d
   Brings up 5 containers:
     pgvector_db            :5432   Postgres + pgvector
     data_ingestion_service  :8001   chunks/embeds kb/ into the vector store
     query_pipeline_service  :8002   retrieval + rerank, called by the agent
     admin_backend_service   :8003   ticket history persistence
     api_service             :8000   FastAPI -- the agent itself

3. Streamlit UI (run outside docker, against the containers above):
     streamlit run app.py
   Opens the Live Demo / Evaluation / Latency / Architecture & KB / Admin tabs.

4. Reproduce the evaluation numbers directly (no UI needed):
     python3 services/eval_labeled.py --limit 256          # serial, quotable latency
     python3 services/eval_labeled.py --limit 256 --workers 8   # faster, accuracy only
   Calls the live api_service for every ticket -- a real accuracy/latency/
   groundedness check, not a simulated one. Writes to data/eval_runs/.


CODE-LEVEL DOCS
----------------
Design rationale lives as comments at the decision, not in a separate doc that
drifts from the code: agent_core/llm_client.py (routing rules, prompt
recalibration history, latency fixes), agent_core/actions.py (safety model),
services/eval_labeled.py (why routing is graded against labels/train_labels.json
and not the ticket's own escalation_flag -- a measurement bug that mattered).
