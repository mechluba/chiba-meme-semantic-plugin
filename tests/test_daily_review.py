"""每日热梗终审汇总与飞书通知测试。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from meme_discovery.daily_review import DailyReviewError, build_daily_review, notify_feishu
from meme_discovery.reviewed_card_review import validate_storage_cards
from scripts.build_reviewed_decision_release import build_reviewed_library


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-a",
        "candidate_kind": "surface_repetition_signal",
        "phrase": "无量空处",
        "aliases": [],
        "signals": {"message_count": 4, "distinct_content_count": 2},
        "examples": [
            {
                "evidence_id": "e1",
                "platform": "bilibili",
                "content_id": "BV1TEST:123",
                "circle": "泛二次元",
            }
        ],
        "occurrence_contexts": [{"circle": "泛二次元"}],
        "web_research": {
            "research_version": "test-research-v1",
            "status": "single_web_source",
            "retrieved_at": "2026-09-07T02:00:00Z",
            "freshness": {"class": "recent_90d"},
            "result_count": 1,
            "results": [
                {
                    "source_id": "web-1",
                    "provider": "so_qa",
                    "source_kind": "question_search_result",
                    "source_tier": "indexed_web",
                    "title": "无量空处是什么梗",
                    "url": "https://example.test/guide",
                    "snippet": "用于夸张表达信息过载和大脑宕机。",
                    "published_at": "2026-09-01T00:00:00Z",
                    "match_quality": "title_exact",
                }
            ],
        },
        "semantic_enrichment": {"status": "pending_human_review"},
        "draft_card": {
            "classification": "meme_candidate",
            "classification_reason": "有互联网材料和跨内容语境支持。",
            "semantic_core": "借作品能力名夸张表示自己因信息过载而无法继续处理内容。",
            "culture_scope": "咒术回战及泛中文互联网",
            "usage_routes": [
                {
                    "route_tag": "信息过载自嘲",
                    "when": "对方刚给出大量复杂信息，用户明确表示自己已经看懵时。",
                    "communicative_intent": "用夸张自嘲告诉对方自己处理不过来，并请求对方简化说明。",
                    "allowed_realizations": ["无量空处"],
                    "confidence": 0.8,
                }
            ],
            "required_context_signals": ["刚出现密集信息", "用户明确表示看懵"],
            "hard_blocks": ["医疗求助", "正式事故复盘"],
            "positive_contexts": [
                {
                    "context": "朋友发来大量复杂设定，用户表示自己已经完全看懵。",
                    "user_intent": "请求对方简化说明。",
                    "expected_action": "USE",
                },
                {
                    "context": "用户主动说自己被这段说明无量空处了。",
                    "user_intent": "表达信息过载。",
                    "expected_action": "UNDERSTAND_ONLY",
                },
            ],
            "negative_contexts": [
                {"context": "用户报告真实认知障碍。", "reason": "医疗风险。", "expected_action": "SKIP"},
                {"context": "用户要求核对事故报告。", "reason": "需要严谨。", "expected_action": "SKIP"},
                {"context": "对话中没有复杂信息。", "reason": "缺少触发。", "expected_action": "SKIP"},
            ],
        },
    }


def test_daily_review_aggregates_semantic_cards_and_exports_ingestible_page(tmp_path: Path) -> None:
    output_root = tmp_path / "archive"
    run_dir = output_root / "runs" / "20260907T030000Z"
    run_dir.mkdir(parents=True)
    unready = deepcopy(_candidate())
    unready["candidate_id"] = "candidate-unready"
    unready["phrase"] = "待补模型候选"
    unready["semantic_enrichment"] = {"status": "not_run"}
    unready["draft_card"] = {}
    source = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "generated_at": "2026-09-07T03:00:00Z",
        "semantic_enrichment_report": {"prompt_version": "semantic-v1"},
        "candidates": [_candidate(), unready],
    }
    (run_dir / "candidates.pending-review.json").write_text(
        json.dumps(source, ensure_ascii=False),
        encoding="utf-8",
    )

    result = build_daily_review(target_date=date(2026, 9, 7), output_root=output_root)

    summary = json.loads(Path(result["summary_file"]).read_text(encoding="utf-8"))
    page = Path(result["review_page"]).read_text(encoding="utf-8")
    assert result["source_document_count"] == 1
    assert result["source_candidate_count"] == 2
    assert result["generated_card_count"] == 1
    assert validate_storage_cards(summary) == []
    assert summary["items"][0]["storage_card"]["canonical_expression"] == "无量空处"
    assert summary["items"][0]["storage_card"]["serving_scope"] == "circle_only"
    assert summary["skipped_candidates"][0]["phrase"] == "待补模型候选"
    assert summary["skipped_candidates"][0]["reason"] == "semantic_not_ready"
    assert "导出审核记录" in page
    assert "存储内容 JSON（已折叠，可直接编辑）" in page
    assert "未生成可入库梗卡" in page
    assert str(tmp_path) not in page

    card = summary["items"][0]["storage_card"]
    decisions_path = tmp_path / "review-decisions.json"
    decisions_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exported_at": "2026-09-08T06:00:00Z",
                "source_prompt_version": "semantic-v1",
                "decisions": {
                    card["card_id"]: {
                        "decision": "approve_use",
                        "json": json.dumps(card, ensure_ascii=False),
                        "note": "每日审核通过",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    library, release_summary = build_reviewed_library(
        base_library={
            "schema_version": 2,
            "library_id": "base",
            "cards": [{"card_id": "base-card", "human_review": {"status": "approved"}}],
        },
        decision_paths=[decisions_path],
        source_library_id="daily-reviewed-source",
    )
    assert release_summary["new_approved_count"] == 1
    assert library["cards"][-1]["human_review"]["status"] == "approved"


def test_feishu_notification_uses_only_official_custom_bot_webhook() -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return b'{"StatusCode":0,"StatusMessage":"success"}'

    def opener(request: Any, *, timeout: int) -> Response:
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    notification = notify_feishu(
        {
            "target_date": "2026-09-07",
            "source_document_count": 2,
            "source_candidate_count": 8,
            "generated_card_count": 3,
            "review_page": "/local/daily-review.html",
        },
        webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test-token。",
        opener=opener,
    )

    assert notification["status"] == "sent"
    assert captured["url"].endswith("/test-token")
    assert captured["payload"]["msg_type"] == "text"
    assert "待审核梗卡：3 / 原始候选 8" in captured["payload"]["content"]["text"]
    assert "test-token" not in json.dumps(notification)

    with pytest.raises(DailyReviewError, match="不是合法"):
        notify_feishu(
            {
                "target_date": "2026-09-07",
                "source_document_count": 0,
                "source_candidate_count": 0,
                "generated_card_count": 0,
                "review_page": "none",
            },
            webhook="https://example.test/hook/token",
            opener=opener,
        )
