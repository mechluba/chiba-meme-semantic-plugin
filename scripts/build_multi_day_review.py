#!/usr/bin/env python3
"""把多日热梗发现与直播抽样归档汇总为本地人工审核页。"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import argparse
import html
import json
import math
import re
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meme_discovery.miner import mine_candidates, normalize_expression  # noqa: E402


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "out" / "p0-meme-discovery"
SOURCE_KIND_LABELS = {
    "comment": "B站评论",
    "danmaku": "B站视频弹幕",
    "public_live_sample": "直播抽样",
}
PLATFORM_LABELS = {"bilibili": "B站", "douyu": "斗鱼", "huya": "虎牙"}
NOISE_EXPRESSIONS = {
    "@用户",
    "用户",
    "[前方高能]",
    "前方高能",
    "请输入文本",
    "生日快乐",
    "来了",
    "有人吗",
    "1分钟前",
    "ai",
    "nb",
    "不是",
    "什么",
    "没有",
    "牛逼",
    "卧槽",
    "我去",
    "笑死了",
    "笑死我了",
    "哈哈",
    "哈哈哈",
}
TIME_LIKE_RE = re.compile(r"^\d+(?:秒|分钟|小时|天)前$")
ASCII_SHORT_RE = re.compile(r"^[a-z\d_]{2,3}$", re.IGNORECASE)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True, help="本地日期，格式 YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="本地日期，格式 YYYY-MM-DD（含）")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--output-dir", type=Path, help="默认写入 output-root/reports/起止日期")
    parser.add_argument(
        "--reviewed-library",
        type=Path,
        default=(
            REPO_ROOT
            / "resources"
            / "releases"
            / "reviewed-semantic-meme-library-20260729-multiprototype-v1"
            / "library.json"
        ),
        help="用于标记已有梗卡的审核后语义库",
    )
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end 不能早于 --start")
    return args


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL 第 {line_number} 行不是对象：{path}")
            result.append(value)
    return result


def _parse_datetime(raw: Any) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _local_date(item: dict[str, Any]) -> date | None:
    value = _parse_datetime(item.get("collected_at") or item.get("observed_at"))
    return value.astimezone(LOCAL_TZ).date() if value else None


def _run_local_date(document: dict[str, Any], fallback_name: str) -> date | None:
    value = _parse_datetime(document.get("generated_at"))
    if value:
        return value.astimezone(LOCAL_TZ).date()
    try:
        parsed = datetime.strptime(fallback_name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TZ).date()
    except ValueError:
        return None


def _in_range(value: date | None, start: date, end: date) -> bool:
    return value is not None and start <= value <= end


def _load_existing_cards(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    document = _read_json(path)
    lookup: dict[str, dict[str, Any]] = {}
    for card in document.get("cards") or []:
        if not isinstance(card, dict):
            continue
        expressions = [card.get("canonical_expression"), *(card.get("aliases") or [])]
        for expression in expressions:
            normalized = normalize_expression(str(expression or ""))
            if normalized:
                lookup.setdefault(normalized, card)
    return lookup


def _load_run_rows(output_root: Path, start: date, end: date) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate_path in sorted((output_root / "runs").glob("*/candidates.pending-review.json")):
        document = _read_json(candidate_path)
        local_day = _run_local_date(document, candidate_path.parent.name)
        if not _in_range(local_day, start, end):
            continue
        source_path = candidate_path.parent / "source-report.json"
        source_document = _read_json(source_path) if source_path.exists() else {"sources": []}
        bilibili = next(
            (item for item in source_document.get("sources") or [] if item.get("source") == "bilibili_public_web"),
            {},
        )
        inbox = next(
            (item for item in source_document.get("sources") or [] if item.get("source") == "authorized_jsonl_inbox"),
            {},
        )
        discovery_sources = Counter()
        for video in bilibili.get("videos") or []:
            source_name = str(video.get("discovery_source") or "固定选题源")
            discovery_sources[source_name] += 1
        summary = document.get("summary") or {}
        result.append(
            {
                "run_id": candidate_path.parent.name,
                "local_date": local_day.isoformat() if local_day else None,
                "generated_at": document.get("generated_at"),
                "fetched_evidence_count": int(summary.get("fetched_evidence_count") or 0),
                "new_evidence_count": int(summary.get("new_evidence_count") or 0),
                "rolling_evidence_count": int(summary.get("rolling_evidence_count") or 0),
                "candidate_count": int(summary.get("candidate_count") or 0),
                "usage_route_count": int(summary.get("usage_route_count") or 0),
                "bilibili_evidence_count": int(bilibili.get("evidence_count") or 0),
                "inbox_evidence_count": int(inbox.get("evidence_count") or 0),
                "video_count": len(bilibili.get("videos") or []),
                "discovery_sources": dict(discovery_sources),
                "error_count": len(bilibili.get("errors") or []) + len(inbox.get("errors") or []),
            }
        )
    return result


def _load_live_summary(output_root: Path, start: date, end: date) -> dict[str, Any]:
    report_paths = sorted((output_root / "live-archive").glob("*/*/sampling-report.json"))
    per_day: dict[str, dict[str, int]] = defaultdict(lambda: {"run_count": 0, "raw_message_count": 0})
    rooms: dict[tuple[str, str], dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    raw_message_count = 0
    included_report_count = 0
    for path in report_paths:
        report = _read_json(path)
        generated_at = _parse_datetime(report.get("generated_at"))
        if generated_at is None:
            try:
                generated_at = datetime.strptime(path.parent.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        local_day_value = generated_at.astimezone(LOCAL_TZ).date()
        if not _in_range(local_day_value, start, end):
            continue
        local_day = local_day_value.isoformat()
        included_report_count += 1
        message_count = int((report.get("summary") or {}).get("message_count") or 0)
        raw_message_count += message_count
        per_day[local_day]["run_count"] += 1
        per_day[local_day]["raw_message_count"] += message_count
        for room in report.get("rooms") or []:
            platform = str(room.get("platform") or "unknown")
            room_id = str(room.get("room_id") or "unknown")
            key = (platform, room_id)
            aggregate = rooms.setdefault(
                key,
                {
                    "platform": platform,
                    "room_id": room_id,
                    "label": str(room.get("label") or room_id),
                    "selected_count": 0,
                    "message_count": 0,
                    "statuses": Counter(),
                },
            )
            status = str(room.get("status") or "unknown")
            aggregate["statuses"][status] += 1
            status_counts[status] += 1
            if status != "skipped_rotation":
                aggregate["selected_count"] += 1
            aggregate["message_count"] += int(room.get("message_count") or 0)

    room_rows = []
    for room in rooms.values():
        room_rows.append(
            {
                **{key: value for key, value in room.items() if key != "statuses"},
                "statuses": dict(room["statuses"]),
            }
        )
    room_rows.sort(key=lambda item: (-item["message_count"], item["platform"], item["room_id"]))
    return {
        "run_count": included_report_count,
        "raw_message_count": raw_message_count,
        "per_day": [{"date": key, **value} for key, value in sorted(per_day.items())],
        "rooms": room_rows,
        "status_counts": dict(status_counts),
    }


def _top(counter: Counter[str], limit: int = 3) -> list[dict[str, Any]]:
    return [{"name": key, "count": value} for key, value in counter.most_common(limit) if key]


def _noise_reasons(phrase: str, distinct_contents: int, concentration: float) -> list[str]:
    normalized = normalize_expression(phrase)
    reasons: list[str] = []
    if phrase in NOISE_EXPRESSIONS or normalized in {normalize_expression(item) for item in NOISE_EXPRESSIONS}:
        reasons.append("常见刷屏/界面文本")
    if phrase.startswith("@"):
        reasons.append("提及占位文本")
    if TIME_LIKE_RE.fullmatch(normalized):
        reasons.append("时间界面文本")
    if ASCII_SHORT_RE.fullmatch(normalized):
        reasons.append("过短英文缩写")
    if distinct_contents <= 2 and concentration >= 0.9:
        reasons.append("高度集中于单一内容")
    return list(dict.fromkeys(reasons))


def _candidate_rows(
    evidence: list[dict[str, Any]],
    existing_cards: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    mining_config = {
        "min_occurrences": 3,
        "min_cross_content_occurrences": 2,
        "min_distinct_contents": 2,
        "max_examples_per_candidate": 1,
        "max_occurrence_context_scopes": 1,
        "max_scene_contexts": 1,
        "max_nearby_messages": 0,
        "context_window_seconds": 6,
    }
    candidates = mine_candidates(evidence, mining_config)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        normalized = normalize_expression(str(item.get("content") or ""))
        if normalized:
            grouped[normalized].append(item)

    result: list[dict[str, Any]] = []
    for candidate in candidates:
        normalized = str(candidate["normalized_expression"])
        items = grouped[normalized]
        contents = Counter(str(item.get("content_id") or "") for item in items)
        days = Counter(
            local_day.isoformat()
            for item in items
            if (local_day := _local_date(item)) is not None
        )
        circles = Counter(str(item.get("circle") or "未分类") for item in items)
        platforms = sorted({str(item.get("platform") or "unknown") for item in items})
        source_kinds = sorted({str(item.get("source_kind") or "unknown") for item in items})
        live_items = [item for item in items if item.get("source_kind") == "public_live_sample"]
        live_sessions = {str(item.get("content_id") or "") for item in live_items}
        live_rooms = Counter(str(item.get("content_title") or "") for item in live_items)
        message_count = len(items)
        distinct_contents = len(contents)
        concentration = (contents.most_common(1)[0][1] / message_count) if message_count else 0.0
        existing = existing_cards.get(normalized)
        noise_reasons = _noise_reasons(str(candidate["phrase"]), distinct_contents, concentration)
        cross_platform = len(platforms) >= 2
        day_count = len(days)
        if not noise_reasons and (
            cross_platform
            or (distinct_contents >= 5 and day_count >= 2 and concentration < 0.85)
            or (len(live_sessions) >= 5 and day_count >= 2)
        ):
            priority = "high"
        elif not noise_reasons and (distinct_contents >= 3 or message_count >= 10):
            priority = "medium"
        else:
            priority = "low"
        score = (
            math.log1p(message_count) * 1.2
            + math.log1p(distinct_contents) * 3.0
            + day_count * 2.0
            + len(platforms) * 2.0
            + len(source_kinds)
            + (3.0 if cross_platform else 0.0)
            + (2.0 if live_items else 0.0)
            - max(0.0, concentration - 0.7) * 8.0
            - len(noise_reasons) * 4.0
        )
        result.append(
            {
                "candidate_id": candidate["candidate_id"],
                "phrase": candidate["phrase"],
                "aliases": candidate.get("aliases") or [],
                "message_count": message_count,
                "distinct_content_count": distinct_contents,
                "day_count": day_count,
                "first_seen_date": min(days) if days else None,
                "last_seen_date": max(days) if days else None,
                "platforms": platforms,
                "source_kinds": source_kinds,
                "cross_platform": cross_platform,
                "live_message_count": len(live_items),
                "live_session_count": len(live_sessions),
                "live_rooms": _top(live_rooms),
                "top_circles": _top(circles),
                "concentration": round(concentration, 4),
                "priority": priority,
                "score": round(score, 3),
                "likely_noise": bool(noise_reasons),
                "noise_reasons": noise_reasons,
                "existing_card": (
                    {
                        "card_id": existing.get("card_id"),
                        "canonical_expression": existing.get("canonical_expression"),
                    }
                    if existing
                    else None
                ),
                "semantic_status": "not_run",
            }
        )
    priority_order = {"high": 0, "medium": 1, "low": 2}
    result.sort(
        key=lambda item: (
            priority_order[item["priority"]],
            -float(item["score"]),
            -int(item["distinct_content_count"]),
            str(item["phrase"]),
        )
    )
    return result


def _build_summary(
    *,
    start: date,
    end: date,
    output_root: Path,
    reviewed_library: Path,
) -> dict[str, Any]:
    store_path = output_root / "evidence-store.jsonl"
    evidence = [
        item
        for item in _read_jsonl(store_path)
        if _in_range(_local_date(item), start, end)
    ]
    existing_cards = _load_existing_cards(reviewed_library)
    candidates = _candidate_rows(evidence, existing_cards)
    live_summary = _load_live_summary(output_root, start, end)
    platform_counts = Counter(str(item.get("platform") or "unknown") for item in evidence)
    source_kind_counts = Counter(str(item.get("source_kind") or "unknown") for item in evidence)
    discovery_counts = Counter(
        str((item.get("context") or {}).get("discovery_source") or "固定选题/直播归档")
        for item in evidence
    )
    day_counts = Counter(
        local_day.isoformat()
        for item in evidence
        if (local_day := _local_date(item)) is not None
    )
    return {
        "schema_version": 1,
        "report_kind": "p0_meme_discovery_multi_day_review",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "date_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timezone": str(LOCAL_TZ),
            "basis": "collected_at",
        },
        "collection_status": "stopped_by_user",
        "semantic_status": {
            "status": "not_run",
            "usage_route_count": 0,
            "note": "本页用于候选初筛；交流意图、使用场景和禁用条件仍需后续语义模型提炼与人工审核。",
        },
        "summary": {
            "unique_evidence_count": len(evidence),
            "candidate_count": len(candidates),
            "high_priority_count": sum(item["priority"] == "high" for item in candidates),
            "cross_platform_count": sum(item["cross_platform"] for item in candidates),
            "live_candidate_count": sum(item["live_message_count"] > 0 for item in candidates),
            "likely_noise_count": sum(item["likely_noise"] for item in candidates),
            "existing_card_match_count": sum(item["existing_card"] is not None for item in candidates),
            "discovery_run_count": len(_load_run_rows(output_root, start, end)),
            "live_run_count": live_summary["run_count"],
            "raw_live_message_count": live_summary["raw_message_count"],
            "deduplicated_live_message_count": int(source_kind_counts.get("public_live_sample", 0)),
        },
        "evidence_breakdown": {
            "by_day": [{"date": key, "count": value} for key, value in sorted(day_counts.items())],
            "by_platform": [
                {"platform": key, "label": PLATFORM_LABELS.get(key, key), "count": value}
                for key, value in platform_counts.most_common()
            ],
            "by_source_kind": [
                {"source_kind": key, "label": SOURCE_KIND_LABELS.get(key, key), "count": value}
                for key, value in source_kind_counts.most_common()
            ],
            "by_discovery_source": [
                {"source": key, "count": value} for key, value in discovery_counts.most_common()
            ],
        },
        "discovery_runs": _load_run_rows(output_root, start, end),
        "live_sampling": live_summary,
        "candidates": candidates,
    }


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _render_html(document: dict[str, Any]) -> str:
    data = _json_for_script(document)
    period = document["date_range"]
    summary = document["summary"]
    title = f"千叶热梗审核 · {period['start']}～{period['end']}"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#f4f4f1;--panel:#fff;--ink:#20221f;--muted:#6f736c;--line:#dedfd9;--brand:#2459d3;--high:#096b46;--warn:#9a6200;--danger:#a5382b}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.page{{max-width:1500px;margin:auto;padding:18px 22px 40px}} header{{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:12px}}
h1{{font-size:23px;margin:0 0 3px}} .sub,.muted{{color:var(--muted)}} .header-actions{{display:flex;gap:7px;flex-wrap:wrap}}
button,select,input{{font:inherit}} button,.btn{{border:1px solid #cfd1ca;background:#fff;border-radius:6px;padding:6px 10px;cursor:pointer;color:var(--ink)}}
button:hover{{border-color:#999}} button.primary{{background:var(--brand);border-color:var(--brand);color:#fff}}
.notice{{display:flex;gap:14px;align-items:center;padding:9px 12px;margin-bottom:10px;border:1px solid #e4c77d;background:#fff7df;border-radius:7px}}
.notice strong{{white-space:nowrap;color:#714900}} .metrics{{display:grid;grid-template-columns:repeat(8,minmax(100px,1fr));gap:7px;margin-bottom:10px}}
.storage-warning{{border-color:#e8b5a8;background:#fff0eb}}
.metric{{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:8px 10px}} .metric b{{display:block;font-size:19px;line-height:1.15}} .metric span{{color:var(--muted);font-size:11px}}
.source-grid{{display:grid;grid-template-columns:1fr 1.35fr;gap:9px;margin-bottom:10px}} .panel{{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:9px 11px;min-width:0}}
.panel h2{{font-size:14px;margin:0 0 6px}} table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;border-top:1px solid #ecece8;padding:4px 6px;white-space:nowrap}} th{{font-size:11px;color:var(--muted);font-weight:600}} td.num{{text-align:right;font-variant-numeric:tabular-nums}}
details>summary{{cursor:pointer}} .toolbar{{position:sticky;top:0;z-index:5;background:rgba(244,244,241,.96);backdrop-filter:blur(8px);border-block:1px solid var(--line);padding:8px 0;margin-bottom:7px}}
.toolbar-row{{display:flex;gap:7px;align-items:center;flex-wrap:wrap}} .toolbar input{{min-width:240px;flex:1;border:1px solid #cfd1ca;border-radius:6px;padding:7px 9px;background:#fff}}
.filters button.active{{background:#26303f;color:#fff;border-color:#26303f}} .progress{{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}}
.candidate-list{{display:grid;gap:5px}} article{{display:grid;grid-template-columns:minmax(190px,1.15fr) minmax(340px,2.3fr) auto;align-items:center;gap:9px;background:#fff;border:1px solid var(--line);border-left:4px solid #bbb;border-radius:7px;padding:7px 9px}}
article.high{{border-left-color:var(--high)}} article.low{{opacity:.82}} .phrase-line{{display:flex;align-items:center;gap:6px;min-width:0}} .phrase{{font-size:16px;font-weight:700;overflow-wrap:anywhere}}
.badges{{display:flex;gap:4px;flex-wrap:wrap;margin-top:3px}} .badge{{display:inline-block;padding:1px 6px;border-radius:999px;background:#eee;color:#50544e;font-size:10px;white-space:nowrap}}
.badge.high{{background:#dcefe5;color:#075d3c}} .badge.live{{background:#e8edff;color:#344f9d}} .badge.cross{{background:#e4f0ff;color:#135b9b}} .badge.known{{background:#f0e5ff;color:#6e359b}} .badge.noise{{background:#ffe8e1;color:#983e2d}}
.facts{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;color:#494d47}} .facts b{{font-variant-numeric:tabular-nums}} .context{{margin-top:2px;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.decisions{{display:flex;gap:4px;align-items:center}} .decisions button{{padding:5px 7px;font-size:11px}} .decisions button.active[data-decision="refine"]{{background:#dcefe5;border-color:#75ab91;color:#075d3c}} .decisions button.active[data-decision="understand"]{{background:#e8edff;border-color:#8da1db;color:#344f9d}} .decisions button.active[data-decision="reject"]{{background:#ffe8e1;border-color:#d69889;color:#8f3024}}
.empty{{padding:35px;text-align:center;color:var(--muted)}} .footnote{{margin-top:10px;color:var(--muted);font-size:11px}} .hidden{{display:none!important}}
@media(max-width:1050px){{.metrics{{grid-template-columns:repeat(4,1fr)}}.source-grid{{grid-template-columns:1fr}} article{{grid-template-columns:1fr}}.decisions{{justify-content:flex-start}}}}
@media(max-width:600px){{.page{{padding:12px}}.metrics{{grid-template-columns:repeat(2,1fr)}}header{{display:block}}.header-actions{{margin-top:8px}}}}
</style></head><body><div class="page">
<header><div><h1>{html.escape(title)}</h1><div class="sub">单文件离线版，候选数据已内嵌 · 按 Asia/Shanghai 的 collected_at 汇总 · 不展示语境证据与重复样本</div></div>
<div class="header-actions"><button id="export" class="primary">导出审核结果 JSON</button><button id="clear">清空本页决定</button></div></header>
<div class="notice"><strong>语义模型未运行</strong><span>本页只用于判断哪些表达值得进入下一轮语义提炼。交流意图、常见使用场景、必需信号与禁用条件均未生成，不能直接接入千叶。</span></div>
<div class="notice storage-warning hidden" id="storage-warning"><strong>浏览器未开放本地存储</strong><span>候选数据仍可正常查看，当前审核决定只在本次打开期间保留；关闭页面前请导出 JSON。</span></div>
<section class="metrics" id="metrics"></section>
<section class="source-grid"><div class="panel"><h2>每日去重证据 / 直播归档</h2><table><thead><tr><th>日期</th><th class="num">去重证据</th><th class="num">直播轮次</th><th class="num">原始弹幕</th></tr></thead><tbody id="days"></tbody></table></div>
<div class="panel"><h2>直播间覆盖</h2><table><thead><tr><th>平台 / 直播间</th><th class="num">抽中</th><th class="num">消息</th><th>状态</th></tr></thead><tbody id="rooms"></tbody></table></div></section>
<details class="panel"><summary><strong>采集运行与来源明细</strong> <span class="muted">（{summary['discovery_run_count']} 次发现运行，点击展开）</span></summary><div id="runs"></div></details>
<div class="toolbar"><div class="toolbar-row"><input id="search" type="search" placeholder="搜索候选、别名、领域或直播间…"><div class="filters" id="filters"></div><select id="sort"><option value="priority">优先级</option><option value="messages">出现次数</option><option value="contents">内容/场次</option><option value="recent">最近出现</option></select><span class="progress" id="progress"></span></div></div>
<main class="candidate-list" id="candidates"></main><div class="footnote">浏览器允许时，审核决定会保存在 localStorage；否则仅在本次打开期间保留。请导出 JSON 后再进入语义提炼流程。页面未内嵌原始弹幕、出现语境或重复表达样本。</div>
</div><script id="report-data" type="application/json">{data}</script><script>
const REPORT=JSON.parse(document.getElementById('report-data').textContent);
const STORAGE_KEY=`chiba-meme-review:${{REPORT.date_range.start}}:${{REPORT.date_range.end}}`;
let storageAvailable=true;
function readDecisions(){{
 try{{return JSON.parse(window.localStorage.getItem(STORAGE_KEY)||'{{}}')}}
 catch(error){{storageAvailable=false;return {{}}}}
}}
function showStorageStatus(){{document.getElementById('storage-warning').classList.toggle('hidden',storageAvailable)}}
function saveDecisions(){{
 try{{window.localStorage.setItem(STORAGE_KEY,JSON.stringify(decisions))}}
 catch(error){{storageAvailable=false;showStorageStatus()}}
}}
function clearSavedDecisions(){{
 try{{window.localStorage.removeItem(STORAGE_KEY)}}
 catch(error){{storageAvailable=false;showStorageStatus()}}
}}
let decisions=readDecisions();
let state={{filter:'all',query:'',sort:'priority'}};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const fmt=n=>Number(n||0).toLocaleString('zh-CN');
const labels={{high:'优先',medium:'一般',low:'低优先',comment:'B站评论',danmaku:'B站弹幕',public_live_sample:'直播抽样',bilibili:'B站',douyu:'斗鱼',huya:'虎牙',ok:'成功',no_messages_observed:'未观察到消息',skipped_rotation:'本轮跳过',offline:'未开播',missing_credentials:'缺少凭证'}};
function renderSummary(){{
 const s=REPORT.summary;
 const metrics=[['去重证据',s.unique_evidence_count],['直播原始消息',s.raw_live_message_count],['候选表达',s.candidate_count],['优先复核',s.high_priority_count],['跨平台',s.cross_platform_count],['含直播证据',s.live_candidate_count],['已有卡命中',s.existing_card_match_count],['可能噪音',s.likely_noise_count]];
 document.getElementById('metrics').innerHTML=metrics.map(([k,v])=>`<div class="metric"><b>${{fmt(v)}}</b><span>${{k}}</span></div>`).join('');
 const liveDays=Object.fromEntries(REPORT.live_sampling.per_day.map(x=>[x.date,x]));
 document.getElementById('days').innerHTML=REPORT.evidence_breakdown.by_day.map(x=>{{const l=liveDays[x.date]||{{}};return `<tr><td>${{x.date}}</td><td class="num">${{fmt(x.count)}}</td><td class="num">${{fmt(l.run_count)}}</td><td class="num">${{fmt(l.raw_message_count)}}</td></tr>`}}).join('');
 document.getElementById('rooms').innerHTML=REPORT.live_sampling.rooms.map(r=>`<tr><td>${{esc(labels[r.platform]||r.platform)}} / ${{esc(r.label)}} <span class="muted">${{esc(r.room_id)}}</span></td><td class="num">${{fmt(r.selected_count)}}</td><td class="num">${{fmt(r.message_count)}}</td><td>${{esc(Object.entries(r.statuses).map(([k,v])=>`${{labels[k]||k}} ${{v}}`).join(' · '))}}</td></tr>`).join('');
 document.getElementById('runs').innerHTML=`<table><thead><tr><th>本地日期 / run</th><th class="num">本轮抓取</th><th class="num">新增去重</th><th class="num">滚动库</th><th class="num">候选</th><th class="num">B站素材</th><th class="num">错误</th></tr></thead><tbody>${{REPORT.discovery_runs.map(r=>`<tr><td>${{r.local_date}} <span class="muted">${{r.run_id}}</span></td><td class="num">${{fmt(r.fetched_evidence_count)}}</td><td class="num">${{fmt(r.new_evidence_count)}}</td><td class="num">${{fmt(r.rolling_evidence_count)}}</td><td class="num">${{fmt(r.candidate_count)}}</td><td class="num">${{fmt(r.bilibili_evidence_count)}}</td><td class="num">${{fmt(r.error_count)}}</td></tr>`).join('')}}</tbody></table>`;
}}
const filterDefs=[['all','全部'],['pending','未处理'],['high','优先'],['cross','跨平台'],['live','直播'],['known','已有卡'],['noise','可能噪音'],['refine','已选提炼'],['understand','仅理解'],['reject','已淘汰']];
function candidateMatches(c){{
 const decision=decisions[c.candidate_id]?.decision;
 if(state.filter==='pending'&&decision)return false;
 if(['refine','understand','reject'].includes(state.filter)&&decision!==state.filter)return false;
 if(state.filter==='high'&&c.priority!=='high')return false;
 if(state.filter==='cross'&&!c.cross_platform)return false;
 if(state.filter==='live'&&!c.live_message_count)return false;
 if(state.filter==='known'&&!c.existing_card)return false;
 if(state.filter==='noise'&&!c.likely_noise)return false;
 if(state.query){{const hay=[c.phrase,...c.aliases,...c.top_circles.map(x=>x.name),...c.live_rooms.map(x=>x.name)].join(' ').toLowerCase();if(!hay.includes(state.query.toLowerCase()))return false;}}
 return true;
}}
function sortedCandidates(){{
 const rows=REPORT.candidates.filter(candidateMatches);
 if(state.sort==='messages')rows.sort((a,b)=>b.message_count-a.message_count||b.score-a.score);
 if(state.sort==='contents')rows.sort((a,b)=>b.distinct_content_count-a.distinct_content_count||b.message_count-a.message_count);
 if(state.sort==='recent')rows.sort((a,b)=>String(b.last_seen_date).localeCompare(String(a.last_seen_date))||b.score-a.score);
 return rows;
}}
function badge(text,kind=''){{return `<span class="badge ${{kind}}">${{esc(text)}}</span>`}}
function renderCandidates(){{
 const rows=sortedCandidates();
 const container=document.getElementById('candidates');
 container.innerHTML=rows.length?rows.map(c=>{{
  const d=decisions[c.candidate_id]?.decision;
  const bs=[badge(labels[c.priority],c.priority)];
  if(c.cross_platform)bs.push(badge('跨平台','cross'));
  if(c.live_message_count)bs.push(badge(`直播 ${{fmt(c.live_message_count)}}`,'live'));
  if(c.existing_card)bs.push(badge(`已有：${{c.existing_card.canonical_expression}}`,'known'));
  if(c.likely_noise)bs.push(badge(c.noise_reasons.join('、'),'noise'));
  const sources=c.source_kinds.map(x=>labels[x]||x).join(' / ');
  const contexts=c.top_circles.map(x=>`${{x.name}} ${{x.count}}`).join(' · ');
  const rooms=c.live_rooms.length?`；直播间 ${{c.live_rooms.map(x=>`${{x.name}} ${{x.count}}`).join(' · ')}}`:'';
  return `<article class="${{c.priority}}" data-id="${{esc(c.candidate_id)}}"><div><div class="phrase-line"><span class="phrase">${{esc(c.phrase)}}</span></div><div class="badges">${{bs.join('')}}</div></div><div><div class="facts"><span><b>${{fmt(c.message_count)}}</b> 次</span><span><b>${{fmt(c.distinct_content_count)}}</b> 内容/场次</span><span><b>${{c.day_count}}</b> 天</span><span>${{c.first_seen_date}}～${{c.last_seen_date}}</span><span>${{esc(sources)}}</span></div><div class="context">领域：${{esc(contexts||'未分类')}}${{esc(rooms)}}</div></div><div class="decisions"><button data-decision="refine" class="${{d==='refine'?'active':''}}">进入提炼</button><button data-decision="understand" class="${{d==='understand'?'active':''}}">仅理解</button><button data-decision="reject" class="${{d==='reject'?'active':''}}">淘汰</button></div></article>`;
 }}).join(''):'<div class="empty">当前筛选下没有候选。</div>';
 container.querySelectorAll('[data-decision]').forEach(btn=>btn.addEventListener('click',()=>setDecision(btn.closest('article').dataset.id,btn.dataset.decision)));
 const done=Object.values(decisions).filter(x=>x?.decision).length;
 document.getElementById('progress').textContent=`显示 ${{fmt(rows.length)}} / ${{fmt(REPORT.candidates.length)}} · 已处理 ${{fmt(done)}}`;
 document.querySelectorAll('#filters button').forEach(btn=>btn.classList.toggle('active',btn.dataset.filter===state.filter));
}}
function setDecision(id,decision){{if(decisions[id]?.decision===decision)delete decisions[id];else decisions[id]={{decision,updated_at:new Date().toISOString()}};saveDecisions();renderCandidates();}}
function exportDecisions(){{
 const payload={{schema_version:1,report_kind:'p0_meme_discovery_triage_decisions',date_range:REPORT.date_range,exported_at:new Date().toISOString(),semantic_status:'not_run',decisions:Object.entries(decisions).map(([candidate_id,value])=>{{const c=REPORT.candidates.find(x=>x.candidate_id===candidate_id);return {{candidate_id,phrase:c?.phrase||'',...value}}}})}};
 const blob=new Blob([JSON.stringify(payload,null,2)+'\\n'],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`meme-review-decisions-${{REPORT.date_range.start}}-${{REPORT.date_range.end}}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}}
document.getElementById('filters').innerHTML=filterDefs.map(([k,v])=>`<button data-filter="${{k}}">${{v}}</button>`).join('');
document.querySelectorAll('#filters button').forEach(btn=>btn.addEventListener('click',()=>{{state.filter=btn.dataset.filter;renderCandidates()}}));
document.getElementById('search').addEventListener('input',e=>{{state.query=e.target.value.trim();renderCandidates()}});
document.getElementById('sort').addEventListener('change',e=>{{state.sort=e.target.value;renderCandidates()}});
document.getElementById('export').addEventListener('click',exportDecisions);
document.getElementById('clear').addEventListener('click',()=>{{if(confirm('清空这份页面中已经保存的全部审核决定？')){{decisions={{}};clearSavedDecisions();renderCandidates()}}}});
showStorageStatus();renderSummary();renderCandidates();
</script></body></html>"""


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


def main() -> int:
    args = _parse_args()
    output_root = args.output_root.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else output_root / "reports" / f"{args.start:%Y%m%d}-{args.end:%Y%m%d}"
    )
    document = _build_summary(
        start=args.start,
        end=args.end,
        output_root=output_root,
        reviewed_library=args.reviewed_library.expanduser().resolve(),
    )
    summary_path = output_dir / "multi-day-summary.json"
    review_path = output_dir / "multi-day-review.html"
    _write_json(summary_path, document)
    _write_text(review_path, _render_html(document))
    print(
        json.dumps(
            {
                "summary_file": str(summary_path),
                "review_page": str(review_path),
                "summary": document["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
