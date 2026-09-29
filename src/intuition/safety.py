"""Safety: trust ladder, imperative filter, secure scope (plan §9).

Ladder: #stated (user's words) > #observed (main assistant saw user actions)
> #inferred:x (model generalised) > #external (web/email/other people).
External content never becomes an instruction or a preference (MINJA defence).
"""

from __future__ import annotations

import re

IMPERATIVE_PATTERNS = (
    r"\bignore (all |any |previous |prior )?(instructions?|prompts?|rules?)\b",
    r"\bfrom now on\b",
    r"\balways (send|email|reply|respond|do|use|write)\b",
    r"\bnever (send|email|reply|respond|do|use|write)\b",
    r"\byou must\b",
    r"\bdisregard\b",
    r"\bnew instructions?\b",
    r"\bdo not tell\b",
)
_IMPERATIVE_RE = re.compile("|".join(IMPERATIVE_PATTERNS), re.IGNORECASE)

PROFILE_MIN_CONFIDENCE = 0.8     # inferred enters profile only at ≥ 0.8 (plan §9.1)


def trust_for(source: str) -> str:
    """Map an inbox `source` to a trust tag (plan §4.8 → §4.3)."""
    return {"user": "stated", "agent": "observed", "external": "external"}[source]


def is_imperative(text: str) -> bool:
    """Instruction-like external text → quarantine (plan §6.4, §9.2)."""
    return bool(_IMPERATIVE_RE.search(text))


def can_enter_profile(trust: str, confidence: float | None = None,
                      n_sources: int = 1) -> bool:
    """plan §9.1: levels 1–2 always; level 3 only if x ≥ 0.8 and ≥ 2 sources."""
    if trust in ("stated", "observed"):
        return True
    if trust == "inferred":
        return (confidence or 0.0) >= PROFILE_MIN_CONFIDENCE and n_sources >= 2
    return False


def can_create_preference_or_decision(source: str) -> bool:
    """Only user or main; never external (plan §6.4 trust rule)."""
    return source in ("user", "agent") and source != "external"


def profile_fact_ok(fact) -> bool:
    return can_enter_profile(fact.trust, fact.confidence, len(fact.sources))


def validate_op_safety(op: dict) -> str | None:
    """Deterministic safety checks on a plan op (plan §6.4). Returns reject reason."""
    op_name = op.get("op", "")
    source = op.get("source", "agent")
    kind = op.get("kind", "")
    text = str(op.get("evidence", "")) + " " + str(op.get("text", ""))
    if op_name in ("create", "add_fact") and kind in ("preference", "decision"):
        if source == "external" or not can_create_preference_or_decision(source):
            return f"trust: {source} may not create {kind}"
    if source == "external" and op.get("trust") == "stated":
        return "trust: external content may not be #stated"
    if source == "external" and is_imperative(text):
        return "imperative filter: external text reads like an instruction"
    if "secure/" in str(op.get("id", "")) + str(op.get("path", "")):
        return "secure: ops may not touch secure/"
    return None
