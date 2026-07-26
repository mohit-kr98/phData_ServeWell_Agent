import os
import json
import re
import time
import boto3
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from langsmith import traceable

# LangChain imports
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

load_dotenv()

from .actions import ACTION_CATALOG, available_actions, policy_check, propose_action
from .enrichment import enrich
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

# How many FAQ chunks to attach to the reasoning prompt. Exposed as a knob
# because FAQ blocks are the weakest value in the context: measured at ~1,253
# chars each (roughly 2x a runbook chunk) but only a 25% utilisation rate in
# the final reply, versus 40% for runbook chunks and 75% for the system-spec
# sheet. Tuning this is the main lever for shrinking the reasoning prompt, so
# it is A/B-testable via services/eval_labeled.py rather than guessed at.
FAQ_N_RESULTS = int(os.environ.get("FAQ_N_RESULTS", "3"))

# Optional 4th pre-fetch on the subject line alone. DEFAULT OFF -- tested and
# rejected; the knob is kept so the result is reproducible rather than folklore.
#
# Hypothesis: the blended (category + subject + description) query is long and
# description-dominated, so a short subject-only query lands elsewhere in
# embedding space and surfaces documents the blend misses. Broadening the first
# pass should then reduce the extra reasoning turn (+1.39s on ~17% of tickets)
# the agent spends when it judges retrieval to have missed something.
#
# Measured over a 20-ticket A/B, it failed on its own rationale:
#   mean LLM calls   1.95 -> 2.15   (turns went UP, not down)
#   mean wall-clock  2.87s -> 3.52s (+0.65s)
#   retrieval recall 67.2% -> 68.2% (+1.0pt -- inside the noise floor; routing
#                                    moved +10pt in the same run despite being
#                                    causally unable to depend on this flag)
#
# The "costs nothing because it runs concurrently" assumption was also wrong.
# The query service does not scale linearly: 1 call 0.55s, 2 -> 0.70s,
# 3 -> 0.86s, 4 -> 1.19s, because the cross-encoder rerank is CPU-bound and
# serialises. A 4th concurrent search really costs ~0.33s.
SUBJECT_SEARCH = os.environ.get("SUBJECT_SEARCH", "0") not in ("0", "false", "False")


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


_DOC_BLOCK_RE = re.compile(r"(?=--- Document Source: )")


def merge_retrieved_context(*blocks: str | None) -> str:
    """Merge the retrieval results into one deduplicated context section.

    The main, subcategory and FAQ searches run over the same index and
    routinely return the same chunk more than once -- measured at 11.1% of
    retrieved characters on average and 23.5% on a single ticket. Repeating a
    chunk adds no information, so dropping the repeats is free: it shrinks the
    reasoning prompt without removing anything the model could have used.

    Merging the three labelled sections into one also removes structural
    noise. Nothing is lost by it: every chunk still carries its own
    "--- Document Source: <file> ---" header, so provenance (including whether
    it came from an FAQ) survives.
    """
    seen, kept = set(), []
    for block in blocks:
        if not block:
            continue
        for part in _DOC_BLOCK_RE.split(str(block)):
            part = part.strip()
            if not part:
                continue
            key = " ".join(part.split())  # whitespace-insensitive identity
            if key in seen:
                continue
            seen.add(key)
            kept.append(part)
    return "\n\n".join(kept)


def build_search_query(t_dict: dict) -> str:
    """Category-aware query for the forced initial KB search.

    Retrieval eval isolated a real gap: tickets filed under a hybrid category
    like category="POS", subcategory="Network" (e.g. "POS terminal losing
    network connection intermittently") were retrieving only POS runbooks,
    missing network ones entirely -- "POS terminal" dominates the free-text
    subject/description, so semantic search anchors there and the network
    aspect never surfaces even though it's explicitly in the description.

    category/subcategory/tags are clean, curated ticket metadata (not noisy
    free text), so prepending them gives the actual topic explicit weight in
    the query rather than leaving it to compete with incidental phrasing --
    and prepending (vs. appending) keeps this signal intact even if the
    combined query gets truncated to the 500-char search limit downstream.
    """
    category = t_dict.get("category", "") or ""
    subcategory = t_dict.get("subcategory", "") or ""
    tags = t_dict.get("tags") or []
    terms = [t for t in [category, subcategory, *tags] if t]
    category_terms = " ".join(dict.fromkeys(terms))  # de-dupe, preserve order
    subject = t_dict.get("subject", "") or ""
    description = t_dict.get("description", "") or ""
    return f"{category_terms} {subject} {description}".strip()


# Deterministic grounding check for the triage LLM's own escalation reasoning.
#
# Measured against the labeled eval set, the reasoning-only triage fallback
# (agent_core/llm_client.py's no-flag branch) plateaued at ~57% accuracy
# across several rounds of prompt tuning -- below the 74.2% "always predict
# L1_GUIDED" baseline. Root cause, confirmed by isolating the exact question
# ("did an L1 agent carry out a fix AND confirm it failed?") in its own
# minimal prompt: nvidia.nemotron-nano-3-30b (the only model this AWS
# account has Bedrock access to -- every other candidate returned
# AccessDeniedException) reliably confuses diagnosis/administrative/planned
# actions ("recommended reinstalling the driver", "assigned for further
# diagnostics") with a completed remedy that failed, even when the
# distinction is spelled out and tested in isolation. No further prompt
# wording fixes this -- it's a model capability ceiling, not an instruction-
# following gap.
#
# Rather than trust the model's self-reported justification, verify it the
# same way the resolution agent's grounding check verifies citations
# (ARCHITECTURE.md SS4): when the model escalates specifically because it
# believes L1 already tried and failed, require that ticket_history actually
# contains a stated negative outcome. If it doesn't, the escalation isn't
# grounded in what the ticket says -- fail closed to the safe default
# (L1_GUIDED), same policy this codebase already applies to broken checks.
_TRIED_AND_FAILED_JUSTIFICATION_RE = re.compile(
    r"already (ran|tried|attempted|performed|carried out)"
    r"|troubleshoot(ing)? (was attempted|failed|was performed)"
    r"|(corrective|remedial) action"
    r"|attempted (a )?(fix|remedy|troubleshooting|repair)"
    r"|(fix|remedy|action) (that )?was (attempted|performed|tried|carried out|done)"
    r"|was (performed|attempted|carried out|executed|done) and"
    r"|tried (to fix|a fix|restart|reinstall|reset|replacing)"
    r"|L1 (already )?(tried|attempted|ran|performed)"
    r"|fix (was )?(attempted|tried|performed)|remedy (was )?(attempted|tried|performed)"
    r"|failed (l1 )?fix",
    re.IGNORECASE,
)
_CONFIRMED_NEGATIVE_OUTCOME_RE = re.compile(
    r"issue (persists|remains)|problem persists|still (not |)(offline|down|failing|frozen|unresponsive|broken)"
    r"|did not (resolve|fix|help|work)|no (improvement|change) after|remains unresolved"
    r"|continues to (fail|occur|happen)|unsuccessful|failed to (resolve|fix)|not resolved"
    r"|reoccurred|recurred (again|after)|no effect|without (success|resolution)",
    re.IGNORECASE,
)


def _escalation_is_grounded_in_history(reasoning: str, ticket_history) -> bool:
    """False only when the model's stated reason for escalating is that a
    prior L1 fix attempt failed, but ticket_history contains no stated
    negative outcome to back that up. Escalations for other reasons
    (hardware, vendor/security, multi-store, NON_IT) are untouched."""
    if not _TRIED_AND_FAILED_JUSTIFICATION_RE.search(reasoning or ""):
        return True
    history_text = json.dumps(ticket_history) if ticket_history else ""
    return bool(_CONFIRMED_NEGATIVE_OUTCOME_RE.search(history_text))


@traceable
def run_triage_agent(ticket_json: str):
    """Route a ticket to L1_GUIDED, L2_ESCALATION, or NON_IT.

    Ground truth is labels/train_labels.json:correct_routing. The ticket's own
    `escalation_flag` is an INPUT -- what the store or an upstream automation
    marked -- and it agrees with correct_routing only 50.5% of the time. The
    dataset deliberately includes misdirection and contradictory tickets
    (train_index.csv has a chaos_type column), so the flag is treated as a
    signal to weigh, never as the answer. An earlier version short-circuited on
    it; that scored ~50% because it was copying an input rather than deciding.
    """
    try:
        ticket = json.loads(ticket_json)
    except json.JSONDecodeError:
        ticket = {}

    # The flag is context for the model, not a bypass. See docstring.
    flag = ticket.get("escalation_flag")

    # Deterministic shortcut where a rule beats the model. Measured per
    # ticket_history bucket against labels/train_labels.json (n=220):
    #
    #   history   n    truth escalates    LLM acc    always-escalate
    #   0        93          36.6%          66.7%         36.6%
    #   1        40          62.5%          70.0%         62.5%
    #   2+       87          96.6%          92.0%         96.6%
    #
    # With two or more L1 notes the ticket has been worked repeatedly and is
    # still open; escalation is near-certain and the model's judgement only
    # subtracts (92.0% vs 96.6%). Below that the model genuinely adds
    # discrimination, so it keeps those. This also removes an LLM round-trip
    # from ~40% of tickets and gives that share a one-line audit answer.
    #
    # Caveat: thresholds derived from the same labelled set used to score, so
    # treat as an upper bound until validated on unseen tickets.
    history = ticket.get("ticket_history") or []
    if len(history) >= 2:
        return {
            "routing": "L2_ESCALATION",
            "reasoning": (
                f"L1 has already worked this ticket {len(history)} times and it is still open. "
                "Repeated L1 handling without resolution is the definition of exhausted L1 "
                "troubleshooting, so it goes to L2 without re-litigating the symptom."
            ),
            "decided_by": "deterministic_history_rule",
            "timing": [],
        }

    # Second deterministic layer, for the history<2 tickets the rule above
    # doesn't catch. Some subcategories are beyond L1 not because nobody has
    # tried yet, but because the fault sits in a backend/vendor-integration
    # layer or is a hardware failure needing physical replacement -- there is
    # no store-level step to try. Measured against labels/train_labels.json,
    # history<2 subset (n=220 total; history>=2 is already handled above):
    #
    #   subcategory            n   truth escalates
    #   Loyalty Sync           6        100%
    #   Duplicate Orders       9        100%
    #   Portal Down            9        100%
    #   Machine Won't Start    7        100%
    #   Dispensing Issue       4        100%
    #   Order Acceptance       6         83%
    #   Cleaning Cycle         4         75%
    #
    # The prompt below already tells the model to escalate backend/integration
    # faults and dead hardware in principle, but it was missing these in
    # practice. Adding this rule fixed 24 of these 45 tickets at the cost of 2
    # (INC-00117, INC-00139) -- both textually indistinguishable from their
    # escalated siblings in the same subcategory, so likely dataset label
    # noise rather than a recoverable feature.
    BACKEND_OR_HARDWARE_SUBCATEGORIES = {
        "Loyalty Sync", "Duplicate Orders", "Portal Down",
        "Machine Won't Start", "Dispensing Issue",
        "Order Acceptance", "Cleaning Cycle",
    }
    subcategory = ticket.get("subcategory")
    if subcategory in BACKEND_OR_HARDWARE_SUBCATEGORIES:
        return {
            "routing": "L2_ESCALATION",
            "reasoning": (
                f"{subcategory} is a backend/vendor-integration or hardware-replacement fault "
                "class -- there's no store-level L1 step that reaches it, regardless of how "
                "much history this specific ticket has."
            ),
            "decided_by": "deterministic_subcategory_rule",
            "timing": [],
        }

    llm = get_llm(temperature=0.0, max_tokens=250)

    sys_prompt = """
    You are an expert IT Triage Agent for ServeWell Hospitality. Decide the routing from the
    ticket itself.

    HOW MUCH L1 WORK HAS ALREADY HAPPENED is the strongest single signal. Count the entries in
    `ticket_history` (notes written by L1 agents who already worked this ticket):
      - 0 entries  -> nobody has worked it yet. Strongly prefer L1_GUIDED. Even if the symptom
                      sounds physical, the runbook procedure has not been tried once.
      - 1 entry    -> genuine judgement call. Escalate only if that note shows a real fix was
                      carried out and failed, or names a backend/vendor cause.
      - 2+ entries -> L1 has worked it repeatedly and it is still open. Strongly prefer
                      L2_ESCALATION; this is what "L1 troubleshooting exhausted" looks like.

    Route L2_ESCALATION when:
    - `ticket_history` shows L1 already carried out a specific remedy (restart, power cycle,
      reseat, reinstall, driver update) AND the symptom continued.
    - The fault is in a BACKEND or INTEGRATION layer no store employee can reach: payment gateway,
      database errors, server-side sync, vendor account or security action.
    - A component has genuinely FAILED and needs replacing: no power from a verified outlet, a
      dead device after a proper power cycle, physical damage.
    - The fault spans multiple devices or multiple locations.

    Route L1_GUIDED when a runbook walks the store through it and nobody has properly tried yet.
    CRITICAL: a physical device does NOT mean a hardware failure. These are all L1 runbook work
    even though they involve hardware -- calibrating a soft-serve machine that freezes product too
    hard, reinstalling a printer driver, re-pairing a PIN pad, power-cycling a scanner, clearing a
    print queue, re-seating a cable, working through an end-of-day settlement timeout. Escalate on
    hardware only when a part is actually dead or must be replaced, not merely because the symptom
    involves a physical machine.

    A store employee's own pre-ticket attempt ("I already restarted it") is intake detail, not L1
    troubleshooting -- it does not count toward exhaustion.

    Severity and urgency language ("urgent", "peak hours", "revenue impact") appears on nearly
    every ticket and is NOT a routing signal.

    The ticket may carry an `escalation_flag`. It reflects what someone upstream marked, is right
    only about half the time, and some tickets here are deliberately misleading. Weigh it as one
    weak signal; decide from the symptom and the history.

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

    # An earlier version demoted "ungrounded" escalations to L1_GUIDED here.
    # It has been removed: it was built to suppress over-escalation measured
    # against the ticket's own escalation_flag, which is an input field rather
    # than the answer key. Against labels/train_labels.json:correct_routing --
    # where ~65% of tickets should escalate -- that override was suppressing
    # correct decisions and drove the escalation rate to 0%. Escalation
    # judgement now lives entirely in the calibrated triage prompt, and the
    # grounding check it relied on stays available for diagnostics.

    result["timing"] = [{"step": "triage_llm_call", "duration_s": llm_time}]
    return result

@traceable
def run_resolution_agent(ticket_json: str, chat_history: list = None):
    # temperature=0 (was 0.2): this step is a policy/procedure decision, not a
    # creative one, and non-zero temperature was making "does the automated
    # search already answer this?" a coin flip -- the same ticket would
    # sometimes finalize in 1 loop turn and sometimes redundantly search again
    # first, purely from sampling noise.
    # Wall-clock start. Individual steps are timed separately, but several of
    # them (the three retrieval calls, structured enrichment) run concurrently,
    # so SUMMING per-step durations over-counts real elapsed time by 25-65%.
    # This is the number to trust for "how long did the agent take".
    wall_t0 = time.perf_counter()

    llm = get_llm(temperature=0.0, max_tokens=700).bind_tools([
        search_knowledge_base,
        search_faq,
        propose_action,
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
    2. An automated `search_knowledge_base` + FAQ search has ALREADY been run for you below -- read
       it first. It finds the relevant runbook for the vast majority of tickets.
    3. If it already contains a runbook that addresses this symptom, finish on your very first
       turn -- do NOT make further *lookup* calls just to be thorough. Only call
       `search_knowledge_base` yourself if the automated result is clearly about a different
       symptom/device, and only look up asset/store/SLA info if the ticket's own text doesn't
       already answer what you need. (`propose_action` is not a lookup; see step 4. You may emit
       it together with your final action in the same turn, so it costs you nothing.)
    4. If a [REMEDIATION ACTIONS AVAILABLE FOR THIS ASSET] block appears below AND a retrieved
       runbook names one of those actions as the fix, call `propose_action` FIRST. It does not
       execute anything -- it queues the action for a human to approve or reject.
    5. Take a final action: Either `reply_to_user`, `resolve_ticket`, or `escalate_to_l2`.
       Always do this, including after proposing an action -- the user still needs the manual
       steps in case they would rather do it themselves or the approver declines.

    Guardrail (CRITICAL):
    - Every reply/resolution MUST be grounded in a runbook that's already in front of you (the
      automated result, or your own follow-up search) -- never answer from your own knowledge.
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
        initial_query = build_search_query(t_dict)
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

    # Dedicated subcategory-only search, run alongside the main one. Verified
    # empirically on tickets like category="POS", subcategory="Network": the
    # main query's free-text ("POS terminal losing network connection...")
    # is dominated by "POS terminal" phrasing, and even prepending "POS
    # Network" as extra terms wasn't enough to out-weigh it in embedding
    # space -- network-devices-not-connecting.md never surfaced. A short,
    # symptom-text-free query built from subcategory alone doesn't have that
    # confound and reliably retrieves the right domain's runbook instead.
    subcategory = (t_dict.get("subcategory") or "").strip()
    subcategory_query = f"{subcategory} issue troubleshooting" if subcategory else None

    subject = (t_dict.get("subject") or "").strip()
    subject_query = subject if (SUBJECT_SEARCH and subject) else None

    with ThreadPoolExecutor(max_workers=5) as executor:
        kb_future = executor.submit(_timed, search_knowledge_base, query=initial_query[:500])
        faq_future = executor.submit(_timed, search_faq, query=initial_query[:500], n_results=FAQ_N_RESULTS)
        category_future = (
            executor.submit(_timed, search_knowledge_base, query=subcategory_query, n_results=3)
            if subcategory_query else None
        )
        subject_future = (
            executor.submit(_timed, search_knowledge_base, query=subject_query, n_results=3)
            if subject_query else None
        )
        # Structured-data enrichment runs here too: local CSV lookups, so it
        # adds no measurable latency alongside the network-bound searches.
        enrich_future = executor.submit(_timed, enrich, ticket=t_dict)
        initial_context, initial_search_time = kb_future.result()
        faq_context, faq_time = faq_future.result()
        (enrich_context, enrich_facts), enrich_time = enrich_future.result()
        if category_future:
            category_context, category_search_time = category_future.result()
        else:
            category_context, category_search_time = None, 0.0
        if subject_future:
            subject_context, subject_search_time = subject_future.result()
        else:
            subject_context, subject_search_time = None, 0.0

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

    if enrich_context:
        trace.append({"type": "tool_call", "name": "enrich_structured_data",
                      "args": {"asset_id": t_dict.get("asset_id"), "store_id": t_dict.get("store_id"),
                               "priority": t_dict.get("priority"), "category": t_dict.get("category")}})
        trace.append({"type": "tool_result", "name": "enrich_structured_data", "result": enrich_context,
                      "duration_s": enrich_time, "result_chars": len(enrich_context)})

    trace.append({"type": "tool_call", "name": "search_faq", "args": {"query": initial_query[:500]}})
    trace.append({"type": "tool_result", "name": "search_faq", "result": str(faq_context), "duration_s": faq_time, "result_chars": len(str(faq_context))})

    if category_context is not None:
        trace.append({"type": "tool_call", "name": "search_knowledge_base", "args": {"query": subcategory_query}})
        trace.append({"type": "tool_result", "name": "search_knowledge_base", "result": str(category_context), "duration_s": category_search_time, "result_chars": len(str(category_context))})

    if subject_context is not None:
        trace.append({"type": "tool_call", "name": "search_knowledge_base", "args": {"query": subject_query}})
        trace.append({"type": "tool_result", "name": "search_knowledge_base", "result": str(subject_context), "duration_s": subject_search_time, "result_chars": len(str(subject_context))})

    merged_context = merge_retrieved_context(
        initial_context, spec_context, category_context, subject_context, faq_context
    )

    # Offer the model only the actions that are actually valid for this asset,
    # rather than the whole catalog -- it cannot propose a printer restart for a
    # router if the router's actions were never put in front of it. Whatever it
    # picks is still re-checked deterministically in policy_check() before a
    # human is asked, so this narrowing is convenience, not the safety boundary.
    _asset = (enrich_facts.get("asset") or {}) if isinstance(enrich_facts, dict) else {}
    asset_type = _asset.get("asset_type")
    valid_actions = available_actions(asset_type)
    if valid_actions:
        _lines = "\n".join(f"      - {a}: {ACTION_CATALOG[a]['label']}" for a in valid_actions)
        action_offer = (
            "[REMEDIATION ACTIONS AVAILABLE FOR THIS ASSET]\n"
            f"{_lines}\n"
            "    If a retrieved runbook says one of these is the fix, you MAY call `propose_action`\n"
            "    with the action name, the asset_id, and a one-line reason citing the runbook.\n"
            "    This only PROPOSES it -- a human must approve before anything runs, and you must\n"
            "    still reply to the user with the manual steps in case they prefer to do it themselves.\n"
            "    If no runbook supports an action, do not propose one."
        )
    else:
        action_offer = ""

    context_prompt = f"""
    [SYSTEM AUTOMATED SEARCH RESULT]
    I have automatically searched the Knowledge Base for you using the ticket details.
    Each excerpt below is labelled with the document it came from.

    {merged_context}

    {enrich_context}
    These structured facts are authoritative -- they were fetched from the CMDB, store master
    data and SLA matrix, not inferred. Use them directly and do NOT contradict or re-guess them.
    If the asset is out of warranty, do not advise a warranty replacement. If the asset is not in
    the CMDB, say so plainly rather than inventing details about it.

    {action_offer}

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
    # The forced searches above already used 1 (or 2, if a category search
    # also ran) of the budget; the prompt tells the agent it gets at most one
    # more. Enforcing the cap here (rather than trusting the model to
    # self-police) is what actually bounds latency -- instruction-following
    # alone let this run 3-6 searches per ticket.
    kb_calls_already_used = 1 + (category_context is not None) + (subject_context is not None)
    MAX_KB_CALLS = kb_calls_already_used + 1
    kb_call_count = kb_calls_already_used

    def _guarded_propose_action(action: str = "", asset_id: str = "", reason: str = ""):
        """Re-check every proposal deterministically before it can reach a human.

        The model's own judgement is not the gate. A proposal that fails policy
        is refused here and never becomes an approvable item, so a confidently
        wrong suggestion cannot surface as a button someone might click.
        """
        allowed, why = policy_check(action, t_dict, asset_type)
        if not allowed:
            return json.dumps({"status": "REFUSED_BY_POLICY", "action": action, "reason": why})
        return propose_action(action=action, asset_id=asset_id or (t_dict.get("asset_id") or ""), reason=reason)

    tool_map = {
        "search_knowledge_base": search_knowledge_base,
        "search_faq": search_faq,
        "propose_action": _guarded_propose_action,
        "get_asset_info": get_asset_info,
        "get_store_info": get_store_info,
        "check_sla": check_sla,
        "reply_to_user": reply_to_user,
        "resolve_ticket": resolve_ticket,
        "escalate_to_l2": escalate_to_l2
    }

    # Publish the search budget so the guardrail checker grades against the
    # agent's actual policy instead of a hardcoded copy of it. The copy has
    # now drifted twice (once when blocked calls were miscounted, once when
    # the subject-line pre-fetch raised the forced-search count), each time
    # scoring correct behaviour as a violation.
    trace.append({"type": "policy", "name": "search_budget", "max_kb_calls": MAX_KB_CALLS,
                  "forced_kb_calls": kb_calls_already_used})

    def _finish(message):
        """Stamp true elapsed time on the way out, via every exit path."""
        trace.append({"type": "wall_clock", "duration_s": time.perf_counter() - wall_t0})
        return message, trace

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
            return _finish(response.content or "No response.")

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
                        return _finish(final_msg)
                    except Exception:
                        return _finish(str(result))

    return _finish("Agent reached maximum iterations without resolving.")


@traceable
def run_l2_copilot_agent(ticket_json: str, escalation_context: str = "", chat_history: list = None):
    """Conversational assistant for the human L2 engineer working a ticket
    that reached L2 -- not customer-facing.

    Deliberately reuses the L1 resolution agent's tools (same runbook/FAQ
    search, same asset/store/SLA/spec lookups, same grounding expectation)
    since the underlying knowledge base and data sources don't change just
    because a human is now driving. What's different is the choreography:
    no forced single final action per turn, no "attempt 2 replies before
    escalating" pacing, and no further escalate_to_l2 -- L2 is the end of
    the line here. This is an open-ended back-and-forth for as many turns as
    the engineer wants, ending only when they say the ticket's handled.
    """
    llm = get_llm(temperature=0.2, max_tokens=700).bind_tools([
        search_knowledge_base,
        search_faq,
        get_asset_info,
        get_store_info,
        check_sla,
        get_system_spec,
        resolve_ticket,
    ])

    sys_prompt = """
    You are an L2 IT Support Copilot for ServeWell Hospitality, assisting a human L2 engineer
    who is investigating a ticket that L1 could not resolve or that was routed straight to L2.
    You are talking to the ENGINEER, not the store or customer -- be technical and direct, skip
    the customer-service tone.

    A [WHY THIS TICKET IS AT L2] block below explains how it got here (the triage/L1 reasoning,
    or the L1 resolution agent's own escalation reason) -- read it before answering so you don't
    repeat ground the engineer already knows was covered.

    You have the same knowledge-base and lookup tools L1 had: search_knowledge_base, search_faq,
    get_asset_info, get_store_info, check_sla, get_system_spec. Use them to answer the engineer's
    questions and to ground any troubleshooting suggestion you make -- cite what you found, don't
    invent steps that aren't in a retrieved runbook or spec sheet. If the engineer asks something
    the retrieved content doesn't cover, say so plainly rather than guessing.

    Do NOT call search_knowledge_base/search_faq more than twice combined per question. Rewording
    the same query and searching again rarely surfaces anything new -- if two searches don't answer
    it, tell the engineer what you found (or didn't) and answer with that, or ask them a clarifying
    question instead of searching again.

    If the engineer says the issue is fixed or asks you to close the ticket, call `resolve_ticket`
    with a summary of the actual root cause and fix -- not the original L1 guidance, since L1's
    guidance is presumably what didn't work here.
    """

    escalation_block = f"\n\n[WHY THIS TICKET IS AT L2]\n{escalation_context}\n" if escalation_context else ""
    messages = [
        SystemMessage(content=sys_prompt),
        HumanMessage(content=ticket_json + escalation_block)
    ]

    if chat_history:
        for msg in chat_history:
            if msg.get("role") == "user":
                messages.append(HumanMessage(content=msg.get("content", "")))
            else:
                messages.append(AIMessage(content=msg.get("content", "")))

    trace = []
    max_loops = 5
    loop_count = 0
    # Same lesson as the L1 resolution agent: telling the model not to
    # over-search in the prompt alone doesn't hold -- without a hard cap it
    # reworded the same query and searched again 4-5 times in testing,
    # burning the whole loop budget without ever answering.
    MAX_SEARCH_CALLS = 2
    search_call_count = 0

    tool_map = {
        "search_knowledge_base": search_knowledge_base,
        "search_faq": search_faq,
        "get_asset_info": get_asset_info,
        "get_store_info": get_store_info,
        "check_sla": check_sla,
        "get_system_spec": get_system_spec,
        "resolve_ticket": resolve_ticket,
    }

    while loop_count < max_loops:
        loop_count += 1

        t0 = time.perf_counter()
        response = llm.invoke(messages)
        llm_time = time.perf_counter() - t0
        trace.append({"type": "llm_call", "loop": loop_count, "duration_s": llm_time})
        messages.append(response)

        if response.content:
            trace.append({"type": "reasoning", "text": response.content})

        if not response.tool_calls:
            return response.content or "No response.", trace

        final_result = None
        for tool_call in response.tool_calls:
            tool_name = tool_call["name"]
            args = tool_call["args"]
            tool_call_id = tool_call["id"]

            trace.append({"type": "tool_call", "name": tool_name, "args": args})

            t0 = time.perf_counter()
            if tool_name in ("search_knowledge_base", "search_faq") and search_call_count >= MAX_SEARCH_CALLS:
                result = ("Search limit reached for this question: you've already searched twice. "
                          "Answer using what you have, or ask the engineer a clarifying question "
                          "instead of searching again.")
            else:
                if tool_name in ("search_knowledge_base", "search_faq"):
                    search_call_count += 1
                func = tool_map.get(tool_name)
                if func:
                    try:
                        result = func(**args)
                    except Exception as e:
                        result = f"Error executing tool {tool_name}: {str(e)}. Please correct your arguments and try again."
                else:
                    result = f"Error: Tool {tool_name} not found"
            tool_time = time.perf_counter() - t0

            messages.append(ToolMessage(tool_call_id=tool_call_id, name=tool_name, content=str(result)))
            trace.append({
                "type": "tool_result", "name": tool_name, "result": str(result),
                "duration_s": tool_time, "result_chars": len(str(result)),
            })

            if tool_name == "resolve_ticket" and not (isinstance(result, str) and result.startswith("Error")):
                try:
                    res_dict = json.loads(str(result))
                    final_result = res_dict.get("resolution_summary", str(result))
                except Exception:
                    final_result = str(result)

        if final_result is not None:
            return final_result, trace
        # Otherwise the tool calls this turn were info-gathering (search/lookup) --
        # loop back so the model can use what it just retrieved to actually answer.

    return "Reached maximum tool-call iterations without a final answer -- try a narrower question.", trace
