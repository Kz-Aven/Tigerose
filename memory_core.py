"""Side-effect-free memory validation and text selection shared by runtimes."""

import math
import re

MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference", "unknown"})


def contains_secret(text: str) -> bool:
    return bool(re.search(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b|"
        r"(?i:bearer\s+[a-z0-9._~+/-]{16,}|(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]{6,})",
        text,
    ))


def make_summary(text: str, limit: int = 240) -> str:
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    # Never create a broken URL in a bounded summary.
    end = limit - 3
    for match in re.finditer(r"https?://\S+", line):
        if match.start() < end < match.end():
            end = match.start()
            break
    return line[:end].rstrip() + "..."


def estimate_tokens(text: str) -> int:
    # Conservative bound for mixed Chinese and Latin text, including metadata.
    return max(1, math.ceil(len(text.encode("utf-8")) / 2))


def _terms(text: str) -> set[str]:
    text = text.casefold()
    terms = set(re.findall(r"[a-z0-9_]{2,}", text))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(segment[i:i + 2] for i in range(len(segment) - 1))
        if len(segment) == 1:
            terms.add(segment)
    return terms


def relevance_score(query: str, text: str) -> float:
    terms = _terms(query)
    return len(terms & _terms(text)) / max(1, len(terms))
