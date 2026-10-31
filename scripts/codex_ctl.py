#!/usr/bin/env python3
"""Control Codex threads through the local app-server daemon.

Every command is a short-lived client: it connects through `codex app-server proxy`
to the daemon's control socket, so threads and running turns survive the client.
The control socket speaks WebSocket; Python on Windows lacks AF_UNIX, so both
platforms use the proxy subprocess as the byte stream.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable

EXIT_COMPLETED, EXIT_ERROR, EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OVERLOADED, EXIT_TIMEOUT, EXIT_IDLE = 0, 1, 2, 3, 4, 5, 6
SUB_AGENT_SOURCES = ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]
NOISY = ("thread/tokenUsage/updated", "account/rateLimits/updated", "thread/goal/cleared", "configWarning",
         "remoteControl/status/changed", "warning", "deprecationNotice", "mcpServer/startupStatus/updated")


def codex_bin() -> str:
    return shutil.which("codex") or "codex"


class ProxySocket:
    """Minimal WebSocket client over the stdio of `codex app-server proxy`."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen([codex_bin(), "app-server", "proxy"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.lock = threading.Lock()
        key = base64.b64encode(os.urandom(16)).decode()
        self._write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self._exact(1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.decode(errors="replace"))

    def _write(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.poll() is not None else ""
                raise EOFError(err or "app-server proxy closed")
            buf += chunk
        return buf

    def _frame(self, opcode: int, data: bytes) -> None:
        n, mask = len(data), os.urandom(4)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.lock:
            self._write(head + mask + masked)

    def send(self, text: str) -> None:
        self._frame(1, text.encode())

    def recv(self) -> str:
        message = b""
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            data, opcode = self._exact(n), b0 & 0x0F
            if opcode == 8:
                raise EOFError("app-server closed the connection")
            if opcode == 9:
                self._frame(10, data)
                continue
            if opcode == 10:
                continue
            message += data
            if b0 & 0x80:
                return message.decode()

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()


class Client:
    def __init__(self) -> None:
        try:
            self.sock = ProxySocket()
        except (EOFError, ConnectionError, OSError):
            # 首次使用或 daemon 已退出：启动后重连一次
            subprocess.run([codex_bin(), "app-server", "daemon", "start"], check=True, capture_output=True)
            self.sock = ProxySocket()
        self.events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.next_id = 0
        threading.Thread(target=self._reader, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "codex-ctl", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        self.sock.send(json.dumps({"method": "initialized"}))

    def _reader(self) -> None:
        try:
            while True:
                msg = json.loads(self.sock.recv())
                if "id" in msg and "method" not in msg and msg["id"] in self.responses:
                    self.responses[msg["id"]].put(msg)
                else:
                    self.events.put(msg)
        except (EOFError, OSError, ValueError):
            self.events.put(None)
            for q in self.responses.values():
                q.put({"error": {"message": "connection closed"}})

    def call(self, method: str, params: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        self.next_id += 1
        rid, box = self.next_id, queue.Queue()
        self.responses[rid] = box
        self.sock.send(json.dumps({"id": rid, "method": method, "params": params}))
        try:
            msg = box.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{method} timed out") from None
        finally:
            self.responses.pop(rid, None)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg["result"]

    def close(self) -> None:
        self.sock.close()


def read_prompt(args: argparse.Namespace) -> str:
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return fh.read()
    if args.prompt == "-":
        return sys.stdin.read()
    if not args.prompt:
        sys.exit("prompt required (argument, '-' for stdin, or --file)")
    return args.prompt


def text_input(prompt: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": prompt}]


def run_options(args: argparse.Namespace) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if getattr(args, "cwd", None):
        opts["cwd"] = os.path.abspath(args.cwd)
    if getattr(args, "model", None):
        opts["model"] = args.model
    if getattr(args, "full_access", True):
        opts.update(approvalPolicy="never", sandbox="danger-full-access")
    return opts


def resume(c: Client, thread_id: str, args: argparse.Namespace | None = None) -> dict[str, Any]:
    """Load the thread into the daemon and subscribe this connection to its events."""
    params = {"threadId": thread_id, "excludeTurns": True, **(run_options(args) if args else {})}
    return c.call("thread/resume", params)["thread"]


def recent_turns(c: Client, thread_id: str, limit: int = 1) -> list[dict[str, Any]]:
    return c.call("thread/turns/list", {"threadId": thread_id, "limit": limit, "sortDirection": "desc",
                                        "itemsView": "notLoaded"})["data"]


def latest_turn(c: Client, thread_id: str) -> dict[str, Any] | None:
    turns = recent_turns(c, thread_id)
    return turns[0] if turns else None


def final_message(c: Client, thread_id: str, turn_id: str) -> str:
    items = c.call("thread/items/list", {"threadId": thread_id, "turnId": turn_id, "limit": 50,
                                         "sortDirection": "desc"})["data"]
    return next((i["item"].get("text", "") for i in items if i["item"].get("type") == "agentMessage"), "")


def emit(args: argparse.Namespace, payload: dict[str, Any], human: str) -> None:
    print(json.dumps(payload, ensure_ascii=False) if args.json else human, flush=True)


def describe_event(msg: dict[str, Any]) -> str | None:
    method, p = msg.get("method", ""), msg.get("params", {})
    if method in NOISY or method.endswith("elta") or method.startswith("item/started"):
        return None
    if method == "item/completed":
        item = p.get("item", {})
        kind = item.get("type")
        if kind == "commandExecution":
            return f"$ {item.get('command', '')}  -> exit {item.get('exitCode')}"
        if kind == "agentMessage":
            return f"agent: {item.get('text', '')}"
        if kind == "fileChange":
            return "files: " + ", ".join(ch.get("path", "?") for ch in item.get("changes", []))
        if kind in ("userMessage", "reasoning"):
            return None
        return f"item: {kind}"
    if method == "error":
        err = p.get("error", {})
        return f"error{' (retrying)' if p.get('willRetry') else ''}: {err.get('message', '')}"
    if method in ("turn/started", "turn/completed"):
        return f"{method} {p.get('turn', {}).get('status', '')}"
    if method == "thread/status/changed":
        return f"status: {p.get('status', {}).get('type')}"
    return method


def is_overload(err: dict[str, Any]) -> bool:
    info = err.get("codexErrorInfo")
    names = list(info.keys()) if isinstance(info, dict) else [info]
    return "serverOverloaded" in names or "overloaded" in str(err.get("message", "")).lower()


def follow(c: Client, args: argparse.Namespace, thread_id: str, turn_id: str, on_line: Callable[[str], None]) -> int:
    deadline = time.time() + args.timeout if args.timeout else None
    last_event = time.time()
    while True:
        remaining = None if deadline is None else deadline - time.time()
        if remaining is not None and remaining <= 0:
            return EXIT_TIMEOUT
        # 任何本会话事件（含输出增量）都算活动；长时间静默的命令同样会触发，由主 Agent 查看后重新 wait
        if args.idle and time.time() - last_event >= args.idle:
            return EXIT_IDLE
        try:
            msg = c.events.get(timeout=min(remaining, 5) if remaining is not None else 5)
        except queue.Empty:
            continue
        if msg is None:
            raise ConnectionError("app-server connection closed while waiting")
        p = msg.get("params", {})
        if p.get("threadId") not in (None, thread_id):
            continue
        last_event = time.time()
        line = describe_event(msg)
        if line:
            on_line(line)
        if msg.get("method") == "error" and is_overload(p.get("error", {})) and not args.keep_on_overload:
            c.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            return EXIT_OVERLOADED
        if msg.get("method") == "turn/completed" and p.get("turn", {}).get("id") == turn_id:
            status = p["turn"].get("status")
            return {"completed": EXIT_COMPLETED, "interrupted": EXIT_INTERRUPTED}.get(status, EXIT_FAILED)


def wait_turn(c: Client, args: argparse.Namespace, thread_id: str, turn_id: str | None = None,
              started: bool = False) -> int:
    """Subscribe first, then decide from the live thread status, so a completion cannot slip in between.

    The persisted turn list lags right after `turn/start` (a new turn can read as interrupted, a new
    thread as unsupported), so it is consulted only for turns that are no longer running.
    """
    thread = resume(c, thread_id)
    running = started or thread["status"].get("type") == "active"
    if running and not started:
        active_id = latest_turn(c, thread_id)["id"]
        running = turn_id in (None, active_id)
        turn_id = turn_id or active_id
    turn = None
    if not running:
        turns = recent_turns(c, thread_id, 20 if turn_id else 1)
        turn = next((t for t in turns if t["id"] == turn_id), None) if turn_id else (turns[0] if turns else None)
        if not turn:
            sys.exit(f"turn {turn_id or '(latest)'} not found in thread {thread_id}")
        turn_id = turn["id"]
    log = open(args.log, "a", encoding="utf-8") if args.log else None

    def on_line(line: str) -> None:
        stamped = time.strftime("[%H:%M:%S] ") + line
        if log:
            log.write(stamped + "\n")
            log.flush()
        if args.verbose:
            print(stamped, file=sys.stderr, flush=True)

    if turn:
        code = {"completed": EXIT_COMPLETED, "interrupted": EXIT_INTERRUPTED}.get(turn["status"], EXIT_FAILED)
    else:
        code = follow(c, args, thread_id, turn_id, on_line)
    status = {EXIT_COMPLETED: "completed", EXIT_FAILED: "failed", EXIT_INTERRUPTED: "interrupted",
              EXIT_OVERLOADED: "overloaded", EXIT_TIMEOUT: "running", EXIT_IDLE: "idle"}[code]
    message = final_message(c, thread_id, turn_id) if code not in (EXIT_TIMEOUT, EXIT_IDLE) else ""
    if log:
        log.write(time.strftime("[%H:%M:%S] ") + f"result: {status}\n")
    emit(args, {"thread": thread_id, "turn": turn_id, "status": status, "message": message},
         f"thread {thread_id}\nturn {turn_id}\nstatus {status}\n\n{message}".rstrip())
    return code


def start_turn(c: Client, args: argparse.Namespace, thread_id: str) -> int:
    turn = c.call("turn/start", {"threadId": thread_id, "input": text_input(read_prompt(args))})["turn"]
    if args.wait:
        return wait_turn(c, args, thread_id, turn["id"], started=True)
    emit(args, {"thread": thread_id, "turn": turn["id"]}, f"thread {thread_id}\nturn {turn['id']}")
    return EXIT_COMPLETED


def cmd_start(c: Client, args: argparse.Namespace) -> int:
    opts = run_options(args)
    if args.provider:
        opts["modelProvider"] = args.provider
    thread = c.call("thread/start", opts)["thread"]
    if args.name:
        c.call("thread/name/set", {"threadId": thread["id"], "name": args.name})
    return start_turn(c, args, thread["id"])


def cmd_send(c: Client, args: argparse.Namespace) -> int:
    thread = resume(c, args.thread, args)
    if thread["status"].get("type") == "active":
        sys.exit("thread has an active turn; use `steer` to add input or `interrupt` first")
    return start_turn(c, args, args.thread)


def active_turn_id(c: Client, thread_id: str) -> str:
    if resume(c, thread_id)["status"].get("type") != "active":
        sys.exit("thread has no active turn; use `send` to start one")
    return latest_turn(c, thread_id)["id"]


def cmd_steer(c: Client, args: argparse.Namespace) -> int:
    turn_id = active_turn_id(c, args.thread)
    c.call("turn/steer", {"threadId": args.thread, "expectedTurnId": turn_id, "input": text_input(read_prompt(args))})
    emit(args, {"thread": args.thread, "turn": turn_id, "steered": True}, f"steered turn {turn_id}")
    return EXIT_COMPLETED


def cmd_interrupt(c: Client, args: argparse.Namespace) -> int:
    turn_id = active_turn_id(c, args.thread)
    c.call("turn/interrupt", {"threadId": args.thread, "turnId": turn_id})
    emit(args, {"thread": args.thread, "turn": turn_id, "interrupted": True},
         f"interrupted turn {turn_id}; commands it started may still be running")
    return EXIT_COMPLETED


def cmd_wait(c: Client, args: argparse.Namespace) -> int:
    return wait_turn(c, args, args.thread, args.turn)


def cmd_status(c: Client, args: argparse.Namespace) -> int:
    thread = c.call("thread/read", {"threadId": args.thread, "includeTurns": False})["thread"]
    turn = latest_turn(c, args.thread)
    payload = {"thread": args.thread, "name": thread.get("name"), "cwd": thread.get("cwd"),
               "source": thread.get("source"), "status": thread["status"].get("type"),
               "turn": turn and {"id": turn["id"], "status": turn["status"], "error": turn.get("error")}}
    if turn and turn["status"] != "inProgress":
        payload["message"] = final_message(c, args.thread, turn["id"])
    human = [f"{k}: {v}" for k, v in payload.items() if k not in ("message", "turn")]
    if turn:
        human.append(f"turn: {turn['id']} {turn['status']}" + (f" ({turn['error']['message']})" if turn.get("error") else ""))
    if payload.get("message"):
        human += ["", payload["message"]]
    emit(args, payload, "\n".join(human))
    return EXIT_COMPLETED


def cmd_list(c: Client, args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"limit": args.limit, "sortKey": "updated_at"}
    if args.cwd:
        params["cwd"] = os.path.abspath(args.cwd)
    # 不传 sourceKinds 时服务端默认会排除 exec，因此两种情况都显式列出
    params["sourceKinds"] = ["cli", "vscode", "exec", "appServer"] + (SUB_AGENT_SOURCES if args.all_sources else [])
    threads = c.call("thread/list", params)["data"]
    rows = [{"thread": t["id"], "updated": time.strftime("%m-%d %H:%M", time.localtime(t["updatedAt"])),
             "source": t.get("source"), "status": t["status"].get("type"), "cwd": t.get("cwd"),
             "title": t.get("name") or " ".join(str(t.get("preview", "")).split())[:60]} for t in threads]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for r in rows:
            print(f"{r['thread']}  {r['updated']}  {str(r['source']):<7} {r['status']:<10} {r['cwd']}  {r['title']}")
    return EXIT_COMPLETED


def cmd_archive(c: Client, args: argparse.Namespace) -> int:
    c.call("thread/archive", {"threadId": args.thread})
    emit(args, {"thread": args.thread, "archived": True}, f"archived {args.thread}")
    return EXIT_COMPLETED


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def prompt_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("prompt", nargs="?", help="prompt text, or '-' to read stdin")
        p.add_argument("-f", "--file", help="read prompt from a UTF-8 file")

    def wait_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--timeout", type=float, default=0, help="seconds to wait; 0 = until the turn ends")
        p.add_argument("--idle", type=float, default=0,
                       help="return exit 6 when the running turn emits no event for this many seconds")
        p.add_argument("--log", help="append timestamped events to this file")
        p.add_argument("-v", "--verbose", action="store_true", help="stream events to stderr")
        p.add_argument("--keep-on-overload", action="store_true",
                       help="do not interrupt the turn when upstream reports overload")

    def run_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cwd", help="working directory (start: default current directory)")
        p.add_argument("--model", help="model override")
        p.add_argument("--sandboxed", dest="full_access", action="store_false",
                       help="keep the configured approval/sandbox policy instead of never + danger-full-access")
        p.add_argument("--wait", action="store_true", help="wait for the new turn like `wait`")
        wait_args(p)

    p = sub.add_parser("start", help="create a thread and start its first turn")
    prompt_args(p); run_args(p)
    p.add_argument("--provider", help="model provider defined in config.toml (default: configured model_provider)")
    p.add_argument("--name", help="thread title shown in `codex resume`")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("send", help="start a new turn on an idle thread")
    p.add_argument("thread"); prompt_args(p); run_args(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("steer", help="add input to the active turn")
    p.add_argument("thread"); prompt_args(p)
    p.set_defaults(func=cmd_steer)

    p = sub.add_parser("interrupt", help="interrupt the active turn")
    p.add_argument("thread")
    p.set_defaults(func=cmd_interrupt)

    p = sub.add_parser("wait", help="wait for a turn to end and print its final message")
    p.add_argument("thread"); p.add_argument("--turn", help="turn id (default: latest)"); wait_args(p)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("status", help="show thread status and the latest turn")
    p.add_argument("thread")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="list recent threads")
    p.add_argument("--cwd"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--all-sources", action="store_true", help="include sub-agent threads")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("archive", help="archive a thread")
    p.add_argument("thread")
    p.set_defaults(func=cmd_archive)

    args = ap.parse_args()
    if args.cmd == "start" and not args.cwd:
        args.cwd = os.getcwd()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        c = Client()
    except Exception as exc:  # noqa: BLE001 - surface any startup failure as a CLI error
        print(f"cannot connect to codex app-server daemon: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        return args.func(c, args)
    except (RuntimeError, TimeoutError, ConnectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
