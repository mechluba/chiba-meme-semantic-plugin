#!/usr/bin/env python3
"""把多轮人工审核结果合并进现有梗包，并可选生成向量 release。"""

from __future__ import annotations

from argparse import ArgumentParser
from ast import literal_eval
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping

import asyncio
import json


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
APPROVED_DECISIONS = {"approve_use", "approve_understand"}


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON 顶层必须是对象: {path}")
    return payload


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(_json_text(item) for item in value if _json_text(item))
    if isinstance(value, Mapping):
        return "；".join(
            f"{key}：{_json_text(item)}"
            for key, item in value.items()
            if _json_text(item)
        )
    return str(value).strip()


def _normalize_semantic_core(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("semantic_core 必须是字符串")
    text = value.strip()
    if not (text.startswith("{") and text.endswith("}")):
        if not text:
            raise ValueError("semantic_core 不能为空")
        return text

    try:
        parsed = literal_eval(text)
    except (SyntaxError, ValueError):
        return text
    if not isinstance(parsed, dict):
        return text

    expression_keys = {"expression", "template", "type"}
    preferred_keys = (
        "core_meaning",
        "meaning",
        "definition",
        "core",
        "pragmatic_meaning",
        "literal_meaning",
        "pragmatic_function",
    )
    qualifier_keys = tuple(
        key
        for key in parsed
        if key not in expression_keys and key not in preferred_keys
    )
    ordered_keys = (*preferred_keys, *qualifier_keys)
    parts: list[str] = []
    for key in ordered_keys:
        if key not in parsed:
            continue
        part = _json_text(parsed[key])
        if part and part not in parts:
            parts.append(part.rstrip("。；"))
    if not parts:
        raise ValueError("semantic_core 对象没有可用语义内容")
    return "；".join(parts) + "。"


def _expression_terms(card: Mapping[str, Any]) -> set[str]:
    values = [card.get("canonical_expression"), *(card.get("aliases") or [])]
    return {str(value).strip() for value in values if str(value).strip()}


def _apply_decision(
    *, card: dict[str, Any], decision: str, note: str, reviewed_at: str
) -> dict[str, Any]:
    normalized = deepcopy(card)
    semantic_core = _normalize_semantic_core(normalized.get("semantic_core"))
    normalized["semantic_core"] = semantic_core

    source_material = (
        normalized.setdefault("knowledge", {})
        .setdefault("source_material", {})
    )
    if not isinstance(source_material, dict):
        raise TypeError("knowledge.source_material 必须是对象")
    source_material["semantic_signature"] = semantic_core

    positive_contexts = normalized.get("positive_contexts")
    if not isinstance(positive_contexts, list) or not positive_contexts:
        raise ValueError(f"梗卡缺少 positive_contexts: {normalized.get('card_id')}")
    if decision == "approve_understand":
        for context in positive_contexts:
            context["expected_action"] = "UNDERSTAND_ONLY"
    elif not any(
        context.get("expected_action") == "USE" for context in positive_contexts
    ):
        positive_contexts[0]["expected_action"] = "USE"

    review_note = "人审通过：可使用" if decision == "approve_use" else "人审通过：仅理解"
    if note.strip():
        review_note = f"{review_note}；{note.strip()}"
    normalized["human_review"] = {
        "status": "approved",
        "note": review_note,
        "updated_at": reviewed_at,
    }
    return normalized


def _read_latest_decisions(
    decision_paths: Iterable[Path],
) -> tuple[dict[str, tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    latest: dict[str, tuple[dict[str, Any], str]] = {}
    batches: list[dict[str, Any]] = []
    for path in decision_paths:
        payload = _load_json_object(path)
        exported_at = str(payload.get("exported_at") or "")
        decisions = payload.get("decisions")
        if not isinstance(decisions, dict):
            raise TypeError(f"decisions 必须是对象: {path}")
        batches.append(
            {
                "source": path.name,
                "exported_at": exported_at,
                "prompt_version": payload.get("source_prompt_version"),
                "decision_count": len(decisions),
            }
        )
        for card_id, row in decisions.items():
            if not isinstance(row, dict):
                raise TypeError(f"审核项必须是对象: {card_id}")
            latest[str(card_id)] = (row, exported_at)
    return latest, batches


def build_reviewed_library(
    *,
    base_library: dict[str, Any],
    decision_paths: Iterable[Path],
    source_library_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    latest, batches = _read_latest_decisions(decision_paths)
    approved_cards: list[dict[str, Any]] = []
    understand_only_ids: list[str] = []
    decision_counts = {"approve_use": 0, "approve_understand": 0, "reject": 0}

    for expected_card_id, (row, reviewed_at) in latest.items():
        decision = str(row.get("decision"))
        if decision not in decision_counts:
            raise ValueError(f"未知审核决定: {decision}")
        decision_counts[decision] += 1
        if decision not in APPROVED_DECISIONS:
            continue
        raw_card = json.loads(str(row.get("json") or ""))
        if not isinstance(raw_card, dict):
            raise TypeError(f"审核 JSON 必须是对象: {expected_card_id}")
        if raw_card.get("card_id") != expected_card_id:
            raise ValueError(f"审核 key 与 card_id 不一致: {expected_card_id}")
        card = _apply_decision(
            card=raw_card,
            decision=decision,
            note=str(row.get("note") or ""),
            reviewed_at=reviewed_at,
        )
        approved_cards.append(card)
        if decision == "approve_understand":
            understand_only_ids.append(expected_card_id)

    new_terms: set[str] = set()
    for card in approved_cards:
        overlap = new_terms & _expression_terms(card)
        if overlap:
            raise ValueError(f"新增梗卡存在表达冲突: {sorted(overlap)}")
        new_terms.update(_expression_terms(card))

    base_cards = base_library.get("cards")
    if not isinstance(base_cards, list) or not base_cards:
        raise ValueError("基础梗包没有 cards")
    retained_base_cards = [
        deepcopy(card)
        for card in base_cards
        if not (_expression_terms(card) & new_terms)
    ]
    superseded_count = len(base_cards) - len(retained_base_cards)
    cards = [*retained_base_cards, *approved_cards]

    card_ids = [str(card.get("card_id")) for card in cards]
    canonicals = [str(card.get("canonical_expression")) for card in cards]
    if len(card_ids) != len(set(card_ids)):
        raise ValueError("合并后 card_id 不唯一")
    if len(canonicals) != len(set(canonicals)):
        raise ValueError("合并后 canonical_expression 不唯一")
    if any(card.get("human_review", {}).get("status") != "approved" for card in cards):
        raise ValueError("合并后存在未经批准的梗卡")

    library = deepcopy(base_library)
    library.pop("vector_index", None)
    library.pop("derived_from_release_id", None)
    library["library_id"] = source_library_id
    library["built_at"] = datetime.now().astimezone().isoformat()
    library["card_count"] = len(cards)
    library["cards"] = cards
    library["reviewed_at"] = max(
        (batch["exported_at"] for batch in batches if batch["exported_at"]),
        default=library["built_at"],
    )
    library["human_review"] = {
        "audit_artifact": "user_supplied_review_decisions",
        "decision_batches": batches,
        "approved_count": len(cards),
        "new_approved_count": len(approved_cards),
        "new_use_count": decision_counts["approve_use"],
        "new_understand_only_count": decision_counts["approve_understand"],
        "new_rejected_count": decision_counts["reject"],
        "superseded_base_count": superseded_count,
        "all_cards_reviewed": True,
    }
    summary = {
        "source_library_id": source_library_id,
        "card_count": len(cards),
        "base_card_count": len(base_cards),
        "new_approved_count": len(approved_cards),
        "new_use_count": decision_counts["approve_use"],
        "new_understand_only_count": decision_counts["approve_understand"],
        "new_rejected_count": decision_counts["reject"],
        "superseded_base_count": superseded_count,
        "understand_only_card_ids": understand_only_ids,
    }
    return library, summary


def _parse_args() -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--base-release-id", required=True)
    parser.add_argument("--review-decisions", action="append", required=True)
    parser.add_argument("--target-release-id", required=True)
    parser.add_argument(
        "--prepare-only",
        type=Path,
        help="只输出合并后的 library.json，不调用在线向量模型",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    releases_root = PLUGIN_ROOT / "resources" / "releases"
    base_dir = (releases_root / args.base_release_id).resolve()
    target_dir = (releases_root / args.target_release_id).resolve()
    if not base_dir.is_relative_to(releases_root.resolve()) or not base_dir.is_dir():
        raise SystemExit(f"基础 release 不存在或越界: {base_dir}")
    if not target_dir.is_relative_to(releases_root.resolve()):
        raise SystemExit("目标 release 越出 releases 目录")

    source_library_id = f"{args.target_release_id}-reviewed-source"
    library, summary = build_reviewed_library(
        base_library=_load_json_object(base_dir / "library.json"),
        decision_paths=[Path(path).resolve() for path in args.review_decisions],
        source_library_id=source_library_id,
    )
    if args.prepare_only:
        output = args.prepare_only.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(library, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({**summary, "output": str(output)}, ensure_ascii=False, indent=2))
        return 0

    from build_multiprototype_release import _build

    with TemporaryDirectory(prefix="reviewed-meme-source-", dir=releases_root) as temporary:
        source_dir = Path(temporary)
        (source_dir / "library.json").write_text(
            json.dumps(library, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (source_dir / "release.json").write_text(
            json.dumps(
                {"schema_version": 2, "release_id": source_library_id},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        built = asyncio.run(
            _build(
                source_dir=source_dir,
                target_dir=target_dir,
                target_release_id=args.target_release_id,
            )
        )
    print(json.dumps({**summary, **built}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
