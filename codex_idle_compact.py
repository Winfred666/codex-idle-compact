"""Fail-closed idle-compaction observer for an existing Codex App Server."""

import argparse
import asyncio
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

VERSION = "0.1.0"
BLOCKER = ("Automatic compaction is unavailable: the audited native API can replace "
           "an active turn and has no atomic idle/queue precondition.")


def context_usage(value):
    """Return (latest footprint, effective window), never lifetime totals."""
    if not isinstance(value, dict) or not isinstance(value.get("last"), dict):
        return None
    last = value["last"]
    fields = [last.get(k) for k in ("inputTokens", "outputTokens", "totalTokens")]
    window = value.get("modelContextWindow")
    if any(type(n) is not int or n < 0 for n in fields + [window]):
        return None
    input_tokens, output_tokens, total = fields
    if window == 0 or total != input_tokens + output_tokens or total > window:
        return None
    return total, window


@dataclass
class Session:
    turn: str = ""
    status: str = "unknown"
    usage: object = None
    complete: bool = False
    ended: bool = False
    compacting: bool = False
    finished: float = 0
    deadline: object = None
    consumed: bool = False
    cooldown: float = 0

    def cancel(self):
        self.complete = False
        self.deadline = None


class Observer:
    """Observed candidates are reports only. This class cannot send RPCs."""

    def __init__(self, idle_seconds=1200, clock=time.monotonic):
        if not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise ValueError("idle duration must be finite and positive")
        self.idle = idle_seconds
        self.clock = clock
        self.sessions = {}

    def message(self, message, client=False):
        if not isinstance(message, dict):
            return
        method = message.get("method", "")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        thread = params.get("threadId")
        if not isinstance(thread, str) or not thread:
            return
        if method in {"thread/closed", "thread/archived", "thread/deleted"}:
            self.sessions.pop(thread, None)
            return
        if client:
            # Even a metadata read may reflect user activity; err on the quiet side.
            if thread in self.sessions:
                self.sessions[thread].cancel()
            return
        s = self.sessions.get(thread)
        if method == "turn/started":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                return
            if s is not None and s.turn == turn["id"]:
                return
            self.sessions[thread] = Session(
                turn=turn["id"], status="active", cooldown=s.cooldown if s else 0)
            return
        # Never infer idle time or arm from a replayed historical snapshot.
        if s is None:
            return
        if method == "thread/tokenUsage/updated" and params.get("turnId") == s.turn:
            s.usage = context_usage(params.get("tokenUsage"))
        elif method == "thread/status/changed":
            status = params.get("status")
            s.status = status.get("type", "unknown") if isinstance(status, dict) else "unknown"
            if s.status != "idle":
                s.cancel()
        elif method == "thread/queue/changed":
            s.cancel()
        elif method in {"item/started", "item/completed"}:
            if params.get("turnId") != s.turn:
                return
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "contextCompaction":
                s.compacting = True
                s.usage = None
                s.cancel()
            elif method == "item/started":
                s.cancel()
        elif method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != s.turn:
                return
            if s.ended:
                return
            s.ended = True
            items = turn.get("items", [])
            if not isinstance(items, list):
                s.cancel()
                return
            s.compacting |= any(isinstance(i, dict) and i.get("type") == "contextCompaction"
                                for i in items)
            s.complete = turn.get("status") == "completed" and not s.compacting
            s.finished = self.clock()
        self._arm(s)

    def _arm(self, s):
        if (s.complete and not s.compacting and not s.consumed and s.status == "idle"
                and s.usage is not None and s.usage[0] * 100 >= s.usage[1] * 40):
            s.deadline = max(s.finished + self.idle, s.cooldown)
        else:
            s.deadline = None

    def tick(self):
        now = self.clock()
        reports = []
        for thread, s in self.sessions.items():
            self._arm(s)
            if s.deadline is None or now < s.deadline:
                continue
            late = now - s.deadline > 60
            s.consumed = True
            s.deadline = None
            s.cooldown = now + self.idle
            reports.append({"event": "skipped_late" if late else "blocked",
                            "threadId": thread, "turnId": s.turn,
                            "usedTokens": s.usage[0], "contextWindow": s.usage[1],
                            "idleSeconds": round(now - s.finished, 3),
                            "compactionStarted": False, "reason": BLOCKER})
        return reports


def log(value):
    print(json.dumps(value, separators=(",", ":")), file=sys.stderr, flush=True)


def validate_socket(path):
    path = Path(path).expanduser().resolve()
    if not stat.S_ISSOCK(path.stat().st_mode):
        raise ValueError("--socket must identify an existing owning App Server socket")
    return path


def endpoint_lock(path):
    """One observer per canonical endpoint; flock releases after a crash."""
    directory = Path(tempfile.gettempdir()) / ("codex-idle-compact-" + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("observer lock directory must be private and owned by this user")
    name = hashlib.sha256(str(path).encode()).hexdigest() + ".lock"
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(fd, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


async def relay(reader, write, observer, client):
    while True:
        line = await reader.readline()
        if not line:
            return
        try:
            observer.message(json.loads(line), client=client)
        except (ValueError, TypeError):
            # Unknown input must not leave a previously armed report behind.
            observer.sessions.clear()
        await write(line)


async def proxy(args):
    from websockets.asyncio.client import unix_connect
    from websockets.exceptions import WebSocketException

    path = validate_socket(args.socket)
    with endpoint_lock(path):
        observer = Observer(args.idle_minutes * 60)
        transport = None
        tasks = []
        try:
            # Codex's Unix listener uses WebSocket frames, not JSONL socket bytes.
            async with unix_connect(str(path), compression=None, max_size=64 * 1024 * 1024,
                                    open_timeout=10, close_timeout=2) as connection:
                log({"event": "observer_started", "automaticCompaction": False, "reason": BLOCKER})
                reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
                transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                    lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)

                async def to_server(line):
                    await connection.send(line.decode("utf-8").removesuffix("\n"))

                async def from_server():
                    async for payload in connection:
                        if not isinstance(payload, str):
                            raise ValueError("expected a JSON text frame")
                        try:
                            observer.message(json.loads(payload))
                        except (ValueError, TypeError):
                            observer.sessions.clear()
                        sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
                        sys.stdout.buffer.flush()

                async def timer():
                    while True:
                        await asyncio.sleep(min(1, observer.idle))
                        for report in observer.tick():
                            log(report)

                tasks = [asyncio.create_task(relay(reader, to_server, observer, True)),
                         asyncio.create_task(from_server()), asyncio.create_task(timer())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except WebSocketException as error:
            raise ValueError("owner WebSocket connection failed") from error
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if transport is not None:
                transport.close()
        return 0


def doctor(args):
    version = subprocess.run([args.codex, "--version"], capture_output=True,
                             text=True, check=True, timeout=15).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="codex-idle-schema-") as directory:
        subprocess.run([args.codex, "app-server", "generate-json-schema", "--experimental",
                        "--out", directory], capture_output=True, check=True, timeout=30)
        schema = json.loads((Path(directory) / "v2/ThreadCompactStartParams.json").read_text())
    fields = sorted(schema.get("properties", {}))
    result = {"cliVersion": version, "compactRequestFields": fields,
              "automaticCompaction": False, "reason": BLOCKER if fields == ["threadId"]
              else "Unreviewed protocol: atomic idle safety has not been verified."}
    if args.socket:
        result["socketExists"] = validate_socket(args.socket).exists()
    print(json.dumps(result, indent=2))
    return 2


def minutes(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("minutes must be a number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("minutes must be finite and greater than zero")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "proxy"):
        command = commands.add_parser(name)
        if name == "doctor":
            command.add_argument("--codex", default="codex", help="native Codex executable")
        command.add_argument("--socket", required=name == "proxy", help="existing owner socket")
        if name == "proxy":
            command.add_argument("--idle-minutes", type=minutes, default=20)
    args = parser.parse_args(argv)
    try:
        return doctor(args) if args.command == "doctor" else asyncio.run(proxy(args))
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        # Never dump protocol messages, prompts, environment, or subprocess output.
        log({"event": "unavailable", "errorType": type(error).__name__,
             "reason": "Check installed dependencies, the executable, owner socket, and observer lock."})
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
