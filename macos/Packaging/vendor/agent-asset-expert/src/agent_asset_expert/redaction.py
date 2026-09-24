"""Credential-only redaction, applied before any persistence."""
from __future__ import annotations

import re
from typing import Any

_KEY = re.compile(r"(?:api[_-]?key|secret|token|cookie|password|authorization|private[_-]?key)", re.I)
_VALUE = re.compile(r"\b(?:bearer|basic|token)\s+[A-Za-z0-9._~+/-]+=*", re.I)
MASK = "[REDACTED]"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): MASK if _KEY.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _VALUE.sub(MASK, value)
    return value
