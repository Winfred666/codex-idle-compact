"""Automatic idle compaction through the owning native Codex App Server."""
import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import time

from idle_plugin import hook, lock, owner_socket, private_directory, read_signal, settings

VERSION = '0.2.0'
wheel = Path(__file__).with_name('vendor') / 'websockets-15.0.1-py3-none-any.whl'
if wheel.exists():
    sys.path.insert(0, str(wheel))
from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException


def context_usage(value):
    """Latest model-response footprint, never lifetime totals or duplicated cache tokens."""
    if not isinstance(value, dict) or not isinstance(value.get('last'), dict):
        return None
    last = value['last']
    fields = [last.get(k) for k in ('inputTokens', 'outputTokens', 'totalTokens')]
    window = value.get('modelContextWindow')
    if any(type(n) is not int or n < 0 for n in fields + [window]):
        return None
    inputs, outputs, total = fields
    if window == 0 or total != inputs + outputs or total > window:
        return None
    return total, window


class RpcError(Exception):
    pass


class RPC:
    def __init__(self, connection, callback):
        self.connection, self.callback = connection, callback
        self.pending, self.serial = {}, 0
        self.reader = asyncio.create_task(self.receive())

    @classmethod
    async def open(cls, path, owner_pid, callback):
        sock, pid = owner_socket(path)
        if pid != owner_pid:
            sock.close()
            raise RpcError('owning server changed')
        try:
            connection = await unix_connect(sock=sock, compression=None, open_timeout=5,
                                            close_timeout=1, max_size=64 * 1024 * 1024)
        except BaseException:
            sock.close()
            raise
        rpc = cls(connection, callback)
        try:
            await rpc.call('initialize', {'clientInfo': {'name': 'codex_idle_compact', 'version': VERSION},
                                         'capabilities': {'experimentalApi': True}})
            await connection.send(json.dumps({'method': 'initialized', 'params': {}}))
        except BaseException:
            await rpc.close()
            raise
        return rpc

    async def call(self, method, params):
        self.serial += 1
        request_id = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.connection.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(request_id, None)

    async def receive(self):
        try:
            async for payload in self.connection:
                message = json.loads(payload)
                if 'method' in message:
                    self.callback(message)  # Never answer another client's approval requests.
                elif message.get('id') in self.pending:
                    future = self.pending[message['id']]
                    if not future.done():
                        if 'error' in message:
                            future.set_exception(RpcError('RPC rejected'))
                        else:
                            future.set_result(message['result'])
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RpcError('owner disconnected'))

    async def close(self):
        await self.connection.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)


@dataclass
class Session:
    thread: str
    owner: tuple
    turn: str = ''
    status: str = 'unknown'
    usage: object = None
    usage_turn: str = ''
    stop_turn: str = ''
    finished: object = None
    ended: bool = False
    compacting: bool = False
    consumed: bool = False
    locked: bool = False
    cooldown: float = 0
    revision: int = 0
    attempt: object = None
    compact_turn: str = ''
    compact_item: str = ''
    item_completed: bool = False

    def cancel(self):
        self.stop_turn = ''
        self.revision += 1

    def ready(self, now, idle):
        return (not self.locked and not self.consumed and not self.compacting
                and self.status == 'idle' and self.ended and self.finished is not None
                and self.stop_turn == self.turn and self.usage_turn == self.turn
                and self.usage is not None and self.usage[0] * 100 >= self.usage[1] * 40
                and now >= max(self.finished + idle, self.cooldown))

    def event(self, message, now):
        method, p = message.get('method'), message.get('params', {})
        if not isinstance(p, dict):
            self.cancel()
            return
        if method == 'turn/started':
            turn = p['turn']['id']
            if turn != self.turn:
                self.turn, self.status, self.finished = turn, 'active', None
                self.ended = self.compacting = self.consumed = False
                self.usage, self.usage_turn = None, ''
                self.cancel()
        elif method == 'thread/tokenUsage/updated':
            value, turn = context_usage(p.get('tokenUsage')), p.get('turnId')
            if (self.usage, self.usage_turn) != (value, turn):
                self.usage, self.usage_turn = value, turn
                self.revision += 1
        elif method == 'thread/status/changed':
            self.status = p['status']['type']
            if self.status != 'idle':
                self.cancel()
        elif method in {'thread/queue/changed', 'thread/settings/updated', 'thread/goal/updated'}:
            self.cancel()
        elif method in {'item/started', 'item/completed'}:
            item = p.get('item', {})
            if item.get('type') == 'contextCompaction':
                self.compacting = True
                self.cancel()
                if self.attempt is not None:
                    if method == 'item/started' and not self.compact_turn:
                        self.compact_turn, self.compact_item = p.get('turnId'), item.get('id')
                    if p.get('turnId') == self.compact_turn and item.get('id') == self.compact_item:
                        self.item_completed = method == 'item/completed'
            elif method == 'item/started':
                self.cancel()
        elif method == 'turn/completed':
            turn = p['turn']
            if any(item.get('type') == 'contextCompaction' for item in turn.get('items', [])):
                self.compacting = True
                self.cancel()
            if self.attempt is not None and turn['id'] == self.compact_turn and not self.attempt.done():
                self.attempt.set_result(turn['status'] == 'completed' and self.item_completed)
            if turn['id'] == self.turn and not self.ended:
                self.ended = True
                self.finished = now
                if turn['status'] != 'completed':
                    self.cancel()
        elif method == 'error':
            self.cancel()
            if self.attempt is not None and p.get('turnId') == self.compact_turn and not self.attempt.done():
                self.attempt.set_result(False)
        elif 'id' in message:  # A server request, including approval / user-input requests.
            self.cancel()


class Watcher:
    def __init__(self, data, emit, clock=time.monotonic):
        self.data, self.emit, self.clock = data, emit, clock
        self.sessions, self.connections, self.signals, self.jobs = {}, {}, {}, set()
        self.started = time.time_ns() - 5_000_000_000
        self.idle = settings()[0]
        self.hook_lock = asyncio.Lock()

    def event(self, owner, message):
        params = message.get('params', {})
        if not isinstance(params, dict):
            return
        thread = params.get('threadId')
        s = self.sessions.get(thread)
        if s is None or s.owner != owner:
            return
        if message.get('method') in {'thread/closed', 'thread/archived', 'thread/deleted'}:
            s.cancel()
            self.sessions.pop(thread, None)
            return
        try:
            s.event(message, self.clock())
        except (ValueError, TypeError, KeyError, AttributeError):
            s.cancel()
            s.usage = None

    async def attach(self, signal):
        thread = signal['threadId']
        owner = signal['socket'], signal['ownerPid']
        rpc = self.connections.get(owner)
        if rpc is None:
            rpc = await RPC.open(*owner, lambda message: self.event(owner, message))
            self.connections[owner] = rpc
        snapshot = (await rpc.call('thread/read', {'threadId': thread, 'includeTurns': False}))['thread']
        if snapshot['id'] != thread or snapshot['status']['type'] == 'notLoaded' or snapshot.get('parentThreadId'):
            raise RpcError('thread is not a loaded root on its verified owner')
        s = Session(thread, owner)
        self.sessions[thread] = s
        # Called only after an owner-PID check and a loaded-thread read; never cold-resume a stored ID.
        result = await rpc.call('thread/resume', {'threadId': thread, 'excludeTurns': True,
                                'initialTurnsPage': {'limit': 1, 'itemsView': 'notLoaded'}})
        if result['thread']['id'] != thread:
            raise RpcError('thread identity changed')
        s.status = result['thread']['status']['type']
        turns = (result.get('initialTurnsPage') or {}).get('data', [])
        if turns and not s.turn:
            turn = turns[0]
            s.turn = turn['id']
            s.ended = turn['status'] == 'completed'
            s.finished = self.clock() if s.ended else None
        self.emit('watching', threadId=thread)
        return s

    async def refresh_hooks(self):
        async with self.hook_lock:
            await self.read_hooks()

    async def read_hooks(self):
        for path in (self.data / 'sessions').glob('*.json'):
            signal = None
            try:
                signal = read_signal(path)
                thread = signal['threadId']
                if path.stem != thread:
                    raise ValueError('signal filename mismatch')
                if signal['stamp'] < self.started or self.signals.get(thread) == signal['stamp']:
                    continue
                self.signals[thread] = signal['stamp']
                if signal['event'] == 'SessionEnd':
                    s = self.sessions.pop(thread, None)
                    if s is not None:
                        s.cancel()
                        await self.connections[s.owner].call('thread/unsubscribe', {'threadId': thread})
                    continue
                s = self.sessions.get(thread)
                owner = signal['socket'], signal['ownerPid']
                if s is not None and s.owner != owner:
                    s.cancel()
                    self.sessions.pop(thread, None)
                    s = None
                if s is None:
                    s = await self.attach(signal)
                if signal['event'] == 'Stop' and signal.get('turnId'):
                    s.stop_turn = signal['turnId']
                    s.revision += 1
                elif signal['event'] != 'SessionStart':
                    s.cancel()
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RpcError, WebSocketException, asyncio.TimeoutError):
                s = self.sessions.pop(path.stem, None)
                if s is not None:
                    s.cancel()
                self.emit('hook_unavailable')

    async def compact(self, s):
        s.locked = s.consumed = True
        revision, turn = s.revision, s.turn
        rpc = self.connections[s.owner]
        try:
            queue = await rpc.call('thread/queue/list', {'threadId': s.thread, 'limit': 1})
            terminals = await rpc.call('thread/backgroundTerminals/list', {'threadId': s.thread, 'limit': 1})
            goal = (await rpc.call('thread/goal/get', {'threadId': s.thread}))['goal']
            # The API defaults to interactive sources and the current provider, hiding subagents.
            descendants = await rpc.call('thread/list', {'ancestorThreadId': s.thread, 'limit': 100,
                'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'unknown'],
                'modelProviders': []})
            turns = await rpc.call('thread/turns/list', {'threadId': s.thread, 'limit': 1, 'itemsView': 'notLoaded'})
            snapshot = (await rpc.call('thread/read', {'threadId': s.thread, 'includeTurns': False}))['thread']
            await self.refresh_hooks()
            latest = turns['data'][0]
            if (queue['data'] or queue.get('nextCursor') or terminals['data'] or terminals.get('nextCursor')
                    or (goal is not None and goal.get('status') not in {'complete', 'paused', 'blocked', 'budgetLimited', 'usageLimited'})
                    or descendants.get('nextCursor')
                    or any(t['status']['type'] not in {'idle', 'notLoaded'} for t in descendants['data'])
                    or latest['id'] != turn or latest['status'] != 'completed'
                    or snapshot['id'] != s.thread or snapshot['status']['type'] != 'idle'
                    or s.revision != revision or self.sessions.get(s.thread) is not s
                    or s.status != 'idle' or s.stop_turn != turn or s.usage_turn != turn
                    or s.usage is None or s.usage[0] * 100 < s.usage[1] * 40
                    or s.finished is None or self.clock() - s.finished < self.idle):
                self.emit('skipped_busy_or_changed', threadId=s.thread)
                return
            s.attempt = asyncio.get_running_loop().create_future()
            s.compact_turn, s.compact_item, s.item_completed = '', '', False
            s.cooldown = self.clock() + self.idle
            # shortcut: the native API has no atomic idle guard; replace this check-and-send when one exists.
            await rpc.call('thread/compact/start', {'threadId': s.thread})
            self.emit('compaction_started', threadId=s.thread, previousTurnId=turn)
            result = await asyncio.wait_for(s.attempt, 180)
            self.emit('compaction_completed' if result else 'compaction_failed', threadId=s.thread)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, RpcError, WebSocketException, asyncio.TimeoutError):
            self.emit('compaction_unconfirmed' if s.attempt is not None else 'preflight_unavailable', threadId=s.thread)
        finally:
            if s.attempt is not None and not s.attempt.done():
                s.attempt.cancel()
            s.attempt = None
            s.locked = False
            # A missing response is not grounds to retry an operation that may have executed.
            if s.turn in {turn, s.compact_turn}:
                s.consumed = True

    async def run(self):
        last_wall, last_mono = time.time(), self.clock()
        empty_since = self.clock()
        try:
            while Path(__file__).exists():
                await self.refresh_hooks()
                try:
                    self.idle = settings()[0]
                except (OSError, ValueError, TypeError):
                    for s in self.sessions.values():
                        s.cancel()
                    await asyncio.sleep(1)
                    continue
                now, wall = self.clock(), time.time()
                if wall - last_wall > 60 or now - last_mono > 60:
                    for s in self.sessions.values():
                        s.cancel()
                    self.emit('skipped_sleep_or_delay')
                last_wall, last_mono = wall, now
                for owner, rpc in list(self.connections.items()):
                    if rpc.reader.done():
                        for thread, s in list(self.sessions.items()):
                            if s.owner == owner:
                                s.cancel()
                                if s.attempt is not None and not s.attempt.done():
                                    s.attempt.set_exception(RpcError('owner disconnected'))
                                self.sessions.pop(thread)
                        await rpc.close()
                        self.connections.pop(owner)
                        self.emit('owner_disconnected')
                for s in list(self.sessions.values()):
                    if s.ready(now, self.idle):
                        s.locked = True
                        task = asyncio.create_task(self.compact(s))
                        self.jobs.add(task)
                        task.add_done_callback(self.jobs.discard)
                if self.sessions or self.jobs:
                    empty_since = now
                elif now - empty_since > 5:
                    return
                await asyncio.sleep(0.25)
        finally:
            for task in self.jobs:
                task.cancel()
            await asyncio.gather(*self.jobs, return_exceptions=True)
            for rpc in self.connections.values():
                await rpc.close()


def run_watcher(data):
    data = private_directory(data)
    try:
        handle = lock(data / 'watcher.lock')
    except BlockingIOError:
        return 0
    with handle:
        logger = logging.getLogger('codex-idle-compact')
        logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(data / 'watcher.log', maxBytes=262144, backupCount=1)
        logger.addHandler(handler)
        def emit(event, **fields):
            logger.info(json.dumps({'event': event, **fields}))
        try:
            asyncio.run(Watcher(data, emit).run())
        finally:
            logger.removeHandler(handler)
            handler.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('hook')
    commands.add_parser('doctor')
    watch = commands.add_parser('watch')
    watch.add_argument('--data', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'hook':
        print(json.dumps(hook()))
        return 0
    try:
        if args.command == 'doctor':
            idle, endpoint = settings()
            sock, pid = owner_socket(endpoint)
            sock.close()
            print(json.dumps({'defaultEnabled': True, 'daemonReachable': True, 'idleMinutes': idle / 60,
                              'socket': str(endpoint), 'ownerPid': pid,
                              'note': 'Requires trusted plugin hooks in a new daemon-backed session; idle checks are best effort.'}, indent=2))
            return 0
        return run_watcher(args.data)
    except (OSError, ValueError, KeyError, TypeError, WebSocketException):
        print('codex-idle-compact: configuration or owning daemon unavailable; no session was started or restarted.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
