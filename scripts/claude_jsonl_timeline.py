#!/usr/bin/env python3
"""Print a compact timeline from an untrusted Claude Code JSONL transcript."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def compact(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def text_from_message(message: dict[str, Any]) -> str:
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
    return "\n".join(parts)


def classify_error(text: str) -> str:
    lowered = text.lower()
    if "invalid pages parameter" in lowered:
        return "invalid_pages"
    if "exceeds maximum allowed tokens" in lowered:
        return "too_large"
    if "api error: 403" in lowered or "failed to authenticate" in lowered:
        return "auth_or_quota"
    return "error"


def summarize_tool_use(item: dict[str, Any], limit: int) -> str:
    name = item.get("name", "?")
    data = item.get("input") or {}
    if name == "Read":
        file_name = Path(str(data.get("file_path", ""))).name
        return (
            f"TOOL Read path={file_name} offset={data.get('offset')} "
            f"limit={data.get('limit')} pages={data.get('pages', '<missing>')!r}"
        )
    if name == "Bash":
        return f"TOOL Bash {compact(str(data.get('command', '')), limit)}"
    if name in {"Grep", "Glob", "Edit", "Write"}:
        return f"TOOL {name} {compact(json.dumps(data, ensure_ascii=False), limit)}"
    return f"TOOL {name}"


def iter_events(path: Path, start: int, end: int | None, text_limit: int, grep: str | None):
    pattern = re.compile(grep, re.IGNORECASE) if grep else None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            if line_no < start:
                continue
            if end is not None and line_no > end:
                break
            try:
                obj = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                yield line_no, f"PARSE_ERROR {exc}"
                continue

            message = obj.get("message")
            events: list[str] = []
            if isinstance(message, dict):
                role = message.get("role", "?")
                text = text_from_message(message)
                if text:
                    events.append(f"{role}: {compact(text, text_limit)}")
                content = message.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "tool_use":
                            events.append(summarize_tool_use(item, text_limit))
                        elif item.get("type") == "tool_result":
                            result = item.get("content", "")
                            if isinstance(result, list):
                                result = " ".join(str(part) for part in result)
                            if item.get("is_error"):
                                events.append(
                                    f"RESULT {classify_error(str(result))}: {compact(str(result), text_limit)}"
                                )
                            else:
                                events.append("RESULT ok")
            elif isinstance(obj.get("content"), str):
                events.append(f"{obj.get('type')}: {compact(obj['content'], text_limit)}")
            elif obj.get("type") in {"ai-title", "pr-link"}:
                events.append(compact(json.dumps(obj, ensure_ascii=False), text_limit))

            if not events:
                continue
            rendered = " | ".join(events)
            if pattern and not pattern.search(rendered):
                continue
            yield line_no, rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int)
    parser.add_argument("--grep")
    parser.add_argument("--text-limit", type=int, default=180)
    args = parser.parse_args()

    for line_no, event in iter_events(args.path, args.start, args.end, args.text_limit, args.grep):
        print(f"{line_no:05d}: {event}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
