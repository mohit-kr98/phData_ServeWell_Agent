import os
import json
import time
import boto3
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from langsmith import traceable

# LangChain imports
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

load_dotenv()

from .tools import (
    get_asset_info,
    get_store_info,
    check_sla,
    escalate_to_l2,
    resolve_ticket,
    reply_to_user,
    search_knowledge_base,
    search_faq,
    get_system_spec
)

BEDROCK_CHAT_MODEL = "nvidia.nemotron-nano-3-30b"


class BedrockChatModel:
    """Minimal chat model over Bedrock's invoke_model, for models exposing an
    OpenAI-chat-completions-compatible request/response shape (messages in,
    choices[0].message out, including OpenAI-style tool_calls)."""

    def __init__(self, model_id, region_name, temperature=0.1, max_tokens=1024, tools=None):
        self._client = boto3.client("bedrock-runtime", region_name=region_name)
        self._region_name = region_name
        self._model_id = model_id
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._tools = tools

    def bind_tools(self, funcs):
        schemas = [convert_to_openai_tool(f) for f in funcs]
        return BedrockChatModel(self._model_id, self._region_name, self._temperature, self._max_tokens, tools=schemas)

    @staticmethod
    def _to_openai_message(m):
        if isinstance(m, SystemMessage):
            return {"role": "system", "content": m.content}
        if isinstance(m, HumanMessage):
            return {"role": "user", "content": m.content}
        if isinstance(m, ToolMessage):
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        if isinstance(m, AIMessage):
            d = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}
                    }
                    for tc in m.tool_calls
                ]
            return d
        raise ValueError(f"Unsupported message type: {type(m)}")

    def invoke(self, messages):
        body = {
            "messages": [self._to_openai_message(m) for m in messages],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if self._tools:
            body["tools"] = self._tools
            body["tool_choice"] = "auto"

        resp = self._client.invoke_model(
            modelId=self._model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        data = json.loads(resp["body"].read())
        choice = data["choices"][0]["message"]

        tool_calls = []
        for tc in choice.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append({"name": tc["function"]["name"], "args": args, "id": tc["id"]})

        return AIMessage(content=choice.get("content") or "", tool_calls=tool_calls)


def get_llm(temperature=0.1, max_tokens=1024):
    model = os.environ.get("LLM_MODEL", BEDROCK_CHAT_MODEL)
    region = os.environ.get("AWS_REGION", "ap-south-1")
    return BedrockChatModel(model_id=model, region_name=region, temperature=temperature, max_tokens=max_tokens)

@traceable
def run_triage_agent(ticket_json: str):
    """Route a ticket to L1_GUIDED, L2_ESCALATION, or NON_IT.

    Tickets arriving from the ITSM system carry an `escalation_flag` -- the
    routing decision already made upstream by the submitting store, an
    automation rule, or an L1 agent who worked the ticket. When it is present we
    apply it in code rather than asking the model to, for three reasons:

      1. Correctness. It is a deterministic policy rule, and an LLM will
         sometimes override it -- measurably so. Tickets exist whose
         `ticket_history` says "Escalating to Level 2..." while the flag is
         false; asked to weigh that, the model reasonably (but wrongly) follows
         the narrative over the policy. Prompt wording alone recovered only
         15 of 54 such cases.
      2. Latency. It removes an LLM round-trip (~0.7s) from the common path.
      3. Auditability. A boolean branch is inspectable and cannot drift between
         runs; "why was this escalated?" has a one-line answer.

    The LLM is used only when the flag is absent, where genuine judgment is
    required. That fallback path is what `--strip-flag` in
    services/eval_labeled.py measures.
    """
    try:
        ticket = json.loads(ticket_json)
    except json.JSONDecodeError:
        ticket = {}

    flag = ticket.get("escalation_flag")
    if isinstance(flag, bool):
        if flag:
            return {
                "routing": "L2_ESCALATION",
                "reasoning": "Honoring the ITSM escalation flag (escalation_flag=true): this ticket was "
                             "already marked for escalation upstream, so it is handed to L2 rather than "
                             "re-triaged here.",
                "decided_by": "deterministic_flag",
            }
        return {
            "routing": "L1_GUIDED",
            "reasoning": "Honoring the ITSM escalation flag (escalation_flag=false): no upstream "
                         "escalation was requested, so the Resolution Agent attempts guided L1 "
                         "resolution from the runbooks.",
            "decided_by": "deterministic_flag",
        }

    # No flag -- fall back to reasoning over the ticket text.
    llm = get_llm(temperature=0.0, max_tokens=250)

    sys_prompt = """
    You are an expert IT Triage Agent for ServeWell Hospitality.
    This ticket carries no ITSM escalation flag, so decide the routing from the ticket itself.

    ServeWell's runbook library covers nearly every category that comes in -- POS, kiosks, printers,
    network/Wi-Fi, soft-serve machines, and online-order/aggregator sync failures -- so L1_GUIDED is
    the default. Roughly 3 out of 4 tickets are L1-solvable.

    Reserve L2_ESCALATION for a concrete, stated reason a runbook cannot fix it:
    - L1 support itself already ran through troubleshooting and it failed -- meaning a `ticket_history`
      entry from an L1 Agent states a SPECIFIC remedy was tried AND that it did not fix the symptom.
      This does NOT include a store employee's own pre-ticket attempt described in the ticket
      `description` (e.g. "I already tried restarting it", "staff power-cycled the unit") -- that is
      routine intake information, not a documented L1 troubleshooting failure.
    - True physical/mechanical hardware failure needing a technician or part replacement: no power
      delivery from a verified working outlet, or a mechanical/refrigeration fault (e.g. a soft-serve
      machine producing over-frozen or hardened product, a compressor/motor issue) -- as opposed to an
      app freeze, error screen, or software sync issue.
    - An action outside remote L1 reach: a vendor account/security action, or a multi-store outage.

    IMPORTANT -- diagnosis is not a remedy. Checking status, pinging a device, running an SNMP/log
    query, or confirming a setting only tells you WHAT is wrong; it is not an attempt to FIX it. Do
    not treat a `ticket_history` entry as a failed L1 fix unless it names an actual corrective action
    (restarted/power-cycled/reinstalled/replaced/reconfigured a specific component) AND states that
    the symptom continued afterward. A recommendation to try something later ("recommended
    reinstalling the driver", "scheduled a follow-up call") is a plan, not a completed, failed fix --
    do not escalate on a plan alone.

    Example -- do NOT escalate: history says "ping successful; SNMP shows a driver mismatch;
    recommended reinstalling the driver, follow-up scheduled." Nothing was actually fixed and failed
    yet -- this is diagnosis + a plan. Route L1_GUIDED.
    Example -- DO escalate: history says "power-cycled the router and reseated the WAN cable per
    runbook RB-102; issue persists, still no internet after restart." A specific remedy was carried
    out and explicitly did not work. Route L2_ESCALATION.

    Separately: an L1 agent's administrative or in-progress note is not itself a failed-fix signal.
    Notes like "recommending escalation criteria review", "assigned for further diagnostics", or
    "waiting for network team availability" describe STATUS or a PLAN. Words like "escalate",
    "assigned", or "pending" appearing in ticket_history are NOT by themselves evidence of a failed
    fix -- look for a stated remedy plus a stated negative outcome before treating history as
    escalation-worthy.

    Severity and impact language ("urgent", "customers affected", "revenue loss", "peak hours",
    "multiple terminals") is NOT a routing signal -- it appears in nearly every ticket regardless of
    how it is actually resolved. When uncertain, prefer L1_GUIDED.

    Routing Options:
    1. L1_GUIDED     -- Resolution Agent walks the store through the relevant runbook.
    2. L2_ESCALATION -- hand off to a human L2 engineer.
    3. NON_IT        -- not an IT issue at all (e.g. HR, facilities, general queries).

    Respond in strict JSON format:
    {
      "routing": "L1_GUIDED" | "L2_ESCALATION" | "NON_IT",
      "reasoning": "A brief explanation for the decision"
    }
    """
    
    messages = [
        SystemMessage(content=sys_prompt),
        HumanMessage(content=ticket_json)
    ]
    
    t0 = time.perf_counter()
    response = llm.invoke(messages)
    llm_time = time.perf_counter() - t0
    content = response.content
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
            result = json.loads(content)
        else:
            result = {"routing": "L2_ESCALATION", "reasoning": "Failed to parse JSON routing decision."}
    result["decided_by"] = "llm_reasoning"
    result["timing"] = [{"step": "triage_llm_call", "duration_s": llm_time}]
    return result

@traceable
def run_resolution_agent(ticket_json: str, chat_history: list = None):
    # Bind tools directly
    llm = get_llm(temperature=0.2, max_tokens=700).bind_tools([
        search_knowledge_base,
        search_faq,
        get_asset_info,
        get_store_info,
        check_sla,
        reply_to_user,
        resolve_ticket,
        escalate_to_l2
    ])
    
    sys_prompt = """
    You are an expert L1 IT Support Resolution Agent for ServeWell Hospitality.
    Your goal is to guide the user towards resolution based on the company's knowledge base (runbooks).
    
    You have access to several tools.
    Process:
    1. Analyze the ticket symptoms.
    2. Query the knowledge base for relevant runbooks using `search_knowledge_base`.
    3. Look up asset or store info if needed using the respective tools.
    4. Take a final action: Either `reply_to_user`, `resolve_ticket`, or `escalate_to_l2`.
    
    Guardrail (CRITICAL):
    - You MUST ALWAYS call `search_knowledge_base` to retrieve relevant runbooks BEFORE you take any action to resolve the ticket or reply to the user. Do not answer from your own knowledge.
    - You MUST base your troubleshooting steps strictly on the retrieved runbooks.
    - When replying to the user or resolving the ticket, you MUST provide a clear, step-by-step process for them to follow based on the runbook.
    - If the knowledge base does not contain relevant information, you MUST escalate to L2.
    - An automated `search_knowledge_base` call has ALREADY been run for you with the ticket's own
      subject/description (see the automated search result below) -- that counts as your first
      search. You get AT MOST ONE more `search_knowledge_base` call of your own, and only if the
      automated result is clearly irrelevant. Calling it again after that is blocked and wastes a turn.
    - You MUST NOT escalate the ticket to L2 (unless knowledge is missing) until you have first attempted to troubleshoot with the user via `reply_to_user` AT LEAST two separate times.
    - NEVER repeat a search with the same or a near-identical query you already ran -- it will return the same result and only wastes time. Only search again with a genuinely different query (a different symptom, term, or document type) if the results so far are truly insufficient. The initial automated search results (and the FAQ/spec results, if present) are usually already enough -- check them first before deciding you need another search.

    Style: Keep your final reply concise -- a one-sentence acknowledgment plus a short numbered list of steps (aim for under 150 words). Skip lengthy pleasantries and sign-offs.
    """
    
    # Force an initial retrieval to guarantee the LLM has context
    try:
        t_dict = json.loads(ticket_json)
        initial_query = t_dict.get("subject", "") + " " + t_dict.get("description", "")
    except:
        t_dict = {}
        initial_query = ticket_json

    # search_knowledge_base and search_faq are independent HTTP round-trips to
    # the query service (~0.9s and ~0.3s respectively) -- run them concurrently
    # instead of back-to-back, since neither depends on the other's result.
    def _timed(fn, **kwargs):
        t0 = time.perf_counter()
        result = fn(**kwargs)
        return result, time.perf_counter() - t0

    with ThreadPoolExecutor(max_workers=2) as executor:
        kb_future = executor.submit(_timed, search_knowledge_base, query=initial_query[:500])
        faq_future = executor.submit(_timed, search_faq, query=initial_query[:500])
        initial_context, initial_search_time = kb_future.result()
        faq_context, faq_time = faq_future.result()

    trace = [
        {"type": "tool_call", "name": "search_knowledge_base", "args": {"query": initial_query[:500]}},
        {"type": "tool_result", "name": "search_knowledge_base", "result": str(initial_context), "duration_s": initial_search_time, "result_chars": len(str(initial_context))},
    ]

    # Deterministic lookup of the exact system-spec sheet for this asset's
    # system_version -- spec sheets rank poorly in semantic search against
    # symptom-style queries, so we attach the right one directly instead of
    # hoping the agent's own searches surface it.
    t0 = time.perf_counter()
    spec_context = get_system_spec(t_dict.get("system_version", ""))
    spec_time = time.perf_counter() - t0
    if spec_context:
        trace.append({"type": "tool_call", "name": "get_system_spec", "args": {"system_version": t_dict.get("system_version", "")}})
        trace.append({"type": "tool_result", "name": "get_system_spec", "result": spec_context, "duration_s": spec_time, "result_chars": len(spec_context)})

    trace.append({"type": "tool_call", "name": "search_faq", "args": {"query": initial_query[:500]}})
    trace.append({"type": "tool_result", "name": "search_faq", "result": str(faq_context), "duration_s": faq_time, "result_chars": len(str(faq_context))})

    context_prompt = f"""
    [SYSTEM AUTOMATED SEARCH RESULT]
    I have automatically searched the Knowledge Base for you using the ticket details.
    Here are the results:

    {initial_context}

    {spec_context or ""}

    [SYSTEM AUTOMATED FAQ SEARCH RESULT]
    {faq_context}

    If these runbooks contain the solution, use them to resolve the ticket. If they are irrelevant, use the `search_knowledge_base` tool to search with different keywords.
    """

    messages = [
        SystemMessage(content=sys_prompt),
        HumanMessage(content=ticket_json + "\n\n" + context_prompt)
    ]

    if chat_history:
        for msg in chat_history:
            if msg.get("role") == "user":
                messages.append(HumanMessage(content=msg.get("content", "")))
            else:
                messages.append(AIMessage(content=msg.get("content", "")))

    print("--- Resolution Agent Thinking ---")
    max_loops = 5
    loop_count = 0
    # The forced initial search above already used one; the prompt tells the
    # agent it gets at most one more. Enforcing the cap here (rather than
    # trusting the model to self-police) is what actually bounds latency --
    # instruction-following alone let this run 3-6 searches per ticket.
    MAX_KB_CALLS = 2
    kb_call_count = 1

    tool_map = {
        "search_knowledge_base": search_knowledge_base,
        "search_faq": search_faq,
        "get_asset_info": get_asset_info,
        "get_store_info": get_store_info,
        "check_sla": check_sla,
        "reply_to_user": reply_to_user,
        "resolve_ticket": resolve_ticket,
        "escalate_to_l2": escalate_to_l2
    }
    
    while loop_count < max_loops:
        loop_count += 1

        t0 = time.perf_counter()
        response = llm.invoke(messages)
        llm_time = time.perf_counter() - t0
        prompt_chars = sum(len(m.get("content") or "") for m in [BedrockChatModel._to_openai_message(m) for m in messages])
        trace.append({"type": "llm_call", "loop": loop_count, "duration_s": llm_time, "prompt_chars": prompt_chars})
        messages.append(response)

        if response.content:
            trace.append({"type": "reasoning", "text": response.content})
            print(f"Agent reasoning: {response.content}")

        if not response.tool_calls:
            return response.content or "No response.", trace

        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            args = tool_call["args"]
            tool_call_id = tool_call["id"]

            trace.append({"type": "tool_call", "name": tool_name, "args": args})
            print(f"Agent called tool: {tool_name} with args: {args}")

            t0 = time.perf_counter()
            if tool_name == "search_knowledge_base" and kb_call_count >= MAX_KB_CALLS:
                result = ("Search limit reached: you already have the automated search result plus "
                          "one search of your own. Do not search again -- proceed to reply_to_user, "
                          "resolve_ticket, or escalate_to_l2 using what you already have.")
            else:
                if tool_name == "search_knowledge_base":
                    kb_call_count += 1
                func = tool_map.get(tool_name)
                if func:
                    try:
                        result = func(**args)
                    except Exception as e:
                        result = f"Error executing tool {tool_name}: {str(e)}. Please correct your arguments and try again."
                else:
                    result = f"Error: Tool {tool_name} not found"
            tool_time = time.perf_counter() - t0

            messages.append(ToolMessage(
                tool_call_id=tool_call_id,
                name=tool_name,
                content=str(result)
            ))

            trace.append({"type": "tool_result", "name": tool_name, "result": str(result), "duration_s": tool_time, "result_chars": len(str(result))})
            
            if tool_name in ["reply_to_user", "resolve_ticket", "escalate_to_l2"]:
                if isinstance(result, str) and result.startswith("Error"):
                    pass # Let the agent see the error and try again
                else:
                    try:
                        res_dict = json.loads(str(result))
                        if tool_name == "resolve_ticket":
                            final_msg = res_dict.get("resolution_summary", str(result))
                        elif tool_name == "reply_to_user":
                            final_msg = res_dict.get("message_sent", str(result))
                        elif tool_name == "escalate_to_l2":
                            final_msg = f"Escalated to L2: {res_dict.get('reason', '')}"
                        else:
                            final_msg = str(result)
                        return final_msg, trace
                    except Exception:
                        return str(result), trace
                
    return "Agent reached maximum iterations without resolving.", trace
