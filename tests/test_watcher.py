import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_idle_compact import Session, Watcher, RpcError, context_usage
from idle_plugin import lock, settings


def event(method, thread='a', **params):
    return {'method': method, 'params': {'threadId': thread, **params}}


def usage(total=400, window=1000):
    return {'last': {'inputTokens': total - 10, 'outputTokens': 10, 'totalTokens': total},
            'modelContextWindow': window, 'total': {'totalTokens': 9000000}}


def completed(s, turn='t', total=400, now=0):
    s.event(event('turn/started', turn={'id': turn}), now)
    s.event(event('thread/tokenUsage/updated', turnId=turn, tokenUsage=usage(total)), now)
    s.stop_turn = turn  # A matching Stop hook is required in addition to native events.
    s.event(event('turn/completed', turn={'id': turn, 'status': 'completed'}), now)
    s.event(event('thread/status/changed', status={'type': 'idle'}), now)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.s = Session('a', ('/owner.sock', 1))
        completed(self.s)

    def test_threshold_uses_latest_footprint_and_waits(self):
        self.assertFalse(self.s.ready(1199, 1200))
        self.assertTrue(self.s.ready(1200, 1200))
        completed(self.s, 'next', total=399)
        self.assertFalse(self.s.ready(1200, 1200))

    def test_missing_stop_or_usage_never_arms(self):
        for field, value in [('stop_turn', ''), ('usage', None), ('usage_turn', 'old')]:
            with self.subTest(field=field):
                self.setUp()
                setattr(self.s, field, value)
                self.assertFalse(self.s.ready(1200, 1200))

    def test_activity_cancels_until_new_completed_turn(self):
        for message in [event('thread/status/changed', status={'type': 'active'}),
                        event('thread/status/changed', status={'type': 'systemError'}),
                        event('thread/queue/changed'), event('thread/goal/updated'),
                        event('item/started', item={'type': 'commandExecution'}),
                        {'id': 1, **event('item/tool/requestUserInput')}]:
            with self.subTest(message=message):
                self.setUp()
                self.s.event(message, 1199)
                self.s.event(event('thread/status/changed', status={'type': 'idle'}), 1200)
                self.s.event(event('turn/completed', turn={'id': 't', 'status': 'completed'}), 1200)
                self.assertFalse(self.s.ready(2400, 1200))
                completed(self.s, 'next', now=2400)
                self.assertTrue(self.s.ready(3600, 1200))

    def test_new_turn_clears_usage_and_disarms(self):
        self.s.event(event('turn/started', turn={'id': 'next'}), 1200)
        self.assertIsNone(self.s.usage)
        self.assertFalse(self.s.ready(99999, 1200))

    def test_late_ambiguous_usage_cancels_eligibility(self):
        self.s.event(event('thread/tokenUsage/updated', turnId='t', tokenUsage={}), 1199)
        self.assertFalse(self.s.ready(1200, 1200))

    def test_failed_turn_and_compaction_do_not_arm(self):
        for status in ['failed', 'interrupted']:
            s = Session('a', self.s.owner)
            s.event(event('turn/started', turn={'id': 't'}), 0)
            s.stop_turn = 't'
            s.event(event('turn/completed', turn={'id': 't', 'status': status}), 0)
            self.assertEqual(s.stop_turn, '')
        self.s.event(event('item/completed', item={'type': 'contextCompaction'}), 100)
        self.s.stop_turn = 't'
        self.assertFalse(self.s.ready(1200, 1200))

    def test_cooldown_and_single_flight(self):
        for field, value in [('cooldown', 2400), ('consumed', True), ('locked', True)]:
            self.setUp()
            setattr(self.s, field, value)
            self.assertFalse(self.s.ready(1200, 1200))

    def test_multiple_sessions_are_independent(self):
        b = Session('b', self.s.owner)
        completed(b)
        self.s.cancel()
        self.assertFalse(self.s.ready(1200, 1200))
        self.assertTrue(b.ready(1200, 1200))

    def test_duplicate_completion_does_not_reset_timer(self):
        self.s.event(event('turn/completed', turn={'id': 't', 'status': 'completed'}), 1199)
        self.assertEqual(self.s.finished, 0)


class BoundaryTests(unittest.TestCase):
    def test_ambiguous_usage(self):
        for value in [None, {}, {'total': {'totalTokens': 900}}, usage(window=0),
                      usage(window=None), usage(window=True), usage(total=1001),
                      usage(total=400.0), usage(total=-1)]:
            self.assertIsNone(context_usage(value))
        bad = usage()
        bad['last']['totalTokens'] = 401
        self.assertIsNone(context_usage(bad))
        cached = usage()
        cached['last'].update(cachedInputTokens=200, reasoningOutputTokens=5)
        self.assertEqual(context_usage(cached), (400, 1000))

    def test_default_and_configurable_durations(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CODEX_HOME=directory):
            self.assertEqual(settings()[0], 1200)
            for minutes in [50, 60, 7.5]:
                Path(directory, 'codex-idle-compact.json').write_text(json.dumps({'idleMinutes': minutes}))
                self.assertEqual(settings()[0], minutes * 60)
            for config in [[], {'idleMinutes': 0}, {'idleMinutes': True}, {'idleMinutes': '20'},
                           {'idleMinutes': -1}, {'idleMinutes': float('nan')}, {'socket': 'relative'}]:
                Path(directory, 'codex-idle-compact.json').write_text(json.dumps(config))
                with self.assertRaises((ValueError, TypeError)):
                    settings()

    def test_exclusive_lock_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'watcher.lock'
            with lock(path):
                with self.assertRaises(BlockingIOError):
                    lock(path)
            with lock(path):
                pass


class FakeRPC:
    def __init__(self, s):
        self.s, self.calls, self.overrides, self.activity = s, [], {}, None
        self.fail = None
        self.complete = True

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == self.fail:
            raise RpcError('unavailable')
        if self.activity is not None:
            action, self.activity = self.activity, None
            action()
        if method in self.overrides:
            return self.overrides[method]
        if method == 'thread/compact/start':
            self.s.event(event('turn/started', turn={'id': 'compact'}), 1200)
            self.s.event(event('item/started', turnId='compact', item={'id': 'c', 'type': 'contextCompaction'}), 1200)
            if self.complete:
                self.s.event(event('item/completed', turnId='compact', item={'id': 'c', 'type': 'contextCompaction'}), 1200)
            self.s.event(event('turn/completed', turn={'id': 'compact', 'status': 'completed'}), 1200)
            return {}
        if method == 'thread/goal/get':
            return {'goal': None}
        if method == 'thread/read':
            return {'thread': {'id': self.s.thread, 'status': {'type': 'idle'}}}
        if method == 'thread/turns/list':
            return {'data': [{'id': self.s.turn, 'status': 'completed'}]}
        return {'data': [], 'nextCursor': None}


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, CODEX_HOME=self.directory.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.logs = []
        self.w = Watcher(Path(self.directory.name), lambda e, **p: self.logs.append(e), clock=lambda: 1200)
        self.s = Session('a', ('/owner.sock', 1))
        completed(self.s)
        self.w.sessions['a'] = self.s
        self.rpc = FakeRPC(self.s)
        self.w.connections[self.s.owner] = self.rpc

    async def test_actually_calls_native_compaction_and_observes_completion(self):
        await self.w.compact(self.s)
        self.assertEqual(self.rpc.calls[-1], ('thread/compact/start', {'threadId': 'a'}))
        descendants = next(p for m, p in self.rpc.calls if m == 'thread/list')
        self.assertIn('subAgent', descendants['sourceKinds'])
        self.assertEqual(descendants['modelProviders'], [])
        self.assertEqual(self.logs, ['compaction_started', 'compaction_completed'])
        self.assertTrue(self.s.consumed)
        self.assertFalse(self.s.ready(99999, 1200))

    async def test_empty_rpc_ack_is_not_completion(self):
        self.rpc.complete = False
        await self.w.compact(self.s)
        self.assertEqual(self.logs[-1], 'compaction_failed')

    async def test_every_busy_or_unknown_preflight_blocks(self):
        cases = [('thread/queue/list', {'data': [1]}),
                 ('thread/backgroundTerminals/list', {'data': [1]}),
                 ('thread/goal/get', {'goal': {'status': 'active'}}),
                 ('thread/list', {'data': [{'status': {'type': 'active'}}]}),
                 ('thread/list', {'data': [], 'nextCursor': 'more'}),
                 ('thread/turns/list', {'data': [{'id': 'other', 'status': 'completed'}]}),
                 ('thread/read', {'thread': {'id': 'a', 'status': {'type': 'active'}}})]
        for method, value in cases:
            with self.subTest(method=method):
                self.rpc.overrides = {method: value}
                self.rpc.calls.clear()
                await self.w.compact(self.s)
                self.assertFalse(any(m == 'thread/compact/start' for m, _ in self.rpc.calls))

    async def test_activity_during_preflight_cancels_send(self):
        self.rpc.activity = self.s.cancel
        await self.w.compact(self.s)
        self.assertNotIn('thread/compact/start', [m for m, _ in self.rpc.calls])

    async def test_usage_rechecked_after_timer(self):
        self.rpc.activity = lambda: self.s.event(event('thread/tokenUsage/updated', turnId='t', tokenUsage=usage(399)), 1200)
        await self.w.compact(self.s)
        self.assertNotIn('thread/compact/start', [m for m, _ in self.rpc.calls])

    async def test_unavailable_preflight_skips_without_retry(self):
        self.rpc.fail = 'thread/queue/list'
        await self.w.compact(self.s)
        self.assertEqual(self.logs, ['preflight_unavailable'])
        self.assertTrue(self.s.consumed)

    async def test_new_turn_during_preflight_can_rearm(self):
        self.rpc.activity = lambda: completed(self.s, 'new', now=1200)
        await self.w.compact(self.s)
        self.assertFalse(self.s.consumed)
        self.assertTrue(self.s.ready(2400, 1200))

    async def test_owner_mismatch_and_closed_events_are_isolated(self):
        self.w.event(('/wrong.sock', 2), event('thread/closed'))
        self.assertIn('a', self.w.sessions)
        self.w.event(self.s.owner, event('thread/closed'))
        self.assertNotIn('a', self.w.sessions)
