"""把人工输入的梗名送入联网检索与语义梗卡生成流程。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .miner import normalize_expression
from .pipeline import _render_review_html
from .semantic_enricher import enrich_candidates, preflight_semantic_enrichment
from .web_research import research_candidates


def build_manual_candidates(names: list[str], *, run_id: str) -> list[dict[str, Any]]:
    """每次人工输入都建立独立候选；不查库存、不去重，也不生成别名。"""
    candidates: list[dict[str, Any]] = []
    for index, raw_name in enumerate(names, 1):
        name = " ".join(str(raw_name).split()).strip()
        if not name:
            raise ValueError(f"第 {index} 个梗名为空")
        if len(name) > 80:
            raise ValueError(f"第 {index} 个梗名超过 80 个字符")
        candidate_id = "manual-" + hashlib.sha256(
            f"{run_id}\x1f{index}\x1f{name}".encode()
        ).hexdigest()[:16]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_kind": "manual_meme_name",
                "phrase": name,
                "normalized_expression": normalize_expression(name),
                "aliases": [],
                "search_context": "在中文互联网、评论区或弹幕中",
                "signals": {
                    "message_count": 0,
                    "distinct_content_count": 0,
                    "source_kinds": ["manual_input"],
                    "manual_input": True,
                },
                "why_queued": "人工明确输入梗名，直接进入联网检索与语义梗卡生成流程",
                "examples": [],
                "occurrence_contexts": [],
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
    if not candidates:
        raise ValueError("至少需要输入一个梗名")
    return candidates


def run_manual_intake(
    names: list[str],
    config: dict[str, Any],
    *,
    repo_root: Path,
    output_root: Path | None = None,
    research_fetchers: dict[str, Callable[[str], list[dict[str, Any]]]] | None = None,
    completion: Callable[[list[dict[str, str]], dict[str, Any]], str] | None = None,
    run_at: datetime | None = None,
) -> dict[str, Any]:
    """运行人工梗名接入；成功后只生成本地待审核 JSON 和 HTML。"""
    semantic_config = config.get("semantic_enrichment") or {}
    web_config = config.get("web_research") or {}
    if not web_config.get("enabled", False):
        raise ValueError("人工梗名接入要求启用 web_research")
    if not semantic_config.get("enabled", False):
        raise ValueError("人工梗名接入要求启用 semantic_enrichment")
    if completion is None:
        preflight_semantic_enrichment(semantic_config)

    timestamp = (run_at or datetime.now(UTC)).astimezone(UTC)
    run_id = timestamp.strftime("%Y%m%dT%H%M%SZ")
    configured_root = output_root or _resolve_output_root(config, repo_root)
    run_dir = _unique_run_dir(configured_root / "manual-runs", run_id)
    candidates = build_manual_candidates(names, run_id=run_dir.name)
    _check_batch_limit(len(candidates), web_config, "web_research")
    _check_batch_limit(len(candidates), semantic_config, "semantic_enrichment")
    run_dir.mkdir(parents=True)

    candidates, web_report = research_candidates(
        candidates,
        web_config,
        cache_dir=configured_root / "web-research-cache",
        fetchers=research_fetchers,
    )
    research_document = _build_document(
        run_id=run_dir.name,
        candidates=candidates,
        web_report=web_report,
        semantic_report={"status": "not_run"},
        pipeline_status="awaiting_semantic_enrichment",
    )
    _write_json(run_dir / "research.pending-semantic.json", research_document)

    candidates, semantic_report = enrich_candidates(
        candidates,
        semantic_config,
        cache_dir=configured_root / "semantic-cache",
        completion=completion,
    )
    document = _build_document(
        run_id=run_dir.name,
        candidates=candidates,
        web_report=web_report,
        semantic_report=semantic_report,
        pipeline_status="pending_human_review",
    )
    pending_path = run_dir / "meme-cards.pending-review.json"
    review_path = run_dir / "review-queue.html"
    _write_json(pending_path, document)
    review_path.write_text(_render_review_html(document), encoding="utf-8")

    result = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "pending_review_file": str(pending_path),
        "review_page": str(review_path),
        "candidate_count": len(candidates),
        "meme_candidate_count": document["summary"]["meme_candidate_count"],
        "usage_route_count": document["summary"]["usage_route_count"],
        "pipeline_status": document["pipeline_status"],
    }
    _write_json(configured_root / "latest-manual-run.json", result)
    return result


def _build_document(
    *,
    run_id: str,
    candidates: list[dict[str, Any]],
    web_report: dict[str, Any],
    semantic_report: dict[str, Any],
    pipeline_status: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "pipeline": "manual_meme_name_intake",
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "review_policy": {
            "status": "pending",
            "auto_publish": False,
            "note": "人工输入只跳过采集与候选挖掘；联网材料和模型草稿仍须人工审核后才能发布。",
        },
        "semantic_policy": {
            "occurrence_context_is_usage_route": False,
            "auto_publish": False,
            "note": "没有弹幕证据时，模型必须只依据可核查的互联网检索材料保守生成使用场景。",
        },
        "web_research_report": web_report,
        "semantic_enrichment_report": semantic_report,
        "pipeline_status": pipeline_status,
        "summary": {
            "candidate_count": len(candidates),
            "meme_candidate_count": sum(
                (candidate.get("draft_card") or {}).get("classification") == "meme_candidate"
                for candidate in candidates
            ),
            "usage_route_count": sum(
                len((candidate.get("draft_card") or {}).get("usage_routes") or [])
                for candidate in candidates
            ),
        },
        "candidates": candidates,
    }


def _resolve_output_root(config: dict[str, Any], repo_root: Path) -> Path:
    path = Path(os.path.expandvars(str(config.get("output_root") or "out/p0-meme-discovery")))
    return path if path.is_absolute() else repo_root / path


def _unique_run_dir(parent: Path, run_id: str) -> Path:
    candidate = parent / run_id
    suffix = 1
    while candidate.exists():
        candidate = parent / f"{run_id}-{suffix}"
        suffix += 1
    return candidate


def _check_batch_limit(candidate_count: int, stage_config: dict[str, Any], stage: str) -> None:
    limit = int(stage_config.get("max_candidates_per_run", 0))
    if limit > 0 and candidate_count > limit:
        raise ValueError(f"本次输入 {candidate_count} 个梗名，超过 {stage} 单次上限 {limit}；请分批运行")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
