"""进入候选挖掘前的可解释黑名单与噪音清洗。"""

from __future__ import annotations

from collections import Counter
from typing import Any

import re

from .miner import normalize_expression


DEFAULT_REGEX_PATTERNS = (
    r"^\d+\s*(?:秒|分钟|小时|天)前[!！…。]*$",
    r"^(?:第?\d+个看完|\d+\s*分钟[!！…。]*|0\s*分钟[!！…。]*）)$",
    r"^@用户(?:\s+@用户)*$",
    r"^我选择【.+】为本场【背锅位】$",
    r"^\[[^\]]+\](?:\[[^\]]+\])*$",
)


def filter_review_evidence(
    evidence: list[dict[str, Any]],
    config: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """只决定哪些证据可进入候选池；原始归档与滚动证据仓不删除。"""
    config = config or {}
    if not config.get("enabled", True):
        return evidence, {
            "status": "disabled",
            "input_count": len(evidence),
            "eligible_count": len(evidence),
            "excluded_count": 0,
            "excluded_by_reason": {},
        }

    exact = {
        normalize_expression(str(value))
        for value in config.get("exact_expressions") or []
        if normalize_expression(str(value))
    }
    contains = [str(value).strip().lower() for value in config.get("contains") or [] if str(value).strip()]
    configured_patterns = [str(value) for value in config.get("regex_patterns") or [] if str(value)]
    pattern_values = configured_patterns or list(DEFAULT_REGEX_PATTERNS)
    patterns = [re.compile(value, re.IGNORECASE) for value in pattern_values]

    eligible: list[dict[str, Any]] = []
    excluded_by_reason: Counter[str] = Counter()
    for item in evidence:
        content = str(item.get("content") or "").strip()
        normalized = normalize_expression(content)
        reason = ""
        if normalized in exact:
            reason = "exact_blacklist"
        elif any(value in content.lower() for value in contains):
            reason = "contains_blacklist"
        elif any(pattern.fullmatch(content) for pattern in patterns):
            reason = "regex_blacklist"
        if reason:
            excluded_by_reason[reason] += 1
        else:
            eligible.append(item)
    return eligible, {
        "status": "ok",
        "input_count": len(evidence),
        "eligible_count": len(eligible),
        "excluded_count": len(evidence) - len(eligible),
        "excluded_by_reason": dict(excluded_by_reason),
        "configured_exact_count": len(exact),
        "configured_contains_count": len(contains),
        "configured_regex_count": len(patterns),
        "raw_archive_preserved": True,
    }
