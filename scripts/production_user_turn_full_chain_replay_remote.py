#!/usr/bin/env python3
"""用生产历史中的真实用户末轮消息重建 Planner→Replyer 梗库回放。

只读取生产数据库与 LLM 快照；模型调用不经过业务执行器，不写数据库、
不调用发送工具，也不生成 TTS。由于滚动日志没有保留足够的同轮 reactive
Planner 快照，Planner 请求使用同会话最新生产模板重建，Replyer 请求仍使用
该历史轮次保存的原始快照。
"""

from __future__ import annotations

from argparse import ArgumentParser
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

import asyncio
import json
import re
import sqlite3

import production_history_meme_replay_remote as base


MEME_TOOL_ARGS = {
    "meme_action",
    "meme_release_id",
    "meme_card_id",
    "meme_route_index",
}


def _select_user_turn_cases(
    *,
    database_path: Path,
    history_path: Path,
    limit: int,
) -> list[dict[str, Any]]:
    """选择 Replyer 前 60 秒内最后一条真实消息来自用户的唯一上下文。"""

    connection = sqlite3.connect(
        f"file:{database_path.resolve()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    sessions = {
        str(row["session_id"]): str(row["account_id"] or "")
        for row in connection.execute(
            """
            SELECT session_id, account_id
            FROM chat_sessions
            WHERE platform = ? AND group_id IS NULL
            """,
            ("galpet_app",),
        )
    }
    eligible: list[dict[str, Any]] = []
    seen_contexts: set[str] = set()
    for path in sorted(history_path.glob("*.json"), reverse=True):
        try:
            snapshot = base._load_json(path)
        except Exception:
            continue
        if (
            snapshot.get("request_type") != "maisaka.replyer"
            or snapshot.get("status") != "success"
        ):
            continue
        internal = snapshot.get("internal_request") or {}
        model_name = str((internal.get("model_info") or {}).get("name") or "")
        if model_name != "deepseek-v4-pro-nonthink":
            continue
        session_id = str(snapshot.get("session_id") or "")
        account_id = sessions.get(session_id)
        if account_id is None:
            continue
        try:
            created = base._timestamp(str(snapshot.get("created_at") or ""))
        except Exception:
            continue
        context, semantic_query, known = base._context_rows(
            connection,
            session_id=session_id,
            account_id=account_id,
            cutoff=created,
        )
        if not semantic_query or not context or context[-1]["role"] != "user":
            continue
        latest_context_at = datetime.fromisoformat(context[-1]["timestamp"])
        context_age_seconds = (created - latest_context_at).total_seconds()
        if not 0 <= context_age_seconds <= 60:
            continue
        context_hash = sha256(semantic_query.encode("utf-8")).hexdigest()
        if context_hash in seen_contexts:
            continue
        seen_contexts.add(context_hash)
        eligible.append(
            {
                "snapshot_path": path,
                "snapshot": snapshot,
                "session_id": session_id,
                "created": created,
                "created_at": str(snapshot.get("created_at") or ""),
                "context": context,
                "semantic_query": semantic_query,
                "query_fingerprint": context_hash[:12],
                "context_age_seconds": round(context_age_seconds, 3),
                "known_identifiers": known,
            }
        )
    connection.close()

    grouped: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for item in sorted(eligible, key=lambda row: row["created"], reverse=True):
        grouped[item["session_id"]].append(item)
    ordered_groups = sorted(grouped.values(), key=len, reverse=True)
    selected: list[dict[str, Any]] = []
    while ordered_groups and len(selected) < limit:
        remaining = []
        for group in ordered_groups:
            if group and len(selected) < limit:
                selected.append(group.popleft())
            if group:
                remaining.append(group)
        ordered_groups = remaining
    selected.sort(key=lambda row: row["created"])
    if not selected:
        raise RuntimeError("没有找到满足条件的真实用户末轮 Replyer 快照")
    return selected


def _planner_templates(history_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """读取每个会话最近的 Planner 模型/系统提示/工具模板。"""

    candidates: list[dict[str, Any]] = []
    for path in history_path.glob("*.json"):
        try:
            snapshot = base._load_json(path)
        except Exception:
            continue
        if (
            snapshot.get("request_type") != "maisaka.planner"
            or snapshot.get("status") != "success"
        ):
            continue
        internal = snapshot.get("internal_request") or {}
        tools = internal.get("tool_options") or []
        if not any(
            str((tool.get("function") or {}).get("name") or tool.get("name") or "")
            == "reply"
            for tool in tools
            if isinstance(tool, Mapping)
        ):
            continue
        candidates.append(snapshot)
    if not candidates:
        raise RuntimeError("没有可用于重建的生产 Planner 请求模板")
    candidates.sort(key=lambda item: str(item.get("created_at") or ""))
    by_session: dict[str, dict[str, Any]] = {}
    for snapshot in candidates:
        by_session[str(snapshot.get("session_id") or "")] = snapshot
    return by_session, candidates[-1]


def _parts_text(message: Mapping[str, Any]) -> str:
    return "\n".join(
        str(part.get("text") or "")
        for part in message.get("parts") or []
        if isinstance(part, Mapping) and part.get("type") == "text"
    )


def _strip_existing_meme_tool_fields(tools: Any) -> list[dict[str, Any]]:
    rendered = deepcopy(tools) if isinstance(tools, list) else []
    for tool in rendered:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        owner = function if isinstance(function, dict) else tool
        name = str(owner.get("name") or "")
        if name != "reply":
            continue
        parameters = owner.get("parameters")
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            for key in MEME_TOOL_ARGS:
                properties.pop(key, None)
        required = parameters.get("required")
        if isinstance(required, list):
            parameters["required"] = [key for key in required if key not in MEME_TOOL_ARGS]
    return rendered


def _planner_context_message(item: Mapping[str, Any]) -> dict[str, Any]:
    is_self = item.get("role") == "assistant"
    user_name = "大肥鱼" if is_self else "用户"
    text = (
        f'<message msg_id="{item.get("message_id", "")}" '
        f'time="{item.get("timestamp", "")}" user="{user_name}" '
        f'is_self_message="{str(is_self).lower()}">\n'
        f'{item.get("text", "")}\n</message>'
    )
    return base._message(str(item.get("role") or "user"), text)


def _build_planner_request(
    *,
    template: Mapping[str, Any],
    case: Mapping[str, Any],
    planner_resource: str,
    release: Any,
    candidates: list[Any],
) -> dict[str, Any]:
    request = deepcopy(template["internal_request"])
    source_messages = request.get("message_list") or []
    system_message = next(
        (deepcopy(message) for message in source_messages if message.get("role") == "system"),
        None,
    )
    if system_message is None:
        raise RuntimeError("Planner 模板缺少 system 消息")
    messages = [system_message]
    messages.extend(_planner_context_message(item) for item in case["context"])
    messages.append(
        base._message(
            "user",
            f"当前时间：{case['created'].strftime('%Y-%m-%d %H:%M:%S')}",
        )
    )
    messages.append(base._message("user", planner_resource))
    tools = _strip_existing_meme_tool_fields(request.get("tool_options"))
    request["message_list"] = messages
    request["tool_options"] = release.augment_reply_tool_definitions(
        tools,
        release_id=release.release_id,
        candidate_card_ids=[candidate.card_id for candidate in candidates],
    )
    request["request_kind"] = "planner"
    return request


def _reply_tool_call(response: Mapping[str, Any]) -> dict[str, Any] | None:
    for call in response.get("tool_calls") or []:
        function = call.get("function") or {}
        if function.get("name") == "reply":
            return call
    return None


def _replace_reply_reason(request: dict[str, Any], reasoning: str) -> None:
    """让历史 Replyer 快照使用本次 Planner 的新推理，而非旧推理。"""

    pattern = re.compile(
        r"(【最新推理】\s*\n).*?(?=\n\n(?:【[^\n]+】|请自然地回复|先按【额外回复要求】))",
        re.S,
    )
    for message in reversed(request.get("message_list") or []):
        for part in reversed(message.get("parts") or []):
            if part.get("type") != "text":
                continue
            text = str(part.get("text") or "")
            if "【最新推理】" not in text:
                continue
            updated, count = pattern.subn(
                lambda match: match.group(1) + reasoning.strip(),
                text,
                count=1,
            )
            if count:
                part["text"] = updated
                return


def _selection_record(
    *,
    action: str,
    card: Mapping[str, Any] | None,
    route_index: int,
    source: str,
    is_new: bool,
) -> dict[str, Any]:
    return {
        "action": action,
        "card_id": str(card.get("card_id") or "") if card else "",
        "expression": str(card.get("canonical_expression") or "") if card else "",
        "route_index": route_index,
        "source": source,
        "is_new_card": is_new,
    }


async def _run(args: Any) -> dict[str, Any]:
    from scripts import replay_llm_request as replay
    from src.config.config import config_manager
    from src.services.embedding_service import EmbeddingServiceClient

    config_manager.initialize()
    plugin = base._load_plugin_module(args.plugin_root)
    release_dir = args.plugin_root / "resources" / "releases" / args.release_id
    release = plugin.MemeRelease.load(release_dir)
    # 给重建脚本暴露与线上 Hook 相同的工具 schema 扩展函数。
    release.augment_reply_tool_definitions = plugin.augment_reply_tool_definitions
    base_release = plugin.MemeRelease.load(
        args.plugin_root
        / "resources"
        / "releases"
        / "reviewed-semantic-meme-library-20260729-multiprototype-v1"
    )
    new_card_ids = set(release.cards_by_id) - set(base_release.cards_by_id)
    understand_only_ids = set(plugin.DEFAULT_UNDERSTAND_ONLY_CARD_IDS)
    cases = _select_user_turn_cases(
        database_path=args.database,
        history_path=args.history,
        limit=args.limit,
    )
    planner_by_session, planner_fallback = _planner_templates(args.history)
    selector_template = base._latest_selector_template(args.history)
    embedding_client = EmbeddingServiceClient(
        task_name="embedding",
        request_type="meme.production_user_turn_full_chain_replay",
    )
    embeddings = await embedding_client.embed_texts(
        [case["semantic_query"] for case in cases],
        max_concurrent=args.concurrency,
    )
    semaphore = asyncio.Semaphore(args.concurrency)

    async def run_case(index: int, case: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            embedding = embeddings[index]
            candidates = release.retrieve(
                embedding.embedding,
                top_k=3,
                minimum_similarity=0.22,
            )
            planner_resource = release.build_planner_resource(
                candidates,
                understand_only_card_ids=understand_only_ids,
            )
            planner_template = planner_by_session.get(case["session_id"], planner_fallback)
            planner_internal = _build_planner_request(
                template=planner_template,
                case=case,
                planner_resource=planner_resource,
                release=release,
                candidates=candidates,
            )
            planner_response, planner_ms = await base._direct_response(
                replay,
                snapshot=planner_template,
                internal_request=planner_internal,
            )
            reply_call = _reply_tool_call(planner_response)
            planner_args = (
                deepcopy((reply_call.get("function") or {}).get("arguments") or {})
                if reply_call
                else {}
            )
            planner_selection_error = ""
            planner_selection = None
            if reply_call:
                try:
                    planner_selection = release.parse_tool_selection(planner_args)
                except Exception as exc:
                    planner_selection_error = f"{type(exc).__name__}: {exc}"

            selector_record = None
            final_selection = None
            decision_source = "none"
            if reply_call and not planner_selection_error:
                if planner_selection is not None and planner_selection.action != "SKIP":
                    final_selection = planner_selection
                    decision_source = "planner"
                else:
                    selector_prompt = release.build_semantic_selector_prompt(
                        candidates,
                        semantic_query_text=case["semantic_query"],
                        understand_only_card_ids=understand_only_ids,
                    )
                    selector_internal = base._selector_request(
                        selector_template,
                        prompt=selector_prompt,
                        max_tokens=260,
                    )
                    selector_response, selector_ms = await base._direct_response(
                        replay,
                        snapshot=selector_template,
                        internal_request=selector_internal,
                    )
                    selector_error = ""
                    try:
                        selector_decision = release.parse_semantic_selector_response(
                            str(selector_response.get("content") or "").strip(),
                            candidates=candidates,
                            understand_only_card_ids=understand_only_ids,
                            minimum_confidence=0.75,
                        )
                    except Exception as exc:
                        selector_error = f"{type(exc).__name__}: {exc}"
                        selector_decision = None
                    selector_record = {
                        "request": selector_internal,
                        "response": selector_response,
                        "duration_ms": selector_ms,
                        "parse_error": selector_error,
                        "requested_action": (
                            selector_decision.requested_action
                            if selector_decision is not None
                            else "ERROR"
                        ),
                        "effective_action": (
                            selector_decision.selection.action
                            if selector_decision is not None
                            else "SKIP"
                        ),
                        "confidence": (
                            selector_decision.confidence
                            if selector_decision is not None
                            else 0.0
                        ),
                        "reason": (
                            selector_decision.reason
                            if selector_decision is not None
                            else selector_error
                        ),
                    }
                    if (
                        selector_decision is not None
                        and selector_decision.selection.action != "SKIP"
                    ):
                        final_selection = selector_decision.selection
                        decision_source = "semantic_selector"
                    else:
                        decision_source = "semantic_selector_skip"

            # 非 reply 工具可能只是 Planner 的中间检索步骤；回放没有执行工具，
            # 不能把它误写成最终 no_action。
            effective_action = "PLANNER_CONTINUE" if not reply_call else "SKIP"
            selected_card = None
            selected_route = None
            if final_selection is not None:
                effective_action = final_selection.action
                if (
                    effective_action == "USE"
                    and final_selection.card_id in understand_only_ids
                ):
                    effective_action = "UNDERSTAND_ONLY"
                selected_card = release.get_card(final_selection.card_id)
                if selected_card is not None:
                    selected_route = selected_card["usage_routes"][
                        final_selection.route_index
                    ]

            original_snapshot = case["snapshot"]
            reply_attempts: list[dict[str, Any]] = []
            final_response: dict[str, Any] | None = None
            gate_record = None
            effect_record = None
            if reply_call:
                replyer_internal = deepcopy(original_snapshot["internal_request"])
                base._strip_old_meme_resources(replyer_internal)
                _replace_reply_reason(
                    replyer_internal,
                    str(planner_response.get("content") or ""),
                )
                if final_selection is not None and selected_card is not None:
                    resource = release.build_replyer_resource(
                        final_selection,
                        effective_action=effective_action,
                        decision_source=decision_source,
                    )
                    base._append_reply_requirement(replyer_internal, resource)
                replay_response, replyer_ms = await base._direct_response(
                    replay,
                    snapshot=original_snapshot,
                    internal_request=replyer_internal,
                )
                reply_attempts.append(
                    {
                        "attempt": 1,
                        "request": deepcopy(replyer_internal),
                        "response": deepcopy(replay_response),
                        "duration_ms": replyer_ms,
                    }
                )
                final_response = replay_response

                if effective_action == "UNDERSTAND_ONLY" and selected_card is not None:
                    gate_prompt = base._quality_gate_prompt(
                        card=selected_card,
                        semantic_query=case["semantic_query"],
                        response=base._visible_text(replay_response.get("content")),
                    )
                    gate_internal = base._selector_request(
                        selector_template,
                        prompt=gate_prompt,
                        max_tokens=180,
                    )
                    gate_response, gate_ms = await base._direct_response(
                        replay,
                        snapshot=selector_template,
                        internal_request=gate_internal,
                    )
                    gate_evaluation = base._safe_json_object(gate_response.get("content"))
                    gate_record = {
                        "request": gate_internal,
                        "response": gate_response,
                        "evaluation": gate_evaluation,
                        "duration_ms": gate_ms,
                    }
                    if gate_evaluation.get("violation") is True:
                        retry_internal = deepcopy(replyer_internal)
                        base._append_reply_requirement(
                            retry_internal,
                            "【重生成约束】\n上一版回复触碰了仅理解梗，完全绕开该表达，"
                            "只回应具体事件、人物、感受或风险。",
                        )
                        retry_response, retry_ms = await base._direct_response(
                            replay,
                            snapshot=original_snapshot,
                            internal_request=retry_internal,
                        )
                        reply_attempts.append(
                            {
                                "attempt": 2,
                                "request": retry_internal,
                                "response": retry_response,
                                "duration_ms": retry_ms,
                            }
                        )
                        final_response = retry_response

                if (
                    effective_action == "USE"
                    and selected_card is not None
                    and selected_route is not None
                    and final_response is not None
                ):
                    effect_prompt = base._effect_prompt(
                        card=selected_card,
                        route=selected_route,
                        semantic_query=case["semantic_query"],
                        response=base._visible_text(final_response.get("content")),
                    )
                    effect_internal = base._selector_request(
                        selector_template,
                        prompt=effect_prompt,
                        max_tokens=300,
                    )
                    effect_response, effect_ms = await base._direct_response(
                        replay,
                        snapshot=selector_template,
                        internal_request=effect_internal,
                    )
                    effect_record = {
                        "request": effect_internal,
                        "response": effect_response,
                        "evaluation": base._safe_json_object(
                            effect_response.get("content")
                        ),
                        "duration_ms": effect_ms,
                    }

            old_visible = base._visible_text(
                (original_snapshot.get("response") or {}).get("content")
            )
            new_visible = base._visible_text(
                final_response.get("content") if final_response else ""
            )
            selected_card_id = (
                final_selection.card_id if final_selection is not None else ""
            )
            is_new_card = selected_card_id in new_card_ids
            literal_match = bool(
                selected_card is not None
                and base._literal_meme_match(selected_card, new_visible)
            )
            effect_evaluation = effect_record.get("evaluation") if effect_record else {}
            semantic_used = bool(effect_evaluation.get("used"))
            selection = _selection_record(
                action=effective_action,
                card=selected_card,
                route_index=(
                    final_selection.route_index if final_selection is not None else 0
                ),
                source=decision_source,
                is_new=is_new_card,
            )
            return {
                "case_id": f"U{index + 1:03d}",
                "created_at": case["created_at"],
                "session": base._redaction_token("session", case["session_id"]),
                "query_fingerprint": case["query_fingerprint"],
                "context_age_seconds": case["context_age_seconds"],
                "context": case["context"],
                "semantic_query": case["semantic_query"],
                "embedding": {
                    "request": {"task_name": "embedding", "text": case["semantic_query"]},
                    "response": {
                        "success": True,
                        "model_name": embedding.model_name,
                        "dimension": len(embedding.embedding),
                        "vector_omitted": True,
                    },
                },
                "candidates": [
                    {
                        "card_id": candidate.card_id,
                        "expression": candidate.canonical_expression,
                        "similarity": round(candidate.similarity, 6),
                        "route_index": candidate.route_index,
                        "is_new_card": candidate.card_id in new_card_ids,
                    }
                    for candidate in candidates
                ],
                "planner": {
                    "template_session_matched": case["session_id"] in planner_by_session,
                    "request": planner_internal,
                    "response": planner_response,
                    "duration_ms": planner_ms,
                    "reply_selected": bool(reply_call),
                    "reply_tool_arguments": planner_args,
                    "selection_parse_error": planner_selection_error,
                    "requested_meme_action": (
                        planner_selection.action if planner_selection is not None else "OMITTED"
                    ),
                },
                "selector": selector_record,
                "selection": selection,
                "historical": {
                    "replyer_request": original_snapshot["internal_request"],
                    "replyer_response": base._stored_response_payload(
                        original_snapshot.get("response")
                    ),
                    "visible_text": old_visible,
                    "model_name": str(
                        (original_snapshot["internal_request"].get("model_info") or {}).get(
                            "name"
                        )
                        or ""
                    ),
                },
                "replay": {
                    "planner_reply_selected": bool(reply_call),
                    "replyer_attempts": reply_attempts,
                    "final_response": final_response,
                    "visible_text": new_visible,
                    "changed": bool(final_response) and old_visible != new_visible,
                },
                "understand_only_gate": gate_record,
                "effect": effect_record,
                "result": {
                    "literal_match": literal_match,
                    "semantic_used": semantic_used,
                    "natural": bool(effect_evaluation.get("natural")),
                    "forced": bool(effect_evaluation.get("forced")),
                    "serious_context_violation": bool(
                        effect_evaluation.get("serious_context_violation")
                    ),
                    "internal_leakage": bool(effect_evaluation.get("internal_leakage")),
                    "new_meme_used": bool(
                        is_new_card
                        and effective_action == "USE"
                        and (semantic_used or literal_match)
                    ),
                },
            }

    results: list[dict[str, Any] | None] = [None] * len(cases)

    async def tracked(index: int, case: dict[str, Any]) -> None:
        try:
            results[index] = await run_case(index, case)
            print(
                f"progress {index + 1}/{len(cases)} "
                f"planner_reply={results[index]['planner']['reply_selected']} "
                f"action={results[index]['selection']['action']}",
                flush=True,
            )
        except Exception as exc:
            results[index] = {
                "case_id": f"U{index + 1:03d}",
                "created_at": case["created_at"],
                "session": base._redaction_token("session", case["session_id"]),
                "query_fingerprint": case["query_fingerprint"],
                "context_age_seconds": case["context_age_seconds"],
                "context": case["context"],
                "semantic_query": case["semantic_query"],
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
            print(
                f"progress {index + 1}/{len(cases)} error={type(exc).__name__}",
                flush=True,
            )

    await asyncio.gather(*(tracked(index, case) for index, case in enumerate(cases)))
    completed = [result for result in results if result is not None]
    for index, (result, case) in enumerate(zip(completed, cases, strict=True)):
        completed[index] = base._redact(
            result,
            known_identifiers=case["known_identifiers"],
        )
    successful = [result for result in completed if result.get("status") != "error"]
    summary = {
        "requested_limit": args.limit,
        "case_count": len(completed),
        "success_count": len(successful),
        "error_count": len(completed) - len(successful),
        "planner_reply_count": sum(
            bool((result.get("planner") or {}).get("reply_selected"))
            for result in successful
        ),
        "planner_non_reply_count": sum(
            not bool((result.get("planner") or {}).get("reply_selected"))
            for result in successful
        ),
        "candidate_new_card_count": sum(
            any(candidate.get("is_new_card") for candidate in result.get("candidates") or [])
            for result in successful
        ),
        "selected_new_card_count": sum(
            bool((result.get("selection") or {}).get("is_new_card"))
            for result in successful
        ),
        "new_meme_used_count": sum(
            bool((result.get("result") or {}).get("new_meme_used"))
            for result in successful
        ),
        "action_counts": {
            action: sum(
                (result.get("selection") or {}).get("action") == action
                for result in successful
            )
            for action in (
                "USE",
                "UNDERSTAND_ONLY",
                "SKIP",
                "PLANNER_CONTINUE",
            )
        },
        "decision_source_counts": {
            source: sum(
                (result.get("selection") or {}).get("source") == source
                for result in successful
            )
            for source in (
                "planner",
                "semantic_selector",
                "semantic_selector_skip",
                "none",
            )
        },
    }
    return {
        "schema_version": "production_user_turn_full_chain_replay_v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "deployed_revision": args.deployed_revision.read_text(encoding="utf-8").strip(),
        "release": release.fingerprint(),
        "method": {
            "source": "生产 MaiBot.db + llm_request_history，只读",
            "sample": (
                "成功的 galpet_app 私聊文本 Replyer 快照；数据库最近六条真实消息中"
                "最后一条必须来自用户，且距 Replyer 请求不超过 60 秒；文本上下文去重后"
                "按会话轮转分层抽样。滚动日志当前仅有这些合格唯一轮次。"
            ),
            "replay": (
                "真实用户上下文 → 新版向量 Top-3 → 重建生产 Planner 请求并调用当前"
                " Planner 模型 → 解析 reply 梗参数；省略/SKIP 时走插件同款独立语义裁判"
                " → 使用历史原 Replyer 快照替换为新 Planner 推理并注入一次性梗资源"
                " → Replyer → 仅理解质量门/USE 效果评估。"
            ),
            "privacy": (
                "会话、用户、账号、消息与请求标识均稳定匿名化；模型内部推理已移除；"
                "没有写业务数据库、执行 Planner 工具、投递消息或生成 TTS。"
            ),
            "caveat": (
                "生产滚动日志未保留足够的同轮 reactive Planner 快照，因此 Planner 请求"
                "使用同会话最新生产 system/tool 模板与该轮真实六条消息重建；Replyer 请求"
                "和历史返回为原始快照。Planner 选择查询类工具时本报告标记为 "
                "PLANNER_CONTINUE 并停止该例，不把中间工具选择误算成最终拒绝回复；"
                "模型具有随机性，差异不能全部归因于梗库。"
            ),
        },
        "summary": summary,
        "cases": completed,
    }


def _parse_args() -> Any:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--deployed-revision", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
