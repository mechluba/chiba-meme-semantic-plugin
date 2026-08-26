"""B 站公开视频评论与分段弹幕采集。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request


POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"
PAGELIST_URL = "https://api.bilibili.com/x/player/pagelist"
REPLY_URL = "https://api.bilibili.com/x/v2/reply/main"
DANMAKU_URL = "https://api.bilibili.com/x/v2/dm/web/seg.so"
SEGMENT_SECONDS = 360
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class SourceError(RuntimeError):
    """远端来源返回了不可用响应。"""


@dataclass(frozen=True)
class DanmakuItem:
    message_id: str
    progress_seconds: float
    content: str
    ctime: int | None


class RateLimitedHttpClient:
    """带全局限速和有界重试的轻量 HTTP 客户端。"""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        minimum_interval_seconds: float,
        max_retries: int,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = minimum_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def get(self, url: str, *, referer: str = "https://www.bilibili.com/") -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            wait_seconds = self.minimum_interval_seconds - (time.monotonic() - self._last_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Referer": referer,
                    "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
                },
            )
            try:
                self._last_request_at = time.monotonic()
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise SourceError(f"响应超过 {MAX_RESPONSE_BYTES} 字节限制: {url}")
                    return payload
            except (urllib.error.URLError, TimeoutError, SourceError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 4))
        raise SourceError(f"请求失败: {url}: {last_error}")

    def get_json(self, url: str) -> dict[str, Any]:
        try:
            value = json.loads(self.get(url).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"远端未返回有效 JSON: {url}") from exc
        if not isinstance(value, dict):
            raise SourceError(f"远端 JSON 根节点不是对象: {url}")
        if value.get("code") not in (None, 0):
            raise SourceError(f"远端接口错误 code={value.get('code')}: {value.get('message')}")
        return value


def _url(base: str, **params: Any) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def fetch_popular_videos(client: RateLimitedHttpClient, config: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = {str(item).strip() for item in config.get("allowed_categories", []) if str(item).strip()}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, int(config.get("pages", 1)) + 1):
        payload = client.get_json(
            _url(POPULAR_URL, pn=page, ps=min(int(config.get("page_size", 20)), 50))
        )
        items = ((payload.get("data") or {}).get("list") or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid") or "").strip()
            category_values = [
                str(item.get("tname") or "").strip(),
                str(item.get("tname_v2") or "").strip(),
                str(item.get("pid_name_v2") or "").strip(),
            ]
            if not bvid or bvid in seen or (allowed and allowed.isdisjoint(set(category_values))):
                continue
            seen.add(bvid)
            result.append(
                {
                    "bvid": bvid,
                    "aid": int(item.get("aid") or 0),
                    "title": str(item.get("title") or "").strip(),
                    "creator": str((item.get("owner") or {}).get("name") or "").strip(),
                    "circle": next((value for value in category_values if value in allowed), "B站热门"),
                    "discovery_source": "bilibili_popular",
                }
            )
            if len(result) >= int(config.get("max_videos", 8)):
                return result
    return result


def fetch_pagelist(client: RateLimitedHttpClient, bvid: str) -> list[dict[str, Any]]:
    payload = client.get_json(_url(PAGELIST_URL, bvid=bvid, jsonp="jsonp"))
    items = payload.get("data") or []
    return [item for item in items if isinstance(item, dict) and int(item.get("cid") or 0) > 0]


def fetch_comments(
    client: RateLimitedHttpClient,
    *,
    aid: int,
    max_comments: int,
) -> list[dict[str, Any]]:
    if aid <= 0 or max_comments <= 0:
        return []
    payload = client.get_json(_url(REPLY_URL, type=1, oid=aid, mode=3, next=0, ps=min(max_comments, 49)))
    replies = ((payload.get("data") or {}).get("replies") or [])
    result: list[dict[str, Any]] = []
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        content = str((reply.get("content") or {}).get("message") or "").strip()
        message_id = str(reply.get("rpid_str") or reply.get("rpid") or "").strip()
        if content and message_id:
            result.append(
                {
                    "message_id": message_id,
                    "content": content,
                    "observed_unix": int(reply.get("ctime") or 0) or None,
                }
            )
        if len(result) >= max_comments:
            break
    return result


def fetch_danmaku_segment(
    client: RateLimitedHttpClient,
    *,
    cid: int,
    segment_index: int,
) -> list[DanmakuItem]:
    payload = client.get(_url(DANMAKU_URL, type=1, oid=cid, segment_index=segment_index))
    return parse_danmaku_reply(payload, segment_index=segment_index)


def segment_count(duration_seconds: int, max_segments: int) -> int:
    return min(max(1, math.ceil(max(duration_seconds, 1) / SEGMENT_SECONDS)), max_segments)


def parse_danmaku_reply(payload: bytes, *, segment_index: int) -> list[DanmakuItem]:
    if segment_index < 1:
        raise ValueError("B站弹幕分段序号必须大于 0")
    items: list[DanmakuItem] = []
    for field_number, _, value in _iter_fields(payload):
        if field_number != 1 or value is None:
            continue
        scalars: dict[int, int] = {}
        strings: dict[int, bytes] = {}
        for elem_number, scalar, elem_value in _iter_fields(value):
            if elem_value is None:
                scalars[elem_number] = scalar
            else:
                strings[elem_number] = elem_value
        progress_ms = scalars.get(2, -1)
        mode = scalars.get(3, 1)
        if progress_ms < 0 or mode < 1 or mode > 6:
            continue
        try:
            content = strings.get(7, b"").decode("utf-8", errors="strict").replace("\x00", "")
            message_id = strings.get(12, b"").decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise SourceError("B站弹幕包含无效 UTF-8") from exc
        content = " ".join(content.split()).strip()
        if not message_id:
            message_id = str(scalars.get(1, 0))
        if not content or len(content) > 120 or message_id == "0":
            continue
        min_seconds = max(0.0, (segment_index - 1) * SEGMENT_SECONDS - 1.0)
        max_seconds = segment_index * SEGMENT_SECONDS + 1.0
        progress_seconds = progress_ms / 1000.0
        if min_seconds <= progress_seconds <= max_seconds:
            items.append(
                DanmakuItem(
                    message_id=message_id,
                    progress_seconds=progress_seconds,
                    content=content,
                    ctime=scalars.get(8),
                )
            )
    return items


def _iter_fields(payload: bytes) -> Iterator[tuple[int, int, bytes | None]]:
    offset = 0
    while offset < len(payload):
        tag, offset = _read_varint(payload, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number <= 0:
            raise SourceError("B站弹幕 Protobuf 字段编号无效")
        if wire_type == 0:
            scalar, offset = _read_varint(payload, offset)
            yield field_number, scalar, None
        elif wire_type == 1:
            offset = _checked_skip(payload, offset, 8)
            yield field_number, 0, None
        elif wire_type == 2:
            size, offset = _read_varint(payload, offset)
            end = _checked_skip(payload, offset, size)
            yield field_number, 0, payload[offset:end]
            offset = end
        elif wire_type == 5:
            offset = _checked_skip(payload, offset, 4)
            yield field_number, 0, None
        else:
            raise SourceError(f"B站弹幕 Protobuf wire type {wire_type} 不受支持")


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(payload):
            raise SourceError("B站弹幕 Protobuf varint 被截断")
        current = payload[offset]
        offset += 1
        value |= (current & 0x7F) << shift
        if current < 0x80:
            return value, offset
    raise SourceError("B站弹幕 Protobuf varint 过长")


def _checked_skip(payload: bytes, offset: int, size: int) -> int:
    end = offset + size
    if size < 0 or end > len(payload):
        raise SourceError("B站弹幕 Protobuf 字段被截断")
    return end
