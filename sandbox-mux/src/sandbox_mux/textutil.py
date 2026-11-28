"""Small helpers for text that came from outside."""

from __future__ import annotations

import json
import unicodedata
from typing import Any


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def clean_text(value: Any, max_chars: int) -> str:
    """Plain text for the agent or a reviewer: no control or invisible formatting characters
    (zero-width and bidirectional marks are used to hide instructions), and a length cap."""
    if value is None:
        return ""
    if isinstance(value, dict):
        for k in ("emailAddress.address", "address", "email", "displayName", "name"):
            v = get_path(value, k)
            if isinstance(v, str) and v:
                value = v
                break
        else:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, list):
        value = ", ".join(clean_text(v, max_chars) for v in value)
    text = str(value)
    text = "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return text if len(text) <= max_chars else text[:max_chars] + "…"
