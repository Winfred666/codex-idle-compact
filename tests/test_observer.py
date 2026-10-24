import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from codex_idle_compact import Observer, context_usage, endpoint_lock, minutes, relay


def event(method, thread="a", **params):
    return {"method": method, "params": {"threadId": thread, **params}}


def usage(total=400, window=1000):
    return {"last": {"inputTokens": total - 10, "outputTokens": 10, "totalTokens": total},
            "modelContextWindow": window, "total": {"totalTokens": 9000000}}


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.observer = Observer(1200, lambda: self.now)

    def send(self, method, thread="a", **params):
        self.observer.message(event(method, thread, **params))

    def start(self, thread="a", turn="t"):
        self.send("turn/started", thread, turn={"id": turn})

    def finish(self, thread="a", turn="t", total=400, status="completed"):
        self.send("thread/tokenUsage/updated", thread, turnId=turn, tokenUsage=usage(total))
        self.send("turn/completed", thread, turn={"id": turn, "status": status})
        self.send("thread/status/changed", thread, status={"type": "idle"})

    def ready(self, thread="a", turn="t", total=400):
        self.start(thread, turn)
        self.finish(thread, turn, total)

    def test_exact_threshold_waits_and_reports_once(self):
        self.ready()
        self.now = 1199
        self.assertEqual(self.observer.tick(), [])
        self.now = 1200
        report, = self.observer.tick()
        self.assertEqual(report["usedTokens"], 400)
        self.assertFalse(report["compactionStarted"])
        self.assertEqual(report["event"], "blocked")
        self.now = 10000
        self.assertEqual(self.observer.tick(), [])

    def test_lifetime_usage_does_not_arm(self):
        self.ready(total=399)
        self.now = 1200
        self.assertEqual(self.observer.tick(), [])

    def test_status_before_completion(self):
        self.start()
        self.send("thread/status/changed", status={"type": "idle"})
        self.finish()
        self.now = 1200
        self.assertEqual(len(self.observer.tick()), 1)

    def test_active_turn_and_tool_cannot_report(self):
        self.start()
        self.send("thread/tokenUsage/updated", turnId="t", tokenUsage=usage())
        self.send("item/started", turnId="t", item={"type": "commandExecution"})
        self.now = 1200
        self.assertEqual(self.observer.tick(), [])

    def test_activity_queue_or_nonidle_cancel_and_duplicate_end_cannot_rearm(self):
        for method, params, client in [
            ("turn/start", {}, True), ("turn/steer", {}, True),
            ("thread/queue/add", {}, True), ("thread/queue/changed", {}, False),
            ("thread/status/changed", {"status": {"type": "active"}}, False),
            ("thread/status/changed", {"status": {"type": "systemError"}}, False),
            ("item/started", {"turnId": "t", "item": {"type": "mcpToolCall"}}, False),
        ]:
            with self.subTest(method=method, params=params):
                self.setUp()
                self.ready()
                self.now = 1100
                self.observer.message(event(method, **params), client)
                self.send("turn/completed", turn={"id": "t", "status": "completed"})
                self.send("thread/status/changed", status={"type": "idle"})
                self.now = 2400
                self.assertEqual(self.observer.tick(), [])

    def test_only_new_completed_turn_rearms(self):
        self.ready()
        self.now = 1200
        self.observer.tick()
        self.now = 1300
        self.ready(turn="next")
        self.now = 2400
        self.assertEqual(self.observer.tick(), [])
        self.now = 2500
        self.assertEqual(len(self.observer.tick()), 1)

    def test_missing_usage_never_uses_previous_turn(self):
        self.ready()
        self.start(turn="next")
        self.send("turn/completed", turn={"id": "next", "status": "completed"})
        self.send("thread/status/changed", status={"type": "idle"})
        self.now = 1200
        self.assertEqual(self.observer.tick(), [])

    def test_usage_recheck_and_wrong_turn(self):
        self.ready()
        self.send("thread/tokenUsage/updated", turnId="old", tokenUsage=usage(399))
        self.assertEqual(self.observer.sessions["a"].usage, (400, 1000))
        self.send("thread/tokenUsage/updated", turnId="t", tokenUsage={})
        self.now = 1200
        self.assertEqual(self.observer.tick(), [])

    def test_failed_interrupted_and_compaction_turns_do_not_arm(self):
        for status in ["failed", "interrupted"]:
            self.setUp()
            self.start()
            self.finish(status=status)
            self.now = 1200
            self.assertEqual(self.observer.tick(), [])
        self.setUp()
        self.start()
        self.send("item/started", turnId="t", item={"type": "contextCompaction"})
        self.finish()
        self.now = 1200
        self.assertEqual(self.observer.tick(), [])

    def test_compaction_in_completed_items_also_disarms(self):
        self.start()
        self.send("thread/tokenUsage/updated", turnId="t", tokenUsage=usage())
        self.send("turn/completed", turn={"id": "t", "status": "completed",
                                         "items": [{"type": "contextCompaction"}]})
        self.send("thread/status/changed", status={"type": "idle"})
        self.now = 1200
        self.assertEqual(self.observer.tick(), [])

    def test_multiple_threads_are_independent(self):
        self.ready("a")
        self.ready("b")
        self.observer.message(event("turn/start", "a"), client=True)
        self.now = 1200
        self.assertEqual([r["threadId"] for r in self.observer.tick()], ["b"])

    def test_closed_thread_is_forgotten(self):
        self.ready()
        self.send("thread/closed")
        self.assertEqual(self.observer.sessions, {})

    def test_history_and_duplicate_notifications(self):
        self.finish()
        self.assertEqual(self.observer.sessions, {})
        self.ready()
        self.now = 500
        self.start()
        self.finish()
        self.now = 1200
        self.assertEqual(len(self.observer.tick()), 1)

    def test_late_wakeup_is_explicitly_skipped(self):
        self.ready()
        self.now = 1400
        self.assertEqual(self.observer.tick()[0]["event"], "skipped_late")

    def test_50_and_60_minute_configuration(self):
        for duration in [50, 60, 7.5]:
            self.observer = Observer(duration * 60, lambda: self.now)
            self.now = 0
            self.ready()
            self.now = duration * 60 - 1
            self.assertEqual(self.observer.tick(), [])
            self.now += 1
            self.assertEqual(len(self.observer.tick()), 1)


class BoundaryTests(unittest.TestCase):
    def test_ambiguous_usage(self):
        for value in [None, {}, {"total": {"totalTokens": 900}}, usage(window=0),
                      usage(window=None), usage(window=True), usage(total=1001),
                      usage(total=400.0), usage(total=-1)]:
            self.assertIsNone(context_usage(value))
        bad = usage()
        bad["last"]["totalTokens"] = 401
        self.assertIsNone(context_usage(bad))
        cached = usage()
        cached["last"].update(cachedInputTokens=200, reasoningOutputTokens=5)
        self.assertEqual(context_usage(cached), (400, 1000))

    def test_invalid_durations(self):
        for value in ["nan", "inf", "0", "-1", "word"]:
            with self.assertRaises(ValueError):
                Observer(float(value))
        for value in ["nan", "inf", "0", "-1", "word"]:
            import argparse
            with self.assertRaises(argparse.ArgumentTypeError):
                minutes(value)

    def test_exclusive_lock_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = Path(directory) / "owner.sock"
            with endpoint_lock(endpoint):
                with self.assertRaises(BlockingIOError):
                    endpoint_lock(endpoint)
            with endpoint_lock(endpoint):
                pass


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_byte_exact_passthrough_no_injected_compact(self):
        reader = asyncio.StreamReader()
        lines = [b'{ "id":1, "method":"initialize", "params":{} }\n',
                 b'{"id":1,"result":{"userAgent":"test"}}\n',
                 b'not-json\n', b'{"method":"future/event","params":{}}\n']
        reader.feed_data(b"".join(lines))
        reader.feed_eof()
        sent = []

        async def write(line):
            sent.append(line)

        await relay(reader, write, Observer(), client=True)
        self.assertEqual(sent, lines)
        self.assertNotIn(b"thread/compact/start", b"".join(sent))

    async def test_malformed_input_clears_candidates(self):
        observer = Observer()
        observer.message(event("turn/started", turn={"id": "t"}))
        reader = asyncio.StreamReader()
        reader.feed_data(b"broken\n")
        reader.feed_eof()

        async def write(line):
            pass

        await relay(reader, write, observer, client=False)
        self.assertEqual(observer.sessions, {})


if __name__ == "__main__":
    unittest.main()
