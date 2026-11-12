from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.messages import HumanMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_google_genai import ChatGoogleGenerativeAI

if __package__:
    from .config import Settings, ConfigError
    from .theme import render_html, validate_html
    from .tools import (
        TOOLS,
        reset_last_valid_script,
        take_last_valid_script,
        validate_script_text,
    )
else:
    from config import Settings, ConfigError
    from theme import render_html, validate_html
    from tools import (
        TOOLS,
        reset_last_valid_script,
        take_last_valid_script,
        validate_script_text,
    )


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    script: Dict[str, Any]
    script_json: str
    html: str


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "agent.md"


def load_system_prompt() -> str:
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentError(f"Could not read prompt file {PROMPT_PATH}: {exc}") from exc
    if not prompt:
        raise AgentError(f"Prompt file is empty: {PROMPT_PATH}")
    return prompt


class DeepAgentRunner:
    def __init__(self, settings: Settings) -> None:
        if not settings.google_api_key:
            raise ConfigError("GOOGLE_API_KEY is required for the agent.")
        self.settings = settings
        try:
            model = ChatGoogleGenerativeAI(
                model=settings.agent_model,
                api_key=settings.google_api_key,
                # The validated script can be several thousand tokens; 8k leaves
                # room for the full JSON without truncating the final answer.
                max_output_tokens=8192,
                # DeepAgent makes multiple model calls per job. Keep them below
                # the small free-tier Gemini RPM quota rather than bursting.
                rate_limiter=InMemoryRateLimiter(
                    requests_per_second=self.settings.agent_requests_per_minute / 60,
                    max_bucket_size=1,
                ),
            )
            self.agent = create_deep_agent(
                model=model,
                tools=TOOLS,
                system_prompt=load_system_prompt(),
                backend=StateBackend(),
            )
        except Exception as exc:
            raise AgentError(f"Could not initialize the DeepAgent: {exc}") from exc

    async def run(self, topic: str) -> AgentResult:
        prompt = (
            f"Topic: {topic}\n\n"
            "Research once, then call validate_script with the complete raw JSON "
            "object. If it returns FAIL, fix the JSON and call it again. When it "
            "returns OK, reply DONE. Do not write files or HTML."
        )
        reset_last_valid_script()
        try:
            result = await self.agent.ainvoke(
                {"messages": [HumanMessage(content=prompt)]},
                config={"recursion_limit": self.settings.agent_max_steps},
            )
        except Exception as exc:
            raise AgentError(f"Agent run failed: {exc}") from exc

        if not isinstance(result, dict):
            raise AgentError("Agent returned an unexpected result.")

        files = result.get("files") or {}
        script_json = (
            take_last_valid_script()
            or _find_file(files, "script.json")
            or _json_from_messages(result.get("messages") or [])
        )
        if not script_json:
            raise AgentError("Agent did not return a script JSON object.")

        try:
            script = json.loads(script_json)
        except json.JSONDecodeError as exc:
            raise AgentError(f"Agent wrote invalid JSON: {exc.msg} at {exc.pos}.") from exc
        if not isinstance(script, dict):
            raise AgentError("Agent script must be a JSON object.")

        check = validate_script_text(script_json)
        if not check.startswith("OK"):
            raise AgentError(f"Agent script failed validation: {check}")

        try:
            duration = sum(float(scene.get("duration") or 0) for scene in script["scenes"])
        except (TypeError, ValueError, KeyError) as exc:
            raise AgentError(f"Could not compute duration: {exc}") from exc

        html = render_html(script)
        html_errors = validate_html(html, duration)
        if html_errors:
            raise AgentError("Rendered HTML failed validation: " + "; ".join(html_errors))

        return AgentResult(script=script, script_json=script_json, html=html)


def _find_file(files: Dict[str, Any], name: str) -> Optional[str]:
    for path, data in files.items():
        if not isinstance(path, str):
            continue
        if path.rstrip("/").split("/")[-1] != name:
            continue
        return _file_content(data)
    return None


def _file_content(data: Any) -> Optional[str]:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        content = data.get("content")
    else:
        content = getattr(data, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return None


def _json_from_messages(messages: List[Any]) -> Optional[str]:
    for message in reversed(messages):
        role = getattr(message, "type", None)
        if role is None and isinstance(message, dict):
            role = message.get("role") or message.get("type")
        if role not in (None, "ai", "AIMessage", "assistant"):
            continue
        if getattr(message, "tool_calls", None):
            continue
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    if part.get("type") in ("text", "text_delta") or "text" in part:
                        parts.append(str(part.get("text") or ""))
            text = "".join(parts)
        else:
            text = str(content or "")
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("scenes"), list):
                return candidate
    return None
