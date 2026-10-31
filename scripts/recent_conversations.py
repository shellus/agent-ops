#!/usr/bin/env python3
"""List recent local agent conversations for a working directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


CLAUDE_ROOT = Path("/root/.claude/projects")
CODEX_DB = Path("/root/.codex/state_5.sqlite")


@dataclass
class Conversation:
    agent: str
    path: Path
    sort_ts: float
    cwd: str | None = None
    title: str | None = None
    created: str | None = None
    updated: str | None = None
    source: str = "jsonl"


def compact(value: str | None, limit: int = 180) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def safe_resolve(value: str) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return str(Path(value).expanduser())


def path_matches(record_cwd: str | None, target_cwd: str, mode: str) -> bool:
    if not record_cwd:
        return False
    record = safe_resolve(record_cwd).rstrip("/")
    target = safe_resolve(target_cwd).rstrip("/")
    if record == target:
        return True
    record_under_target = record.startswith(target + "/")
    target_under_record = target.startswith(record + "/")
    if mode == "under":
        return record_under_target
    if mode == "related":
        return record_under_target or target_under_record
    return False


def iso_from_timestamp(value: float | int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value)).isoformat(timespec="seconds")


def iter_jsonl_files(root: Path, max_files: int):
    count = 0
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", "node_modules"}]
        for filename in filenames:
            if not filename.endswith(".jsonl"):
                continue
            count += 1
            if max_files and count > max_files:
                return
            yield Path(dirpath) / filename


def extract_cwd(obj: dict[str, Any]) -> str | None:
    if isinstance(obj.get("cwd"), str):
        return obj["cwd"]
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
        return payload["cwd"]
    return None


def extract_user_text(obj: dict[str, Any]) -> str | None:
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if payload.get("role") == "user":
            return text_from_content(payload.get("content"))
        if payload.get("type") == "message" and payload.get("role") == "user":
            return text_from_content(payload.get("content"))
        if isinstance(payload.get("title"), str):
            return payload["title"]

    message = obj.get("message")
    if isinstance(message, dict) and message.get("role") == "user":
        return text_from_content(message.get("content"))
    if isinstance(obj.get("content"), str):
        return obj["content"]
    return None


def text_from_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content"):
                    if isinstance(item.get(key), str):
                        parts.append(item[key])
    return "\n".join(parts) if parts else None


def inspect_jsonl(
    path: Path,
    agent: str,
    target_cwd: str,
    match_mode: str,
    sample_lines: int,
    max_bytes: int,
) -> Conversation | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if max_bytes and stat.st_size > max_bytes:
        return None

    cwd: str | None = None
    title: str | None = None
    created: str | None = None
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    with handle:
        for index, line in enumerate(handle):
            if sample_lines and index >= sample_lines:
                break
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ts = obj.get("timestamp") or obj.get("created_at")
            if isinstance(ts, str):
                created = created or ts
            cwd = cwd or extract_cwd(obj)
            title = title or compact(extract_user_text(obj))
            if cwd and title and created:
                break

    if not path_matches(cwd, target_cwd, match_mode):
        return None

    mtime = stat.st_mtime
    updated = iso_from_timestamp(mtime)
    return Conversation(
        agent=agent,
        path=path,
        cwd=cwd,
        title=title,
        created=created,
        updated=updated,
        sort_ts=mtime,
    )


def iter_codex_threads(target_cwd: str, match_mode: str) -> list[Conversation]:
    if not CODEX_DB.exists():
        return []
    query = """
        SELECT rollout_path, created_at, updated_at, created_at_ms, updated_at_ms, cwd, title
        FROM threads
        WHERE archived = 0
        ORDER BY updated_at DESC
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(CODEX_DB)
        rows = conn.execute(query).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

    conversations: list[Conversation] = []
    for rollout_path, created_at, updated_at, created_ms, updated_ms, cwd, title in rows:
        if not path_matches(cwd, target_cwd, match_mode):
            continue
        created_ts = (created_ms / 1000) if created_ms else created_at
        updated_ts = (updated_ms / 1000) if updated_ms else updated_at
        path = Path(rollout_path)
        conversations.append(
            Conversation(
                agent="codex",
                path=path,
                cwd=cwd,
                title=compact(title),
                created=iso_from_timestamp(created_ts),
                updated=iso_from_timestamp(updated_ts),
                sort_ts=float(updated_ts or 0),
                source="sqlite",
            )
        )
    return conversations


def collect_conversations(args: argparse.Namespace) -> list[Conversation]:
    agents = set(args.agent or ["claude", "codex"])
    conversations: list[Conversation] = []

    if "codex" in agents:
        conversations.extend(iter_codex_threads(args.cwd, args.match))
    if "claude" in agents:
        for path in iter_jsonl_files(CLAUDE_ROOT, args.max_files):
            item = inspect_jsonl(path, "claude", args.cwd, args.match, args.sample_lines, args.max_bytes)
            if item:
                conversations.append(item)
    conversations.sort(key=lambda item: item.sort_ts, reverse=True)
    return conversations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.getcwd(), help="Target cwd. Defaults to the current shell directory.")
    parser.add_argument(
        "--match",
        choices=["exact", "under", "related"],
        default="under",
        help="cwd match mode: exact, under target cwd, or either direction.",
    )
    parser.add_argument("--agent", action="append", choices=["claude", "codex"], help="Limit to an agent.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-files", type=int, default=2000, help="Maximum JSONL files per JSONL-backed agent; 0 means no limit.")
    parser.add_argument("--max-bytes", type=int, default=0, help="Skip JSONL files larger than this; 0 means no limit.")
    parser.add_argument("--sample-lines", type=int, default=80, help="JSONL lines to inspect for cwd/title metadata.")
    args = parser.parse_args()

    conversations = collect_conversations(args)
    for index, item in enumerate(conversations[: args.limit], 1):
        print(f"[{index}] agent={item.agent} source={item.source} updated={item.updated}")
        print(f"    path={item.path}")
        print(f"    cwd={item.cwd}")
        if item.created:
            print(f"    created={item.created}")
        if item.title:
            print(f"    title={item.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
