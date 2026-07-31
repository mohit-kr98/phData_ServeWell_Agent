"""Prompt store: every model-facing instruction the agents use lives here as a file.

Why files rather than string literals in llm_client.py: prompts are the part of
this system most often changed and most in need of review, and burying them in
the middle of orchestration code made them hard to diff, hard to find, and easy
to change by accident while editing control flow. A prompt change is a
behaviour change, and it should read like one in a diff.

  triage_system.md      -- routing decision (L1 / L2 / NON_IT). Static.
  resolution_system.md  -- L1 resolution agent's operating rules. Static.
  resolution_context.md -- per-ticket context block wrapped around retrieval
                           results. Templated: $merged_context, $enrich_context,
                           $action_offer.
  l2_copilot_system.md  -- assistant for the human L2 engineer. Static.

Templating uses string.Template ($name), NOT str.format. The triage prompt
contains a literal JSON example, and .format() raises KeyError on the braces in
it -- a footgun that would only surface at runtime on the triage path. $-style
sidesteps braces entirely, and safe_substitute leaves any unrecognised $token
in place rather than raising.

Leading indentation is preserved exactly as it was when these were triple-quoted
string literals. It looks odd in a file, but it means the extraction was
verifiable as byte-identical -- the prompts are unchanged by the move, so any
later behaviour change is attributable to a deliberate edit and not to this
refactor.

Files are read on each call rather than cached: a ~3KB read is negligible next
to a multi-second LLM round-trip, and it means editing a prompt takes effect on
the next request without restarting the container.
"""
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).parent


def load(name: str) -> str:
    """Return a prompt verbatim. `name` is the filename without .md."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in PROMPTS_DIR.glob("*.md")))
        raise FileNotFoundError(f"No prompt named {name!r} in {PROMPTS_DIR}. Available: {available}")
    # rstrip only: leading indentation is significant (see module docstring),
    # the trailing newline added when the file was written is not.
    return path.read_text().rstrip("\n")


def render(name: str, **values: str) -> str:
    """Load a templated prompt and substitute $placeholders."""
    return Template(load(name)).safe_substitute(**values)
