"""Planner / Replyer Hook 级联测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import json
import logging

import pytest

from chiba_meme_semantic_plugin.meme_runtime import (
    MEME_ACTION_ARG,
    MEME_CARD_ID_ARG,
    MEME_RELEASE_ID_ARG,
    MEME_ROUTE_INDEX_ARG,
)
from chiba_meme_semantic_plugin.plugin import PLUGIN_VERSION, MemeSemanticPlugin


TARGET_SESSION = "galpet-session-1"


@dataclass
class FakeChat:
    streams: list[dict[str, Any]]
    calls: list[str] = field(default_factory=list)

    async def get_all_streams(self, *, platform: str) -> list[dict[str, Any]]:
        self.calls.append(platform)
        return list(self.streams)


@dataclass
class FakeMessage:
    messages: list[dict[str, Any]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def get_recent(
        self,
        *,
        chat_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.calls.append({"chat_id": chat_id, "limit": limit})
        return list(self.messages[-limit:])


@dataclass
class FakeLLM:
    embedding: list[float]
    embedding_model: str
    selector_json: str = (
        '{"action":"SKIP","card_id":"","route_index":0,'
        '"confidence":0.0,"matched_hard_blocks":[],"reason":"测试跳过"}'
    )
    quality_gate_json: str = (
        '{"violation":false,"reason":"没有复读或解释"}'
    )
    evaluation_json: str = (
        '{"used":true,"natural":true,"forced":false,'
        '"serious_context_violation":false,"internal_leakage":false,'
        '"reason":"自然呼应"}'
    )
    embed_calls: list[dict[str, Any]] = field(default_factory=list)
    generate_calls: list[dict[str, Any]] = field(default_factory=list)

    async def embed(self, **kwargs: Any) -> dict[str, Any]:
        self.embed_calls.append(dict(kwargs))
        return {
            "success": True,
            "embedding": list(self.embedding),
            "model_name": self.embedding_model,
        }

    async def generate(
        self,
        prompt: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.generate_calls.append({"prompt": prompt, **kwargs})
        if "梗语义机会裁判" in prompt:
            response = self.selector_json
        elif "梗语义输出质量闸门" in prompt:
            response = self.quality_gate_json
        else:
            response = self.evaluation_json
        return {
            "success": True,
            "response": response,
            "model_name": "fake-utils",
        }


@dataclass
class FakeContext:
    chat: FakeChat
    message: FakeMessage
    llm: FakeLLM
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("test.meme-semantic-plugin")
    )


def _reply_tool() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "reply",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string"},
                        "msg_id": {"type": "string"},
                    },
                },
            },
        }
    ]


def _row_for_expression(plugin: MemeSemanticPlugin, expression: str) -> int:
    release = plugin._require_release()
    for item in release.route_items:
        card = release.cards_by_id[str(item["card_id"])]
        if card["canonical_expression"] == expression:
            return int(item["row"])
    raise AssertionError(f"找不到梗: {expression}")


def _make_plugin(
    *,
    target: bool = True,
    understand_only_card_ids: list[str] | None = None,
    semantic_selector_enabled: bool = False,
) -> tuple[MemeSemanticPlugin, FakeContext]:
    plugin = MemeSemanticPlugin()
    plugin.set_plugin_config(
        {
            "plugin": {
                "enabled": True,
                "config_version": PLUGIN_VERSION,
            },
            "serving": {
                "minimum_similarity": -1.0,
                "semantic_selector_enabled": semantic_selector_enabled,
                "understand_only_card_ids": understand_only_card_ids or [],
            },
        }
    )
    plugin._load_release()
    release = plugin._require_release()
    row = _row_for_expression(plugin, "我们是冠军")
    streams = (
        [
            {
                "session_id": TARGET_SESSION,
                "platform": "galpet_app",
                "account_id": "galpet",
                "scope": "private",
                "is_group_session": False,
            }
        ]
        if target
        else []
    )
    fake_context = FakeContext(
        chat=FakeChat(streams),
        message=FakeMessage(
            [
                {
                    "message_id": "user-1",
                    "timestamp": "1",
                    "processed_plain_text": "这波配合太漂亮了，我们拿下！",
                    "message_info": {
                        "user_info": {
                            "user_id": "user-1",
                        }
                    },
                    "is_command": False,
                    "is_notify": False,
                }
            ]
        ),
        llm=FakeLLM(
            embedding=release.vectors[row].tolist(),
            embedding_model=release.embedding_model,
        ),
    )
    plugin._set_context(fake_context)
    return plugin, fake_context


async def _run_planner(
    plugin: MemeSemanticPlugin,
    *,
    session_id: str = TARGET_SESSION,
) -> dict[str, Any]:
    return await plugin.handle_planner_before_request(
        session_id=session_id,
        messages=[
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "这波配合太漂亮了，我们拿下！"},
            {"role": "user", "content": "尾部内部资源"},
        ],
        tool_definitions=_reply_tool(),
        model_name="",
        selected_history_count=1,
        built_message_count=3,
        selection_reason="test",
    )


def _selected_args(
    plugin: MemeSemanticPlugin,
    *,
    action: str,
) -> dict[str, Any]:
    release = plugin._require_release()
    state = plugin._candidate_states[TARGET_SESSION]
    card_id = state.candidate_card_ids[0]
    route_index = next(
        int(item["route_index"])
        for item in release.route_items
        if item["card_id"] == card_id
    )
    return {
        MEME_ACTION_ARG: action,
        MEME_RELEASE_ID_ARG: release.release_id,
        MEME_CARD_ID_ARG: card_id,
        MEME_ROUTE_INDEX_ARG: route_index,
    }


@pytest.mark.asyncio
async def test_planner_hook_retrieves_and_augments_reply_tool() -> None:
    plugin, context = _make_plugin()
    result = await _run_planner(plugin)
    modified = result["modified_kwargs"]
    assert len(context.llm.embed_calls) == 1
    assert context.llm.embed_calls[0]["task_name"] == "embedding"
    assert "尾部内部资源" not in context.llm.embed_calls[0]["text"]
    assert context.llm.embed_calls[0]["text"] == (
        "user: 这波配合太漂亮了，我们拿下！"
    )
    assert context.message.calls == [
        {"chat_id": TARGET_SESSION, "limit": 6}
    ]
    assert "内部梗语义候选" in modified["messages"][-1]["content"]
    properties = modified["tool_definitions"][0]["function"]["parameters"][
        "properties"
    ]
    assert MEME_ACTION_ARG in properties
    assert properties[MEME_CARD_ID_ARG]["enum"] == list(
        plugin._candidate_states[TARGET_SESSION].candidate_card_ids
    )


@pytest.mark.asyncio
async def test_planner_hook_reuses_embedding_for_same_visible_context() -> None:
    plugin, context = _make_plugin()
    first = await _run_planner(plugin)
    second = await _run_planner(plugin)
    assert "modified_kwargs" in first
    assert "modified_kwargs" in second
    assert len(context.llm.embed_calls) == 1
    assert len(context.message.calls) == 2


@pytest.mark.asyncio
async def test_out_of_scope_session_has_zero_embedding_cost() -> None:
    plugin, context = _make_plugin(target=False)
    result = await _run_planner(plugin)
    assert result == {"action": "continue"}
    assert context.llm.embed_calls == []
    assert context.message.calls == []
    assert TARGET_SESSION not in plugin._candidate_states


@pytest.mark.asyncio
async def test_skip_selection_needs_no_release_or_card_fields() -> None:
    plugin, _ = _make_plugin()
    await _run_planner(plugin)
    result = await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="已有要求",
        reply_tool_args={MEME_ACTION_ARG: "SKIP"},
    )
    extra_prompt = result["modified_kwargs"]["extra_prompt"]
    assert extra_prompt.startswith("已有要求")
    assert "本轮内部候选表达记忆" in extra_prompt
    assert "一次性语义表达记忆" in extra_prompt


@pytest.mark.asyncio
async def test_planner_omission_still_gives_replyer_semantic_candidates() -> None:
    plugin, _ = _make_plugin()
    await _run_planner(plugin)
    result = await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args={"msg_id": "user-1"},
    )
    assert "本轮内部候选表达记忆" in (
        result["modified_kwargs"]["extra_prompt"]
    )


@pytest.mark.asyncio
async def test_semantic_selector_promotes_planner_skip_to_one_resource() -> None:
    plugin, context = _make_plugin(semantic_selector_enabled=True)
    await _run_planner(plugin)
    release = plugin._require_release()
    candidate = plugin._session_candidates(session_id=TARGET_SESSION)[0]
    context.llm.selector_json = json.dumps(
        {
            "action": "USE",
            "card_id": candidate.card_id,
            "route_index": candidate.route_index,
            "confidence": 0.93,
            "matched_hard_blocks": [],
            "reason": "语义明确适合",
        },
        ensure_ascii=False,
    )
    result = await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args={MEME_ACTION_ARG: "SKIP"},
    )
    extra_prompt = result["modified_kwargs"]["extra_prompt"]
    assert "本轮内部最终表达决策" in extra_prompt
    assert "本轮内部候选表达记忆" not in extra_prompt
    assert release.cards_by_id[candidate.card_id][
        "canonical_expression"
    ] in extra_prompt
    selector_call = context.llm.generate_calls[0]
    assert selector_call["model"] == "utils"
    assert "不得使用关键词、正则或字面命中" in selector_call["prompt"]
    assert "reviewed_positive_contexts" in selector_call["prompt"]
    assert (
        plugin._replyer_decision_states[TARGET_SESSION].source
        == "semantic_selector"
    )
    assert result["modified_kwargs"]["reply_tool_args"][
        MEME_ACTION_ARG
    ] == "USE"


@pytest.mark.asyncio
async def test_semantic_selector_low_confidence_use_fails_closed() -> None:
    plugin, context = _make_plugin(semantic_selector_enabled=True)
    await _run_planner(plugin)
    candidate = plugin._session_candidates(session_id=TARGET_SESSION)[0]
    context.llm.selector_json = json.dumps(
        {
            "action": "USE",
            "card_id": candidate.card_id,
            "route_index": candidate.route_index,
            "confidence": 0.4,
            "matched_hard_blocks": [],
            "reason": "不够确定",
        },
        ensure_ascii=False,
    )
    result = await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args={MEME_ACTION_ARG: "SKIP"},
    )
    assert result == {"action": "continue"}
    assert TARGET_SESSION not in plugin._replyer_decision_states


@pytest.mark.asyncio
async def test_semantic_selector_hard_block_overrides_use() -> None:
    plugin, context = _make_plugin(semantic_selector_enabled=True)
    await _run_planner(plugin)
    candidate = plugin._session_candidates(session_id=TARGET_SESSION)[0]
    context.llm.selector_json = json.dumps(
        {
            "action": "USE",
            "card_id": candidate.card_id,
            "route_index": candidate.route_index,
            "confidence": 0.99,
            "matched_hard_blocks": ["严肃场景"],
            "reason": "存在阻断",
        },
        ensure_ascii=False,
    )
    result = await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args={MEME_ACTION_ARG: "SKIP"},
    )
    assert result == {"action": "continue"}
    assert TARGET_SESSION not in plugin._replyer_decision_states


@pytest.mark.asyncio
async def test_use_selection_reaches_replyer_as_optional_expression() -> None:
    plugin, _ = _make_plugin()
    await _run_planner(plugin)
    args = _selected_args(plugin, action="USE")
    result = await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="已有要求",
        reply_tool_args=args,
    )
    extra_prompt = result["modified_kwargs"]["extra_prompt"]
    assert extra_prompt.startswith("已有要求")
    assert "本轮内部可选表达资源" in extra_prompt
    assert "已经通过语义许可的 USE 决策" in extra_prompt


@pytest.mark.asyncio
async def test_understand_only_card_reaches_replyer_without_literal() -> None:
    plugin, _ = _make_plugin()
    await _run_planner(plugin)
    args = _selected_args(plugin, action="USE")
    selected_card = str(args[MEME_CARD_ID_ARG])
    literal = plugin._require_release().cards_by_id[selected_card][
        "canonical_expression"
    ]
    plugin.set_plugin_config(
        {
            "plugin": {
                "enabled": True,
                "config_version": PLUGIN_VERSION,
            },
            "serving": {
                "minimum_similarity": -1.0,
                "understand_only_card_ids": [selected_card],
            }
        }
    )
    result = await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args=args,
    )
    extra_prompt = result["modified_kwargs"]["extra_prompt"]
    assert "本轮内部语义理解与禁止复述约束" in extra_prompt
    assert "不得复读" in extra_prompt
    assert literal not in extra_prompt


@pytest.mark.asyncio
async def test_understand_only_quality_gate_requests_semantic_retry() -> None:
    plugin, context = _make_plugin()
    await _run_planner(plugin)
    args = _selected_args(plugin, action="USE")
    selected_card = str(args[MEME_CARD_ID_ARG])
    plugin.set_plugin_config(
        {
            "plugin": {
                "enabled": True,
                "config_version": PLUGIN_VERSION,
            },
            "serving": {
                "minimum_similarity": -1.0,
                "understand_only_card_ids": [selected_card],
            },
        }
    )
    await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args=args,
    )
    context.llm.quality_gate_json = (
        '{"violation":true,"reason":"回复主动复读并评价了该梗"}'
    )
    result = await plugin.gate_understand_only_response(
        session_id=TARGET_SESSION,
        response="把用户引用的梗又说了一遍",
        retry_count=0,
    )
    assert result["modified_kwargs"]["retry"] is True
    assert "只回应" in result["modified_kwargs"]["retry_reason"]
    prompt = context.llm.generate_calls[-1]["prompt"]
    assert "不使用关键词、正则或字面包含规则" in prompt


@pytest.mark.asyncio
async def test_understand_only_quality_gate_passes_clean_reply() -> None:
    plugin, context = _make_plugin()
    await _run_planner(plugin)
    args = _selected_args(plugin, action="USE")
    selected_card = str(args[MEME_CARD_ID_ARG])
    plugin.set_plugin_config(
        {
            "plugin": {
                "enabled": True,
                "config_version": PLUGIN_VERSION,
            },
            "serving": {
                "minimum_similarity": -1.0,
                "understand_only_card_ids": [selected_card],
            },
        }
    )
    await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args=args,
    )
    result = await plugin.gate_understand_only_response(
        session_id=TARGET_SESSION,
        response="小猫这么小，确实看得人心里一紧。",
        retry_count=0,
    )
    assert result == {"action": "continue"}


@pytest.mark.asyncio
async def test_replyer_rejects_card_outside_current_candidates() -> None:
    plugin, _ = _make_plugin()
    await _run_planner(plugin)
    release = plugin._require_release()
    state = plugin._candidate_states[TARGET_SESSION]
    outside_card_id = next(
        card_id
        for card_id in release.cards_by_id
        if card_id not in state.candidate_card_ids
    )
    with pytest.raises(Exception, match="候选集合之外"):
        await plugin.handle_replyer_before_request(
            session_id=TARGET_SESSION,
            extra_prompt="",
            reply_tool_args={
                MEME_ACTION_ARG: "USE",
                MEME_RELEASE_ID_ARG: release.release_id,
                MEME_CARD_ID_ARG: outside_card_id,
                MEME_ROUTE_INDEX_ARG: 0,
            },
        )


@pytest.mark.asyncio
async def test_after_response_observer_semantically_scores_visible_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin, context = _make_plugin()
    await _run_planner(plugin)
    args = _selected_args(plugin, action="USE")
    with caplog.at_level(logging.INFO):
        result = await plugin.observe_replyer_after_response(
            session_id=TARGET_SESSION,
            reply_message_id="reply-1",
            reply_tool_args=args,
            response="稳住，漂亮拿下。",
        )
    assert result == {"action": "continue"}
    assert len(context.llm.generate_calls) == 1
    prompt = context.llm.generate_calls[0]["prompt"]
    assert "只做语义判断，不使用关键词或正则命中" in prompt
    assert "最终可见回复" in prompt
    structured = next(
        record.message
        for record in caplog.records
        if record.message.startswith("meme_semantic_effect ")
    )
    event = json.loads(structured.removeprefix("meme_semantic_effect "))
    assert event["used"] is True
    assert event["natural"] is True
    assert event["forced"] is False


@pytest.mark.asyncio
async def test_observer_scores_selector_promoted_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin, context = _make_plugin(semantic_selector_enabled=True)
    await _run_planner(plugin)
    candidate = plugin._session_candidates(session_id=TARGET_SESSION)[0]
    context.llm.selector_json = json.dumps(
        {
            "action": "USE",
            "card_id": candidate.card_id,
            "route_index": candidate.route_index,
            "confidence": 0.95,
            "matched_hard_blocks": [],
            "reason": "适合",
        },
        ensure_ascii=False,
    )
    await plugin.handle_replyer_before_request(
        session_id=TARGET_SESSION,
        extra_prompt="",
        reply_tool_args={MEME_ACTION_ARG: "SKIP"},
    )
    with caplog.at_level(logging.INFO):
        result = await plugin.observe_replyer_after_response(
            session_id=TARGET_SESSION,
            reply_message_id="reply-selector-1",
            reply_tool_args={MEME_ACTION_ARG: "SKIP"},
            response="稳住，这波拿下。",
        )
    assert result == {"action": "continue"}
    assert len(context.llm.generate_calls) == 2
    structured = next(
        record.message
        for record in caplog.records
        if record.message.startswith("meme_semantic_effect ")
    )
    event = json.loads(structured.removeprefix("meme_semantic_effect "))
    assert event["decision_source"] == "semantic_selector"
