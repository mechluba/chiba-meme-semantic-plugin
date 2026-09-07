#!/usr/bin/env python3
"""用 Chiba 文本/向量模型重新提炼已有候选文件，并生成独立人工审核页。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import argparse
import json
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from meme_discovery.pipeline import _render_review_html  # noqa: E402
from meme_discovery.semantic_calibrator import (  # noqa: E402
    calibrate_candidates,
    preflight_semantic_calibration,
)
from meme_discovery.semantic_enricher import enrich_candidates, preflight_semantic_enrichment  # noqa: E402


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="已有 candidates.pending-review.json")
    parser.add_argument("--config", type=Path, required=True, help="包含 semantic_enrichment 的 JSON 配置")
    parser.add_argument("--output", type=Path, required=True, help="另存的语义候选 JSON；不会覆盖输入")
    parser.add_argument("--review-page", type=Path, required=True, help="另存的 HTML 审核页")
    parser.add_argument("--cache-dir", type=Path, required=True, help="语义模型缓存目录")
    args = parser.parse_args()

    document = _load_object(args.input.resolve())
    config = _load_object(args.config.resolve()).get("semantic_enrichment") or {}
    calibration_config = config.get("embedding_calibration") or {}
    preflight_semantic_enrichment(config)
    preflight_semantic_calibration(calibration_config)

    candidates, semantic_report = enrich_candidates(
        document.get("candidates") or [],
        config,
        cache_dir=args.cache_dir.resolve(),
    )
    calibration_report = calibrate_candidates(candidates, calibration_config)
    document["candidates"] = candidates
    document["semantic_enrichment_report"] = semantic_report
    document["semantic_calibration_report"] = calibration_report
    document.setdefault("semantic_policy", {}).update(
        {
            "occurrence_context_is_usage_route": False,
            "auto_publish": False,
            "note": "文本模型提炼交流意图，向量模型只校准旧卡近邻；两者均待人工审核。",
        }
    )
    summary = document.setdefault("summary", {})
    summary["usage_route_count"] = sum(
        len((candidate.get("draft_card") or {}).get("usage_routes") or []) for candidate in candidates
    )
    summary["semantically_enriched_candidate_count"] = sum(
        (candidate.get("semantic_enrichment") or {}).get("status") == "pending_human_review"
        for candidate in candidates
    )
    summary["meme_candidate_count"] = sum(
        (candidate.get("draft_card") or {}).get("classification") == "meme_candidate" for candidate in candidates
    )

    output = args.output.resolve()
    review_page = args.review_page.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    review_page.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review_page.write_text(_render_review_html(document), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "review_page": str(review_page),
                "summary": summary,
                "semantic_enrichment_report": semantic_report,
                "semantic_calibration_report": calibration_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
