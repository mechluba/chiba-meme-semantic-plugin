"""从脱敏证据中生成仅供人工审核的重复表达候选。"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import bisect
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
    context_index = _build_progress_index(evidence)
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
        occurrence_contexts = _build_occurrence_contexts(
            phrase=phrase,
            normalized_expression=normalized,
            items=items,
            context_index=context_index,
            config=config,
        )
        candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_kind": "surface_repetition_signal",
                "phrase": phrase,
                "normalized_expression": normalized,
                # 每个候选独立生成梗卡，不把相近写法自动合并为别名。
                "aliases": [],
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
                "occurrence_contexts": occurrence_contexts,
                "review": {
                    "status": "pending",
                    "decision": None,
                    "reviewer": None,
                    "reviewed_at": None,
                    "notes": "",
                },
                "draft_card": {
                    "classification": None,
                    "classification_reason": None,
                    "semantic_core": None,
                    "culture_scope": None,
                    "usage_routes": [],
                    "required_context_signals": [],
                    "hard_blocks": [],
                    "positive_contexts": [],
                    "negative_contexts": [],
                    "allowed_realizations": [],
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


def _build_progress_index(
    evidence: list[dict[str, Any]],
) -> dict[str, tuple[list[float], list[dict[str, Any]]]]:
    grouped: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for item in evidence:
        progress = _progress_seconds(item)
        content_id = str(item.get("content_id") or "")
        if progress is not None and content_id:
            grouped[content_id].append((progress, item))
    result: dict[str, tuple[list[float], list[dict[str, Any]]]] = {}
    for content_id, rows in grouped.items():
        rows.sort(key=lambda row: (row[0], str(row[1].get("message_id") or "")))
        result[content_id] = ([row[0] for row in rows], [row[1] for row in rows])
    return result


def _build_occurrence_contexts(
    *,
    phrase: str,
    normalized_expression: str,
    items: list[dict[str, Any]],
    context_index: dict[str, tuple[list[float], list[dict[str, Any]]]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        circle = str(item.get("circle") or "未分类").strip() or "未分类"
        source_kind = str(item.get("source_kind") or "unknown").strip() or "unknown"
        grouped[(circle, source_kind)].append(item)
    max_scenarios = max(1, int(config.get("max_occurrence_context_scopes", 3)))
    ranked = sorted(
        grouped.items(),
        key=lambda pair: (
            -len({str(item.get("content_id") or "") for item in pair[1]}),
            -len(pair[1]),
            pair[0],
        ),
    )
    result: list[dict[str, Any]] = []
    for rank, ((circle, source_kind), scene_items) in enumerate(ranked[:max_scenarios], 1):
        content_ids = {str(item.get("content_id") or "") for item in scene_items}
        scene_key = f"{normalize_expression(circle)}\x1f{source_kind}"
        scene_id = "scene-" + hashlib.sha256(
            f"{normalized_expression}\x1f{scene_key}".encode("utf-8")
        ).hexdigest()[:16]
        result.append(
            {
                "scene_id": scene_id,
                "rank": rank,
                "circle": circle,
                "source_kind": source_kind,
                "scope_description": _scope_description(phrase, circle, source_kind),
                "basis": "observed_context_only",
                "confidence": "observed_cross_content" if len(content_ids) >= 2 else "observed_single_content",
                "evidence_count": len(scene_items),
                "distinct_content_count": len(content_ids),
                "representative_contexts": _representative_contexts(
                    scene_items,
                    normalized_expression=normalized_expression,
                    context_index=context_index,
                    window_seconds=float(config.get("context_window_seconds", 6)),
                    max_nearby=max(0, int(config.get("max_nearby_messages", 6))),
                    max_contexts=max(1, int(config.get("max_scene_contexts", 3))),
                ),
                "review": {"status": "pending", "notes": ""},
            }
        )
    return result


def _scope_description(phrase: str, circle: str, source_kind: str) -> str:
    if source_kind == "danmaku":
        return (
            f"“{phrase}”在{circle}相关视频的弹幕时间窗中重复出现；"
            "这里只记录出现位置与前后证据，不代表已经判断其交流意图。"
        )
    if source_kind == "comment":
        return (
            f"“{phrase}”在{circle}相关视频的评论区重复出现；"
            "这里只记录来源范围，不代表已经形成可供对话使用的 usage route。"
        )
    if source_kind == "authorized_live_export":
        return (
            f"“{phrase}”在{circle}直播间授权导出中重复出现；"
            "这里只记录同场证据，具体交流动作必须经过语义提炼和人工审核。"
        )
    if source_kind == "public_live_sample":
        return (
            f"“{phrase}”在{circle}公开直播间短时抽样中重复出现；"
            "这里只记录同场证据，具体交流动作必须经过语义提炼和人工审核。"
        )
    return f"在{circle}相关内容的 {source_kind} 语料中观察到“{phrase}”重复使用；尚未判断交流意图。"


def _representative_contexts(
    items: list[dict[str, Any]],
    *,
    normalized_expression: str,
    context_index: dict[str, tuple[list[float], list[dict[str, Any]]]],
    window_seconds: float,
    max_nearby: int,
    max_contexts: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        items,
        key=lambda item: (
            str(item.get("content_id") or ""),
            _progress_seconds(item) if _progress_seconds(item) is not None else float("inf"),
            str(item.get("message_id") or ""),
        ),
    )
    representative_items: list[dict[str, Any]] = []
    selected_evidence_ids: set[str] = set()
    seen_content_ids: set[str] = set()
    seen_time_buckets: set[str] = set()
    for item in ordered:
        content_id = str(item.get("content_id") or "")
        if content_id in seen_content_ids:
            continue
        seen_content_ids.add(content_id)
        representative_items.append(item)
        selected_evidence_ids.add(str(item.get("evidence_id") or ""))
        progress = _progress_seconds(item)
        if progress is not None:
            seen_time_buckets.add(f"{content_id}:{int(progress // max(window_seconds * 2, 1))}")
        if len(representative_items) >= max_contexts:
            break
    for item in ordered:
        if len(representative_items) >= max_contexts:
            break
        if str(item.get("evidence_id") or "") in selected_evidence_ids:
            continue
        content_id = str(item.get("content_id") or "")
        progress = _progress_seconds(item)
        if progress is not None:
            representative_key = f"{content_id}:{int(progress // max(window_seconds * 2, 1))}"
        else:
            representative_key = f"{content_id}:{item.get('message_id')}"
        if representative_key in seen_time_buckets:
            continue
        seen_time_buckets.add(representative_key)
        representative_items.append(item)

    selected: list[dict[str, Any]] = []
    for item in representative_items:
        content_id = str(item.get("content_id") or "")
        progress = _progress_seconds(item)
        selected.append(
            {
                "evidence_id": item["evidence_id"],
                "content_id": content_id,
                "content_title": item.get("content_title", ""),
                "message": item.get("content", ""),
                "observed_at": item.get("observed_at"),
                "position_seconds": progress,
                "nearby_messages": _nearby_messages(
                    item,
                    normalized_expression=normalized_expression,
                    context_index=context_index,
                    window_seconds=window_seconds,
                    max_messages=max_nearby,
                ),
            }
        )
    return selected


def _nearby_messages(
    item: dict[str, Any],
    *,
    normalized_expression: str,
    context_index: dict[str, tuple[list[float], list[dict[str, Any]]]],
    window_seconds: float,
    max_messages: int,
) -> list[dict[str, Any]]:
    if max_messages <= 0:
        return []
    progress = _progress_seconds(item)
    content_id = str(item.get("content_id") or "")
    indexed = context_index.get(content_id)
    if progress is None or indexed is None:
        return []
    positions, rows = indexed
    left = bisect.bisect_left(positions, progress - window_seconds)
    right = bisect.bisect_right(positions, progress + window_seconds)
    nearby: list[tuple[float, dict[str, Any]]] = []
    # positions 与 rows 由同一批元组拆分得到，长度始终一致；不使用 Python 3.10 才加入的 strict 参数，
    # 让仅依赖标准库的离线采集脚本也能在部分旧版系统 Python 上运行。
    for neighbor_position, neighbor in zip(positions[left:right], rows[left:right]):
        if neighbor.get("evidence_id") == item.get("evidence_id"):
            continue
        message = str(neighbor.get("content") or "").strip()
        if not message or normalize_expression(message) == normalized_expression:
            continue
        nearby.append((neighbor_position - progress, neighbor))
    nearby.sort(key=lambda row: (abs(row[0]), row[0], str(row[1].get("message_id") or "")))
    nearby = nearby[:max_messages]
    nearby.sort(key=lambda row: row[0])
    return [
        {"offset_seconds": round(offset, 3), "message": str(neighbor.get("content") or "")}
        for offset, neighbor in nearby
    ]


def _progress_seconds(item: dict[str, Any]) -> float | None:
    value = (item.get("context") or {}).get("progress_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
