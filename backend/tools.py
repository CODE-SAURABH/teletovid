from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from langchain_core.tools import tool

DDG_HTML = "https://html.duckduckgo.com/html/"
USER_AGENT = "Mozilla/5.0 (compatible; tele-to-vid/1.0; +https://render.com)"
ALLOWED_SCENE_TYPES = {
    "title",
    "statement",
    "bullets",
    "steps",
    "bigNumber",
    "code",
    "chat",
    "outro",
}
MIN_DURATION = 135.0
MAX_DURATION = 180.0
MIN_SCENES = 12
MAX_SCENES = 16


class _ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: List[Dict[str, str]] = []
        self._current: Dict[str, str] = {}
        self._in_title = False
        self._in_snippet = False

    def _commit(self) -> None:
        if self._current.get("title"):
            self.results.append(self._current)
        self._current = {}
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str]]) -> None:
        values = dict(attrs)
        classes = values.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._commit()
            self._current = {"title": "", "url": values.get("href", ""), "snippet": ""}
            self._in_title = True
        elif "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        elif self._in_snippet and tag in {"a", "div", "span"}:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title:
            self._current["title"] = (self._current.get("title", "") + " " + text).strip()
        elif self._in_snippet:
            self._current["snippet"] = (self._current.get("snippet", "") + " " + text).strip()

    def close(self) -> None:
        super().close()
        self._commit()


def _clean_url(raw: str) -> str:
    if not raw:
        return ""
    url = urljoin(DDG_HTML, raw)
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or url
    return url


async def _search(query: str, max_results: int) -> List[Dict[str, str]]:
    params = {"q": query, "kl": "us-en"}
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(DDG_HTML, params=params, headers=headers)
        response.raise_for_status()
        parser = _ResultParser()
        parser.feed(response.text)
        parser.close()
    results = parser.results[:max_results]
    for result in results:
        result["url"] = _clean_url(result.get("url", ""))
    return results


@tool
async def research_topic(query: str, max_results: int = 5) -> str:
    """Research a topic across overview, mechanism, and evidence searches."""
    query = (query or "").strip()
    if not query:
        return "FAIL: query is empty."
    max_results = max(3, min(int(max_results), 6))
    queries = [
        ("OVERVIEW", query),
        ("MECHANISM", f"{query} how it works evidence"),
        ("CONTEXT", f"{query} data history implications official report"),
    ]
    try:
        batches = await asyncio.gather(
            *(_search(search_query, max_results) for _, search_query in queries)
        )
    except Exception as exc:
        return f"FAIL: web search unavailable ({exc}). Use model knowledge and flag uncertainty."

    results: List[tuple[str, Dict[str, str]]] = []
    seen_urls = set()
    for (label, _), batch in zip(queries, batches):
        for item in batch:
            url = item.get("url", "")
            key = url.lower().rstrip("/") or item.get("title", "").lower()
            if key and key not in seen_urls:
                seen_urls.add(key)
                results.append((label, item))
            if len(results) >= 9:
                break
        if len(results) >= 12:
            break
    if not results:
        return "No web results. Use model knowledge and flag uncertainty."

    lines = [f"Research dossier for: {query}", "Use at least 4 distinct sources below."]
    for index, (label, item) in enumerate(results, start=1):
        title = item.get("title") or "Untitled"
        # Search snippets are leads, not source text. Keeping them bounded leaves
        # the model enough hourly quota to write the actual explainer.
        snippet = (item.get("snippet") or "")[:320]
        url = item.get("url") or ""
        lines.append(f"{index}. [{label}] {title}\n   {snippet}\n   {url}")
    return "\n".join(lines)


def _fail(errors: List[str]) -> str:
    return "FAIL:\n- " + "\n- ".join(errors)


def validate_script_text(script_json: str) -> str:
    try:
        data = json.loads(script_json)
    except json.JSONDecodeError as exc:
        return f"FAIL: invalid JSON ({exc.msg} at {exc.pos})."

    if not isinstance(data, dict):
        return "FAIL: root must be an object."

    errors: List[str] = []
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a non-empty string.")
    elif len(title.strip()) > 80:
        errors.append("title must be 80 characters or fewer.")

    scenes = data.get("scenes")
    if not isinstance(scenes, list):
        return "FAIL: scenes must be a list."
    if not (MIN_SCENES <= len(scenes) <= MAX_SCENES):
        errors.append(f"scenes must contain {MIN_SCENES}-{MAX_SCENES} entries (got {len(scenes)}).")

    total = 0.0
    scene_types = set()
    for index, scene in enumerate(scenes):
        prefix = f"scenes[{index}]"
        if not isinstance(scene, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        scene_type = str(scene.get("type") or "")
        scene_types.add(scene_type)
        if scene_type not in ALLOWED_SCENE_TYPES:
            errors.append(f"{prefix}.type must be one of {sorted(ALLOWED_SCENE_TYPES)}.")
        try:
            duration = float(scene.get("duration"))
        except (TypeError, ValueError):
            errors.append(f"{prefix}.duration must be a number.")
            continue
        total += duration
        if not (5.0 <= duration <= 20.0):
            errors.append(f"{prefix}.duration must be 5-20 seconds.")
        narration = scene.get("narration")
        if not isinstance(narration, str) or len(narration.strip()) < 8:
            errors.append(f"{prefix}.narration must be a sentence.")
        scene_title = str(scene.get("title") or "").strip()
        if not scene_title:
            errors.append(f"{prefix}.title is required.")
        elif len(scene_title) > 70:
            errors.append(f"{prefix}.title must be 70 characters or fewer.")
        if len(str(scene.get("body") or "")) > 180:
            errors.append(f"{prefix}.body must be 180 characters or fewer.")
        if scene_type in {"bullets", "steps", "chat"}:
            items = scene.get("items")
            if not isinstance(items, list) or len(items) < 2:
                errors.append(f"{prefix}.items must contain at least 2 entries.")
            elif isinstance(items, list):
                limit = 4 if scene_type == "chat" else 5
                if len(items) > limit:
                    errors.append(f"{prefix}.items must contain at most {limit} entries.")
                for item_index, item in enumerate(items):
                    if isinstance(item, str):
                        text = item
                    elif isinstance(item, dict):
                        text = str(item.get("text") or item.get("label") or "")
                    else:
                        errors.append(f"{prefix}.items[{item_index}] must be a string or object.")
                        continue
                    if len(text) > 120:
                        errors.append(f"{prefix}.items[{item_index}] must be 120 characters or fewer.")
        if scene_type == "code" and not str(scene.get("code") or "").strip():
            errors.append(f"{prefix}.code is required for code scenes.")

    if not (MIN_DURATION <= total <= MAX_DURATION):
        errors.append(f"total duration must be {MIN_DURATION:.0f}-{MAX_DURATION:.0f}s (got {total:.1f}s).")

    declared = data.get("total_duration")
    if declared is not None:
        try:
            if abs(float(declared) - total) > 0.5:
                errors.append("total_duration must match the sum of scene durations.")
        except (TypeError, ValueError):
            errors.append("total_duration must be a number.")

    words = 0
    for scene in scenes:
        if isinstance(scene, dict):
            words += len(re.findall(r"[A-Za-z0-9']+", str(scene.get("narration") or "")))
    if not (420 <= words <= 650):
        errors.append(f"narration should be 420-650 words (got {words}).")
    if len(scene_types & ALLOWED_SCENE_TYPES) < 5:
        errors.append("use at least 5 different scene types so the video does not repeat one layout.")

    sources = data.get("sources")
    if not isinstance(sources, list) or not (4 <= len(sources) <= 8):
        errors.append("sources must contain 4-8 researched sources.")
    elif isinstance(sources, list):
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                errors.append(f"sources[{index}] must be an object.")
                continue
            if not str(source.get("title") or "").strip():
                errors.append(f"sources[{index}].title is required.")
            url = str(source.get("url") or "").strip()
            if not re.match(r"https?://", url):
                errors.append(f"sources[{index}].url must be an http(s) URL.")

    visual = data.get("visual_style")
    if not isinstance(visual, dict) or not str(visual.get("theme") or "").strip():
        errors.append("visual_style.theme is required.")

    if errors:
        return _fail(errors)
    return f"OK: {len(scenes)} scenes, {total:.1f}s, {words} narration words."


@tool
def validate_script(script_json: str) -> str:
    """Validate a script JSON string against the detailed storyboard contract."""
    return validate_script_text(script_json)


TOOLS = [research_topic, validate_script]
