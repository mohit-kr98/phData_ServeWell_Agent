import json


def _mask_name(name: str) -> str:
    """'Sunita Kumar' -> 'S***** K****' -- first letter of each word kept, rest masked."""
    def mask_word(w):
        return w[0] + "*" * (len(w) - 1) if len(w) > 1 else w
    return " ".join(mask_word(w) for w in name.split())


def _mask_phone(phone: str) -> str:
    """'+91-74840-86888' -> '+91-XXXXX-X6888' -- keep the last 4 digits, mask the rest."""
    digits = sum(1 for c in phone if c.isdigit())
    if digits <= 4:
        return "X" * len(phone)
    to_mask = digits - 4
    masked_count = 0
    out = []
    for c in phone:
        if c.isdigit():
            if masked_count < to_mask:
                out.append("X")
                masked_count += 1
            else:
                out.append(c)
        else:
            out.append(c)
    return "".join(out)


# Fields to mask, mapped to their masking function. Neither field is read by
# any tool or resolution logic anywhere in agent_core -- asset_id/store_id/
# system_version fully identify what needs fixing -- so masking them costs
# nothing functionally.
PII_FIELDS = {
    "submitted_by": _mask_name,
    "contact_phone": _mask_phone,
}


def mask_pii(ticket_json: str) -> str:
    """Mask reporter name and phone number before a ticket reaches any LLM
    call, LangSmith trace, or other structured record.

    Must run at the API boundary, before run_triage_agent/run_resolution_agent/
    run_l2_copilot_agent are ever called -- all three are @traceable, and
    LangSmith captures a traced function's arguments at call time, before its
    body runs. Masking inside those functions would be too late: the raw,
    unmasked ticket would already be in the trace LangSmith recorded.
    """
    try:
        ticket = json.loads(ticket_json)
    except (json.JSONDecodeError, TypeError):
        return ticket_json
    for field, masker in PII_FIELDS.items():
        value = ticket.get(field)
        if isinstance(value, str) and value:
            ticket[field] = masker(value)
    return json.dumps(ticket)
