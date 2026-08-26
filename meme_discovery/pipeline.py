"""P0 热梗候选发现主流水线。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import hashlib
import html
import json
import os
import re

from .bilibili import (
    RateLimitedHttpClient,
    SourceError,
    fetch_comments,
    fetch_danmaku_segment,
    fetch_pagelist,
    fetch_popular_videos,
    segment_count,
)
from .miner import mine_candidates


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(unix_seconds: int | None = None) -> str:
    value = datetime.fromtimestamp(unix_seconds, timezone.utc) if unix_seconds else _utc_now()
    return value.isoformat().replace("+00:00", "Z")


def _evidence_id(platform: str, source_kind: str, content_id: str, message_id: str) -> str:
    raw = "\x1f".join((platform, source_kind, content_id, message_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _make_evidence(
    *,
    platform: str,
    source_kind: str,
    source_id: str,
    content_id: str,
    content_title: str,
    circle: str,
    message_id: str,
    content: str,
    observed_at: str | None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evidence_id": _evidence_id(platform, source_kind, content_id, message_id),
        "platform": platform,
        "source_kind": source_kind,
        "source_id": source_id,
        "content_id": content_id,
        "content_title": content_title,
        "circle": circle,
        "message_id": message_id,
        "content": _redact_mentions(" ".join(content.split()).strip()),
        "observed_at": observed_at or _iso(),
        "collected_at": _iso(),
        "context": context or {},
    }


def _redact_mentions(content: str) -> str:
    """移除消息正文中可直接识别的 @ 昵称；不尝试推断普通文本中的人名。"""
    return re.sub(r"[@＠][^\s@＠]{1,32}", "@用户", content)


def collect_bilibili(config: dict[str, Any], client: RateLimitedHttpClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_config = config.get("bilibili") or {}
    videos: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if (source_config.get("popular") or {}).get("enabled", True):
        try:
            videos.extend(fetch_popular_videos(client, source_config.get("popular") or {}))
        except SourceError as exc:
            errors.append({"stage": "popular", "error": str(exc)})
    for seed in source_config.get("seed_videos", []):
        if not isinstance(seed, dict) or not seed.get("bvid"):
            continue
        videos.append(
            {
                "bvid": str(seed["bvid"]),
                "aid": int(seed.get("aid") or 0),
                "title": str(seed.get("title") or ""),
                "creator": str(seed.get("creator") or ""),
                "circle": str(seed.get("circle") or "种子视频"),
                "discovery_source": "bilibili_seed",
            }
        )
    videos = list({video["bvid"]: video for video in videos}.values())
    evidence: list[dict[str, Any]] = []
    video_reports: list[dict[str, Any]] = []
    max_parts = int(source_config.get("max_parts_per_video", 1))
    max_segments = int(source_config.get("max_segments_per_part", 2))
    max_messages = int(source_config.get("max_danmaku_per_video", 2000))
    max_comments = int(source_config.get("max_comments_per_video", 20))
    for video in videos:
        bvid = video["bvid"]
        report: dict[str, Any] = {
            "bvid": bvid,
            "title": video["title"],
            "status": "ok",
            "counts": {"danmaku": 0, "comments": 0, "parts": 0, "evidence": 0},
            "errors": [],
        }
        before_count = len(evidence)
        try:
            pages = fetch_pagelist(client, bvid)[:max_parts]
            if not pages:
                raise SourceError(f"视频没有可用分P: {bvid}")
            if not video["title"]:
                video["title"] = str(pages[0].get("part") or bvid)
            for page in pages:
                cid = int(page["cid"])
                duration = int(page.get("duration") or 0)
                collected_for_video = 0
                for index in range(1, segment_count(duration, max_segments) + 1):
                    for item in fetch_danmaku_segment(client, cid=cid, segment_index=index):
                        if collected_for_video >= max_messages:
                            break
                        evidence.append(
                            _make_evidence(
                                platform="bilibili",
                                source_kind="danmaku",
                                source_id=bvid,
                                content_id=f"{bvid}:{cid}",
                                content_title=video["title"],
                                circle=video["circle"],
                                message_id=item.message_id,
                                content=item.content,
                                observed_at=_iso(item.ctime),
                                context={"progress_seconds": item.progress_seconds, "part": page.get("page")},
                            )
                        )
                        collected_for_video += 1
            report["counts"]["parts"] = len(pages)
            report["counts"]["danmaku"] = len(evidence) - before_count
        except (SourceError, ValueError) as exc:
            report["status"] = "error"
            report["errors"].append({"stage": "danmaku", "error": str(exc)})
            errors.append({"stage": "video", "source_id": bvid, "error": str(exc)})
        try:
            comments = fetch_comments(
                client,
                aid=int(video.get("aid") or 0),
                max_comments=max_comments,
            )
            for item in comments:
                evidence.append(
                    _make_evidence(
                        platform="bilibili",
                        source_kind="comment",
                        source_id=bvid,
                        content_id=bvid,
                        content_title=video["title"],
                        circle=video["circle"],
                        message_id=item["message_id"],
                        content=item["content"],
                        observed_at=_iso(item.get("observed_unix")),
                    )
                )
            report["counts"]["comments"] = len(comments)
        except SourceError as exc:
            report["errors"].append({"stage": "comments", "error": str(exc)})
            errors.append({"stage": "comments", "source_id": bvid, "error": str(exc)})
            if report["status"] == "ok":
                report["status"] = "partial"
        report["counts"]["evidence"] = len(evidence) - before_count
        video_reports.append(report)
    return evidence, {
        "source": "bilibili_public_web",
        "status": "partial" if evidence and errors else ("ok" if evidence else ("error" if errors else "no_evidence")),
        "access_level": "experimental_public_web_endpoint",
        "videos": video_reports,
        "errors": errors,
        "evidence_count": len(evidence),
    }


def check_room_anchors(config: dict[str, Any], client: RateLimitedHttpClient) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for anchor in config.get("room_anchors", []):
        url = str(anchor.get("url") or "")
        report = {
            "platform": anchor.get("platform"),
            "room_id": str(anchor.get("room_id") or ""),
            "label": anchor.get("label"),
            "mode": "metadata_only",
            "chat_collected": False,
        }
        if not url.startswith("https://"):
            report.update({"status": "invalid_config", "error": "只允许 HTTPS 房间地址"})
            reports.append(report)
            continue
        try:
            payload = client.get(url, referer=url)[:300_000]
            text = payload.decode("utf-8", errors="ignore")
            title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
            report.update(
                {
                    "status": "reachable",
                    "page_title": html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
                    if title_match
                    else "",
                    "note": "只验证公开房间页可达；未取得正式弹幕接口授权，因此不采集聊天内容。",
                }
            )
        except SourceError as exc:
            report.update({"status": "unreachable", "error": str(exc)})
        reports.append(report)
    return {"source": "live_room_anchors", "rooms": reports}


def collect_jsonl_inbox(config: dict[str, Any], repo_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inbox_config = config.get("jsonl_inbox") or {}
    if not inbox_config.get("enabled", True):
        return [], {"source": "authorized_jsonl_inbox", "status": "disabled", "evidence_count": 0}
    inbox = _resolve_path(repo_root, str(inbox_config.get("path") or "out/p0-meme-discovery/inbox"))
    inbox.mkdir(parents=True, exist_ok=True)
    evidence: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    files = sorted(inbox.glob("*.jsonl"))
    for path in files:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                platform = str(item["platform"]).strip()
                room_id = str(item["room_id"]).strip()
                message_id = str(item["message_id"]).strip()
                content = str(item["content"]).strip()
                if not all((platform, room_id, message_id, content)):
                    raise ValueError("必填字段为空")
                evidence.append(
                    _make_evidence(
                        platform=platform,
                        source_kind="authorized_live_export",
                        source_id=f"{platform}:{room_id}",
                        content_id=str(item.get("session_id") or f"{platform}:{room_id}:{path.stem}"),
                        content_title=str(item.get("room_title") or ""),
                        circle=str(item.get("circle") or "直播间"),
                        message_id=message_id,
                        content=content,
                        observed_at=str(item.get("observed_at") or _iso()),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                errors.append({"file": path.name, "line": line_number, "error": str(exc)})
    return evidence, {
        "source": "authorized_jsonl_inbox",
        "status": "ok",
        "path": str(inbox),
        "files": [path.name for path in files],
        "evidence_count": len(evidence),
        "errors": errors,
        "privacy_note": "输入中的昵称、用户 ID 等字段不会写入标准化证据。",
    }


def run_discovery(config: dict[str, Any], *, repo_root: Path) -> dict[str, Any]:
    output_root = _resolve_path(repo_root, str(config.get("output_root") or "out/p0-meme-discovery"))
    run_at = _utc_now()
    run_id = run_at.strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / "runs" / run_id
    suffix = 1
    while run_dir.exists():
        run_dir = output_root / "runs" / f"{run_id}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)
    request_config = config.get("request") or {}
    client = RateLimitedHttpClient(
        user_agent=str(request_config.get("user_agent") or "Mozilla/5.0"),
        timeout_seconds=float(request_config.get("timeout_seconds", 20)),
        minimum_interval_seconds=float(request_config.get("minimum_interval_seconds", 0.5)),
        max_retries=int(request_config.get("max_retries", 2)),
    )
    bilibili_evidence, bilibili_report = collect_bilibili(config.get("sources") or {}, client)
    inbox_evidence, inbox_report = collect_jsonl_inbox(config.get("sources") or {}, repo_root)
    room_report = check_room_anchors(config.get("sources") or {}, client)
    deduplicated = {item["evidence_id"]: item for item in bilibili_evidence + inbox_evidence}
    fetched_evidence = sorted(deduplicated.values(), key=lambda item: (item["content_id"], item["message_id"]))
    store_path = output_root / "evidence-store.jsonl"
    stored_evidence = _read_jsonl(store_path)
    stored_ids = {item["evidence_id"] for item in stored_evidence}
    new_evidence = [item for item in fetched_evidence if item["evidence_id"] not in stored_ids]
    retention_days = max(1, int(config.get("evidence_retention_days", 14)))
    cutoff = run_at - timedelta(days=retention_days)
    rolling_evidence = [
        item
        for item in {item["evidence_id"]: item for item in stored_evidence + new_evidence}.values()
        if _evidence_datetime(item) >= cutoff
    ]
    rolling_evidence.sort(key=lambda item: (item["content_id"], item["message_id"]))
    for item in rolling_evidence:
        item["retention_deadline"] = (_evidence_datetime(item) + timedelta(days=retention_days)).isoformat().replace(
            "+00:00", "Z"
        )
        item["policy_note"] = (
            "本地授权导出；授权范围与原始文件保留由数据提供方负责。"
            if item.get("source_kind") == "authorized_live_export"
            else "公开 Web 端小流量研究入口；不作为稳定 Open API 或批量再分发授权。"
        )
    candidates = mine_candidates(rolling_evidence, config.get("mining") or {})
    usage_scene_count = sum(len(candidate["observed_usage_scenarios"]) for candidate in candidates)
    candidate_document = {
        "schema_version": 1,
        "pipeline": "p0_meme_discovery_shadow",
        "run_id": run_dir.name,
        "generated_at": _iso(),
        "review_policy": {
            "status": "pending",
            "auto_publish": False,
            "note": "重复表达只是待审信号，不代表已经认定为梗；审核前不得进入运行时 Release。",
        },
        "method_limitations": "当前只按跨内容/内容内重复生成表层信号；普通话、刷屏仪式和引用台词必须人工排除。",
        "usage_scene_policy": {
            "basis": "observed_context_only",
            "auto_semantic_confirmation": False,
            "note": "场景草稿按圈层和来源聚合，并引用真实内容与邻近弹幕；准确梗义和触发条件仍需人工确认。",
        },
        "summary": {
            "fetched_evidence_count": len(fetched_evidence),
            "new_evidence_count": len(new_evidence),
            "rolling_evidence_count": len(rolling_evidence),
            "evidence_retention_days": retention_days,
            "candidate_count": len(candidates),
            "usage_scene_count": usage_scene_count,
        },
        "candidates": candidates,
    }
    source_report = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "generated_at": _iso(),
        "sources": [bilibili_report, room_report, inbox_report],
    }
    _write_jsonl(store_path, rolling_evidence)
    _write_jsonl(run_dir / "evidence.jsonl", new_evidence)
    _write_json(run_dir / "candidates.pending-review.json", candidate_document)
    _write_json(run_dir / "source-report.json", source_report)
    (run_dir / "review-queue.html").write_text(_render_review_html(candidate_document), encoding="utf-8")
    latest = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "pending_review_file": str(run_dir / "candidates.pending-review.json"),
        "review_page": str(run_dir / "review-queue.html"),
        "candidate_count": len(candidates),
        "usage_scene_count": usage_scene_count,
        "fetched_evidence_count": len(fetched_evidence),
        "new_evidence_count": len(new_evidence),
        "rolling_evidence_count": len(rolling_evidence),
    }
    _write_json(output_root / "latest-run.json", latest)
    return latest


def _resolve_path(repo_root: Path, value: str) -> Path:
    path = Path(os.path.expandvars(value))
    return path if path.is_absolute() else repo_root / path


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"滚动证据仓第 {line_number} 行不是有效 JSON: {path}") from exc
        if not isinstance(item, dict) or not item.get("evidence_id"):
            raise ValueError(f"滚动证据仓第 {line_number} 行缺少 evidence_id: {path}")
        result.append(item)
    return result


def _evidence_datetime(item: dict[str, Any]) -> datetime:
    for key in ("observed_at", "collected_at"):
        raw = str(item.get(key) or "").strip()
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return _utc_now()


def _render_review_html(document: dict[str, Any]) -> str:
    rows: list[str] = []
    for candidate in document["candidates"]:
        examples = "".join(
            f"<li><code>{html.escape(str(item['content_id']))}</code> {html.escape(str(item['message']))}</li>"
            for item in candidate["examples"]
        )
        scenes = "".join(_render_scene_html(scene) for scene in candidate["observed_usage_scenarios"])
        signals = candidate["signals"]
        rows.append(
            "<article>"
            f"<h2>{html.escape(candidate['phrase'])}</h2>"
            f"<p>待审 · {html.escape(candidate['why_queued'])} · {signals['message_count']} 条 / "
            f"{signals['distinct_content_count']} 个内容</p>"
            f"<h3>常见使用场景草稿</h3><ol>{scenes}</ol><h3>重复表达样本</h3><ul>{examples}</ul>"
            f"<p><small>{html.escape(candidate['candidate_id'])}</small></p>"
            "</article>"
        )
    body = "".join(rows) or "<p>本次没有达到阈值的候选。</p>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>热梗候选人工审核</title><style>
body{{font:16px/1.6 system-ui,sans-serif;max-width:920px;margin:40px auto;padding:0 20px;color:#202124}}
header{{border-bottom:1px solid #ddd;margin-bottom:24px}}article{{border:1px solid #ddd;border-radius:12px;padding:8px 20px;margin:16px 0}}
code{{color:#666}}small{{color:#777}}
</style></head><body><header><h1>热梗候选人工审核</h1>
<p>运行 {html.escape(document['run_id'])}；滚动窗口共 {document['summary']['rolling_evidence_count']} 条证据，
{document['summary']['candidate_count']} 个候选。所有候选均为 pending，不会自动发布。</p></header>{body}</body></html>"""


def _render_scene_html(scene: dict[str, Any]) -> str:
    contexts: list[str] = []
    for context in scene["representative_contexts"]:
        position = context.get("position_seconds")
        position_text = f" · {position:.1f}s" if isinstance(position, (int, float)) else ""
        nearby = " / ".join(str(item["message"]) for item in context.get("nearby_messages", []))
        nearby_html = f"<br><small>前后文：{html.escape(nearby)}</small>" if nearby else ""
        contexts.append(
            f"<li><code>{html.escape(str(context['content_id']))}{position_text}</code> "
            f"{html.escape(str(context['message']))}{nearby_html}</li>"
        )
    return (
        f"<li><strong>{html.escape(str(scene['draft_description']))}</strong>"
        f"<br><small>{scene['evidence_count']} 条证据 / {scene['distinct_content_count']} 个内容 · "
        f"{html.escape(str(scene['confidence']))} · 待审</small><ul>{''.join(contexts)}</ul></li>"
    )
