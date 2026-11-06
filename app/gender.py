"""Deterministic detection of the chatting user's gender.

Why this exists: the persona prompt already tells the model to read the
other person's gender from their Hinglish grammar, but the small free
models keep slipping — they answer a boy who wrote "mai thak gaya hu"
with feminine forms ("top karegi", "fresh feel karegi") because Misha's
own feminine voice dominates generation. So the gender is detected
HERE, in code, persisted on the users row, and handed to the LLM as a
hard FACT line (see llm.chat_reply) instead of a hopeful instruction.

Detection is deliberately conservative — only unambiguous Hinglish
self-reference verb forms count, matched on word boundaries so a
substring can never trigger ("gayab" must not read as "gaya",
"gayatri" must not read as "gaya" either). A text carrying signals
for BOTH genders (quoting someone, talking about a third person of
the other gender) resolves to unknown rather than a coin flip.
"""
from __future__ import annotations

import re

MALE = "M"
FEMALE = "F"

# High-confidence masculine self-reference forms (Roman Hinglish).
_MALE_PATTERNS = [
    r"raha\s+hu(?:n)?\b",  # kar raha hu / raha hun
    r"aaya\s+hu(?:n)?\b",  # mai aaya hu
    r"gaya\s+hu(?:n)?\b",  # mai gaya hu
    r"ho\s+gaya\b",        # ho gaya
    r"gaya\s+tha\b",       # gaya tha
    r"gaya\b",             # standalone gaya
    r"tha\b",              # standalone tha
]
# High-confidence feminine self-reference forms (Roman Hinglish).
_FEMALE_PATTERNS = [
    r"rahi\s+hu(?:n)?\b",  # kar rahi hu / rahi hun
    r"aayi\s+hu(?:n)?\b",  # mai aayi hu
    r"gayi\s+hu(?:n)?\b",  # mai gayi hu
    r"ho\s+gayi\b",        # ho gayi
    r"gayi\s+thi\b",       # gayi thi
    r"gayi\b",             # standalone gayi
    r"thi\b",              # standalone thi
]
_MALE_RE = [re.compile(r"\b(?:" + p + r")", re.IGNORECASE) for p in _MALE_PATTERNS]
_FEMALE_RE = [re.compile(r"\b(?:" + p + r")", re.IGNORECASE) for p in _FEMALE_PATTERNS]

_FACT_LINES = {
    MALE: (
        "FACT: The person chatting with you is a BOY. When you talk "
        "ABOUT HIM, use masculine forms (karega, gaya, thak gaya hai). "
        "Your own forms stay feminine."
    ),
    FEMALE: (
        "FACT: The person chatting with you is a GIRL. When you talk "
        "ABOUT HER, use feminine forms (karegi, gayi, thak gayi hai). "
        "Your own forms stay feminine."
    ),
}


def detect_gender(text: str) -> str | None:
    """'M', 'F', or None (no signal, or conflicting signals both ways)."""
    if not text:
        return None
    male = any(rx.search(text) for rx in _MALE_RE)
    female = any(rx.search(text) for rx in _FEMALE_RE)
    if male == female:  # neither side, or BOTH sides — not confident
        return None
    return MALE if male else FEMALE


def detect_gender_in_texts(texts) -> str | None:
    """First confident detection across an iterable of texts, in order
    (callers pass newest first when scanning chat history)."""
    for text in texts or []:
        found = detect_gender(text)
        if found is not None:
            return found
    return None


def resolve_gender(stored: str | None, detected: str | None) -> str | None:
    """Sticky merge of a stored value with a fresh detection.

    A confident fresh detection always wins — including over a stored
    opposite value, so a one-time misdetection can still be corrected
    by a later clear signal. No detection (None) never clears or
    changes the stored value.
    """
    if detected in (MALE, FEMALE):
        return detected
    return stored if stored in (MALE, FEMALE) else None


def gender_fact_line(gender: str | None) -> str | None:
    """The hard context line injected into the LLM prompt, or None
    when the gender is unknown (persona's neutral rule then stands)."""
    return _FACT_LINES.get(gender or "")
