import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from idle_plugin import hook, lock, read_signal
from codex_idle_compact import Session, Watcher


class HookTests(unittest.TestCase):
    def test_supported_hook_starts_watcher_and_lock_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CODEX_HOME=directory, PLUGIN_DATA=directory):
            thread, turn = str(uuid.uuid4()), str(uuid.uuid4())
            payload = {'hook_event_name': 'Stop', 'session_id': thread, 'turn_id': turn, 'prompt': 'PRIVATE'}
            def invoke():
                with patch('sys.stdin', Mock(buffer=io.BytesIO(json.dumps(payload).encode()))):
                    return hook()
            with patch('idle_plugin.owner_socket', return_value=(Mock(), 12)), \
                 patch('idle_plugin.ancestors', return_value={12}), \
                 patch('idle_plugin.subprocess.Popen') as start:
                self.assertEqual(invoke(), {})
                start.assert_called_once()
                signal = read_signal(Path(directory, 'sessions', thread + '.json'))
                self.assertEqual(signal['ownerPid'], 12)
                self.assertEqual(signal['turnId'], turn)
                self.assertNotIn('PRIVATE', json.dumps(signal))
                with lock(Path(directory, 'watcher.lock')):
                    invoke()
                start.assert_called_once()

    def test_wrong_owner_gives_session_hint_without_starting_anything(self):
        payload = {'hook_event_name': 'SessionStart', 'session_id': str(uuid.uuid4())}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CODEX_HOME=directory), \
             patch('idle_plugin.owner_socket', return_value=(Mock(), 12)), \
             patch('idle_plugin.ancestors', return_value={99}), \
             patch('idle_plugin.subprocess.Popen') as start, \
             patch('sys.stdin', Mock(buffer=io.BytesIO(json.dumps(payload).encode()))):
            self.assertIn('No session was restarted', hook()['systemMessage'])
            start.assert_not_called()


class SignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_arms_activity_cancels_and_session_end_forgets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CODEX_HOME=directory):
            root = Path(directory)
            (root / 'sessions').mkdir()
            thread, turn = str(uuid.uuid4()), str(uuid.uuid4())
            s = Session(thread, ('/owner.sock', 12))
            w = Watcher(root, lambda *a, **k: None)
            w.sessions[thread] = s
            w.connections[s.owner] = Mock(call=AsyncMock(return_value={}))
            for event in ['Stop', 'UserPromptSubmit', 'Stop', 'SessionEnd']:
                (root / 'sessions' / (thread + '.json')).write_text(json.dumps({
                    'threadId': thread, 'turnId': turn, 'event': event, 'ownerPid': 12,
                    'socket': '/owner.sock', 'stamp': time.time_ns(), 'at': time.monotonic()}))
                await w.refresh_hooks()
                if event == 'Stop':
                    self.assertEqual(s.stop_turn, turn)
                else:
                    self.assertEqual(s.stop_turn, '')
            self.assertNotIn(thread, w.sessions)

    async def test_restart_does_not_replay_old_signal_or_cold_resume(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, CODEX_HOME=directory):
            root = Path(directory)
            (root / 'sessions').mkdir()
            thread = str(uuid.uuid4())
            (root / 'sessions' / (thread + '.json')).write_text(json.dumps({
                'threadId': thread, 'turnId': str(uuid.uuid4()), 'event': 'Stop', 'ownerPid': 12,
                'socket': '/owner.sock', 'stamp': 1, 'at': 0}))
            w = Watcher(root, lambda *a, **k: None)
            with patch.object(w, 'attach', new_callable=AsyncMock) as attach:
                await w.refresh_hooks()
                attach.assert_not_called()
            rpc = Mock(call=AsyncMock(return_value={'thread': {
                'id': thread, 'status': {'type': 'notLoaded'}}}))
            w.connections[('/owner.sock', 12)] = rpc
            from codex_idle_compact import RpcError
            with self.assertRaises(RpcError):
                await w.attach({'threadId': thread, 'socket': '/owner.sock', 'ownerPid': 12})
            self.assertEqual(rpc.call.call_count, 1)
            self.assertEqual(rpc.call.call_args.args[0], 'thread/read')
