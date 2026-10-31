#!/usr/bin/env python3
"""Search local agent JSONL conversations and rank candidate files."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


CLAUDE_ROOTS = [
    Path("/root/.claude/projects"),
]

ALL_AGENT_ROOTS = [
    *CLAUDE_ROOTS,
    Path("/root/.codex/sessions"),
]


@dataclass
class Match:
    path: Path
    score: int = 0
    keyword_hits: int = 0
    cwd_hits: int = 0
    line_hits: list[int] = field(default_factory=list)
    first_ts: str | None = None
    last_ts: str | None = None
    cwd: str | None = None
    mtime: float = 0.0
    size: int = 0


def compact(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def iter_jsonl_files(roots: list[Path], max_files: int):
    count = 0
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".git"}]
            for filename in filenames:
                if filename.endswith(".jsonl"):
                    count += 1
                    if max_files and count > max_files:
                        return
                    yield Path(dirpath) / filename


def extract_text(obj: dict[str, Any]) -> str:
    parts: list[str] = []
    if isinstance(obj.get("content"), str):
        parts.append(obj["content"])
    message = obj.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    for key in ("text", "content"):
                        if isinstance(item.get(key), str):
                            parts.append(item[key])
                elif isinstance(item, str):
                    parts.append(item)
    if isinstance(obj.get("payload"), dict):
        payload = obj["payload"]
        for key in ("text", "last_agent_message", "title"):
            if isinstance(payload.get(key), str):
                parts.append(payload[key])
    return "\n".join(parts)


def inspect_file(
    path: Path,
    keyword_patterns: list[re.Pattern[str]],
    cwd_pattern: re.Pattern[str] | None,
    require_cwd: bool,
    max_bytes: int,
) -> Match | None:
    match = Match(path=path)
    try:
        stat = path.stat()
    except OSError:
        return None
    match.mtime = stat.st_mtime
    match.size = stat.st_size
    if max_bytes and stat.st_size > max_bytes:
        return None

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    with handle:
        for line_no, line in enumerate(handle, 1):
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                obj = {}
                text = line
            else:
                ts = obj.get("timestamp") or obj.get("created_at")
                if ts:
                    match.first_ts = match.first_ts or ts
                    match.last_ts = ts
                if isinstance(obj.get("cwd"), str):
                    match.cwd = obj["cwd"]
                text = extract_text(obj)

            if cwd_pattern and (cwd_pattern.search(str(match.cwd or "")) or cwd_pattern.search(line)):
                match.cwd_hits += 1
            line_keyword_hits = sum(1 for pattern in keyword_patterns if pattern.search(text) or pattern.search(line))
            if line_keyword_hits:
                match.keyword_hits += line_keyword_hits
                if len(match.line_hits) < 12:
                    match.line_hits.append(line_no)

    if match.keyword_hits <= 0:
        return None
    if require_cwd and cwd_pattern and match.cwd_hits <= 0:
        return None

    match.score = match.keyword_hits * 10 + min(match.cwd_hits, 20)
    if match.score <= 0:
        return None
    return match


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keywords", nargs="+", help="Keywords or regexes to search for")
    parser.add_argument("--cwd", help="Optional cwd regex; filters matches by default")
    parser.add_argument("--no-cwd-filter", action="store_true", help="Use --cwd only as a score boost")
    parser.add_argument("--root", action="append", type=Path, help="Search root; may be repeated")
    parser.add_argument("--all-agents", action="store_true", help="Search Claude and Codex roots")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-files", type=int, default=1000, help="Maximum JSONL files to inspect; 0 means no limit")
    parser.add_argument("--max-bytes", type=int, default=5_000_000, help="Skip files larger than this; 0 means no limit")
    parser.add_argument("--literal", action="store_true", help="Treat keywords as literal strings")
    args = parser.parse_args()

    flags = re.IGNORECASE
    keyword_patterns = [
        re.compile(re.escape(word) if args.literal else word, flags)
        for word in args.keywords
    ]
    cwd_pattern = re.compile(args.cwd, flags) if args.cwd else None
    if args.root:
        roots = args.root
    elif args.all_agents:
        roots = ALL_AGENT_ROOTS
    else:
        roots = CLAUDE_ROOTS

    matches: list[Match] = []
    for path in iter_jsonl_files(roots, args.max_files):
        match = inspect_file(path, keyword_patterns, cwd_pattern, not args.no_cwd_filter, args.max_bytes)
        if match:
            matches.append(match)

    matches.sort(key=lambda item: (item.score, item.mtime), reverse=True)
    for item in matches[: args.limit]:
        mtime = datetime.fromtimestamp(item.mtime).isoformat(timespec="seconds")
        print(f"score={item.score} keyword_hits={item.keyword_hits} cwd_hits={item.cwd_hits}")
        print(f"  path={item.path}")
        print(f"  mtime={mtime} size={item.size}")
        print(f"  time={item.first_ts} -> {item.last_ts}")
        print(f"  cwd={item.cwd}")
        print(f"  lines={item.line_hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
