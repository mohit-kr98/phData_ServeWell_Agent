"""
Deterministic structured-data enrichment (CMDB / store master / SLA matrix).

Design principle: anything answerable from ground-truth data is FETCHED, not
inferred. These lookups run unconditionally in code before the LLM sees the
ticket, so the model receives asset/store/SLA facts as *context* rather than
being asked to recall or judge them.

Why deterministic rather than letting the model decide to call the tools:
get_asset_info / get_store_info / check_sla were bound to the LLM as tools and
were invoked ZERO times across a 60-ticket evaluation. The resolution prompt
(correctly, for latency) pushes the agent to answer in a single turn when the
retrieved runbook already suffices, and a single-turn answer never spends a
turn on optional lookups. Rather than fight that with prompt pressure -- which
would add an LLM round-trip per ticket and still be unreliable -- the facts are
simply always fetched. Deterministic lookups cannot hallucinate, cannot be
skipped, and are trivially auditable in the trace.

Derived fields (warranty state, SLA budget) are computed here too, for the same
reason: date arithmetic and threshold comparison are things code should do and
an LLM should not be trusted to do.
"""
import datetime
import json

from .tools import check_sla, get_asset_info, get_store_info


def _parse(raw):
    """Tools return JSON strings, or a plain 'No X found...' message on a miss."""
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _warranty_state(expiry: str | None) -> str | None:
    """Whether the asset is still under warranty -- a real decision input:
    an out-of-warranty device changes whether a hardware swap is even an
    option, so it belongs in front of the agent rather than being guessed."""
    if not expiry:
        return None
    try:
        exp = datetime.date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return None
    today = datetime.date.today()
    days = (exp - today).days
    if days < 0:
        return f"OUT OF WARRANTY (expired {exp.isoformat()}, {abs(days)} days ago)"
    return f"in warranty (expires {exp.isoformat()}, {days} days remaining)"


def enrich(ticket: dict) -> tuple[str, dict]:
    """Fetch asset + store + SLA context for a ticket.

    Returns (context_block, raw_facts). context_block is formatted for the
    LLM prompt; raw_facts is kept for the trace so a reviewer can see exactly
    what was looked up and what came back.
    """
    asset_id = (ticket.get("asset_id") or "").strip()
    store_id = (ticket.get("store_id") or "").strip()
    priority = (ticket.get("priority") or "").strip()
    category = (ticket.get("category") or "").strip()

    facts, lines = {}, []

    if asset_id:
        asset = _parse(get_asset_info(asset_id))
        facts["asset"] = asset
        if asset:
            bits = [f"{asset.get('make','?')} {asset.get('model','')}".strip(),
                    f"type={asset.get('asset_type','?')}",
                    f"version={asset.get('system_version','?')}",
                    f"serial={asset.get('serial_number','?')}"]
            lines.append(f"ASSET {asset_id}: " + ", ".join(b for b in bits if b))
            w = _warranty_state(asset.get("warranty_expiry"))
            if w:
                lines.append(f"  warranty: {w}")
            if asset.get("last_service_date"):
                lines.append(f"  last serviced: {asset['last_service_date']}")
        else:
            # Deliberately surfaced rather than hidden: an unregistered asset is
            # a real edge case, and the agent should reason from ticket text
            # while the CMDB gap stays visible to whoever reviews the trace.
            lines.append(f"ASSET {asset_id}: NOT FOUND IN CMDB — asset is unregistered. "
                         f"Proceed from the ticket description and flag the CMDB gap.")

    if store_id:
        store = _parse(get_store_info(store_id))
        facts["store"] = store
        if store:
            lines.append(f"STORE {store_id}: {store.get('store_name','?')}, "
                         f"{store.get('city','?')} · type={store.get('store_type','?')} · "
                         f"region manager={store.get('region_manager','?')}")
        else:
            lines.append(f"STORE {store_id}: not found in store master data.")

    if priority and category:
        sla = _parse(check_sla(priority, category))
        facts["sla"] = sla
        if sla:
            lines.append(
                f"SLA ({priority}/{category}): first response {sla.get('first_response_minutes','?')} min · "
                f"resolution target {sla.get('resolution_target_minutes','?')} min · "
                f"auto-escalate to L2 after {sla.get('escalation_to_l2_minutes','?')} min"
            )
        else:
            lines.append(f"SLA ({priority}/{category}): no matching SLA row.")

    if not lines:
        return "", facts
    return "[STRUCTURED DATA — fetched deterministically from CMDB / store master / SLA matrix]\n" + \
           "\n".join(lines) + "\n", facts
