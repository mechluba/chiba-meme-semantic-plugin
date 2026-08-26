"""P0 热梗候选离线发现测试。"""

from pathlib import Path

import json
import pytest

from meme_discovery.bilibili import parse_danmaku_reply
from meme_discovery.miner import mine_candidates, normalize_expression
from meme_discovery.semantic_enricher import SemanticEnrichmentError, enrich_candidates
from meme_discovery import pipeline


def _varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _scalar(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def test_danmaku_parser_extracts_only_required_public_fields() -> None:
    elem = b"".join(
        [
            _scalar(1, 123),
            _scalar(2, 8_500),
            _scalar(3, 1),
            _bytes(6, b"viewer-hash-must-not-leak"),
            _bytes(7, "无量空处".encode()),
            _scalar(8, 1_786_000_000),
            _bytes(12, b"dm-123"),
        ]
    )
    items = parse_danmaku_reply(_bytes(1, elem), segment_index=1)

    assert len(items) == 1
    assert items[0].content == "无量空处"
    assert items[0].message_id == "dm-123"
    assert "viewer-hash" not in repr(items[0])


def test_miner_only_creates_pending_review_signal() -> None:
    evidence = []
    for index, content_id in enumerate(("BV1:1", "BV2:2", "BV2:2"), 1):
        evidence.append(
            {
                "evidence_id": f"e{index}",
                "platform": "bilibili",
                "source_kind": "danmaku",
                "content_id": content_id,
                "content_title": "测试视频",
                "circle": "游戏",
                "content": " 无 量 空 处！" if index == 1 else "无量空处",
                "observed_at": "2026-08-26T00:00:00Z",
                "message_id": f"m{index}",
                "context": {"progress_seconds": 10.0 + index},
            }
        )
    evidence.extend(
        [
            {
                "evidence_id": "context-1",
                "platform": "bilibili",
                "source_kind": "danmaku",
                "content_id": "BV1:1",
                "content_title": "测试视频一",
                "circle": "游戏",
                "content": "信息一下子太多了",
                "observed_at": "2026-08-26T00:00:00Z",
                "message_id": "context-m1",
                "context": {"progress_seconds": 10.5},
            },
            {
                "evidence_id": "context-2",
                "platform": "bilibili",
                "source_kind": "danmaku",
                "content_id": "BV2:2",
                "content_title": "测试视频二",
                "circle": "游戏",
                "content": "完全看懵了",
                "observed_at": "2026-08-26T00:00:00Z",
                "message_id": "context-m2",
                "context": {"progress_seconds": 13.5},
            },
        ]
    )
    candidates = mine_candidates(
        evidence,
        {"min_occurrences": 3, "min_cross_content_occurrences": 2, "min_distinct_contents": 2},
    )

    assert normalize_expression(" 无 量 空 处！") == "无量空处"
    assert len(candidates) == 1
    assert candidates[0]["review"]["status"] == "pending"
    assert candidates[0]["candidate_kind"] == "surface_repetition_signal"
    assert candidates[0]["draft_card"]["semantic_core"] is None
    assert candidates[0]["signals"]["distinct_content_count"] == 2
    assert candidates[0]["draft_card"]["usage_routes"] == []
    scene = candidates[0]["occurrence_contexts"][0]
    assert scene["review"]["status"] == "pending"
    assert scene["confidence"] == "observed_cross_content"
    assert scene["distinct_content_count"] == 2
    assert any(
        nearby["message"] == "信息一下子太多了"
        for context in scene["representative_contexts"]
        for nearby in context["nearby_messages"]
    )


def test_pipeline_writes_local_review_artifacts_without_user_fields(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "out" / "inbox"
    inbox.mkdir(parents=True)
    rows = [
        {
            "platform": "douyu",
            "room_id": "6657",
            "session_id": "session-a",
            "message_id": f"m{index}",
            "content": "这下真成无量空处了",
            "nickname": "不应保存",
            "user_id": "secret-user-id",
        }
        for index in range(3)
    ]
    (inbox / "authorized.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        pipeline,
        "collect_bilibili",
        lambda config, client: ([], {"source": "bilibili_public_web", "status": "no_evidence"}),
    )
    monkeypatch.setattr(
        pipeline,
        "check_room_anchors",
        lambda config, client: {"source": "live_room_anchors", "rooms": []},
    )
    config = {
        "output_root": "out/discovery",
        "sources": {"jsonl_inbox": {"enabled": True, "path": "out/inbox"}},
        "mining": {"min_occurrences": 3, "min_cross_content_occurrences": 2, "min_distinct_contents": 2},
    }

    result = pipeline.run_discovery(config, repo_root=tmp_path)

    review = json.loads(Path(result["pending_review_file"]).read_text(encoding="utf-8"))
    evidence_text = (Path(result["run_dir"]) / "evidence.jsonl").read_text(encoding="utf-8")
    assert result["candidate_count"] == 1
    assert result["occurrence_context_count"] == 1
    assert result["usage_route_count"] == 0
    assert result["new_evidence_count"] == 3
    assert result["rolling_evidence_count"] == 3
    assert review["review_policy"]["auto_publish"] is False
    assert review["semantic_policy"]["occurrence_context_is_usage_route"] is False
    assert review["candidates"][0]["draft_card"]["usage_routes"] == []
    assert review["candidates"][0]["semantic_enrichment"]["status"] == "not_run"
    assert review["candidates"][0]["occurrence_contexts"][0]["source_kind"] == "authorized_live_export"
    assert "不应保存" not in evidence_text
    assert "secret-user-id" not in evidence_text

    second = pipeline.run_discovery(config, repo_root=tmp_path)
    assert second["new_evidence_count"] == 0
    assert second["rolling_evidence_count"] == 3
    assert second["candidate_count"] == 1


def test_evidence_redacts_explicit_mentions() -> None:
    item = pipeline._make_evidence(
        platform="bilibili",
        source_kind="comment",
        source_id="BV1",
        content_id="BV1",
        content_title="测试",
        circle="游戏",
        message_id="r1",
        content="@具体昵称 这个梗好用",
        observed_at="2026-08-26T00:00:00Z",
    )

    assert item["content"] == "@用户 这个梗好用"


def test_semantic_enricher_builds_specific_communicative_intent(tmp_path: Path) -> None:
    candidate = _semantic_candidate()
    model_output = {
        "classification": "meme_candidate",
        "classification_reason": "多条证据都把该表达用于信息突然过量后的看懵状态，并且存在跨内容复用。",
        "semantic_core": "借作品能力名夸张表示自己因信息过载而无法继续处理内容。",
        "culture_scope": "咒术回战及泛中文互联网",
        "usage_routes": [
            {
                "route_tag": "信息过载自嘲",
                "when": "对方一次抛出过多设定、步骤或复杂信息，当前对话已经明确表现出跟不上时。",
                "communicative_intent": "用夸张自嘲告诉对方自己已经看懵，并轻松地请求对方暂停或简化说明。",
                "response_function": "承接用户的混乱感并降低交流压力，同时暗示接下来应帮助梳理信息。",
                "required_context_signals": ["刚出现密集复杂信息", "用户明确表示看不懂或脑子宕机"],
                "audience_requirements": ["对方熟悉该作品或已经主动使用同类表达"],
                "allowed_realizations": ["无量空处"],
                "evidence_ids": ["e1"],
                "confidence": 0.82,
            }
        ],
        "required_context_signals": ["信息量突然升高", "对话中已有困惑或过载信号"],
        "hard_blocks": ["医疗意义上的认知异常", "需要严谨澄清的正式技术讨论"],
        "positive_contexts": [
            {
                "context": "朋友一次发来十几条复杂设定，用户说自己完全看懵了。",
                "user_intent": "用自嘲表达信息过载并希望对方讲慢一点。",
                "expected_action": "USE",
            },
            {
                "context": "用户主动说这段说明看得自己无量空处了。",
                "user_intent": "引用梗表达大脑宕机，希望得到理解和梳理。",
                "expected_action": "UNDERSTAND_ONLY",
            },
        ],
        "negative_contexts": [
            {"context": "用户因药物出现意识模糊并寻求帮助。", "reason": "属于医疗风险，不能用梗淡化。", "expected_action": "SKIP"},
            {"context": "用户要求逐条核对生产事故报告。", "reason": "需要清晰严谨处理，不应插入作品梗。", "expected_action": "SKIP"},
            {"context": "对方不熟悉咒术回战且只是在询问普通问题。", "reason": "缺少共同语境，主动复读会造成困惑。", "expected_action": "SKIP"},
        ],
    }

    enriched, report = enrich_candidates(
        [candidate],
        {"enabled": True, "required": True, "base_url": "https://example.test/v1", "model": "test-model"},
        cache_dir=tmp_path / "cache",
        completion=lambda messages, config: json.dumps(model_output, ensure_ascii=False),
    )

    route = enriched[0]["draft_card"]["usage_routes"][0]
    assert report["enriched_count"] == 1
    assert enriched[0]["semantic_enrichment"]["status"] == "pending_human_review"
    assert "请求对方暂停或简化说明" in route["communicative_intent"]
    assert route["when"].startswith("对方一次抛出过多设定")
    rendered = pipeline._render_review_html(
        {
            "run_id": "semantic-test",
            "summary": {"rolling_evidence_count": 3, "candidate_count": 1},
            "candidates": enriched,
        }
    )
    assert "交流意图与使用路线（模型草稿，待人审）" in rendered
    assert "轻松地请求对方暂停或简化说明" in rendered
    assert "出现语境证据（不是使用场景）" in rendered


def test_semantic_enricher_rejects_vague_intent(tmp_path: Path) -> None:
    candidate = _semantic_candidate()
    invalid = {
        "classification": "meme_candidate",
        "classification_reason": "证据中多次出现这个表达。",
        "semantic_core": "表示一种反应。",
        "culture_scope": "中文互联网",
        "usage_routes": [
            {
                "route_tag": "泛化反应",
                "when": "在相关内容出现的时候使用。",
                "communicative_intent": "即时反应或形成共鸣",
                "response_function": "让回复显得更加自然。",
                "required_context_signals": ["出现相关内容", "有人发出弹幕"],
                "audience_requirements": [],
                "allowed_realizations": ["无量空处"],
                "evidence_ids": ["e1"],
                "confidence": 0.5,
            }
        ],
        "required_context_signals": ["出现相关内容", "有人发出弹幕"],
        "hard_blocks": ["严肃场合", "陌生受众"],
        "positive_contexts": [],
        "negative_contexts": [],
    }

    with pytest.raises(SemanticEnrichmentError, match="过于空泛"):
        enrich_candidates(
            [candidate],
            {"enabled": True, "required": True, "base_url": "https://example.test/v1", "model": "test-model"},
            cache_dir=tmp_path / "cache",
            completion=lambda messages, config: json.dumps(invalid, ensure_ascii=False),
        )


def test_required_semantic_model_fails_before_collection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_TEST_LLM_KEY", raising=False)
    collected = False

    def unexpected_collect(config: dict[str, object], client: object) -> tuple[list[object], dict[str, object]]:
        nonlocal collected
        collected = True
        return [], {}

    monkeypatch.setattr(pipeline, "collect_bilibili", unexpected_collect)
    config = {
        "output_root": "out/discovery",
        "semantic_enrichment": {
            "enabled": True,
            "required": True,
            "base_url": "https://example.test/v1",
            "model": "test-model",
            "api_key_env": "MISSING_TEST_LLM_KEY",
        },
    }

    with pytest.raises(SemanticEnrichmentError, match="MISSING_TEST_LLM_KEY"):
        pipeline.run_discovery(config, repo_root=tmp_path)

    assert collected is False
    assert not (tmp_path / "out" / "discovery").exists()


def _semantic_candidate() -> dict[str, object]:
    candidates = mine_candidates(
        [
            {
                "evidence_id": f"e{index}",
                "platform": "bilibili",
                "source_kind": "danmaku",
                "content_id": "BV1:1" if index < 3 else "BV2:2",
                "content_title": "复杂设定讲解",
                "circle": "咒术回战",
                "content": "无量空处",
                "observed_at": "2026-08-26T00:00:00Z",
                "message_id": f"m{index}",
                "context": {"progress_seconds": 10.0 + index},
            }
            for index in range(1, 4)
        ],
        {"min_occurrences": 3, "min_cross_content_occurrences": 2, "min_distinct_contents": 2},
    )
    return candidates[0]
