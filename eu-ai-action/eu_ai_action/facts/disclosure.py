from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

_DATA = Path(__file__).parent / "data" / "disclosure_patterns.yaml"


@lru_cache
def _patterns() -> dict[str, list[re.Pattern]]:
    raw = yaml.safe_load(_DATA.read_text(encoding="utf-8"))
    out: dict[str, list[re.Pattern]] = {}
    for strength in ("strong", "weak"):
        pats = []
        for lang_patterns in raw[strength].values():
            pats.extend(re.compile(p, re.IGNORECASE) for p in lang_patterns)
        out[strength] = pats
    return out


def detect_disclosure(text: str) -> tuple[str, str]:
    """Returns (strength, matched_text). strength is 'strong', 'weak' or 'none'."""
    if not text:
        return "none", ""
    pats = _patterns()
    for strength in ("strong", "weak"):
        for p in pats[strength]:
            m = p.search(text)
            if m:
                return strength, m.group(0)
    return "none", ""
