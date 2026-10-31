#!/usr/bin/env python3
"""Summarize an untrusted Claude Code JSONL transcript."""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any


SECRET_PATTERNS = {
    "openai_sk": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "anthropic_sk": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    "jwt_like": re.compile(
        r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    ),
    "private_key_header": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def compact(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str) and item.get("type") != "tool_result":
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
    return "\n".join(parts)


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                yield line_no, json.loads(line), None
            except Exception as exc:  # noqa: BLE001
                yield line_no, None, str(exc)


def classify_error(text: str) -> str:
    lowered = text.lower()
    if "invalid pages parameter" in lowered:
        return "invalid_pages"
    if "exceeds maximum allowed tokens" in lowered:
        return "too_large"
    if "failed to authenticate" in lowered or "api error: 403" in lowered:
        return "auth_or_quota"
    if "permission" in lowered:
        return "permission"
    return "other"


def summarize(path: Path, tail: int, text_limit: int) -> dict[str, Any]:
    counts = collections.Counter()
    roles = collections.Counter()
    tools = collections.Counter()
    error_classes = collections.Counter()
    parse_errors: list[tuple[int, str]] = []
    text_messages: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    secret_counts = collections.Counter()
    first_ts = None
    last_ts = None
    cwd = None
    branch = None
    version = None
    total_lines = 0

    full_text = path.read_text(encoding="utf-8", errors="replace")
    for name, pattern in SECRET_PATTERNS.items():
        secret_counts[name] = len(pattern.findall(full_text))

    for line_no, obj, err in iter_jsonl(path):
        total_lines = line_no
        if err:
            parse_errors.append((line_no, err))
            continue

        assert obj is not None
        typ = obj.get("type", "?")
        counts[typ] += 1
        ts = obj.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        cwd = obj.get("cwd") or cwd
        branch = obj.get("gitBranch") or branch
        version = obj.get("version") or version

        message = obj.get("message")
        if not isinstance(message, dict):
            continue

        role = message.get("role", "?")
        roles[role] += 1
        text = content_text(message)
        if text:
            text_messages.append(
                {
                    "line": line_no,
                    "timestamp": ts,
                    "role": role,
                    "text": compact(text, text_limit),
                }
            )

        content = message.get("content")
        if not isinstance(content, list):
            continue

        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "tool_use":
                name = item.get("name", "?")
                tools[name] += 1
                tool_events.append(
                    {
                        "line": line_no,
                        "timestamp": ts,
                        "kind": "tool_use",
                        "tool": name,
                    }
                )
            elif item_type == "tool_result":
                result_text = item.get("content", "")
                if isinstance(result_text, list):
                    result_text = " ".join(str(part) for part in result_text)
                if item.get("is_error"):
                    error_classes[classify_error(str(result_text))] += 1
                    tool_events.append(
                        {
                            "line": line_no,
                            "timestamp": ts,
                            "kind": "tool_error",
                            "error": classify_error(str(result_text)),
                            "text": compact(str(result_text), text_limit),
                        }
                    )

    return {
        "path": str(path),
        "lines": total_lines,
        "parse_errors": parse_errors[:20],
        "time_range": [first_ts, last_ts],
        "cwd": cwd,
        "git_branch": branch,
        "version": version,
        "types": dict(counts.most_common()),
        "roles": dict(roles.most_common()),
        "tools": dict(tools.most_common()),
        "tool_errors": dict(error_classes.most_common()),
        "secret_pattern_counts": dict(secret_counts),
        "recent_text_messages": text_messages[-tail:],
        "recent_tool_errors": [event for event in tool_events if event.get("kind") == "tool_error"][-tail:],
    }


def print_human(data: dict[str, Any]) -> None:
    print(f"path: {data['path']}")
    print(f"lines: {data['lines']}")
    print(f"parse_errors: {len(data['parse_errors'])}")
    print(f"time: {data['time_range'][0]} -> {data['time_range'][1]}")
    print(f"cwd: {data['cwd']}")
    print(f"git_branch: {data['git_branch']}")
    print(f"version: {data['version']}")
    print(f"types: {data['types']}")
    print(f"roles: {data['roles']}")
    print(f"tools: {data['tools']}")
    print(f"tool_errors: {data['tool_errors']}")
    print(f"secret_pattern_counts: {data['secret_pattern_counts']}")
    print("\nrecent_text_messages:")
    for item in data["recent_text_messages"]:
        print(f"  line {item['line']} {item['timestamp']} {item['role']}: {item['text']}")
    print("\nrecent_tool_errors:")
    for item in data["recent_tool_errors"]:
        print(f"  line {item['line']} {item['timestamp']} {item['error']}: {item['text']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--tail", type=int, default=12)
    parser.add_argument("--text-limit", type=int, default=240)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    data = summarize(args.path, args.tail, args.text_limit)
    if args.as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print_human(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
