"""Lightweight web search support for knowledge QA.

Results are treated as external evidence and are never written into the project
knowledge base. The implementation uses a no-key search page as discovery, then
fetches top result pages and extracts relevant body passages.
"""

from __future__ import annotations

import html
import logging
import re
import asyncio
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


class _DuckDuckGoHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict] = []
        self._in_title = False
        self._in_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._current_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: v or "" for k, v in attrs}
        classes = attr_map.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._in_title = True
            self._title_parts = []
            self._current_href = attr_map.get("href", "")
        elif "result__snippet" in classes:
            self._in_snippet = True
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_snippet:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            title = _clean_text(" ".join(self._title_parts))
            url = _normalize_duckduckgo_url(self._current_href)
            if title and url:
                self.results.append({"title": title, "url": url, "snippet": ""})
            self._in_title = False
            self._title_parts = []
            self._current_href = ""
        elif self._in_snippet and tag in {"a", "div", "span"}:
            snippet = _clean_text(" ".join(self._snippet_parts))
            if snippet and self.results and not self.results[-1].get("snippet"):
                self.results[-1]["snippet"] = snippet
            self._in_snippet = False
            self._snippet_parts = []


class _ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""
        self._in_title = False
        self._skip_depth = 0
        self._current_block: str | None = None
        self._block_parts: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): v or "" for k, v in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta" and attr_map.get("name", "").lower() in {"description", "og:description"}:
            self.description = _clean_text(attr_map.get("content", ""))
            return
        if tag in {"p", "li", "h1", "h2", "h3", "article"}:
            self._flush_block()
            self._current_block = tag
            self._block_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        if self._current_block:
            self._block_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            self.title = _clean_text(self.title)
            return
        if self._current_block == tag:
            self._flush_block()

    def close(self) -> None:
        self._flush_block()
        super().close()

    def _flush_block(self) -> None:
        if not self._current_block:
            return
        text = _clean_text(" ".join(self._block_parts))
        if len(text) >= 24:
            self.blocks.append(text)
        self._current_block = None
        self._block_parts = []


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _normalize_duckduckgo_url(value: str) -> str:
    href = html.unescape(value or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = "https://duckduckgo.com" + href

    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return href


def _dedupe_results(results: list[dict], limit: int) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in results:
        url = item.get("url", "")
        title = item.get("title", "")
        if not url or url in seen:
            continue
        seen.add(url)
        snippet = item.get("snippet") or title
        deduped.append({
            "title": title,
            "url": url,
            "snippet": snippet,
        })
        if len(deduped) >= limit:
            break
    return deduped


def _result(title: str, url: str, snippet: str = "") -> dict:
    return {"title": _clean_text(title), "url": url.strip(), "snippet": _clean_text(snippet)}


def _parse_brave_results(payload: dict) -> list[dict]:
    web = payload.get("web") or {}
    rows = web.get("results") or []
    return [_result(r.get("title", ""), r.get("url", ""), r.get("description", "")) for r in rows]


def _parse_tavily_results(payload: dict) -> list[dict]:
    rows = payload.get("results") or []
    return [_result(r.get("title", ""), r.get("url", ""), r.get("raw_content") or r.get("content", "")) for r in rows]


def _parse_serper_results(payload: dict) -> list[dict]:
    rows = payload.get("organic") or []
    return [_result(r.get("title", ""), r.get("link", ""), r.get("snippet", "")) for r in rows]


def _query_terms(query: str) -> list[str]:
    raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_-]{2,}", query)
    stop = {"什么", "怎么", "如何", "哪些", "一下", "介绍", "资料", "情况", "about", "what", "how"}
    terms: list[str] = []
    for raw in raw_terms:
        term = raw.lower()
        if term in stop:
            continue
        terms.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            for size in (2, 3, 4):
                for index in range(0, max(len(raw) - size + 1, 0)):
                    terms.append(raw[index:index + size].lower())

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in stop or term in seen:
            continue
        seen.add(term)
        deduped.append(term)
    return deduped[:24]


def _rank_blocks(blocks: list[str], query: str) -> list[str]:
    terms = _query_terms(query)
    if not terms:
        return blocks[:5]

    scored: list[tuple[float, int, str]] = []
    for index, block in enumerate(blocks):
        lowered = block.lower()
        term_hits = sum(1 for term in terms if term in lowered)
        if term_hits == 0 and index > 8:
            continue
        density = term_hits / max(len(block), 1)
        score = term_hits * 10 + density * 1000 - index * 0.05
        scored.append((score, index, block))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [block for _, _, block in scored[:5]] or blocks[:5]


def _merge_passages(passages: list[str], *, max_chars: int = 1400) -> str:
    merged: list[str] = []
    total = 0
    for passage in passages:
        if not passage:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        piece = passage[:remaining]
        merged.append(piece)
        total += len(piece) + 1
    return "\n".join(merged).strip()


def _extract_readable_page(html_text: str, query: str) -> tuple[str, str]:
    parser = _ReadableHtmlParser()
    parser.feed(html_text)
    parser.close()
    title = parser.title
    passages = []
    if parser.description:
        passages.append(parser.description)
    passages.extend(_rank_blocks(parser.blocks, query))
    return title, _merge_passages(passages)


async def _fetch_page_snippet(client: httpx.AsyncClient, item: dict, query: str) -> dict:
    url = item.get("url", "")
    if not url.startswith(("http://", "https://")):
        return item
    try:
        response = await client.get(url)
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 400 or "text/html" not in content_type:
            return item
        title, body = _extract_readable_page(response.text, query)
        if body:
            item = dict(item)
            item["title"] = title or item.get("title", "")
            item["snippet"] = body
            item["fetched"] = True
    except Exception as exc:
        logger.debug("web page fetch failed url=%s error=%s", url, exc)
    return item


async def _search_duckduckgo(client: httpx.AsyncClient, query: str, max_results: int) -> list[dict]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    parser = _DuckDuckGoHtmlParser()
    parser.feed(response.text)
    return _dedupe_results(parser.results, max_results)


async def _search_brave(client: httpx.AsyncClient, query: str, max_results: int, api_key: str, base_url: str | None) -> list[dict]:
    url = (base_url or "https://api.search.brave.com/res/v1/web/search").rstrip("/")
    response = await client.get(
        url,
        params={"q": query, "count": max_results, "search_lang": "zh-hans"},
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
    )
    response.raise_for_status()
    return _dedupe_results(_parse_brave_results(response.json()), max_results)


async def _search_tavily(client: httpx.AsyncClient, query: str, max_results: int, api_key: str, base_url: str | None) -> list[dict]:
    url = base_url or "https://api.tavily.com/search"
    response = await client.post(
        url,
        json={
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced",
            "max_results": max_results,
            "include_raw_content": True,
        },
    )
    response.raise_for_status()
    return _dedupe_results(_parse_tavily_results(response.json()), max_results)


async def _search_serper(client: httpx.AsyncClient, query: str, max_results: int, api_key: str, base_url: str | None) -> list[dict]:
    url = base_url or "https://google.serper.dev/search"
    response = await client.post(
        url,
        json={"q": query, "num": max_results},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    return _dedupe_results(_parse_serper_results(response.json()), max_results)


async def _fetch_result_pages(client: httpx.AsyncClient, rows: list[dict], query: str, max_results: int) -> list[dict]:
    fetched_results = await asyncio.gather(
        *[_fetch_page_snippet(client, item, query) for item in rows[:max_results]],
        return_exceptions=True,
    )
    return [item for item in fetched_results if not isinstance(item, Exception)]


def _to_citations(rows: list[dict], query: str) -> list[dict]:
    citations = []
    for index, result in enumerate(rows):
        item = result
        citations.append({
            "source_kind": "web_search",
            "source_id": item["url"],
            "chunk_id": None,
            "title": item["title"],
            "snippet": item["snippet"][:1400],
            "url": item["url"],
            "evidence_type": "web_search",
            "matched_query": query,
            "score": max(0.1, (1.2 if item.get("fetched") else 1.0) - index * 0.08),
        })
    return citations


async def search_web(
    query: str,
    *,
    limit: int | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Search the public web and return citation-shaped external evidence."""

    normalized = query.strip()
    if not normalized or not settings.WEB_SEARCH_ENABLED:
        return []

    max_results = max(1, min(limit or settings.WEB_SEARCH_MAX_RESULTS, settings.WEB_SEARCH_MAX_RESULTS, 10))
    search_provider = (provider or settings.WEB_SEARCH_PROVIDER or "duckduckgo").strip().lower()
    search_key = (api_key if api_key is not None else settings.WEB_SEARCH_API_KEY).strip()
    search_base_url = (base_url if base_url is not None else settings.WEB_SEARCH_BASE_URL).strip() or None

    try:
        async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            if search_provider == "brave":
                if not search_key:
                    logger.warning("Brave web search selected but API key is missing")
                    return []
                raw_results = await _search_brave(client, normalized, max_results, search_key, search_base_url)
            elif search_provider == "tavily":
                if not search_key:
                    logger.warning("Tavily web search selected but API key is missing")
                    return []
                raw_results = await _search_tavily(client, normalized, max_results, search_key, search_base_url)
            elif search_provider in {"serper", "google"}:
                if not search_key:
                    logger.warning("Serper web search selected but API key is missing")
                    return []
                raw_results = await _search_serper(client, normalized, max_results, search_key, search_base_url)
            else:
                raw_results = await _search_duckduckgo(client, normalized, max_results)

            fetched = await _fetch_result_pages(client, raw_results, normalized, max_results)
            return _to_citations(fetched, normalized)
    except Exception as exc:
        logger.warning("web search failed provider=%s error=%s", search_provider, exc)
        return []
