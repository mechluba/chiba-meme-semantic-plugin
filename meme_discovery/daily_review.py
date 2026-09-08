"""汇总指定本地日期的语义候选，生成可直接导出入库审核 JSON 的页面。"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .reviewed_card_review import render_review_html, validate_storage_cards


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
WEBHOOK_ENV = "FEISHU_MEME_REVIEW_WEBHOOK"


class DailyReviewError(RuntimeError):
    """每日审核汇总无法安全完成。"""


def build_daily_review(
    *,
    target_date: date,
    output_root: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """汇总自动发现和人工输入结果，写入最终语义卡审核页。"""
    resolved_root = output_root.expanduser().resolve()
    resolved_output = (
        output_dir.expanduser().resolve()
        if output_dir
        else resolved_root / "daily-reviews" / target_date.strftime("%Y%m%d")
    )
    source_documents = _load_source_documents(resolved_root, target_date)
    items: list[dict[str, Any]] = []
    skipped_candidates: list[dict[str, Any]] = []
    source_candidate_count = 0
    skipped_counts: dict[str, int] = {}
    for source in source_documents:
        for candidate in source["document"].get("candidates") or []:
            source_candidate_count += 1
            skip_reason = _skip_reason(candidate)
            if skip_reason:
                skipped_counts[skip_reason] = skipped_counts.get(skip_reason, 0) + 1
                skipped_candidates.append(
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "phrase": candidate.get("phrase"),
                        "reason": skip_reason,
                        "semantic_status": (candidate.get("semantic_enrichment") or {}).get("status"),
                        "classification": (candidate.get("draft_card") or {}).get("classification"),
                        "source_kind": source["source_kind"],
                        "run_id": source["run_id"],
                    }
                )
                continue
            items.append(_review_item(candidate, source))

    document = {
        "schema_version": 1,
        "report_kind": "daily_semantic_meme_card_review",
        "review_title": f"千叶热梗每日审核 · {target_date.isoformat()}",
        "prompt_version": _prompt_version(source_documents),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "date_range": {
            "start": target_date.isoformat(),
            "end": target_date.isoformat(),
            "timezone": str(LOCAL_TZ),
            "basis": "source document generated_at",
        },
        "summary": {
            "source_document_count": len(source_documents),
            "source_candidate_count": source_candidate_count,
            "generated_card_count": len(items),
            "failed_card_count": source_candidate_count - len(items),
            "skipped_counts": skipped_counts,
        },
        "items": items,
        "skipped_candidates": skipped_candidates,
        "source_documents": [
            {
                "source_kind": source["source_kind"],
                "run_id": source["run_id"],
                "generated_at": source["document"].get("generated_at"),
                "relative_path": source["relative_path"],
                "candidate_count": len(source["document"].get("candidates") or []),
            }
            for source in source_documents
        ],
    }
    errors = validate_storage_cards(document)
    if errors:
        raise DailyReviewError("每日审核页中存在非法梗卡：\n" + "\n".join(errors))

    resolved_output.mkdir(parents=True, exist_ok=True)
    summary_path = resolved_output / "daily-review-summary.json"
    review_path = resolved_output / "daily-review.html"
    _write_json(summary_path, document)
    _write_text(review_path, render_review_html(document))
    result = {
        "target_date": target_date.isoformat(),
        "output_dir": str(resolved_output),
        "summary_file": str(summary_path),
        "review_page": str(review_path),
        "source_document_count": len(source_documents),
        "source_candidate_count": source_candidate_count,
        "generated_card_count": len(items),
        "skipped_counts": skipped_counts,
        "notification": {"status": "not_requested"},
    }
    _write_json(resolved_root / "latest-daily-review.json", result)
    return result


def notify_feishu(
    result: dict[str, Any],
    *,
    webhook: str | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """通过飞书自定义机器人发送摘要；不上传本地 HTML。"""
    target = _validated_webhook(webhook or os.environ.get(WEBHOOK_ENV, ""))
    text = (
        f"千叶热梗昨日审核已生成\n"
        f"日期：{result['target_date']}\n"
        f"来源批次：{result['source_document_count']}\n"
        f"待审核梗卡：{result['generated_card_count']} / 原始候选 {result['source_candidate_count']}\n"
        f"未生成卡片：{result['source_candidate_count'] - result['generated_card_count']}\n"
        f"审核页面（本地归档）：{result['review_page']}\n"
        "审核完成后请在页面导出审核记录 JSON，再用插件梗库脚本构建新 Release。"
    )
    request = urllib.request.Request(
        target,
        data=json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DailyReviewError(f"飞书通知发送失败：HTTP {exc.code}") from exc
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise DailyReviewError(f"飞书通知发送失败：{type(exc).__name__}") from exc
    raw_code = payload.get("code", payload.get("StatusCode", -1)) if isinstance(payload, dict) else -1
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        message = payload.get("msg") or payload.get("StatusMessage") or "未知错误"
        raise DailyReviewError(f"飞书机器人拒绝消息：{message}")
    return {"status": "sent", "sent_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}


def record_notification(output_root: Path, result: dict[str, Any], notification: dict[str, Any]) -> None:
    """在通知成功后更新 latest 指针；不把 webhook 写入任何结果。"""
    result["notification"] = notification
    _write_json(output_root.expanduser().resolve() / "latest-daily-review.json", result)


def _load_source_documents(output_root: Path, target_date: date) -> list[dict[str, Any]]:
    patterns = (
        ("discovery", "runs/*/candidates.pending-review.json"),
        ("manual", "manual-runs/*/meme-cards.pending-review.json"),
    )
    result: list[dict[str, Any]] = []
    for source_kind, pattern in patterns:
        for path in sorted(output_root.glob(pattern)):
            document = _read_json(path)
            generated = _parse_datetime(document.get("generated_at"))
            if generated is None or generated.astimezone(LOCAL_TZ).date() != target_date:
                continue
            result.append(
                {
                    "source_kind": source_kind,
                    "run_id": str(document.get("run_id") or path.parent.name),
                    "relative_path": str(path.relative_to(output_root)),
                    "document": document,
                }
            )
    return result


def _skip_reason(candidate: dict[str, Any]) -> str | None:
    enrichment = candidate.get("semantic_enrichment") or {}
    draft = candidate.get("draft_card") or {}
    if enrichment.get("status") != "pending_human_review":
        return "semantic_not_ready"
    if draft.get("classification") != "meme_candidate":
        return str(draft.get("classification") or "classification_missing")
    if not draft.get("usage_routes"):
        return "usage_routes_missing"
    return None


def _review_item(candidate: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    draft = candidate["draft_card"]
    phrase = str(candidate.get("phrase") or "").strip()
    group_id = f"{source['source_kind']}:{source['run_id']}:{candidate['candidate_id']}"
    card_id = "discovered-meme-" + hashlib.sha256(group_id.encode()).hexdigest()[:16]
    routes = [
        {
            "route_tag": str(route.get("route_tag") or ""),
            "when": str(route.get("when") or ""),
            "communicative_intent": str(route.get("communicative_intent") or ""),
            "allowed_realizations": list(route.get("allowed_realizations") or [phrase]),
        }
        for route in draft.get("usage_routes") or []
    ]
    positive_contexts = [
        {
            "context": str(row.get("context") or ""),
            "expected_action": str(row.get("expected_action") or "UNDERSTAND_ONLY"),
        }
        for row in draft.get("positive_contexts") or []
    ]
    negative_contexts = [
        {
            "context": str(row.get("context") or ""),
            "expected_action": "SKIP",
            "reason": str(row.get("reason") or ""),
        }
        for row in draft.get("negative_contexts") or []
    ]
    policy = (
        "refine"
        if any(row.get("expected_action") == "USE" for row in positive_contexts)
        else "understand"
    )
    research = candidate.get("web_research") or {}
    storage_card = {
        "card_id": card_id,
        "canonical_expression": phrase,
        "aliases": [],
        "language_market": "zh-CN",
        "semantic_core": str(draft.get("semantic_core") or ""),
        "culture_scope": str(draft.get("culture_scope") or "中文互联网，具体来源待核"),
        "serving_scope": "circle_only",
        "age_class": _age_class(research),
        "usage_routes": routes,
        "required_context_signals": list(draft.get("required_context_signals") or []),
        "hard_blocks": list(draft.get("hard_blocks") or []),
        "positive_contexts": positive_contexts,
        "negative_contexts": negative_contexts,
        "retrieval_facets": _retrieval_facets(phrase, draft),
        "knowledge": {
            "eligible": True,
            "source_class": (
                "manual_web_grounded_meme_name"
                if source["source_kind"] == "manual"
                else "automated_web_grounded_meme_discovery"
            ),
            "source_material": {
                "kind": "daily_reviewed_semantic_candidate",
                "semantic_signature": str(draft.get("semantic_core") or ""),
                "usage_hypothesis": "；".join(route["when"] for route in routes),
                "source_bvids": _source_bvids(candidate),
                "source_circle_names": _source_circles(candidate),
                "evidence_ids": _evidence_ids(candidate),
                "proposal_confidence": _proposal_confidence(candidate),
                "web_research": {
                    "research_version": research.get("research_version"),
                    "retrieved_at": research.get("retrieved_at"),
                    "retrieval_status": research.get("status"),
                    "freshness": research.get("freshness") or {},
                    "sources": [
                        {
                            key: row.get(key)
                            for key in (
                                "source_id",
                                "provider",
                                "source_kind",
                                "source_tier",
                                "title",
                                "url",
                                "snippet",
                                "published_at",
                                "match_quality",
                            )
                        }
                        for row in research.get("results") or []
                    ],
                },
            },
        },
        "serving": {
            "candidate": True,
            "default_action": "SKIP",
            "allowed_planner_actions": ["USE", "UNDERSTAND_ONLY", "SKIP"],
            "requires_semantic_gate": True,
            "requires_circle_anchor": True,
            "canonical_expression_is_not_instruction": True,
        },
        "human_review": {
            "status": "pending",
            "note": "昨日自动或人工接入结果汇总，待人工终审",
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        },
    }
    return {
        "group_id": group_id,
        "canonical_expression": phrase,
        "aliases": [],
        "content_type": "meme",
        "prior_serving_policy": policy,
        "semantic_status": "pending_human_review",
        "storage_card": storage_card,
        "web_research": research,
        "source_run": {
            "source_kind": source["source_kind"],
            "run_id": source["run_id"],
            "relative_path": source["relative_path"],
            "candidate_id": candidate.get("candidate_id"),
        },
    }


def _retrieval_facets(phrase: str, draft: dict[str, Any]) -> list[str]:
    values = [phrase, str(draft.get("semantic_core") or "")]
    for route in draft.get("usage_routes") or []:
        values.extend([str(route.get("route_tag") or ""), str(route.get("when") or "")])
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))[:8]


def _source_bvids(candidate: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for row in candidate.get("examples") or []:
        content_id = str(row.get("content_id") or "").split(":", 1)[0]
        if content_id.startswith("BV") and content_id not in result:
            result.append(content_id)
    return result


def _source_circles(candidate: dict[str, Any]) -> list[str]:
    values = [str(row.get("circle") or "").strip() for row in candidate.get("occurrence_contexts") or []]
    return list(dict.fromkeys(value for value in values if value))


def _evidence_ids(candidate: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for row in candidate.get("examples") or []:
        evidence_id = str(row.get("evidence_id") or "")
        if evidence_id and evidence_id not in result:
            result.append(evidence_id)
    return result


def _proposal_confidence(candidate: dict[str, Any]) -> float:
    values = [
        float(route.get("confidence"))
        for route in (candidate.get("draft_card") or {}).get("usage_routes") or []
        if isinstance(route.get("confidence"), (int, float))
    ]
    return round(sum(values) / len(values), 3) if values else 0.5


def _age_class(research: dict[str, Any]) -> str:
    freshness = str((research.get("freshness") or {}).get("class") or "")
    return "established" if freshness == "established" else "current_observed"


def _prompt_version(sources: list[dict[str, Any]]) -> str:
    versions = [
        str((source["document"].get("semantic_enrichment_report") or {}).get("prompt_version") or "")
        for source in sources
    ]
    values = list(dict.fromkeys(value for value in versions if value))
    return "+".join(values) or "unknown"


def _validated_webhook(raw: str) -> str:
    value = str(raw or "").strip().rstrip("。")
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "open.feishu.cn"
        or not parsed.path.startswith("/open-apis/bot/v2/hook/")
        or not parsed.path.removeprefix("/open-apis/bot/v2/hook/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DailyReviewError(f"{WEBHOOK_ENV} 不是合法的飞书自定义机器人 webhook")
    return value


def _parse_datetime(raw: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DailyReviewError(f"JSON 根节点不是对象：{path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)
