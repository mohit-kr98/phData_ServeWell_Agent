import os
import json
from dotenv import load_dotenv
from langsmith import traceable

# LangChain imports
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

load_dotenv()

from .tools import (
    get_asset_info,
    get_store_info,
    check_sla,
    escalate_to_l2,
    resolve_ticket,
    reply_to_user,
    search_knowledge_base
)

MODEL_NAME = "openai/gpt-oss-20b:free"

def get_llm(temperature=0.1):
    api_base = os.environ.get("OPENAI_API_BASE", "https://openrouter.ai/api/v1")
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or "lm-studio"
    model = os.environ.get("LLM_MODEL", MODEL_NAME)
    
    if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("OPENROUTER_API_KEY") and not os.environ.get("OPENAI_API_BASE"):
        print("WARNING: No API Key or local API base set. Requests may fail.")
        
    return ChatOpenAI(
        model=model,
        openai_api_base=api_base,
        openai_api_key=api_key,
        temperature=temperature
    )

@traceable
def run_triage_agent(ticket_json: str):
    llm = get_llm(temperature=0.1)
    
    sys_prompt = """
    You are an expert IT Triage Agent for ServeWell Hospitality.
    Your job is to analyze the incoming incident ticket and determine the routing.
    
    Routing Options:
    1. L1_GUIDED: The issue seems to be a common L1 issue (e.g., POS offline, receipt printer issue, Wi-Fi down) that can be handled by the Resolution Agent using runbooks.
    2. L2_ESCALATION: The issue is highly complex, requires physical intervention, or is a P1 critical issue that cannot wait for L1 guided steps.
    3. NON_IT: The issue is not related to IT (e.g., HR, facilities, general queries).
    
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
    
    response = llm.invoke(messages)
    content = response.content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
            return json.loads(content)
        return {"routing": "L2_ESCALATION", "reasoning": "Failed to parse JSON routing decision."}

@traceable
def run_resolution_agent(ticket_json: str, chat_history: list = None):
    # Bind tools directly
    llm = get_llm(temperature=0.2).bind_tools([
        search_knowledge_base,
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
    - Do NOT call `search_knowledge_base` more than 2 times. If you cannot find a relevant runbook after 2 attempts, immediately call `escalate_to_l2`.
    - You MUST NOT escalate the ticket to L2 (unless knowledge is missing) until you have first attempted to troubleshoot with the user via `reply_to_user` AT LEAST two separate times.
    """
    
    # Force an initial retrieval to guarantee the LLM has context
    try:
        t_dict = json.loads(ticket_json)
        initial_query = t_dict.get("title", "") + " " + t_dict.get("description", "")
    except:
        initial_query = ticket_json
        
    initial_context = search_knowledge_base(query=initial_query[:500])
    
    context_prompt = f"""
    [SYSTEM AUTOMATED SEARCH RESULT]
    I have automatically searched the Knowledge Base for you using the ticket details. 
    Here are the results:
    
    {initial_context}
    
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
    
    trace = []
    
    print("--- Resolution Agent Thinking ---")
    max_loops = 5
    loop_count = 0
    
    tool_map = {
        "search_knowledge_base": search_knowledge_base,
        "get_asset_info": get_asset_info,
        "get_store_info": get_store_info,
        "check_sla": check_sla,
        "reply_to_user": reply_to_user,
        "resolve_ticket": resolve_ticket,
        "escalate_to_l2": escalate_to_l2
    }
    
    while loop_count < max_loops:
        loop_count += 1
        
        response = llm.invoke(messages)
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
            
            func = tool_map.get(tool_name)
            if func:
                try:
                    result = func(**args)
                except Exception as e:
                    result = f"Error executing tool {tool_name}: {str(e)}. Please correct your arguments and try again."
            else:
                result = f"Error: Tool {tool_name} not found"
                
            messages.append(ToolMessage(
                tool_call_id=tool_call_id,
                name=tool_name,
                content=str(result)
            ))
            
            trace.append({"type": "tool_result", "name": tool_name, "result": str(result)})
            
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
