"""为人工保留的梗候选检索公开互联网解释与近期用法。"""

from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import concurrent.futures
import hashlib
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .miner import normalize_expression


RESEARCH_VERSION = "meme-web-research-v3-question-query"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
)


class WebResearchError(RuntimeError):
    """单个公开检索源不可用。"""


class RateLimitedResearchClient:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 20,
        minimum_interval_seconds: float = 0.65,
        max_retries: int = 2,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = minimum_interval_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._lock = threading.Lock()

    def get_text(self, url: str, *, referer: str | None = None) -> str:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            with self._lock:
                wait = self.minimum_interval_seconds - (time.monotonic() - self._last_request_at)
                if wait > 0:
                    time.sleep(wait)
                self._last_request_at = time.monotonic()
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            if referer:
                headers["Referer"] = referer
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise WebResearchError("响应体超过限制")
                    charset = response.headers.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
            except (urllib.error.URLError, TimeoutError, OSError, WebResearchError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 4))
        raise WebResearchError(f"检索失败: {last_error}")

    def get_json(self, url: str, *, referer: str | None = None) -> dict[str, Any]:
        try:
            value = json.loads(self.get_text(url, referer=referer))
        except json.JSONDecodeError as exc:
            raise WebResearchError("检索源未返回 JSON") from exc
        if not isinstance(value, dict):
            raise WebResearchError("检索源 JSON 根节点不是对象")
        return value


def research_reviewed_groups(
    document: dict[str, Any],
    config: dict[str, Any],
    *,
    cache_dir: Path,
    fetchers: dict[str, Callable[[str], list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """逐组检索，保留短摘要和 URL；单源失败不会丢弃整组。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    source_names = list(config.get("sources") or ["gengwh", "moyu", "bilibili"])
    workers = max(1, min(int(config.get("workers", 4)), 6))
    maximum_results = max(1, min(int(config.get("max_results_per_item", 8)), 16))
    clients = {
        name: RateLimitedResearchClient(
            timeout_seconds=float(config.get("timeout_seconds", 20)),
            minimum_interval_seconds=float(config.get("minimum_interval_seconds", 0.65)),
            max_retries=int(config.get("max_retries", 2)),
        )
        for name in source_names
    }
    active_fetchers = fetchers or {
        "gengwh": lambda value: search_gengwh(clients["gengwh"], value),
        "moyu": lambda value: search_moyu(clients["moyu"], value),
        "bilibili": lambda value: search_bilibili(clients["bilibili"], value),
        "bing": lambda value: search_bing(clients["bing"], value),
        "so_qa": lambda value: search_so_question(clients["so_qa"], value),
    }
    retrieved_at = datetime.now(timezone.utc).isoformat()

    def process(item: dict[str, Any]) -> dict[str, Any]:
        expressions = _search_expressions(item)
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for expression in expressions:
            for source in source_names:
                if source not in active_fetchers:
                    errors.append({"source": source, "query": expression, "error": "没有对应检索器"})
                    continue
                search_query = _source_query(source, expression, item)
                cache_key = hashlib.sha256(
                    f"{RESEARCH_VERSION}\n{source}\n{search_query}".encode("utf-8")
                ).hexdigest()
                cache_path = cache_dir / f"{cache_key}.json"
                try:
                    if cache_path.exists():
                        source_results = json.loads(cache_path.read_text(encoding="utf-8"))["results"]
                    else:
                        source_results = active_fetchers[source](search_query)
                        _write_json(
                            cache_path,
                            {
                                "research_version": RESEARCH_VERSION,
                                "source": source,
                                "expression": expression,
                                "query": search_query,
                                "retrieved_at": retrieved_at,
                                "results": source_results,
                            },
                        )
                    for row in source_results:
                        row.setdefault("matched_query", search_query)
                    results.extend(source_results)
                except (WebResearchError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    errors.append({"source": source, "query": search_query, "error": str(exc)})
        results = _deduplicate_and_rank(results, expressions[0])[:maximum_results]
        return {
            "research_version": RESEARCH_VERSION,
            "queries": expressions,
            "question_queries": [
                _source_query("so_qa", expression, item)
                for expression in expressions
                if "so_qa" in source_names
            ],
            "retrieved_at": retrieved_at,
            "status": _research_status(results),
            "freshness": _freshness(results, retrieved_at),
            "provider_count": len({row["provider"] for row in results}),
            "result_count": len(results),
            "results": results,
            "errors": errors,
        }

    items = document.get("items") or []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_items = {executor.submit(process, item): item for item in items}
        for future in concurrent.futures.as_completed(future_items):
            item = future_items[future]
            item["web_research"] = future.result()
            completed += 1
            print(
                f"[{completed}/{len(items)}] research "
                f"{item.get('canonical_expression') or item.get('phrase')}: "
                f"{item['web_research']['status']} / {item['web_research']['result_count']}",
                flush=True,
            )
    status_counts: dict[str, int] = {}
    provider_hits: dict[str, int] = {}
    for item in items:
        research = item.get("web_research") or {}
        status = str(research.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        for provider in {row.get("provider") for row in research.get("results") or []}:
            if provider:
                provider_hits[str(provider)] = provider_hits.get(str(provider), 0) + 1
    document["web_research_report"] = {
        "status": "ok",
        "research_version": RESEARCH_VERSION,
        "retrieved_at": retrieved_at,
        "item_count": len(items),
        "status_counts": status_counts,
        "provider_item_hits": provider_hits,
        "result_count": sum((item.get("web_research") or {}).get("result_count", 0) for item in items),
    }
    return document


def research_candidates(
    candidates: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    cache_dir: Path,
    fetchers: dict[str, Callable[[str], list[dict[str, Any]]]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """在语义提炼前检索候选表达；超出预算的候选留待后续批次。"""
    if not config.get("enabled", False):
        for candidate in candidates:
            candidate["web_research"] = {
                "status": "disabled",
                "research_version": RESEARCH_VERSION,
                "result_count": 0,
                "results": [],
                "errors": [],
            }
        return candidates, {
            "status": "disabled",
            "research_version": RESEARCH_VERSION,
            "candidate_count": len(candidates),
            "selected_count": 0,
            "result_count": 0,
        }

    maximum_candidates = max(0, int(config.get("max_candidates_per_run", 0)))
    selected_count = (
        len(candidates)
        if maximum_candidates <= 0
        else min(maximum_candidates, len(candidates))
    )
    selected = candidates[:selected_count]
    for candidate in candidates[selected_count:]:
        candidate["web_research"] = {
            "status": "deferred",
            "reason": "max_candidates_per_run",
            "research_version": RESEARCH_VERSION,
            "result_count": 0,
            "results": [],
            "errors": [],
        }

    document = {"items": selected}
    research_reviewed_groups(
        document,
        config,
        cache_dir=cache_dir,
        fetchers=fetchers,
    )
    report = dict(document["web_research_report"])
    report.update(
        {
            "candidate_count": len(candidates),
            "selected_count": selected_count,
            "deferred_count": len(candidates) - selected_count,
        }
    )
    return candidates, report


def search_gengwh(client: RateLimitedResearchClient, expression: str) -> list[dict[str, Any]]:
    url = "https://www.gengwh.com/operate/search?" + urllib.parse.urlencode(
        {"range": 1, "keyword": expression}
    )
    body = client.get_text(url, referer="https://www.gengwh.com/")
    blocks = re.findall(
        r'<div class="search-result-item">(.*?)(?=<div class="search-result-item">|</div>\s*</div>\s*\ufeff)',
        body,
        flags=re.DOTALL,
    )
    results: list[dict[str, Any]] = []
    for block in blocks[:4]:
        link = re.search(r'class="result-title"><a href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        excerpt = re.search(r'class="result-excerpt">(.*?)</div>', block, re.DOTALL)
        date = re.search(r'<span>📅\s*(.*?)</span>', block, re.DOTALL)
        if not link:
            continue
        results.append(
            _result(
                provider="gengwh",
                source_kind="meme_encyclopedia",
                title=_clean_html(link.group(2)),
                url=urllib.parse.urljoin("https://www.gengwh.com/", html.unescape(link.group(1))),
                snippet=_clean_html(excerpt.group(1) if excerpt else ""),
                published_at=_relative_date(date.group(1) if date else ""),
                source_tier="community_encyclopedia",
            )
        )
    return _relevant(results, expression)


def search_moyu(client: RateLimitedResearchClient, expression: str) -> list[dict[str, Any]]:
    url = "https://www.moyuoo.com/?" + urllib.parse.urlencode({"s": expression, "type": "post"})
    body = client.get_text(url, referer="https://www.moyuoo.com/")
    blocks = re.findall(
        r'<li class="post-list-item[^>]*>(.*?)(?=<li class="post-list-item|</ul>)',
        body,
        flags=re.DOTALL,
    )
    results: list[dict[str, Any]] = []
    for block in blocks[:4]:
        link = re.search(r'<h2><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>', block, re.DOTALL)
        excerpt = re.search(r'class="post-excerpt">(.*?)</div>', block, re.DOTALL)
        date = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        if not link:
            continue
        results.append(
            _result(
                provider="moyu",
                source_kind="meme_encyclopedia",
                title=_clean_html(link.group(2)),
                url=html.unescape(link.group(1)),
                snippet=_clean_html(excerpt.group(1) if excerpt else ""),
                published_at=_iso_date(date.group(1) if date else ""),
                source_tier="community_encyclopedia",
            )
        )
    return _relevant(results, expression)


def search_bilibili(client: RateLimitedResearchClient, expression: str) -> list[dict[str, Any]]:
    url = "https://api.bilibili.com/x/web-interface/search/all/v2?" + urllib.parse.urlencode(
        {
            "keyword": expression + " 梗",
            "page": 1,
            "page_size": 8,
        }
    )
    payload = client.get_json(url, referer="https://search.bilibili.com/")
    if payload.get("code") != 0:
        raise WebResearchError(f"B站搜索返回 code={payload.get('code')}")
    groups = ((payload.get("data") or {}).get("result") or [])
    video_group = next(
        (group for group in groups if isinstance(group, dict) and group.get("result_type") == "video"),
        {},
    )
    results: list[dict[str, Any]] = []
    for row in (video_group.get("data") or [])[:12]:
        if not isinstance(row, dict) or not row.get("bvid"):
            continue
        published = datetime.fromtimestamp(int(row.get("pubdate") or 0), timezone.utc).isoformat()
        results.append(
            _result(
                provider="bilibili",
                source_kind="recent_video_usage",
                title=_clean_html(str(row.get("title") or "")),
                url=f"https://www.bilibili.com/video/{row['bvid']}/",
                snippet=_clean_html(str(row.get("description") or "")),
                published_at=published,
                source_tier="contemporaneous_platform_usage",
                extra={"bvid": str(row["bvid"]), "creator": str(row.get("author") or "")},
            )
        )
    return _relevant(results, expression)[:4]


def search_bing(client: RateLimitedResearchClient, expression: str) -> list[dict[str, Any]]:
    query = f'"{expression}" 什么梗 OR 来源 OR 名场面'
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query, "count": 8})
    body = client.get_text(url, referer="https://cn.bing.com/")
    parser = _BingResultParser()
    parser.feed(body)
    results: list[dict[str, Any]] = []
    for row in parser.results[:8]:
        domain = urllib.parse.urlparse(row["url"]).netloc.lower()
        kind = "indexed_web_explanation"
        tier = "indexed_web"
        if "bilibili.com" in domain:
            kind, tier = "recent_video_usage", "contemporaneous_platform_usage"
        elif any(value in domain for value in ("moegirl", "baike", "wikipedia", "moyuoo", "gengwh")):
            kind, tier = "encyclopedia_search_result", "community_encyclopedia"
        results.append(
            _result(
                provider="bing",
                source_kind=kind,
                title=row["title"],
                url=row["url"],
                snippet=row["snippet"],
                published_at=_date_from_text(row["snippet"]),
                source_tier=tier,
            )
        )
    return _relevant(results, expression)[:5]


def search_so_question(client: RateLimitedResearchClient, question: str) -> list[dict[str, Any]]:
    """用完整用户问句检索通用网页，补足只搜关键词时漏掉的新梗。"""
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": question})
    body = client.get_text(url, referer="https://www.so.com/")
    blocks = re.findall(
        r'<li class="res-list[^"<>]*"[^>]*>(.*?)(?=<li class="res-list|</ol>)',
        body,
        flags=re.DOTALL,
    )
    results: list[dict[str, Any]] = []
    for block in blocks[:10]:
        heading = re.search(
            r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>(.*?)</h3>',
            block,
            re.DOTALL,
        )
        if not heading:
            continue
        anchor = re.search(r"<a\s+([^>]+)>(.*?)</a>", heading.group(1), re.DOTALL)
        if not anchor:
            continue
        attrs, title_html = anchor.groups()
        direct = re.search(r'data-mdurl="([^"]+)"', attrs)
        href = re.search(r'href="([^"]+)"', attrs)
        target_match = direct or href
        target = html.unescape(target_match.group(1)) if target_match else ""
        if not target or target.startswith("https://www.so.com/link?"):
            continue
        summary = re.search(
            r'class="(?:res-list-summary|res-desc)[^"]*"[^>]*>(.*?)</(?:span|p)>',
            block,
            re.DOTALL,
        )
        snippet = _clean_html(summary.group(1) if summary else "")
        results.append(
            _result(
                provider="so_qa",
                source_kind="question_search_result",
                title=_clean_html(title_html),
                url=target,
                snippet=snippet,
                published_at=_date_from_text(snippet),
                source_tier="indexed_web",
                extra={"search_question": question},
            )
        )
    return _relevant(results, _question_expression(question))[:5]


class _BingResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._in_result = False
        self._in_title = False
        self._in_snippet = False
        self._row: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            self._in_result = True
            self._row = {"title": "", "url": "", "snippet": ""}
        elif self._in_result and tag == "h2":
            self._in_title = True
        elif self._in_title and tag == "a" and values.get("href"):
            self._row["url"] = str(values["href"])
        elif self._in_result and tag == "p" and "b_lineclamp2" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self._in_title = False
        elif tag == "p":
            self._in_snippet = False
        elif tag == "li" and self._in_result:
            if self._row.get("title") and self._row.get("url"):
                self.results.append({key: value.strip() for key, value in self._row.items()})
            self._in_result = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._row["title"] += data
        elif self._in_snippet:
            self._row["snippet"] += data


def _result(
    *,
    provider: str,
    source_kind: str,
    title: str,
    url: str,
    snippet: str,
    published_at: str | None,
    source_tier: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = {
        "source_id": "web-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
        "provider": provider,
        "source_kind": source_kind,
        "source_tier": source_tier,
        "title": title[:240],
        "url": url,
        "snippet": snippet[:600],
        "published_at": published_at,
    }
    value.update(extra or {})
    return value


def _relevant(results: list[dict[str, Any]], expression: str) -> list[dict[str, Any]]:
    needle = normalize_expression(expression)
    if not needle:
        return []
    relevant: list[dict[str, Any]] = []
    for row in results:
        title = normalize_expression(str(row.get("title") or ""))
        snippet = normalize_expression(str(row.get("snippet") or ""))
        if needle in title:
            row["match_quality"] = "title_exact"
            row["relevance_score"] = 1.0
        elif needle in snippet:
            row["match_quality"] = "snippet_exact"
            row["relevance_score"] = 0.72
        else:
            continue
        relevant.append(row)
    return relevant


def _deduplicate_and_rank(results: list[dict[str, Any]], expression: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in results:
        url = str(row.get("url") or "")
        if not url:
            continue
        current = unique.get(url)
        if current is None or float(row.get("relevance_score") or 0) > float(
            current.get("relevance_score") or 0
        ):
            unique[url] = row
    tier_order = {
        "community_encyclopedia": 0,
        "contemporaneous_platform_usage": 1,
        "indexed_web": 2,
    }
    return sorted(
        unique.values(),
        key=lambda row: (
            tier_order.get(str(row.get("source_tier") or ""), 9),
            -float(row.get("relevance_score") or 0),
            _published_sort_key(row.get("published_at")),
        ),
    )


def _published_sort_key(value: Any) -> tuple[int, float]:
    if not value:
        return (1, 0)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return (1, 0)
    return (0, -parsed.timestamp())


def _research_status(results: list[dict[str, Any]]) -> str:
    providers = {row.get("provider") for row in results}
    has_encyclopedia = any(row.get("source_tier") == "community_encyclopedia" for row in results)
    has_usage = any(row.get("source_tier") == "contemporaneous_platform_usage" for row in results)
    if has_encyclopedia and has_usage:
        return "origin_and_current_usage"
    if len(providers) >= 2:
        return "multi_source_support"
    if has_encyclopedia:
        return "encyclopedia_only"
    if has_usage:
        return "current_usage_only"
    if results:
        return "single_web_source"
    return "no_reliable_match"


def _freshness(results: list[dict[str, Any]], retrieved_at: str) -> dict[str, Any]:
    retrieved = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    dates: list[datetime] = []
    for row in results:
        value = row.get("published_at")
        if not value:
            continue
        try:
            dates.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except ValueError:
            continue
    if not dates:
        return {"class": "unknown", "newest_source_at": None, "oldest_source_at": None}
    newest, oldest = max(dates), min(dates)
    age_days = max(0, (retrieved - newest).days)
    if age_days <= 90:
        value = "recent_90d"
    elif age_days <= 365:
        value = "recent_1y"
    else:
        value = "established_or_stale"
    return {
        "class": value,
        "newest_source_at": newest.isoformat(),
        "oldest_source_at": oldest.isoformat(),
        "newest_age_days": age_days,
    }


def _search_expressions(item: dict[str, Any]) -> list[str]:
    canonical = str(item.get("canonical_expression") or item.get("phrase") or "").strip()
    aliases = [str(value).strip() for value in item.get("aliases") or [] if str(value).strip()]
    values = [canonical, *aliases]
    if " / " in canonical and aliases:
        values = [*aliases, canonical]
    elif "（" in canonical and aliases:
        values = [aliases[0], canonical, *aliases[1:]]
    return list(dict.fromkeys(value for value in values if value))[:2]


def _source_query(source: str, expression: str, item: dict[str, Any]) -> str:
    if source != "so_qa":
        return expression
    rooms = (item.get("signals") or {}).get("live_rooms") or []
    circles = (item.get("signals") or {}).get("top_circles") or []
    occurrence_contexts = item.get("occurrence_contexts") or []
    if rooms and rooms[0].get("name"):
        context = f"在{rooms[0]['name']}直播间弹幕中"
    elif circles and circles[0].get("name"):
        context = f"在{circles[0]['name']}视频弹幕中"
    elif occurrence_contexts:
        first = occurrence_contexts[0]
        representative = first.get("representative_contexts") or []
        title = str(representative[0].get("content_title") or "").strip() if representative else ""
        source_kind = str(first.get("source_kind") or "")
        circle = str(first.get("circle") or "").strip()
        if title and source_kind in {"public_live_sample", "authorized_live_export"}:
            context = f"在{title}直播间弹幕中"
        elif circle:
            context = f"在{circle}视频弹幕中"
        else:
            context = "在视频弹幕中"
    else:
        context = "在视频弹幕中"
    return f"{context}看到“{expression}”是什么意思，是什么梗"


def _question_expression(question: str) -> str:
    match = re.search(r"[“\"](.+?)[”\"]", question)
    return match.group(1) if match else question


def _clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _relative_date(value: str) -> str | None:
    now = datetime.now(timezone.utc)
    text = _clean_html(value)
    match = re.search(r"(\d+)\s*(分钟|小时|天|月|年)前", text)
    if not match:
        return _date_from_text(text)
    count = int(match.group(1))
    days = count if match.group(2) == "天" else count * 30 if match.group(2) == "月" else count * 365
    if match.group(2) in {"分钟", "小时"}:
        days = 0
    return datetime.fromtimestamp(now.timestamp() - days * 86400, timezone.utc).isoformat()


def _iso_date(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        return None


def _date_from_text(value: str) -> str | None:
    match = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", value)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc
        ).isoformat()
    except ValueError:
        return None


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
