"""从脱敏证据中生成仅供人工审核的重复表达候选。"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import hashlib
import re
import unicodedata


SPACE_RE = re.compile(r"\s+")
EDGE_PUNCT_RE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
ONLY_DIGITS_RE = re.compile(r"^[\d\s.,，。!?！？~～+-]+$")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def normalize_expression(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = SPACE_RE.sub("", text)
    return EDGE_PUNCT_RE.sub("", text)


def is_generic_noise(value: str) -> bool:
    normalized = normalize_expression(value)
    if len(normalized) < 2 or len(normalized) > 48:
        return True
    if URL_RE.search(normalized) or ONLY_DIGITS_RE.fullmatch(normalized):
        return True
    meaningful = [char for char in normalized if char.isalnum() or "\u4e00" <= char <= "\u9fff"]
    if len(meaningful) < 2 or len(set(meaningful)) == 1:
        return True
    return False


def mine_candidates(evidence: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        content = str(item.get("content") or "").strip()
        if is_generic_noise(content):
            continue
        normalized = normalize_expression(content)
        if normalized:
            groups[normalized].append(item)

    min_occurrences = int(config.get("min_occurrences", 3))
    min_cross_content_occurrences = int(config.get("min_cross_content_occurrences", 2))
    min_distinct_contents = int(config.get("min_distinct_contents", 2))
    max_examples = int(config.get("max_examples_per_candidate", 8))
    candidates: list[dict[str, Any]] = []
    for normalized, items in groups.items():
        content_ids = {str(item.get("content_id") or "") for item in items}
        cross_content = len(items) >= min_cross_content_occurrences and len(content_ids) >= min_distinct_contents
        local_repetition = len(items) >= min_occurrences
        if not (cross_content or local_repetition):
            continue
        variants = Counter(str(item["content"]).strip() for item in items)
        phrase = variants.most_common(1)[0][0]
        source_kinds = sorted({str(item.get("source_kind") or "") for item in items})
        candidate_id = "candidate-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        examples = [
            {
                "evidence_id": item["evidence_id"],
                "platform": item["platform"],
                "source_kind": item["source_kind"],
                "content_id": item["content_id"],
                "content_title": item.get("content_title", ""),
                "circle": item.get("circle", ""),
                "message": item["content"],
                "observed_at": item.get("observed_at"),
            }
            for item in items[:max_examples]
        ]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_kind": "surface_repetition_signal",
                "phrase": phrase,
                "normalized_expression": normalized,
                "aliases": [variant for variant, _ in variants.most_common(10)],
                "signals": {
                    "message_count": len(items),
                    "distinct_content_count": len(content_ids),
                    "source_kinds": source_kinds,
                    "cross_content_repetition": cross_content,
                    "local_repetition": local_repetition,
                },
                "why_queued": (
                    "在多个独立内容中重复出现" if cross_content else "在单个内容或直播场次中高频重复"
                ),
                "examples": examples,
                "review": {
                    "status": "pending",
                    "decision": None,
                    "reviewer": None,
                    "reviewed_at": None,
                    "notes": "",
                },
                "draft_card": {
                    "semantic_core": None,
                    "usage_scenarios": [],
                    "allowed_realizations": [],
                    "hard_negative_contexts": [],
                },
            }
        )
    candidates.sort(
        key=lambda item: (
            -int(item["signals"]["distinct_content_count"]),
            -int(item["signals"]["message_count"]),
            str(item["phrase"]),
        )
    )
    return candidates
