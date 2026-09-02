"""P0 热梗候选离线发现测试。"""

from pathlib import Path

import json
import numpy as np
import pytest

from meme_discovery.bilibili import (
    fetch_creator_watchlist_videos,
    fetch_recommended_videos,
    parse_danmaku_reply,
)
from meme_discovery.chiba_model_config import public_model_metadata, resolve_chiba_task
from meme_discovery.evidence_filter import filter_review_evidence
from meme_discovery.live_sampler import (
    _select_rooms,
    bilibili_event_to_message,
    decode_bilibili_packets,
    decode_douyu_packets,
    douyu_record_to_message,
)
from meme_discovery.lifecycle import (
    select_inventory_decay_review,
    select_rejected_for_rereview,
    time_decay_weight,
)
from meme_discovery.miner import mine_candidates, normalize_expression
from meme_discovery.semantic_calibrator import calibrate_candidates
from meme_discovery.semantic_enricher import SemanticEnrichmentError, enrich_candidates
from meme_discovery.reviewed_card_enricher import enrich_reviewed_groups, prepare_reviewed_groups
from meme_discovery.reviewed_card_review import (
    ONLINE_CARD_KEYS,
    build_pending_library,
    render_review_html,
    validate_storage_cards,
)
from meme_discovery.web_research import (
    research_reviewed_groups,
    search_gengwh,
    search_so_question,
)
from meme_discovery import pipeline


def _douyu_packet(text: str) -> bytes:
    import struct

    body = text.encode("utf-8") + b"\x00"
    length = len(body) + 8
    return struct.pack("<IIHBB", length, length, 690, 0, 0) + body


def test_douyu_live_parser_keeps_message_but_drops_identity_fields() -> None:
    packet = _douyu_packet(
        "type@=chatmsg/rid@=6979222/uid@=secret-user/nn@=不应保存/"
        "txt@=这波@S直接@A无量空处/cid@=chat-1/cst@=1787817600000/"
    )
    records = decode_douyu_packets(packet)
    message = douyu_record_to_message(
        {"room_id": "6657", "label": "玩机器", "circle": "CS2/游戏"},
        records[0],
        collected_at=pipeline._utc_now(),
    )

    assert message is not None
    assert message["content"] == "这波/直接@用户"
    assert message["message_id"] == "chat-1"
    assert message["room_id"] == "6657"
    assert "nickname" not in message
    assert "user_id" not in message
    assert "secret-user" not in json.dumps(message, ensure_ascii=False)
    assert "不应保存" not in json.dumps(message, ensure_ascii=False)


def _bilibili_live_packet(event: dict, *, version: int = 0) -> bytes:
    import brotli
    import struct

    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    inner = struct.pack(">IHHII", len(body) + 16, 16, 0, 5, 1) + body
    if version != 3:
        return inner
    compressed = brotli.compress(inner)
    return struct.pack(">IHHII", len(compressed) + 16, 16, 3, 5, 1) + compressed


def test_bilibili_live_parser_keeps_danmaku_but_drops_identity_fields() -> None:
    collected_at = pipeline._utc_now()
    timestamp_ms = int(collected_at.timestamp() * 1000)
    event = {
        "cmd": "DANMU_MSG:4:0:2:2:2:0",
        "info": [
            [0, 0, 0, 0, timestamp_ms],
            "这下真无量空处了",
            [123456, "不应保存的昵称"],
            [],
            {},
            "",
            0,
            0,
            0,
            json.dumps({"id_str": "bili-live-1", "user_hash": "不应保存"}),
        ],
    }
    decoded = decode_bilibili_packets(_bilibili_live_packet(event, version=3))
    message = bilibili_event_to_message(
        {"room_id": "13", "label": "哔哩哔哩刀塔2赛事", "circle": "DOTA2/游戏赛事"},
        decoded[0],
        collected_at=collected_at,
    )

    assert message is not None
    assert message["platform"] == "bilibili"
    assert message["source_kind"] == "public_live_sample"
    assert message["message_id"] == "bili-live-1"
    assert message["content"] == "这下真无量空处了"
    assert "123456" not in json.dumps(message, ensure_ascii=False)
    assert "不应保存" not in json.dumps(message, ensure_ascii=False)


def test_live_room_rotation_prefers_current_schedule_and_circle_coverage() -> None:
    from datetime import datetime, timezone

    rooms = [
        {
            "platform": "douyu",
            "room_id": "game-a",
            "sampling_bucket": "游戏",
            "preferred_local_hours": [20],
        },
        {
            "platform": "douyu",
            "room_id": "game-b",
            "sampling_bucket": "游戏",
            "preferred_local_hours": [20],
        },
        {
            "platform": "bilibili",
            "room_id": "anime",
            "sampling_bucket": "泛二次元",
            "preferred_local_hours": [20],
        },
        {
            "platform": "bilibili",
            "room_id": "virtual",
            "sampling_bucket": "虚拟主播",
            "preferred_local_hours": [20],
        },
        {
            "platform": "bilibili",
            "room_id": "daytime-tech",
            "sampling_bucket": "科技",
            "preferred_local_hours": [10],
        },
    ]
    run_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)  # 上海 20:00

    selected, skipped = _select_rooms(rooms, max_rooms=3, run_at=run_at)

    assert len(selected) == 3
    assert {room["sampling_bucket"] for room in selected} == {"游戏", "泛二次元", "虚拟主播"}
    assert any(room["room_id"] == "daytime-tech" for room in skipped)


def test_bilibili_recommended_feed_only_keeps_public_video_metadata() -> None:
    class FakeClient:
        def get_json(self, url: str) -> dict:
            assert "feed/rcmd" in url
            return {
                "code": 0,
                "data": {
                    "item": [
                        {
                            "goto": "av",
                            "bvid": "BV1REC",
                            "id": 101,
                            "title": "推荐视频",
                            "owner": {"name": "公开作者", "mid": 987654},
                        },
                        {"goto": "ad", "bvid": "BV1AD", "id": 102, "title": "广告"},
                    ]
                },
            }

    videos = fetch_recommended_videos(FakeClient(), {"max_videos": 3, "circle": "B站匿名推荐"})

    assert videos == [
        {
            "bvid": "BV1REC",
            "aid": 101,
            "title": "推荐视频",
            "creator": "公开作者",
            "circle": "B站匿名推荐",
            "discovery_source": "bilibili_recommended",
        }
    ]
    assert "987654" not in json.dumps(videos, ensure_ascii=False)


def test_creator_watchlist_only_keeps_exact_mid_and_recent_videos() -> None:
    class FakeClient:
        def get_json(self, url: str) -> dict:
            assert "search/type" in url
            return {
                "code": 0,
                "data": {
                    "result": [
                        {
                            "mid": 63231,
                            "bvid": "BV1FANSHI",
                            "aid": 101,
                            "title": "<em class=\"keyword\">泛式</em>聊新番",
                            "pubdate": 1_800_000_000,
                        },
                        {
                            "mid": 999,
                            "bvid": "BV1OTHER",
                            "aid": 102,
                            "title": "同名混入",
                            "pubdate": 1_800_000_000,
                        },
                    ]
                },
            }

    videos, errors = fetch_creator_watchlist_videos(
        FakeClient(),
        {
            "rotation_index": 0,
            "max_creators_per_run": 1,
            "max_videos_per_creator": 1,
            "max_age_days": 3650,
            "creators": [{"name": "泛式", "mid": "63231", "circle": "泛二次元"}],
        },
    )

    assert errors == []
    assert videos == [
        {
            "bvid": "BV1FANSHI",
            "aid": 101,
            "title": "泛式聊新番",
            "creator": "泛式",
            "circle": "泛二次元",
            "discovery_source": "bilibili_creator_watchlist",
        }
    ]


def test_source_configs_cover_lol_streamers_and_gacha_communities() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    live_config = json.loads((repo_root / "ops" / "live_sampling.example.json").read_text(encoding="utf-8"))
    discovery_config = json.loads((repo_root / "ops" / "p0_discovery.example.json").read_text(encoding="utf-8"))
    watchlist = json.loads((repo_root / "ops" / "meme_source_watchlist.json").read_text(encoding="utf-8"))

    live_room_ids = {str(item["room_id"]) for item in live_config["rooms"]}
    assert {"252140", "96291", "1126960", "138243", "6682963"} <= live_room_ids
    assert {"34348", "1151716", "942101"} <= live_room_ids
    assert {"21987615", "27263119", "32805602", "27354807", "5555734"} <= live_room_ids
    huya_rooms = [item for item in live_config["rooms"] if item["platform"] == "huya"]
    assert huya_rooms and all(item.get("enabled") is False for item in huya_rooms)

    creators = discovery_config["sources"]["bilibili"]["creator_watchlist"]["creators"]
    creator_mids = {str(item["mid"]) for item in creators}
    assert {"14110780", "1871001", "1773346"} <= creator_mids
    assert {"401742377", "1340190821", "1636034895", "1955897084", "161775300"} <= creator_mids

    names = {item["name"] for item in watchlist["sources"]}
    assert {"Doinb", "东北大鹌鹑", "余小C", "洞主", "Ning", "Uzi", "姿态", "TheShy"} <= names
    assert {"原神", "崩坏星穹铁道", "绝区零", "鸣潮", "明日方舟", "尘白禁区", "战双帕弥什"} <= names


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


def test_pre_review_filter_excludes_blacklist_without_mutating_archive_rows() -> None:
    evidence = [
        {"evidence_id": "e1", "content": "@用户"},
        {"evidence_id": "e2", "content": "3分钟前"},
        {"evidence_id": "e3", "content": "合成大西瓜"},
        {"evidence_id": "e4", "content": "无量空处"},
    ]

    eligible, report = filter_review_evidence(
        evidence,
        {"enabled": True, "exact_expressions": ["合成大西瓜"]},
    )

    assert [item["evidence_id"] for item in eligible] == ["e4"]
    assert report["excluded_count"] == 3
    assert report["excluded_by_reason"] == {"regex_blacklist": 2, "exact_blacklist": 1}
    assert report["raw_archive_preserved"] is True
    assert len(evidence) == 4


def test_lifecycle_decay_only_proposes_human_review() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    weight = time_decay_weight(
        "2026-07-18T00:00:00Z",
        now=now,
        half_life_days=30,
    )
    queue = select_inventory_decay_review(
        [
            {
                "card_id": "old-card",
                "canonical_expression": "旧梗",
                "age_class": "current_observed",
                "lifecycle": {"weight": 1.0},
            }
        ],
        {"old-card": {"last_observed_at": "2026-07-18T00:00:00Z", "observed_30d_count": 0}},
        {"default_half_life_days": 30, "review_below_weight": 0.5, "minimum_age_days": 21},
        now=now,
    )

    assert weight < 0.5
    assert queue[0]["review_actions"] == ["KEEP", "DOWNRANK", "RETIRE"]
    assert queue[0]["auto_apply"] is False


def test_rejected_rereview_respects_cooldown_and_seed() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rejected = [
        {"candidate_id": "eligible-a", "rejected_at": "2026-07-01T00:00:00Z"},
        {"candidate_id": "eligible-b", "rejected_at": "2026-07-02T00:00:00Z"},
        {"candidate_id": "cooling", "rejected_at": "2026-08-20T00:00:00Z"},
    ]

    first = select_rejected_for_rereview(
        rejected,
        {"cooldown_days": 30, "sample_size": 2},
        now=now,
        seed="weekly-queue",
    )
    second = select_rejected_for_rereview(
        rejected,
        {"cooldown_days": 30, "sample_size": 2},
        now=now,
        seed="weekly-queue",
    )

    assert first == second
    assert {item["candidate_id"] for item in first} == {"eligible-a", "eligible-b"}
    assert all(item["auto_apply"] is False for item in first)


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
    review_html = Path(result["review_page"]).read_text(encoding="utf-8")
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
    assert "出现语境证据" not in review_html
    assert "重复表达样本" not in review_html
    assert "普通表达 / 证据不足（1）" in review_html

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
    assert "meme_candidate" in rendered
    assert "<b>意图：</b>" in rendered
    assert "轻松地请求对方暂停或简化说明" in rendered
    assert "出现语境证据（不是使用场景）" not in rendered
    assert "重复表达样本" not in rendered
    assert "信息一下子太多了" not in rendered


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


def test_semantic_enricher_normalizes_non_meme_placeholder_fields(tmp_path: Path) -> None:
    candidate = _semantic_candidate()
    ordinary = {
        "classification": "ordinary_expression",
        "classification_reason": "只是普通感叹，现有证据不能支持稳定的梗语用路线。",
        "semantic_core": "普通感叹",
        "culture_scope": "中文日常交流",
        "usage_routes": "不适用",
        "required_context_signals": "不适用",
        "hard_blocks": "不适用",
        "positive_contexts": ["不适用"],
        "negative_contexts": ["不适用"],
    }

    enriched, report = enrich_candidates(
        [candidate],
        {"enabled": True, "required": True, "base_url": "https://example.test/v1", "model": "test-model"},
        cache_dir=tmp_path / "cache",
        completion=lambda messages, config: json.dumps(ordinary, ensure_ascii=False),
    )

    draft = enriched[0]["draft_card"]
    assert report["enriched_count"] == 1
    assert draft["classification"] == "ordinary_expression"
    assert draft["usage_routes"] == []
    assert draft["required_context_signals"] == []
    assert draft["positive_contexts"] == []


def test_semantic_enricher_repairs_invalid_model_structure_once(tmp_path: Path) -> None:
    candidate = _semantic_candidate()
    base = {
        "classification": "ordinary_expression",
        "classification_reason": "只是普通问候，当前证据不能支持稳定的梗语用路线。",
        "semantic_core": "普通问候",
        "culture_scope": "中文日常交流",
        "required_context_signals": [],
        "hard_blocks": [],
        "positive_contexts": [],
        "negative_contexts": [],
    }
    outputs = [
        {**base, "usage_routes": [{"route_tag": "不应存在"}]},
        {**base, "usage_routes": []},
    ]
    calls: list[list[dict[str, str]]] = []

    def completion(messages: list[dict[str, str]], config: dict[str, object]) -> str:
        calls.append(messages)
        return json.dumps(outputs[len(calls) - 1], ensure_ascii=False)

    enriched, report = enrich_candidates(
        [candidate],
        {"enabled": True, "required": True, "base_url": "https://example.test/v1", "model": "test-model"},
        cache_dir=tmp_path / "cache",
        completion=completion,
    )

    assert enriched[0]["draft_card"]["classification"] == "ordinary_expression"
    assert report["validation_repair_count"] == 1
    assert len(calls) == 2
    assert "非 meme_candidate 不得生成 usage_routes" in calls[1][-1]["content"]


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


def test_chiba_model_config_resolves_task_without_exposing_key(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "model_config.toml"
    config_path.write_text(
        """
[model_task_config.utils]
model_list = ["text-main"]
temperature = 0.4
hard_timeout = 30

[[models]]
name = "text-main"
model_identifier = "provider-text-id"
api_provider = "ProviderA"

[[api_providers]]
name = "ProviderA"
base_url = "https://example.test/v1"
api_key = ""
api_key_env = "TEST_CHIBA_KEY"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_CHIBA_KEY", "secret-value")

    resolved = resolve_chiba_task(config_path, "utils")
    public = public_model_metadata(resolved)

    assert resolved["api_key"] == "secret-value"
    assert resolved["model"] == "provider-text-id"
    assert public == {
        "task": "utils",
        "configured_model": "text-main",
        "model_identifier": "provider-text-id",
        "provider": "ProviderA",
    }
    assert "secret-value" not in json.dumps(public)


def test_vector_calibration_attaches_existing_route_neighbors(tmp_path: Path, monkeypatch) -> None:
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    (release_dir / "vector_index.json").write_text(
        json.dumps(
            {
                "embedding_model": "embedding-main",
                "release_id": "reviewed-test-v1",
                "dimension": 2,
                "items": [
                    {
                        "card_id": "card-overload",
                        "canonical_expression": "无量空处",
                        "route_index": 0,
                        "route_tag": "信息过载",
                        "serving_scope": "circle_only",
                        "anchor_kind": "intent",
                        "anchor_text": "表示自己看懵了",
                    },
                    {
                        "card_id": "card-win",
                        "canonical_expression": "我们是冠军",
                        "route_index": 0,
                        "route_tag": "胜利庆祝",
                        "serving_scope": "general",
                        "anchor_kind": "intent",
                        "anchor_text": "表达胜利喜悦",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    np.save(release_dir / "vectors.npy", np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))
    config_path = tmp_path / "model_config.toml"
    config_path.write_text(
        """
[model_task_config.embedding]
model_list = ["embedding-main"]

[[models]]
name = "embedding-main"
model_identifier = "provider-embedding-id"
api_provider = "ProviderA"

[[api_providers]]
name = "ProviderA"
base_url = "https://example.test/v1"
api_key_env = "TEST_EMBEDDING_KEY"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret-value")
    candidate = _semantic_candidate()
    candidate["semantic_enrichment"] = {"status": "pending_human_review"}
    candidate["draft_card"] = {
        "classification": "meme_candidate",
        "semantic_core": "信息过载而看懵",
        "usage_routes": [
            {
                "when": "对方一次给出太多复杂信息时",
                "communicative_intent": "告诉对方自己看懵并希望简化解释",
                "response_function": "承接困惑并转入信息梳理",
            }
        ],
    }

    report = calibrate_candidates(
        [candidate],
        {
            "enabled": True,
            "required": True,
            "chiba_model_config_path": str(config_path),
            "release_dir": str(release_dir),
            "top_k": 2,
        },
        embed=lambda text: [1.0, 0.0],
    )

    calibration = candidate["draft_card"]["usage_routes"][0]["semantic_calibration"]
    assert report["calibrated_route_count"] == 1
    assert calibration["nearest_existing_routes"][0]["card_id"] == "card-overload"
    assert calibration["nearest_existing_routes"][0]["similarity"] == 1.0
    assert calibration["auto_merge"] is False


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


def test_reviewed_card_pipeline_only_uses_explicitly_retained_groups(tmp_path: Path) -> None:
    second_pass = {
        "source_decisions": {"date_range": "2026-08-27..2026-08-31", "reviewed_count": 3},
        "groups": [
            {
                "group_id": "keep",
                "canonical_expression": "无量空处",
                "members": ["无量空处", "无量空处了"],
                "content_type_proposal": "meme",
                "serving_policy_proposal": "understand",
                "message_count": 3,
            },
            {
                "group_id": "unreviewed",
                "canonical_expression": "待定口癖",
                "members": ["待定口癖"],
                "content_type_proposal": "catchphrase",
                "serving_policy_proposal": "pending",
            },
        ],
    }
    summary = {"candidates": [{"phrase": "无量空处", "aliases": ["无量空处了"]}]}
    evidence = [
        {
            "evidence_id": "e1",
            "content": "无量空处",
            "content_id": "BV1:1",
            "platform": "bilibili",
            "source_id": "BV1TEST",
            "circle": "泛二次元",
        }
    ]
    prepared = prepare_reviewed_groups(second_pass, summary, evidence)

    assert prepared["summary"]["prepared_group_count"] == 1
    assert prepared["items"][0]["aliases"] == ["无量空处了"]
    prepared["items"][0]["web_research"] = {
        "research_version": "test-v1",
        "retrieved_at": "2026-09-02T00:00:00+00:00",
        "status": "current_usage_only",
        "freshness": {"class": "recent_90d"},
        "results": [
            {
                "source_id": "web-guide",
                "provider": "bilibili",
                "source_kind": "recent_video_usage",
                "source_tier": "contemporaneous_platform_usage",
                "title": "无量空处梗指南",
                "url": "https://www.bilibili.com/video/BV1GUIDE/",
                "snippet": "用于形容一次接收太多信息而彻底看懵。",
                "published_at": "2026-08-30T00:00:00+00:00",
                "match_quality": "title_exact",
            }
        ],
    }

    semantic = {
        "semantic_core": "借领域展开造成的信息过载，告知对方自己已经完全看懵",
        "culture_scope": "中文互联网泛二次元",
        "serving_scope": "circle_only",
        "usage_routes": [
            {
                "route_tag": "看懵求简化",
                "when": "对方连续抛出大量设定和术语，自己已无法继续跟上时",
                "communicative_intent": "让对方意识到信息密度过高，并把说明改成更容易理解的版本",
                "allowed_realizations": ["无 量 空 处！", "无量空处了"],
            }
        ],
        "required_context_signals": ["前文信息密集", "说话者明确表示看不懂"],
        "hard_blocks": ["严肃求助场景", "对方不了解该圈层"],
        "positive_contexts": [
            {"context": "用户在复杂说明后说脑子已经被信息塞满了", "expected_action": "UNDERSTAND_ONLY"},
            {"context": "用户引用该词表示自己完全跟不上设定讲解", "expected_action": "UNDERSTAND_ONLY"},
        ],
        "negative_contexts": [
            {"context": "用户正在询问作品中术式的准确设定是什么", "expected_action": "SKIP", "reason": "需要直接回答事实"},
            {"context": "用户描述真实身体不适和认知困难需要帮助", "expected_action": "SKIP", "reason": "健康求助不应玩梗"},
            {"context": "对话中没有信息过载也没有相关圈层信号", "expected_action": "SKIP", "reason": "缺少语义锚点"},
        ],
        "retrieval_facets": ["信息过载", "完全看懵", "复杂设定"],
        "research_synthesis": {
            "origin_summary": "公开检索未找到足以核定典故的来源，暂按圈层用法理解",
            "current_usage_summary": "弹幕中用于告诉对方自己被高密度信息彻底弄懵",
            "freshness_assessment": "current",
            "research_confidence": "medium",
            "supporting_source_ids": ["web-guide"],
        },
    }
    config = {"resolved_model": {"api_key": "test", "model": "fake"}}
    enriched = enrich_reviewed_groups(
        prepared,
        config,
        cache_dir=tmp_path / "cache",
        completion=lambda messages, resolved: json.dumps(semantic, ensure_ascii=False),
    )
    card = enriched["items"][0]["storage_card"]

    assert set(card) == ONLINE_CARD_KEYS
    assert card["human_review"]["status"] == "pending"
    assert card["usage_routes"][0]["allowed_realizations"] == ["无量空处", "无量空处了"]
    assert card["positive_contexts"][0]["expected_action"] == "UNDERSTAND_ONLY"
    assert card["knowledge"]["source_material"]["web_research"]["sources"][0]["source_id"] == "web-guide"
    assert validate_storage_cards(enriched) == []
    assert build_pending_library(enriched)["card_count"] == 1

    page = render_review_html(enriched)
    assert "存储内容 JSON（已折叠，可直接编辑）" in page
    assert "互联网检索材料" in page
    assert "全部检索状态" in page
    assert "<textarea" in page
    assert "fetch(" not in page
    assert "/Users/" not in page


def test_reviewed_card_rejects_vague_intent_even_inside_long_sentence(tmp_path: Path) -> None:
    semantic = {
        "semantic_core": "用夸张说法表示自己被大量信息冲击到无法继续理解",
        "culture_scope": "中文互联网",
        "serving_scope": "general",
        "usage_routes": [
            {
                "route_tag": "空泛路线",
                "when": "对方给出大量复杂说明而用户已经无法跟上时",
                "communicative_intent": "让对方知道自己正在参与玩梗并形成共鸣",
                "allowed_realizations": ["无量空处"],
            }
        ],
        "required_context_signals": ["信息密集", "用户表示看懵"],
        "hard_blocks": ["严肃求助", "无上下文"],
        "positive_contexts": [
            {"context": "复杂说明后用户明确表示自己已经看不懂了", "expected_action": "USE"},
            {"context": "用户希望对方把高密度内容重新简化说明", "expected_action": "UNDERSTAND_ONLY"},
        ],
        "negative_contexts": [
            {"context": "用户询问作品中的准确设定和事实信息", "expected_action": "SKIP", "reason": "应直接回答事实"},
            {"context": "用户报告真实身体不适并希望获得帮助", "expected_action": "SKIP", "reason": "不应玩笑回应"},
            {"context": "对话没有出现任何信息过载或理解困难", "expected_action": "SKIP", "reason": "缺少触发信号"},
        ],
        "retrieval_facets": ["信息过载", "看不懂", "复杂说明"],
        "research_synthesis": {
            "origin_summary": "公开检索未找到足以核定典故的来源，暂按圈层用法理解",
            "current_usage_summary": "弹幕中用于告诉对方自己被高密度信息彻底弄懵",
            "freshness_assessment": "current",
            "research_confidence": "low",
            "supporting_source_ids": [],
        },
    }
    document = {
        "items": [
            {
                "group_id": "g1",
                "canonical_expression": "无量空处",
                "aliases": [],
                "prior_serving_policy": "refine",
                "signals": {},
                "evidence": [],
            }
        ]
    }
    enriched = enrich_reviewed_groups(
        document,
        {"validation_repair_attempts": 0},
        cache_dir=tmp_path / "cache",
        completion=lambda messages, resolved: json.dumps(semantic, ensure_ascii=False),
    )

    assert enriched["semantic_enrichment_report"]["failure_count"] == 1
    assert "过于空泛" in enriched["items"][0]["semantic_error"]


def test_web_research_combines_encyclopedia_and_recent_usage(tmp_path: Path) -> None:
    document = {"items": [{"canonical_expression": "你币有了", "aliases": []}]}
    encyclopedia = {
        "source_id": "web-encyclopedia",
        "provider": "gengwh",
        "source_kind": "meme_encyclopedia",
        "source_tier": "community_encyclopedia",
        "title": "你币有了是什么梗？",
        "url": "https://example.test/encyclopedia",
        "snippet": "B站观众用于表示已经投币，也可以调侃催投币。",
        "published_at": "2026-08-30T00:00:00+00:00",
        "match_quality": "title_exact",
        "relevance_score": 1.0,
    }
    recent_video = {
        "source_id": "web-video",
        "provider": "bilibili",
        "source_kind": "recent_video_usage",
        "source_tier": "contemporaneous_platform_usage",
        "title": "你币有了：投币名场面",
        "url": "https://example.test/video",
        "snippet": "近期弹幕用法整理",
        "published_at": "2026-08-31T00:00:00+00:00",
        "match_quality": "title_exact",
        "relevance_score": 1.0,
    }
    enriched = research_reviewed_groups(
        document,
        {"sources": ["gengwh", "bilibili"], "workers": 2},
        cache_dir=tmp_path / "research-cache",
        fetchers={"gengwh": lambda value: [encyclopedia], "bilibili": lambda value: [recent_video]},
    )

    research = enriched["items"][0]["web_research"]
    assert research["status"] == "origin_and_current_usage"
    assert research["freshness"]["class"] == "recent_90d"
    assert research["provider_count"] == 2


def test_gengwh_search_parser_keeps_short_public_excerpt() -> None:
    class FakeClient:
        def get_text(self, url: str, *, referer: str | None = None) -> str:
            assert "/operate/search?" in url
            return """
            <div class="search-result-item">
              <div class="result-title"><a href="/read/351">你币有了是什么梗？</a></div>
              <div class="result-excerpt">B站观众用“<span>你币有了</span>”表示已经投币。</div>
              <span>📅 3天前</span>
            </div></div>﻿
            """

    results = search_gengwh(FakeClient(), "你币有了")

    assert len(results) == 1
    assert results[0]["url"] == "https://www.gengwh.com/read/351"
    assert results[0]["snippet"] == "B站观众用“ 你币有了 ”表示已经投币。"


def test_question_search_uses_expression_inside_natural_language_query() -> None:
    class FakeClient:
        def get_text(self, url: str, *, referer: str | None = None) -> str:
            assert "%E5%BC%B9%E5%B9%95" in url
            return """
            <ol>
              <li class="res-list">
                <h3 class="res-title"><a href="https://www.so.com/link?x=1"
                  data-mdurl="https://example.test/guide">设备玩耍是什么梗</a></h3>
                <p class="res-desc">玩机器直播间用来调侃 device 选手的固定说法。</p>
              </li>
            </ol>
            """

    results = search_so_question(
        FakeClient(), "在玩机器直播间弹幕中看到“设备玩耍”是什么意思，是什么梗"
    )

    assert len(results) == 1
    assert results[0]["url"] == "https://example.test/guide"
    assert results[0]["search_question"].startswith("在玩机器直播间")
