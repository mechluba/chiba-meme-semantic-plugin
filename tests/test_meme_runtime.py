"""版本化梗包与纯语义运行逻辑测试。"""

from pathlib import Path
from shutil import copytree
from copy import deepcopy

import json

import numpy as np
import pytest

from chiba_meme_semantic_plugin.meme_runtime import (
    MEME_ACTION_ARG,
    MEME_CARD_ID_ARG,
    MEME_RELEASE_ID_ARG,
    MEME_ROUTE_INDEX_ARG,
    MemeRelease,
    MemeReleaseError,
    augment_reply_tool_definitions,
    build_semantic_query_from_session_messages,
    extract_semantic_query_text,
)
from chiba_meme_semantic_plugin.plugin import (
    DEFAULT_RELEASE_ID,
    DEFAULT_UNDERSTAND_ONLY_CARD_IDS,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = PLUGIN_ROOT / "resources" / "releases" / DEFAULT_RELEASE_ID


@pytest.fixture(scope="module")
def release() -> MemeRelease:
    return MemeRelease.load(RELEASE_DIR)


def _find_route_row(release: MemeRelease, expression: str) -> int:
    for item in release.route_items:
        card = release.cards_by_id[str(item["card_id"])]
        if card["canonical_expression"] == expression:
            return int(item["row"])
    raise AssertionError(f"找不到梗: {expression}")


def test_release_loads_only_reviewed_cards_and_matches_fingerprint(
    release: MemeRelease,
) -> None:
    fingerprint = release.fingerprint()
    assert fingerprint["release_id"] == DEFAULT_RELEASE_ID
    assert fingerprint["embedding_model"] == "volcengine-ark-embedding"
    assert fingerprint["dimension"] == 2048
    assert fingerprint["card_count"] == 156
    assert fingerprint["route_count"] == 356
    assert fingerprint["vector_count"] == 1780
    assert all(
        card["human_review"]["status"] == "approved"
        for card in release.cards_by_id.values()
    )


def test_understand_only_policy_only_references_released_cards(
    release: MemeRelease,
) -> None:
    understand_only_ids = set(DEFAULT_UNDERSTAND_ONLY_CARD_IDS)
    assert len(understand_only_ids) == 59
    assert understand_only_ids <= set(release.cards_by_id)
    assert all(
        all(
            context["expected_action"] == "UNDERSTAND_ONLY"
            for context in release.cards_by_id[card_id]["positive_contexts"]
        )
        for card_id in understand_only_ids
        if card_id.startswith("reviewed-meme-")
    )


def test_release_rejects_tampered_reviewed_library(tmp_path: Path) -> None:
    copied = tmp_path / DEFAULT_RELEASE_ID
    copytree(RELEASE_DIR, copied)
    library_path = copied / "library.json"
    library = json.loads(library_path.read_text(encoding="utf-8"))
    library["cards"][0]["semantic_core"] = "被篡改"
    library_path.write_text(
        json.dumps(library, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(MemeReleaseError, match="哈希不一致"):
        MemeRelease.load(copied)


def test_release_allows_duplicate_canonical_expressions_with_unique_card_ids(
    release: MemeRelease,
) -> None:
    library = deepcopy(release.library)
    library["cards"][1]["canonical_expression"] = library["cards"][0]["canonical_expression"]

    MemeRelease._validate_payloads(
        release_id=release.release_id,
        manifest=release.release_manifest,
        library=library,
        vector_index=release.vector_index,
        vectors=release.vectors,
    )


def test_vector_retrieval_returns_exact_route_first(release: MemeRelease) -> None:
    row = _find_route_row(release, "我们是冠军")
    expected_card_id = str(release.route_items[row]["card_id"])
    candidates = release.retrieve(
        release.vectors[row],
        top_k=3,
        minimum_similarity=0.0,
    )
    assert candidates[0].card_id == expected_card_id
    assert candidates[0].canonical_expression == "我们是冠军"
    assert candidates[0].similarity == pytest.approx(1.0, abs=1e-5)
    assert len({candidate.card_id for candidate in candidates}) == len(candidates)


def test_vector_retrieval_rejects_wrong_dimension(release: MemeRelease) -> None:
    with pytest.raises(MemeReleaseError, match="维度不一致"):
        release.retrieve(
            np.zeros(release.dimension - 1, dtype=np.float32),
            top_k=3,
            minimum_similarity=0.0,
        )


def test_query_text_only_uses_selected_real_history() -> None:
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": [{"type": "text", "text": "第二句"}]},
        {"role": "user", "content": "内部候选绝不能进入查询"},
    ]
    result = extract_semantic_query_text(
        messages,
        selected_history_count=2,
        message_limit=6,
        max_chars=1000,
    )
    assert result == "user: 第一句\nassistant: 第二句"
    assert "内部候选" not in result


def test_query_text_uses_ordered_visible_session_messages() -> None:
    messages = [
        {
            "timestamp": "3",
            "processed_plain_text": "最后一句",
            "message_info": {"user_info": {"user_id": "user-1"}},
        },
        {
            "timestamp": "1",
            "raw_message": [{"type": "text", "data": "开头"}],
            "message_info": {"user_info": {"user_id": "user-1"}},
        },
        {
            "timestamp": "2",
            "processed_plain_text": "收到",
            "message_info": {"user_info": {"user_id": "galpet"}},
        },
        {
            "timestamp": "4",
            "processed_plain_text": "内部通知",
            "message_info": {"user_info": {"user_id": "system"}},
            "is_notify": True,
        },
    ]
    result = build_semantic_query_from_session_messages(
        messages,
        bot_account_id="galpet",
        message_limit=4,
        max_chars=1000,
    )
    assert result == "user: 开头\nassistant: 收到\nuser: 最后一句"
    assert "内部通知" not in result


def test_reply_tool_schema_is_augmented_without_mutating_source(
    release: MemeRelease,
) -> None:
    source = [
        {
            "type": "function",
            "function": {
                "name": "reply",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                },
            },
        }
    ]
    candidate_ids = list(release.cards_by_id)[:3]
    updated = augment_reply_tool_definitions(
        source,
        release_id=release.release_id,
        candidate_card_ids=candidate_ids,
    )
    properties = updated[0]["function"]["parameters"]["properties"]
    assert MEME_ACTION_ARG not in source[0]["function"]["parameters"]["properties"]
    assert properties[MEME_ACTION_ARG]["enum"] == [
        "USE",
        "UNDERSTAND_ONLY",
        "SKIP",
    ]
    assert properties[MEME_RELEASE_ID_ARG]["enum"] == [release.release_id]
    assert properties[MEME_CARD_ID_ARG]["enum"] == candidate_ids
    assert properties[MEME_ROUTE_INDEX_ARG]["type"] == "integer"


def test_understand_only_resource_excludes_the_literal_expression(
    release: MemeRelease,
) -> None:
    row = _find_route_row(release, "我们是冠军")
    item = release.route_items[row]
    selection = release.parse_tool_selection(
        {
            MEME_ACTION_ARG: "UNDERSTAND_ONLY",
            MEME_RELEASE_ID_ARG: release.release_id,
            MEME_CARD_ID_ARG: item["card_id"],
            MEME_ROUTE_INDEX_ARG: item["route_index"],
        }
    )
    assert selection is not None
    resource = release.build_replyer_resource(
        selection,
        effective_action="UNDERSTAND_ONLY",
    )
    assert "本轮内部语义理解与禁止复述约束" in resource
    assert "我们是冠军" not in resource
    assert "不得复读" in resource


def test_planner_scope_metadata_is_not_a_hard_circle_gate(
    release: MemeRelease,
) -> None:
    row = _find_route_row(release, "公若不弃，布愿拜为义父")
    candidates = release.retrieve(
        release.vectors[row],
        top_k=1,
        minimum_similarity=-1.0,
    )
    resource = release.build_planner_resource(
        candidates,
        understand_only_card_ids=set(),
    )
    assert "不得仅因用户没有主动提及作品名、圈层名或梗名而 SKIP" in resource
    assert "只有 required_context_signals 和 hard_blocks" in resource
    assert "Planner 不负责最终措辞" in resource
    assert "必须选 USE" in resource
    assert "缺圈层锚点" not in resource


def test_semantic_selector_prompt_uses_reviewed_examples(
    release: MemeRelease,
) -> None:
    row = _find_route_row(release, "公若不弃，布愿拜为义父")
    candidates = release.retrieve(
        release.vectors[row],
        top_k=1,
        minimum_similarity=-1.0,
    )
    prompt = release.build_semantic_selector_prompt(
        candidates,
        semantic_query_text="user: 我把攻略和代码都整理给你了",
        understand_only_card_ids=set(),
    )
    assert "不得使用关键词、正则或字面命中" in prompt
    assert "reviewed_positive_contexts" in prompt
    assert "游戏队友带飞后" in prompt
    assert "工作场合同事帮忙后" in prompt


def test_semantic_selector_hard_block_or_low_confidence_fails_closed(
    release: MemeRelease,
) -> None:
    row = _find_route_row(release, "公若不弃，布愿拜为义父")
    candidates = release.retrieve(
        release.vectors[row],
        top_k=1,
        minimum_similarity=-1.0,
    )
    candidate = candidates[0]
    base_payload = {
        "action": "USE",
        "card_id": candidate.card_id,
        "route_index": candidate.route_index,
        "confidence": 0.95,
        "matched_hard_blocks": [],
        "reason": "适合",
    }
    accepted = release.parse_semantic_selector_response(
        json.dumps(base_payload, ensure_ascii=False),
        candidates=candidates,
        understand_only_card_ids=set(),
        minimum_confidence=0.75,
    )
    assert accepted.selection.action == "USE"

    base_payload["matched_hard_blocks"] = ["正式场合"]
    blocked = release.parse_semantic_selector_response(
        json.dumps(base_payload, ensure_ascii=False),
        candidates=candidates,
        understand_only_card_ids=set(),
        minimum_confidence=0.75,
    )
    assert blocked.requested_action == "USE"
    assert blocked.selection.action == "SKIP"

    base_payload["matched_hard_blocks"] = []
    base_payload["confidence"] = 0.4
    uncertain = release.parse_semantic_selector_response(
        json.dumps(base_payload, ensure_ascii=False),
        candidates=candidates,
        understand_only_card_ids=set(),
        minimum_confidence=0.75,
    )
    assert uncertain.selection.action == "SKIP"


def test_tool_selection_rejects_release_mismatch(release: MemeRelease) -> None:
    with pytest.raises(MemeReleaseError, match="release_id 不匹配"):
        release.parse_tool_selection(
            {
                MEME_ACTION_ARG: "USE",
                MEME_RELEASE_ID_ARG: "wrong-release",
                MEME_CARD_ID_ARG: next(iter(release.cards_by_id)),
                MEME_ROUTE_INDEX_ARG: 0,
            }
        )


def test_skip_tool_selection_accepts_action_only(release: MemeRelease) -> None:
    selection = release.parse_tool_selection({MEME_ACTION_ARG: "SKIP"})
    assert selection is not None
    assert selection.action == "SKIP"
    assert selection.release_id == release.release_id
    assert selection.card_id == ""
