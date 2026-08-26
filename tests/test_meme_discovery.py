"""P0 热梗候选离线发现测试。"""

from pathlib import Path

import json

from meme_discovery.bilibili import parse_danmaku_reply
from meme_discovery.miner import mine_candidates, normalize_expression
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
            }
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
    assert result["new_evidence_count"] == 3
    assert result["rolling_evidence_count"] == 3
    assert review["review_policy"]["auto_publish"] is False
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
