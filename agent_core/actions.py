"""
Remediation actions, and the human-in-the-loop gate around them.

Safety model, in order of precedence:

  1. The LLM can only PROPOSE, never execute. It picks a name out of a closed
     vocabulary; nothing it emits reaches an actuator.
  2. A deterministic policy check can veto any proposal, whatever the model
     said. Categories the knowledge base marks as immediate-escalate
     (kb/sop/escalation-procedure.md "Common Escalation Triggers") are refused
     outright -- payment failures, full outages, suspected security incidents,
     network-infrastructure and multi-location faults.
  3. Execution requires approved=True, enforced *inside* execute_action rather
     than only in the UI. A missing approval is refused at the tool boundary,
     so no future caller -- another agent, a script, a retry path -- can reach
     an actuator by skipping the screen that was supposed to ask.

Every attempt, approved or not, is appended to logs/ticket_actions.jsonl. In a
real deployment this is the ITSM audit trail; here it is a local file with the
same contract.

Scope is deliberately narrow: soft, reversible, single-asset operations that a
runbook already describes as safe for L1. Nothing here can brick a device,
change network configuration, or touch more than one asset.
"""
import datetime
import json
from pathlib import Path

AUDIT_LOG = Path("logs/ticket_actions.jsonl")

# Closed vocabulary. Each action names the asset types it may target, so a
# proposal to power-cycle a router "because the printer is offline" is refused
# on type grounds rather than relying on the model to be sensible.
ACTION_CATALOG = {
    "restart_pos_terminal": {
        "label": "Remotely restart the POS terminal",
        "applies_to": ("POS Terminal",),
        "runbook": "pos-startup-failure.md / pos-slow-performance.md",
        "reversible": True,
    },
    "restart_printer": {
        "label": "Remotely restart the receipt printer",
        "applies_to": ("Receipt Printer", "Printer", "Kitchen Printer"),
        "runbook": "printer-offline.md",
        "reversible": True,
    },
    "clear_print_queue": {
        "label": "Clear the stuck print queue",
        "applies_to": ("Receipt Printer", "Printer", "Kitchen Printer"),
        "runbook": "printer-offline.md",
        "reversible": True,
    },
    "clear_app_cache": {
        "label": "Clear the application cache",
        "applies_to": ("POS Terminal", "Self-Order Kiosk", "Kiosk"),
        "runbook": "kiosk-frozen.md / pos-slow-performance.md",
        "reversible": True,
    },
}

# Straight from kb/sop/escalation-procedure.md -> "Common Escalation Triggers".
# Matched against the ticket's category/subcategory. These never get an
# automation regardless of what the model proposes; they go to a human.
NO_AUTOMATION_PATTERNS = (
    "payment", "outage", "security", "breach", "unauthorized",
    "database", "data loss", "network infrastructure", "router offline",
    "multi-store", "multiple locations", "integration",
)


def _audit(entry: dict) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": datetime.datetime.now().isoformat(), **entry}
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def available_actions(asset_type: str | None) -> list[str]:
    """Actions valid for this asset type -- the vocabulary offered to the model."""
    if not asset_type:
        return []
    at = asset_type.strip().lower()
    return [name for name, spec in ACTION_CATALOG.items()
            if any(t.lower() in at or at in t.lower() for t in spec["applies_to"])]


def policy_check(action: str, ticket: dict, asset_type: str | None) -> tuple[bool, str]:
    """Deterministic veto, applied before any human is even asked to approve.

    Returns (allowed, reason). Runs independently of the model's reasoning, so
    a confidently-wrong proposal still cannot reach the approval screen.
    """
    if action not in ACTION_CATALOG:
        return False, f"'{action}' is not in the approved action catalog."

    haystack = " ".join(str(ticket.get(k, "") or "") for k in ("category", "subcategory", "subject")).lower()
    for pattern in NO_AUTOMATION_PATTERNS:
        if pattern in haystack:
            return False, (f"Blocked by policy: this ticket matches an immediate-escalate trigger "
                           f"('{pattern}') in kb/sop/escalation-procedure.md. These go to a human, "
                           f"never to an automation.")

    valid = available_actions(asset_type)
    if valid and action not in valid:
        return False, (f"'{action}' does not apply to asset type '{asset_type}'. "
                       f"Valid here: {', '.join(valid) or 'none'}.")
    return True, "Passed policy checks; awaiting human approval."


def execute_action(action: str, asset_id: str, ticket_id: str = "",
                   approved: bool = False, approved_by: str = "") -> str:
    """Execute a remediation action. Refuses without explicit human approval.

    The approval check lives here, not in the UI, so it holds for every caller.
    Simulated: writes to the audit log instead of calling a device-management
    API. The contract -- approval-gated, single-asset, audited -- is what a real
    platform tool call would enforce.
    """
    if not approved:
        _audit({"event": "execution_refused", "action": action, "asset_id": asset_id,
                "ticket_id": ticket_id, "reason": "no human approval"})
        return json.dumps({
            "status": "REFUSED",
            "reason": "Human approval is required before any remediation action runs.",
        })

    if action not in ACTION_CATALOG:
        _audit({"event": "execution_refused", "action": action, "asset_id": asset_id,
                "ticket_id": ticket_id, "reason": "unknown action"})
        return json.dumps({"status": "REFUSED", "reason": f"Unknown action '{action}'."})

    _audit({"event": "executed", "action": action, "asset_id": asset_id,
            "ticket_id": ticket_id, "approved_by": approved_by or "unknown"})
    return json.dumps({
        "status": "EXECUTED",
        "action": action,
        "label": ACTION_CATALOG[action]["label"],
        "asset_id": asset_id,
        "ticket_id": ticket_id,
        "approved_by": approved_by or "unknown",
        "note": "Simulated device-management call; recorded in logs/ticket_actions.jsonl.",
    })


# NOTE: the docstring below is model-facing. LangChain turns it into the tool
# description the LLM reads when deciding whether to call this, so it states
# when to use the tool rather than why it is implemented this way. (An earlier
# version put the implementation rationale here and the model never called the
# tool at all.) The design rationale lives in this comment instead: propose_action
# is deliberately inert and kept separate from execute_action so that "the model
# decided" and "the system acted" are distinct code paths in the trace.
def propose_action(action: str, asset_id: str, reason: str) -> str:
    """Propose a remediation action for human approval, e.g. restarting a
    printer or POS terminal, or clearing a stuck print queue.

    Call this when a retrieved runbook says one of the actions listed in the
    "REMEDIATION ACTIONS AVAILABLE FOR THIS ASSET" block is the fix for this
    ticket. It does NOT run the action -- it queues it for a human to approve
    or reject, so it is always safe to call. After calling it, still reply to
    the user with the manual steps.

    Args:
        action: the action name exactly as listed in the available-actions block.
        asset_id: the asset the action targets, e.g. PRN-0120-P1.
        reason: one line on why, citing the runbook that recommends it.
    """
    _audit({"event": "proposed", "action": action, "asset_id": asset_id, "reason": reason})
    return json.dumps({
        "status": "PROPOSED_AWAITING_APPROVAL",
        "action": action,
        "asset_id": asset_id,
        "reason": reason,
        "label": ACTION_CATALOG.get(action, {}).get("label", action),
    })
