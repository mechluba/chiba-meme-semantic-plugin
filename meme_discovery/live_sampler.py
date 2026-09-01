"""直播间公开网页弹幕的短时、匿名化抽样与本地归档。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from zoneinfo import ZoneInfo

import brotli
import websockets


class LiveSamplingError(RuntimeError):
    """直播间抽样无法安全继续。"""


MAX_DECOMPRESSED_FRAME_BYTES = 16 * 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact_content(content: str) -> str:
    normalized = " ".join(content.replace("\x00", " ").split()).strip()
    return re.sub(r"[@＠][^\s@＠]{1,32}", "@用户", normalized)


def decode_douyu_packets(payload: bytes) -> list[dict[str, str]]:
    """解码斗鱼网页 WebSocket 中的 STT 消息；只返回键值，不保留原始帧。"""
    records: list[dict[str, str]] = []
    offset = 0
    while len(payload) - offset >= 12:
        packet_length = struct.unpack_from("<I", payload, offset)[0]
        packet_end = offset + packet_length + 4
        if packet_length < 9 or packet_end > len(payload):
            break
        body = payload[offset + 12 : packet_end - 1].rstrip(b"\x00")
        try:
            text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            offset = packet_end
            continue
        record: dict[str, str] = {}
        for field in text.split("/"):
            if "@=" not in field:
                continue
            key, value = field.split("@=", 1)
            if key:
                record[key] = value.replace("@S", "/").replace("@A", "@")
        if record:
            records.append(record)
        offset = packet_end
    return records


def douyu_record_to_message(
    room: dict[str, Any],
    record: dict[str, str],
    *,
    collected_at: datetime,
) -> dict[str, Any] | None:
    if record.get("type") not in {"chatmsg", "comm_chatmsg"}:
        return None
    content = _redact_content(record.get("txt") or "")
    if not content or len(content) > 200:
        return None
    observed_at = collected_at
    raw_timestamp = str(record.get("cst") or "")
    if raw_timestamp.isdigit():
        try:
            parsed = datetime.fromtimestamp(int(raw_timestamp) / 1000, timezone.utc)
            if abs((parsed - collected_at).total_seconds()) <= 86_400:
                observed_at = parsed
        except (OverflowError, OSError, ValueError):
            pass
    room_id = str(room.get("room_id") or "").strip()
    stable_id = str(record.get("cid") or "").strip()
    if not stable_id:
        raw_id = "\x1f".join(("douyu", room_id, _iso(observed_at), content))
        stable_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]
    return {
        "platform": "douyu",
        "source_kind": "public_live_sample",
        "room_id": room_id,
        "session_id": "",
        "message_id": stable_id,
        "content": content,
        "observed_at": _iso(observed_at),
        "room_title": str(room.get("label") or ""),
        "circle": str(room.get("circle") or "直播间"),
    }


def decode_bilibili_packets(payload: bytes, *, _depth: int = 0) -> list[dict[str, Any]]:
    """解码 B 站直播 WebSocket 包，包括 zlib / Brotli 嵌套包。"""
    if _depth > 3:
        return []
    events: list[dict[str, Any]] = []
    offset = 0
    while len(payload) - offset >= 16:
        packet_length, header_length, version, operation, _ = struct.unpack_from(">IHHII", payload, offset)
        packet_end = offset + packet_length
        if packet_length < 16 or header_length < 16 or packet_end > len(payload):
            break
        body = payload[offset + header_length : packet_end]
        offset = packet_end
        if version in {2, 3}:
            try:
                unpacked = zlib.decompress(body) if version == 2 else brotli.decompress(body)
            except (zlib.error, brotli.error):
                continue
            if len(unpacked) <= MAX_DECOMPRESSED_FRAME_BYTES:
                events.extend(decode_bilibili_packets(unpacked, _depth=_depth + 1))
            continue
        if operation != 5:
            continue
        try:
            event = json.loads(body.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def bilibili_event_to_message(
    room: dict[str, Any],
    event: dict[str, Any],
    *,
    collected_at: datetime,
) -> dict[str, Any] | None:
    if str(event.get("cmd") or "").split(":", 1)[0] != "DANMU_MSG":
        return None
    info = event.get("info") or []
    if not isinstance(info, list) or len(info) < 2:
        return None
    content = _redact_content(str(info[1] or ""))
    if not content or len(content) > 200:
        return None
    observed_at = collected_at
    metadata = info[0] if isinstance(info[0], list) else []
    raw_timestamp = metadata[4] if len(metadata) > 4 else None
    try:
        parsed = datetime.fromtimestamp(int(raw_timestamp) / 1000, timezone.utc)
        if abs((parsed - collected_at).total_seconds()) <= 86_400:
            observed_at = parsed
    except (TypeError, ValueError, OverflowError, OSError):
        pass

    extra: dict[str, Any] = {}
    if len(info) > 9 and isinstance(info[9], str):
        try:
            parsed_extra = json.loads(info[9])
            if isinstance(parsed_extra, dict):
                extra = parsed_extra
        except json.JSONDecodeError:
            pass
    room_id = str(room.get("room_id") or "").strip()
    stable_id = str(extra.get("id_str") or extra.get("id") or "").strip()
    if not stable_id:
        raw_id = "\x1f".join(("bilibili", room_id, _iso(observed_at), content))
        stable_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:32]
    return {
        "platform": "bilibili",
        "source_kind": "public_live_sample",
        "room_id": room_id,
        "session_id": "",
        "message_id": stable_id,
        "content": content,
        "observed_at": _iso(observed_at),
        "room_title": str(room.get("label") or ""),
        "circle": str(room.get("circle") or "B站直播"),
    }


def _find_chrome(configured_path: str) -> str:
    candidates = [
        os.path.expandvars(configured_path),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise LiveSamplingError("没有找到可用于公开网页抽样的 Chrome/Chromium")


def _read_json_url(url: str, *, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))


async def _wait_for_cdp_target(profile_dir: Path, *, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    active_port = profile_dir / "DevToolsActivePort"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            lines = active_port.read_text(encoding="utf-8").splitlines()
            port = int(lines[0])
            targets = await asyncio.to_thread(
                _read_json_url,
                f"http://127.0.0.1:{port}/json/list",
                timeout=1.0,
            )
            page = next(item for item in targets if item.get("type") == "page")
            return port, page
        except (OSError, ValueError, StopIteration, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            await asyncio.sleep(0.2)
    raise LiveSamplingError(f"Chrome 调试端口在 {timeout_seconds:g} 秒内未就绪: {last_error}")


def _chrome_command(chrome_path: str, profile_dir: Path, url: str) -> list[str]:
    return [
        chrome_path,
        "--headless=new",
        "--disable-gpu",
        "--disable-default-apps",
        "--disable-background-networking",
        "--no-first-run",
        "--no-default-browser-check",
        "--mute-audio",
        "--remote-allow-origins=*",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        url,
    ]


async def sample_douyu_room(
    room: dict[str, Any],
    *,
    chrome_path: str,
    duration_seconds: float,
    max_messages: int,
    startup_timeout_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    room_id = str(room.get("room_id") or "").strip()
    url = str(room.get("url") or f"https://www.douyu.com/{room_id}")
    profile_dir = Path(tempfile.mkdtemp(prefix=f"chiba-live-douyu-{room_id}-"))
    process = subprocess.Popen(
        _chrome_command(chrome_path, profile_dir, url),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started_at = _utc_now()
    messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    websocket_urls: set[str] = set()
    report: dict[str, Any] = {
        "platform": "douyu",
        "room_id": room_id,
        "label": room.get("label"),
        "mode": "sampled_public_web_client",
        "started_at": _iso(started_at),
        "sample_duration_seconds": duration_seconds,
        "max_messages": max_messages,
        "chat_collected": False,
    }
    try:
        _, page = await _wait_for_cdp_target(profile_dir, timeout_seconds=startup_timeout_seconds)
        debugger_url = str(page.get("webSocketDebuggerUrl") or "")
        if not debugger_url.startswith("ws://"):
            raise LiveSamplingError("Chrome 页面没有可用的本地调试 WebSocket")
        async with websockets.connect(debugger_url, max_size=8 * 1024 * 1024) as connection:
            await connection.send(json.dumps({"id": 1, "method": "Network.enable"}))
            await connection.send(
                json.dumps({"id": 2, "method": "Page.reload", "params": {"ignoreCache": True}})
            )
            deadline = time.monotonic() + duration_seconds
            while time.monotonic() < deadline and len(messages) < max_messages:
                try:
                    event = json.loads(await asyncio.wait_for(connection.recv(), timeout=2.0))
                except asyncio.TimeoutError:
                    continue
                if event.get("method") == "Network.webSocketCreated":
                    event_url = str((event.get("params") or {}).get("url") or "")
                    if event_url:
                        websocket_urls.add(event_url)
                    continue
                if event.get("method") != "Network.webSocketFrameReceived":
                    continue
                response = ((event.get("params") or {}).get("response") or {})
                raw_payload = response.get("payloadData") or ""
                try:
                    payload = base64.b64decode(raw_payload) if int(response.get("opcode") or 1) == 2 else raw_payload.encode()
                except (ValueError, TypeError):
                    continue
                for record in decode_douyu_packets(payload):
                    message = douyu_record_to_message(room, record, collected_at=_utc_now())
                    if not message or message["message_id"] in seen_ids:
                        continue
                    seen_ids.add(message["message_id"])
                    messages.append(message)
                    if len(messages) >= max_messages:
                        break
        report.update(
            {
                "status": "ok" if messages else "no_messages_observed",
                "chat_collected": bool(messages),
                "message_count": len(messages),
                "public_chat_stream_seen": any("douyu.com" in item for item in websocket_urls),
                "websocket_count": len(websocket_urls),
            }
        )
    except Exception as exc:  # 单个房间失败必须进入归档，不能拖垮其他房间。
        report.update({"status": "error", "message_count": len(messages), "error": str(exc)})
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(profile_dir, ignore_errors=True)
    report["finished_at"] = _iso()
    return messages, report


async def sample_bilibili_room(
    room: dict[str, Any],
    *,
    chrome_path: str,
    duration_seconds: float,
    max_messages: int,
    startup_timeout_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    room_id = str(room.get("room_id") or "").strip()
    url = str(room.get("url") or f"https://live.bilibili.com/{room_id}")
    started_at = _utc_now()
    report: dict[str, Any] = {
        "platform": "bilibili",
        "room_id": room_id,
        "label": room.get("label"),
        "mode": "sampled_public_web_client",
        "started_at": _iso(started_at),
        "sample_duration_seconds": duration_seconds,
        "max_messages": max_messages,
        "chat_collected": False,
    }
    try:
        metadata = await asyncio.to_thread(
            _read_json_url,
            f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}",
            timeout=5.0,
        )
        data = metadata.get("data") or {}
        if metadata.get("code") == 0:
            report.update(
                {
                    "resolved_room_id": str(data.get("room_id") or room_id),
                    "live_status": int(data.get("live_status") or 0),
                    "current_title": str(data.get("title") or ""),
                }
            )
            if report["live_status"] == 0:
                report.update(
                    {
                        "status": "offline",
                        "message_count": 0,
                        "finished_at": _iso(),
                    }
                )
                return [], report
    except (OSError, TypeError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        report["metadata_status"] = "unavailable"

    profile_dir = Path(tempfile.mkdtemp(prefix=f"chiba-live-bilibili-{room_id}-"))
    process = subprocess.Popen(
        _chrome_command(chrome_path, profile_dir, url),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    websocket_urls: set[str] = set()
    try:
        _, page = await _wait_for_cdp_target(profile_dir, timeout_seconds=startup_timeout_seconds)
        debugger_url = str(page.get("webSocketDebuggerUrl") or "")
        if not debugger_url.startswith("ws://"):
            raise LiveSamplingError("Chrome 页面没有可用的本地调试 WebSocket")
        async with websockets.connect(debugger_url, max_size=8 * 1024 * 1024) as connection:
            await connection.send(json.dumps({"id": 1, "method": "Network.enable"}))
            await connection.send(
                json.dumps({"id": 2, "method": "Page.reload", "params": {"ignoreCache": True}})
            )
            deadline = time.monotonic() + duration_seconds
            while time.monotonic() < deadline and len(messages) < max_messages:
                try:
                    event = json.loads(await asyncio.wait_for(connection.recv(), timeout=2.0))
                except asyncio.TimeoutError:
                    continue
                if event.get("method") == "Network.webSocketCreated":
                    event_url = str((event.get("params") or {}).get("url") or "")
                    if event_url:
                        websocket_urls.add(event_url)
                    continue
                if event.get("method") != "Network.webSocketFrameReceived":
                    continue
                response = ((event.get("params") or {}).get("response") or {})
                raw_payload = response.get("payloadData") or ""
                try:
                    payload = (
                        base64.b64decode(raw_payload)
                        if int(response.get("opcode") or 1) == 2
                        else raw_payload.encode()
                    )
                except (ValueError, TypeError):
                    continue
                for decoded_event in decode_bilibili_packets(payload):
                    message = bilibili_event_to_message(room, decoded_event, collected_at=_utc_now())
                    if not message or message["message_id"] in seen_ids:
                        continue
                    seen_ids.add(message["message_id"])
                    messages.append(message)
                    if len(messages) >= max_messages:
                        break
        report.update(
            {
                "status": "ok" if messages else "no_messages_observed",
                "chat_collected": bool(messages),
                "message_count": len(messages),
                "public_chat_stream_seen": any("chat.bilibili.com" in item for item in websocket_urls),
                "websocket_count": len(websocket_urls),
            }
        )
    except Exception as exc:  # 单个房间失败必须进入归档，不能拖垮其他房间。
        report.update({"status": "error", "message_count": len(messages), "error": str(exc)})
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        shutil.rmtree(profile_dir, ignore_errors=True)
    report["finished_at"] = _iso()
    return messages, report


def _preferred_now(room: dict[str, Any], run_at: datetime) -> bool:
    timezone_name = str(room.get("schedule_timezone") or "Asia/Shanghai")
    try:
        local = run_at.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        local = run_at.astimezone(timezone.utc)
    weekdays = {int(value) for value in room.get("preferred_weekdays") or []}
    hours = {int(value) for value in room.get("preferred_local_hours") or []}
    return (not weekdays or local.weekday() in weekdays) and (not hours or local.hour in hours)


def _select_rooms(
    rooms: list[dict[str, Any]],
    *,
    max_rooms: int,
    run_at: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enabled = [room for room in rooms if room.get("enabled", True)]
    required = [room for room in enabled if room.get("always_sample")]
    rotating = [room for room in enabled if not room.get("always_sample")]
    selected = required[:max_rooms]
    capacity = max(0, max_rooms - len(selected))
    if rotating and capacity:
        start = int(run_at.timestamp() // 7200) % len(rotating)
        rotated = (rotating + rotating)[start : start + len(rotating)]
        ranked = [room for room in rotated if _preferred_now(room, run_at)] + [
            room for room in rotated if not _preferred_now(room, run_at)
        ]
        used_buckets = {str(room.get("sampling_bucket") or "") for room in selected}
        for prefer_new_bucket in (True, False):
            for room in ranked:
                if room in selected:
                    continue
                bucket = str(room.get("sampling_bucket") or room.get("circle") or room.get("platform") or "other")
                if prefer_new_bucket and bucket in used_buckets:
                    continue
                selected.append(room)
                used_buckets.add(bucket)
                if len(selected) >= max_rooms:
                    break
            if len(selected) >= max_rooms:
                break
    selected_ids = {(str(item.get("platform")), str(item.get("room_id"))) for item in selected}
    skipped = [
        room
        for room in enabled
        if (str(room.get("platform")), str(room.get("room_id"))) not in selected_ids
    ]
    return selected, skipped


async def _sample_selected_rooms(
    rooms: list[dict[str, Any]],
    *,
    chrome_path: str,
    duration_seconds: float,
    max_messages: int,
    startup_timeout_seconds: float,
) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
    tasks = []
    immediate: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    for room in rooms:
        platform = str(room.get("platform") or "")
        if platform == "douyu":
            tasks.append(
                sample_douyu_room(
                    room,
                    chrome_path=chrome_path,
                    duration_seconds=duration_seconds,
                    max_messages=max_messages,
                    startup_timeout_seconds=startup_timeout_seconds,
                )
            )
        elif platform == "bilibili":
            tasks.append(
                sample_bilibili_room(
                    room,
                    chrome_path=chrome_path,
                    duration_seconds=duration_seconds,
                    max_messages=max_messages,
                    startup_timeout_seconds=startup_timeout_seconds,
                )
            )
        elif platform == "huya":
            credential_names = ("HUYA_OPEN_APP_ID", "HUYA_OPEN_SECRET")
            missing = [name for name in credential_names if not os.environ.get(name)]
            immediate.append(
                (
                    [],
                    {
                        "platform": platform,
                        "room_id": str(room.get("room_id") or ""),
                        "label": room.get("label"),
                        "mode": "official_barrage_api",
                        "status": "missing_credentials" if missing else "adapter_not_configured",
                        "chat_collected": False,
                        "message_count": 0,
                        "missing_environment_variables": missing,
                        "note": "虎牙官方弹幕接口需要 appId 和签名密钥；任务不会绕过官方鉴权。",
                    },
                )
            )
        else:
            immediate.append(
                (
                    [],
                    {
                        "platform": platform,
                        "room_id": str(room.get("room_id") or ""),
                        "label": room.get("label"),
                        "status": "unsupported_platform",
                        "chat_collected": False,
                        "message_count": 0,
                    },
                )
            )
    sampled = await asyncio.gather(*tasks) if tasks else []
    return list(sampled) + immediate


def run_live_sampling(config: dict[str, Any], *, repo_root: Path) -> dict[str, Any]:
    if config.get("schema_version") != 1:
        raise ValueError("只支持 schema_version=1 的直播抽样配置")
    run_at = _utc_now()
    output_root = _resolve_path(repo_root, str(config.get("output_root") or "out/p0-meme-discovery"))
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = run_at.strftime("%Y%m%dT%H%M%SZ")
    archive_dir = output_root / "live-archive" / run_at.strftime("%Y-%m-%d") / run_id
    suffix = 1
    while archive_dir.exists():
        archive_dir = archive_dir.with_name(f"{run_id}-{suffix}")
        suffix += 1
    archive_dir.mkdir(parents=True)

    sampling = config.get("sampling") or {}
    rooms = [item for item in config.get("rooms") or [] if isinstance(item, dict)]
    selected, skipped = _select_rooms(
        rooms,
        max_rooms=max(1, int(sampling.get("max_rooms_per_run", 2))),
        run_at=run_at,
    )
    needs_chrome = any(str(room.get("platform")) in {"douyu", "bilibili"} for room in selected)
    chrome_path = _find_chrome(str(config.get("chrome_path") or "")) if needs_chrome else ""
    results = asyncio.run(
        _sample_selected_rooms(
            selected,
            chrome_path=chrome_path,
            duration_seconds=max(5.0, float(sampling.get("duration_seconds", 45))),
            max_messages=max(1, int(sampling.get("max_messages_per_room", 200))),
            startup_timeout_seconds=max(5.0, float(sampling.get("startup_timeout_seconds", 20))),
        )
    )
    messages: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for room_messages, room_report in results:
        for message in room_messages:
            message["session_id"] = f"live:{archive_dir.name}:{message['platform']}:{message['room_id']}"
        messages.extend(room_messages)
        reports.append(room_report)
    reports.extend(
        {
            "platform": room.get("platform"),
            "room_id": str(room.get("room_id") or ""),
            "label": room.get("label"),
            "status": "skipped_rotation",
            "chat_collected": False,
            "message_count": 0,
        }
        for room in skipped
    )
    messages.sort(key=lambda item: (item["platform"], item["room_id"], item["observed_at"], item["message_id"]))
    report_document = {
        "schema_version": 1,
        "run_id": archive_dir.name,
        "generated_at": _iso(),
        "policy": {
            "sampling_only": True,
            "local_only": True,
            "identity_fields_stored": False,
            "auto_publish": False,
        },
        "summary": {
            "configured_room_count": len(rooms),
            "selected_room_count": len(selected),
            "message_count": len(messages),
            "rooms_with_messages": sum(1 for item in reports if item.get("chat_collected")),
        },
        "rooms": sorted(reports, key=lambda item: (str(item.get("platform")), str(item.get("room_id")))),
    }
    _write_jsonl(archive_dir / "messages.jsonl", messages)
    _write_json(archive_dir / "sampling-report.json", report_document)
    inbox_path = output_root / "inbox" / f"live-{archive_dir.name}.jsonl"
    _write_jsonl(inbox_path, messages)
    latest = {
        "run_id": archive_dir.name,
        "archive_dir": str(archive_dir),
        "messages_file": str(archive_dir / "messages.jsonl"),
        "sampling_report": str(archive_dir / "sampling-report.json"),
        "inbox_file": str(inbox_path),
        "message_count": len(messages),
        "rooms_with_messages": report_document["summary"]["rooms_with_messages"],
    }
    _write_json(output_root / "latest-live-sampling.json", latest)
    return latest


def _resolve_path(repo_root: Path, value: str) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
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
