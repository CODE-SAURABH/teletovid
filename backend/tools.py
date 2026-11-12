from __future__ import annotations

import asyncio
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
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
    "compare",
    "timeline",
    "stat",
    "quote",
    "mythFact",
    "outro",
}
ALLOWED_LAYOUTS = {
    "stack",
    "split",
    "spotlight",
    "compare",
    "timeline",
    "metric",
    "quote",
}
ALLOWED_STRUCTURES = {
    "system",
    "timeline",
    "argument",
    "profile",
    "fieldguide",
}
MIN_DURATION = 180.0
MAX_DURATION = 240.0
MIN_SCENES = 18
MAX_SCENES = 24
MIN_WORDS = 650
MAX_WORDS = 900
ITEM_LIMITS = {
    "bullets": (2, 5),
    "steps": (2, 5),
    "chat": (2, 4),
    "compare": (2, 4),
    "timeline": (3, 6),
    "stat": (2, 3),
}


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


class _PageTextParser(HTMLParser):
    """Pull readable paragraphs from a result page without external parsers."""

    BLOCKS = {"p", "li", "h2", "h3", "blockquote", "pre"}
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "form", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCKS and not self._skip:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCKS and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text + " ")

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


async def _fetch_excerpt(url: str, limit: int = 2200) -> str:
    if not url.lower().startswith(("http://", "https://")):
        return ""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type and "text" not in content_type:
            return ""
        raw = response.content[:600_000]
        parser = _PageTextParser()
        parser.feed(raw.decode(response.encoding or "utf-8", errors="replace"))
        parser.close()
        text = parser.text()
        if len(text) < 240:
            return ""
        return text[:limit]
    except Exception:
        return ""


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
    """Research overview, mechanism, evidence, examples, and limits, then read source pages."""
    query = (query or "").strip()
    if not query:
        return "FAIL: query is empty."
    max_results = max(3, min(int(max_results), 6))
    queries = [
        ("OVERVIEW", query),
        ("MECHANISM", f"{query} how it works explained"),
        ("EVIDENCE", f"{query} data study official report"),
        ("EXAMPLE", f"{query} case study real world example"),
        ("LIMITS", f"{query} limitations risks criticism debate"),
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
            if len(results) >= 10:
                break
        if len(results) >= 10:
            break
    if not results:
        return "No web results. Use model knowledge and flag uncertainty."

    lines = [
        f"Research dossier for: {query}",
        "Use 6-10 distinct sources. Prefer official documentation, original research, "
        "public agencies, standards bodies, and direct company sources.",
        "",
        "SEARCH LEADS",
    ]
    for index, (label, item) in enumerate(results, start=1):
        title = item.get("title") or "Untitled"
        snippet = (item.get("snippet") or "")[:360]
        url = item.get("url") or ""
        lines.append(f"{index}. [{label}] {title}\n   {snippet}\n   {url}")

    excerpt_urls = [item.get("url") or "" for _, item in results[:4]]
    excerpts = await asyncio.gather(
        *(_fetch_excerpt(url) for url in excerpt_urls), return_exceptions=True
    )
    lines.append("")
    lines.append("SOURCE EXCERPTS (read these before writing; do not invent beyond them)")
    for index, excerpt in enumerate(excerpts, start=1):
        if isinstance(excerpt, str) and excerpt:
            lines.append(f"[{index}] {excerpt}")
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
        layout = str(scene.get("layout") or "").strip().lower()
        if layout and layout not in ALLOWED_LAYOUTS:
            errors.append(f"{prefix}.layout must be one of {sorted(ALLOWED_LAYOUTS)}.")
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
        body_limit = 280 if scene_type == "quote" else 180
        if len(str(scene.get("body") or "")) > body_limit:
            errors.append(f"{prefix}.body must be {body_limit} characters or fewer.")
        if scene_type in ITEM_LIMITS:
            items = scene.get("items")
            minimum, maximum = ITEM_LIMITS[scene_type]
            if not isinstance(items, list) or len(items) < minimum:
                errors.append(
                    f"{prefix}.items must contain at least {minimum} entries."
                )
            elif isinstance(items, list):
                if len(items) > maximum:
                    errors.append(
                        f"{prefix}.items must contain at most {maximum} entries."
                    )
                for item_index, item in enumerate(items):
                    if isinstance(item, str):
                        label = ""
                        text = item
                    elif isinstance(item, dict):
                        label = str(item.get("label") or "")
                        text = str(item.get("text") or item.get("label") or "")
                    else:
                        errors.append(f"{prefix}.items[{item_index}] must be a string or object.")
                        continue
                    if len(text) > 120:
                        errors.append(f"{prefix}.items[{item_index}] must be 120 characters or fewer.")
                    if len(label) > 40:
                        errors.append(f"{prefix}.items[{item_index}].label must be 40 characters or fewer.")
                    if scene_type in {"timeline", "stat"} and not label.strip():
                        errors.append(f"{prefix}.items[{item_index}].label is required.")
        if scene_type == "code" and not str(scene.get("code") or "").strip():
            errors.append(f"{prefix}.code is required for code scenes.")
        if scene_type == "quote" and len(str(scene.get("body") or "").strip()) < 20:
            errors.append(f"{prefix}.body must contain the quoted passage.")
        if scene_type == "mythFact":
            if len(str(scene.get("body") or "").strip()) < 10:
                errors.append(f"{prefix}.body must contain the myth.")
            fact = str(scene.get("fact") or "").strip()
            if len(fact) < 10:
                errors.append(f"{prefix}.fact must contain the reality.")
            elif len(fact) > 180:
                errors.append(f"{prefix}.fact must be 180 characters or fewer.")

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
    if not (MIN_WORDS <= words <= MAX_WORDS):
        errors.append(f"narration should be {MIN_WORDS}-{MAX_WORDS} words (got {words}).")
    if len(scene_types & ALLOWED_SCENE_TYPES) < 6:
        errors.append("use at least 6 different scene types so the video does not repeat one layout.")

    sources = data.get("sources")
    if not isinstance(sources, list) or not (5 <= len(sources) <= 10):
        errors.append("sources must contain 5-10 researched sources.")
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
    elif isinstance(visual, dict):
        structure = str(visual.get("structure") or "").strip().lower()
        if structure and structure not in ALLOWED_STRUCTURES:
            errors.append(
                f"visual_style.structure must be one of {sorted(ALLOWED_STRUCTURES)}."
            )

    if errors:
        return _fail(errors)
    return f"OK: {len(scenes)} scenes, {total:.1f}s, {words} narration words."


_LAST_VALID_SCRIPT: Optional[str] = None


@tool
def validate_script(script_json: str) -> str:
    """Submit and validate the complete script JSON; call again only after fixing a FAIL."""
    global _LAST_VALID_SCRIPT
    result = validate_script_text(script_json)
    if result.startswith("OK"):
        _LAST_VALID_SCRIPT = script_json
    return result


def reset_last_valid_script() -> None:
    global _LAST_VALID_SCRIPT
    _LAST_VALID_SCRIPT = None


def take_last_valid_script() -> Optional[str]:
    return _LAST_VALID_SCRIPT


TOOLS = [research_topic, validate_script]
