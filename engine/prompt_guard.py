"""Guards on the mutable layer: prompt word cap, risk-vocabulary linter,
one-change diff check, and web-summary sanitizer (prompt-injection defense).
"""

import difflib
import re

WORD_CAP_DEFAULT = 500

# Anything the risk engine owns may not appear in a strategy prompt.
RISK_PATTERNS = [
    r"\bkelly\b", r"\bbankroll\b", r"\bposition siz\w*", r"\bbet siz\w*",
    r"\bsize (?:of )?(?:the )?(?:bet|position|trade)s?\b",
    r"\bstop.?loss\b", r"\bloss (?:cap|limit)s?\b", r"\bdrawdown\b",
    r"\bmax(?:imum)?\b[^.\n]{0,24}\b(?:bets?|trades?|positions?|wagers?)\b",
    r"\b(?:bet|trade|wager) (?:at least|every|no more than)\b",
    r"\bstake\b", r"\bleverage\b", r"\b\d+(?:\.\d+)?\s*% of (?:the )?(?:bankroll|capital|funds)\b",
]

# Imperative / injection language stripped from research summaries.
INJECTION_PATTERNS = [
    r"\bignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules|context)\b",
    r"\byou (?:must|should|need to)\b", r"\b(?:buy|sell|bet|trade|go long|go short)\b",
    r"\bcertain(?:ly)? (?:to|will)\b", r"\bguaranteed\b", r"\bact now\b",
    r"\bsystem prompt\b", r"\bnew instructions?\b", r"\bdisregard\b",
]


def word_count(text: str) -> int:
    return len(text.split())


def lint_prompt(text: str, word_cap: int = WORD_CAP_DEFAULT) -> list[str]:
    """Return a list of violations; empty list means the prompt is acceptable."""
    errs = []
    wc = word_count(text)
    if wc > word_cap:
        errs.append(f"word cap exceeded: {wc} > {word_cap}")
    low = text.lower()
    for pat in RISK_PATTERNS:
        m = re.search(pat, low)
        if m:
            errs.append(f"risk-engine vocabulary not allowed in strategy prompt: '{m.group(0)}'")
    return errs


def diff_hunks(old: str, new: str) -> int:
    """Number of contiguous changed regions between prompt versions.

    'One change' = one addition, deletion, or rewrite of a single rule/paragraph,
    which shows up as a single contiguous replace/insert/delete region.
    """
    sm = difflib.SequenceMatcher(a=old.split("\n"), b=new.split("\n"))
    return sum(1 for op, *_ in sm.get_opcodes() if op != "equal")


def sanitize_summary(text: str) -> str:
    """Strip imperative/injection language from a research summary line-by-line.

    Web-derived text is data, never instructions. Any line carrying trading
    imperatives or instruction-override language is replaced with a redaction marker.
    """
    out = []
    for line in text.split("\n"):
        low = line.lower()
        if any(re.search(p, low) for p in INJECTION_PATTERNS):
            out.append("[line removed: imperative/instruction-like language]")
        else:
            out.append(line)
    return "\n".join(out)
