#!/usr/bin/env python3
"""在生产机内存中用指定梗包回放历史 Replyer 请求，不写业务数据库或投递消息。"""

from __future__ import annotations

from argparse import ArgumentParser
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

import asyncio
import json
import re
import sqlite3
import sys


FACE_PATTERN = re.compile(r"⟦(?:face|vocal)=[^⟧]+⟧")
LONG_IDENTIFIER_PATTERN = re.compile(r"\b[A-Fa-f0-9]{24,64}\b")
MEME_MARKERS = (
    "【本轮内部可选表达资源】",
    "【本轮内部最终表达决策：覆盖此前对该候选的 SKIP 判断】",
    "【本轮内部语义理解与禁止复述约束】",
    "【本轮内部候选表达记忆】",
)


def _load_plugin_module(plugin_root: Path) -> Any:
    module_name = "_history_replay_meme_plugin"
    spec = spec_from_file_location(
        module_name,
        plugin_root / "plugin.py",
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载回放梗插件")
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON 顶层必须是对象: {path}")
    return value


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=None)


def _response_payload(response: Any) -> dict[str, Any]:
    usage = response.usage
    return {
        "content": response.content,
        "reasoning_content": "[已移除模型内部推理]" if response.reasoning_content else None,
        "tool_calls": [
            {
                "id": call.call_id,
                "function": {"name": call.func_name, "arguments": call.args},
            }
            for call in (response.tool_calls or [])
        ],
        "usage": (
            {
                "completion_tokens": usage.completion_tokens,
                "prompt_tokens": usage.prompt_tokens,
                "total_tokens": usage.total_tokens,
                "model_name": usage.model_name,
                "provider_name": usage.provider_name,
            }
            if usage
            else None
        ),
    }


def _stored_response_payload(value: Any) -> dict[str, Any]:
    payload = deepcopy(value) if isinstance(value, dict) else {"content": value}
    if payload.get("reasoning_content"):
        payload["reasoning_content"] = "[已移除模型内部推理]"
    return payload


def _message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "parts": [{"type": "text", "text": text}]}


def _append_reply_requirement(request: dict[str, Any], resource: str) -> None:
    messages = request.get("message_list")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Replyer 请求没有 message_list")
    addition = "【额外回复要求】\n" + resource
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in reversed(parts):
            if part.get("type") == "text":
                part["text"] = str(part.get("text") or "").rstrip() + "\n\n" + addition
                return
    messages.append(_message("user", addition))


def _strip_old_meme_resources(request: dict[str, Any]) -> None:
    messages = request.get("message_list")
    if not isinstance(messages, list):
        return
    for message in messages:
        for part in message.get("parts") or []:
            text = str(part.get("text") or "")
            earliest = min(
                (text.find(marker) for marker in MEME_MARKERS if marker in text),
                default=-1,
            )
            if earliest >= 0:
                part["text"] = text[:earliest].rstrip()


def _visible_text(value: Any) -> str:
    return FACE_PATTERN.sub("", str(value or "")).strip()


def _normalize_match_text(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _literal_meme_match(card: Mapping[str, Any], response: str) -> bool:
    normalized = _normalize_match_text(response)
    if not normalized:
        return False
    expressions = [card.get("canonical_expression"), *(card.get("aliases") or [])]
    return any(
        len(term) >= 2 and term in normalized
        for value in expressions
        if (term := _normalize_match_text(value))
    )


def _redaction_token(kind: str, value: str) -> str:
    return f"<{kind}-{sha256(value.encode('utf-8')).hexdigest()[:10]}>"


def _redact(
    value: Any,
    *,
    known_identifiers: Mapping[str, str],
    key: str = "",
) -> Any:
    lowered = key.lower()
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(
                item_value,
                known_identifiers=known_identifiers,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [
            _redact(item, known_identifiers=known_identifiers, key=key)
            for item in value
        ]
    if not isinstance(value, str):
        return value
    if value in known_identifiers:
        return known_identifiers[value]
    sensitive_key = any(
        token in lowered
        for token in (
            "session_id",
            "user_id",
            "account_id",
            "message_id",
            "msg_id",
            "person_id",
            "relationship_id",
            "trace_id",
            "request_id",
        )
    )
    if sensitive_key and value:
        return _redaction_token("id", value)
    rendered = value
    for raw, replacement in sorted(
        known_identifiers.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if raw:
            rendered = rendered.replace(raw, replacement)
    return LONG_IDENTIFIER_PATTERN.sub(
        lambda match: _redaction_token("opaque", match.group(0)),
        rendered,
    )


def _context_rows(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    account_id: str,
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")
    rows = connection.execute(
        """
        SELECT message_id, user_id, processed_plain_text, timestamp
        FROM mai_messages
        WHERE session_id = ? AND timestamp <= ?
          AND COALESCE(is_notify, 0) = 0
          AND COALESCE(processed_plain_text, '') != ''
        ORDER BY timestamp DESC
        LIMIT 6
        """,
        (session_id, cutoff_text),
    ).fetchall()[::-1]
    context: list[dict[str, Any]] = []
    known = {
        session_id: _redaction_token("session", session_id),
        account_id: _redaction_token("account", account_id),
    }
    lines: list[str] = []
    for row in rows:
        message_id = str(row["message_id"] or "")
        user_id = str(row["user_id"] or "")
        known[message_id] = _redaction_token("message", message_id)
        known[user_id] = (
            _redaction_token("account", user_id)
            if user_id == account_id
            else _redaction_token("user", user_id)
        )
        role = "assistant" if user_id == account_id else "user"
        text = str(row["processed_plain_text"] or "").strip()
        context.append(
            {
                "message_id": known[message_id],
                "role": role,
                "text": text,
                "timestamp": str(row["timestamp"] or ""),
            }
        )
        lines.append(f"{role}: {text}")
    return context, "\n".join(lines), known


def _select_cases(
    *,
    database_path: Path,
    history_path: Path,
    limit: int,
) -> list[dict[str, Any]]:
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
            WHERE platform = 'galpet_app' AND group_id IS NULL
            """
        )
    }
    eligible: list[dict[str, Any]] = []
    seen_contexts: set[str] = set()
    for path in sorted(history_path.glob("*.json"), reverse=True):
        try:
            snapshot = _load_json(path)
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
        created_at = str(snapshot.get("created_at") or "")
        try:
            created = _timestamp(created_at)
        except Exception:
            continue
        context, semantic_query, known = _context_rows(
            connection,
            session_id=session_id,
            account_id=account_id,
            cutoff=created,
        )
        if not semantic_query:
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
                "created_at": created_at,
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
    if len(selected) != limit:
        raise RuntimeError(f"可回放唯一文本上下文不足: {len(selected)} < {limit}")
    selected.sort(key=lambda row: row["created"])
    return selected


def _latest_selector_template(history_path: Path) -> dict[str, Any]:
    for path in sorted(history_path.glob("*.json"), reverse=True):
        try:
            snapshot = _load_json(path)
        except Exception:
            continue
        if (
            snapshot.get("request_type") == "plugin.chiba.meme-semantic-plugin"
            and snapshot.get("status") == "success"
            and (snapshot.get("internal_request") or {}).get("request_kind")
            == "response"
        ):
            return snapshot
    raise RuntimeError("没有可用的生产 utils 模型请求模板")


def _selector_request(
    template: Mapping[str, Any],
    *,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    request = deepcopy(template["internal_request"])
    request["message_list"] = [_message("user", prompt)]
    request["temperature"] = 0.0
    request["max_tokens"] = max_tokens
    request["tool_options"] = []
    return request


async def _direct_response(
    replay: Any,
    *,
    snapshot: Mapping[str, Any],
    internal_request: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    provider = replay._resolve_api_provider(str(snapshot["api_provider"]["name"]))
    client = replay.client_registry.get_client_class_instance(provider, force_new=True)
    started = perf_counter()
    response = await client.get_response(
        replay._build_response_request(dict(internal_request))
    )
    return _response_payload(response), round((perf_counter() - started) * 1000, 2)


def _quality_gate_prompt(
    *,
    card: Mapping[str, Any],
    semantic_query: str,
    response: str,
) -> str:
    return (
        "你是梗语义输出质量闸门。只做整体语义判断，不使用关键词、"
        "正则或字面包含规则。本轮动作是 UNDERSTAND_ONLY：模型可以理解"
        "用户引用的梗，但最终回复不得复读、改写、解释、评价该梗是否贴切，"
        "也不得主动点出它；只能回应梗所指向的具体事件、人物、感受或风险。"
        "判断最终回复是否违反这一约束。严格只返回 JSON："
        '{"violation":true,"reason":"不超过60字"}。\n\n'
        "语义资源："
        + json.dumps(
            {
                "canonical_expression": card["canonical_expression"],
                "aliases": card.get("aliases") or [],
                "semantic_core": card["semantic_core"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + f"\n\n真实对话：\n{semantic_query}"
        + f"\n\n最终候选回复：\n{response}"
    )


def _effect_prompt(
    *,
    card: Mapping[str, Any],
    route: Mapping[str, Any],
    semantic_query: str,
    response: str,
) -> str:
    return (
        "你是线上回复的梗语义效果评估器。只做语义判断，不使用关键词或正则命中。"
        "判断最终回复是否真正使用或自然呼应了候选梗，以及是否生硬、抢话、"
        "误伤严肃语境或暴露内部信息。严格只返回 JSON："
        '{"used":true,"natural":true,"forced":false,'
        '"serious_context_violation":false,"internal_leakage":false,'
        '"reason":"不超过100字"}\n\n'
        f"真实对话上下文：\n{semantic_query}\n\n"
        "候选梗：\n"
        + json.dumps(
            {
                "canonical_expression": card["canonical_expression"],
                "semantic_core": card["semantic_core"],
                "route": route,
                "hard_blocks": card["hard_blocks"],
            },
            ensure_ascii=False,
        )
        + f"\n\n最终可见回复：\n{response}"
    )


def _safe_json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or ""))
    except Exception as exc:
        return {"parse_error": type(exc).__name__, "raw": str(raw or "")}
    return value if isinstance(value, dict) else {"raw": value}


async def _run(args: Any) -> dict[str, Any]:
    from scripts import replay_llm_request as replay
    from src.config.config import config_manager
    from src.services.embedding_service import EmbeddingServiceClient

    config_manager.initialize()
    plugin = _load_plugin_module(args.plugin_root)
    release_dir = args.plugin_root / "resources" / "releases" / args.release_id
    release = plugin.MemeRelease.load(release_dir)
    base_release = plugin.MemeRelease.load(
        args.plugin_root
        / "resources"
        / "releases"
        / "reviewed-semantic-meme-library-20260729-multiprototype-v1"
    )
    new_card_ids = set(release.cards_by_id) - set(base_release.cards_by_id)
    understand_only_ids = set(plugin.DEFAULT_UNDERSTAND_ONLY_CARD_IDS)
    cases = _select_cases(
        database_path=args.database,
        history_path=args.history,
        limit=args.limit,
    )
    selector_template = _latest_selector_template(args.history)
    embedding_client = EmbeddingServiceClient(
        task_name="embedding",
        request_type="meme.production_history_replay",
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
            selector_prompt = release.build_semantic_selector_prompt(
                candidates,
                semantic_query_text=case["semantic_query"],
                understand_only_card_ids=understand_only_ids,
            )
            selector_internal = _selector_request(
                selector_template,
                prompt=selector_prompt,
                max_tokens=260,
            )
            selector_response, selector_ms = await _direct_response(
                replay,
                snapshot=selector_template,
                internal_request=selector_internal,
            )
            selector_error = ""
            try:
                decision = release.parse_semantic_selector_response(
                    str(selector_response.get("content") or "").strip(),
                    candidates=candidates,
                    understand_only_card_ids=understand_only_ids,
                    minimum_confidence=0.75,
                )
            except Exception as exc:
                selector_error = f"{type(exc).__name__}: {exc}"
                decision = None

            original_snapshot = case["snapshot"]
            replay_internal = deepcopy(original_snapshot["internal_request"])
            _strip_old_meme_resources(replay_internal)
            selected_card = None
            selected_route = None
            effective_action = "SKIP"
            if decision is not None:
                effective_action = decision.selection.action
                if effective_action != "SKIP":
                    selected_card = release.get_card(decision.selection.card_id)
                    if selected_card is not None:
                        selected_route = selected_card["usage_routes"][
                            decision.selection.route_index
                        ]
                        resource = release.build_replyer_resource(
                            decision.selection,
                            effective_action=effective_action,
                            decision_source="semantic_selector",
                        )
                        _append_reply_requirement(replay_internal, resource)

            replay_response, replyer_ms = await _direct_response(
                replay,
                snapshot=original_snapshot,
                internal_request=replay_internal,
            )
            reply_attempts = [
                {
                    "attempt": 1,
                    "request": deepcopy(replay_internal),
                    "response": deepcopy(replay_response),
                    "duration_ms": replyer_ms,
                }
            ]
            final_response = replay_response
            gate_record = None
            if effective_action == "UNDERSTAND_ONLY" and selected_card is not None:
                gate_prompt = _quality_gate_prompt(
                    card=selected_card,
                    semantic_query=case["semantic_query"],
                    response=_visible_text(replay_response.get("content")),
                )
                gate_internal = _selector_request(
                    selector_template,
                    prompt=gate_prompt,
                    max_tokens=180,
                )
                gate_response, gate_ms = await _direct_response(
                    replay,
                    snapshot=selector_template,
                    internal_request=gate_internal,
                )
                gate_evaluation = _safe_json_object(gate_response.get("content"))
                gate_record = {
                    "request": gate_internal,
                    "response": gate_response,
                    "evaluation": gate_evaluation,
                    "duration_ms": gate_ms,
                }
                if gate_evaluation.get("violation") is True:
                    retry_internal = deepcopy(replay_internal)
                    _append_reply_requirement(
                        retry_internal,
                        "【重生成约束】\n上一版回复触碰了仅理解梗，完全绕开该表达，"
                        "只回应具体事件、人物、感受或风险。",
                    )
                    retry_response, retry_ms = await _direct_response(
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

            effect_record = None
            if (
                effective_action == "USE"
                and selected_card is not None
                and selected_route is not None
            ):
                effect_prompt = _effect_prompt(
                    card=selected_card,
                    route=selected_route,
                    semantic_query=case["semantic_query"],
                    response=_visible_text(final_response.get("content")),
                )
                effect_internal = _selector_request(
                    selector_template,
                    prompt=effect_prompt,
                    max_tokens=300,
                )
                effect_response, effect_ms = await _direct_response(
                    replay,
                    snapshot=selector_template,
                    internal_request=effect_internal,
                )
                effect_record = {
                    "request": effect_internal,
                    "response": effect_response,
                    "evaluation": _safe_json_object(effect_response.get("content")),
                    "duration_ms": effect_ms,
                }

            old_visible = _visible_text(
                (original_snapshot.get("response") or {}).get("content")
            )
            new_visible = _visible_text(final_response.get("content"))
            selected_card_id = (
                decision.selection.card_id if decision is not None else ""
            )
            is_new_card = selected_card_id in new_card_ids
            literal_match = bool(
                selected_card is not None
                and _literal_meme_match(selected_card, new_visible)
            )
            effect_evaluation = (
                effect_record.get("evaluation") if effect_record else {}
            )
            semantic_used = bool(effect_evaluation.get("used"))
            return {
                "case_id": f"H{index + 1:03d}",
                "created_at": case["created_at"],
                "session": _redaction_token("session", case["session_id"]),
                "query_fingerprint": case["query_fingerprint"],
                "context_age_seconds": case["context_age_seconds"],
                "context": case["context"],
                "semantic_query": case["semantic_query"],
                "embedding": {
                    "request": {
                        "task_name": "embedding",
                        "text": case["semantic_query"],
                    },
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
                "selector": {
                    "request": selector_internal,
                    "response": selector_response,
                    "duration_ms": selector_ms,
                    "parse_error": selector_error,
                    "requested_action": (
                        decision.requested_action if decision is not None else "ERROR"
                    ),
                    "effective_action": effective_action,
                    "confidence": decision.confidence if decision is not None else 0.0,
                    "reason": decision.reason if decision is not None else selector_error,
                },
                "selection": {
                    "action": effective_action,
                    "card_id": selected_card_id,
                    "expression": (
                        selected_card.get("canonical_expression")
                        if selected_card is not None
                        else ""
                    ),
                    "route_index": (
                        decision.selection.route_index if decision is not None else 0
                    ),
                    "is_new_card": is_new_card,
                },
                "historical": {
                    "request": original_snapshot["internal_request"],
                    "response": _stored_response_payload(
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
                    "attempts": reply_attempts,
                    "final_response": final_response,
                    "visible_text": new_visible,
                    "changed": old_visible != new_visible,
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
                    "internal_leakage": bool(
                        effect_evaluation.get("internal_leakage")
                    ),
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
                f"action={results[index]['selection']['action']}",
                flush=True,
            )
        except Exception as exc:
            results[index] = {
                "case_id": f"H{index + 1:03d}",
                "created_at": case["created_at"],
                "session": _redaction_token("session", case["session_id"]),
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
        completed[index] = _redact(
            result,
            known_identifiers=case["known_identifiers"],
        )

    successful = [result for result in completed if result.get("status") != "error"]
    summary = {
        "case_count": len(completed),
        "success_count": len(successful),
        "error_count": len(completed) - len(successful),
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
            for action in ("USE", "UNDERSTAND_ONLY", "SKIP")
        },
    }
    return {
        "schema_version": "production_history_meme_replay_v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "deployed_revision": args.deployed_revision.read_text(encoding="utf-8").strip(),
        "release": release.fingerprint(),
        "method": {
            "source": "生产 MaiBot.db + llm_request_history，只读",
            "sample": (
                "成功的 galpet_app 私聊文本 Replyer 请求；最近六条消息上下文去重；"
                "数据库最后消息距请求不超过 60 秒；按会话轮转分层抽样"
            ),
            "replay": (
                "新版向量 Top-3 → 插件同款语义裁判 → 原 Replyer 请求追加一次性资源 → "
                "仅理解质量门/USE 效果评估"
            ),
            "privacy": (
                "结果中的会话、用户、账号、消息与请求标识均稳定匿名化；"
                "模型内部推理已移除；未写业务数据库、未投递消息、未生成 TTS"
            ),
            "caveat": "模型具有随机性；历史回复与回放回复差异不应全部归因于梗库。",
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
