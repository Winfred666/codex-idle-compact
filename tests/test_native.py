"""Opt-in native integration test. Local mock responses; no API key or model bill."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_idle_compact import RPC
from idle_plugin import owner_socket


@unittest.skipUnless(os.environ.get('CODEX_IDLE_COMPACT_TEST_CODEX'),
                     'set CODEX_IDLE_COMPACT_TEST_CODEX to a native Codex binary')
class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_plugin_automatically_compacts_on_same_owner(self):
        await self.exercise()

    async def test_active_turn_cancels_idle_compaction_until_completed(self):
        await self.exercise(active_turn=True)

    async def exercise(self, active_turn=False):
        executable = os.environ['CODEX_IDLE_COMPACT_TEST_CODEX']
        repository = Path(__file__).resolve().parents[1]
        requests, notifications = [], []
        seed_release = asyncio.Event()
        active_request = asyncio.Event()
        release = asyncio.Event()
        normal_requests = 2 if active_turn else 1

        async def provider(reader, writer):
            try:
                headers = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                              if line.lower().startswith(b'content-length:'))
                requests.append(await reader.readexactly(length))
                n = len(requests)
                if n == 1:
                    await seed_release.wait()
                if active_turn and n == 2:
                    active_request.set()
                    await release.wait()
                tokens = 45000 if n <= normal_requests else 200
                events = [
                    {'type': 'response.output_item.done', 'item': {'type': 'message',
                     'role': 'assistant', 'id': 'm' + str(n),
                     'content': [{'type': 'output_text', 'text': 'Test reply' if n == 1 else 'Test summary'}]}},
                    {'type': 'response.completed', 'response': {'id': 'r' + str(n), 'usage': {
                     'input_tokens': tokens, 'input_tokens_details': None, 'output_tokens': 0,
                     'output_tokens_details': None, 'total_tokens': tokens}}},
                ]
                body = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        http = await asyncio.start_server(provider, '127.0.0.1', 0)
        port = http.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory(prefix='cic-', dir='/tmp') as directory:
            root = Path(directory)
            home = root / 'home'
            home.mkdir()
            def watcher_logs():
                return '\n'.join(p.read_text() for p in (home / 'plugins/data').rglob('watcher.log'))
            sock = root / 'owner.sock'
            env = dict(os.environ, CODEX_HOME=str(home))
            # The trust bypass is confined to this disposable test home and our own fixture plugin.
            for command in [['plugin', 'marketplace', 'add', str(repository)],
                            ['plugin', 'add', 'codex-idle-compact@codex-idle-compact', '--json']]:
                child = await asyncio.create_subprocess_exec(executable, *command, env=env, cwd=root,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await child.communicate()
                self.assertEqual(child.returncode, 0, (out + err).decode())
            config = (home / 'config.toml').read_text()
            self.assertIn('enabled = true', config)
            (home / 'config.toml').write_text('''model = "mock-model"
model_provider = "mock_provider"
model_context_window = 100000
model_auto_compact_token_limit = 1000000
model_post_turn_compact_threshold_percent = 0
approval_policy = "never"
sandbox_mode = "read-only"
''' + config + '''
[model_providers.mock_provider]
name = "Local test fixture"
base_url = "http://127.0.0.1:''' + str(port) + '''/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
''')
            idle_seconds = 1.2
            (home / 'codex-idle-compact.json').write_text(json.dumps({'idleMinutes': idle_seconds / 60, 'socket': str(sock)}))
            with (root / 'server.log').open('wb') as log:
                server = await asyncio.create_subprocess_exec(executable, 'app-server', '--listen', 'unix://' + str(sock), env=env, cwd=root,
                    stdout=asyncio.subprocess.DEVNULL, stderr=log)
                rpc = None
                try:
                    for _ in range(200):
                        if sock.exists() or server.returncode is not None:
                            break
                        await asyncio.sleep(0.05)
                    self.assertTrue(sock.exists(), (root / 'server.log').read_text())
                    probe, owner_pid = owner_socket(sock)
                    probe.close()
                    # npm launchers may parent the native process; use the actual Unix peer PID.
                    rpc = await RPC.open(sock, owner_pid, notifications.append)
                    result = await rpc.call('thread/start', {'model': 'mock-model', 'cwd': str(root),
                                                            'historyMode': 'paginated',
                                                            'config': {'bypass_hook_trust': True}})
                    thread = result['thread']['id']
                    await rpc.call('turn/start', {'threadId': thread, 'input': [
                        {'type': 'text', 'text': 'Fixture seed', 'text_elements': []}]})
                    # SessionStart runs on the first turn; hold its reply until usage can be observed.
                    for _ in range(200):
                        if 'watching' in watcher_logs():
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail('Watcher did not subscribe.\n' + (root / 'server.log').read_text()[-4000:])
                    seed_release.set()
                    if active_turn:
                        for _ in range(300):
                            if any(e.get('method') == 'turn/completed' for e in notifications):
                                break
                            await asyncio.sleep(0.01)
                        else:
                            self.fail('Seed turn did not complete')
                        await rpc.call('turn/start', {'threadId': thread, 'input': [
                            {'type': 'text', 'text': 'Hold this turn active', 'text_elements': []}]})
                        await asyncio.wait_for(active_request.wait(), 10)
                        for _ in range(3):
                            await asyncio.sleep(idle_seconds)
                            snapshot = (await rpc.call('thread/read', {'threadId': thread, 'includeTurns': False}))['thread']
                            self.assertEqual(snapshot['status']['type'], 'active')
                            logs = watcher_logs()
                            self.assertIn('watching', logs, 'the watcher must be running during active work')
                            self.assertNotIn('compaction_started', logs)
                            self.assertEqual(len(requests), 2, 'no compaction request during the active turn')
                            self.assertFalse(any(e.get('method') == 'item/started'
                                and e.get('params', {}).get('item', {}).get('type') == 'contextCompaction'
                                for e in notifications))
                        release.set()
                    for _ in range(300):
                        logs = watcher_logs()
                        if 'compaction_completed' in logs:
                            break
                        await asyncio.sleep(0.1)
                    else:
                        signals = '\n'.join(p.read_text() for p in (home / 'plugins/data').rglob('sessions/*.json'))
                        self.fail('No automatic compaction.\n' + logs + '\nSignals:\n' + signals
                                  + '\nNotifications:\n' + json.dumps(notifications)[-18000:]
                                  + '\nServer:\n' + (root / 'server.log').read_text()[-4000:])
                    self.assertEqual(len(requests), normal_requests + 1, 'normal replies and one native compaction only')
                    self.assertIn('compaction_started', logs)
                    items = [e for e in notifications if e.get('method') == 'item/completed'
                             and e.get('params', {}).get('item', {}).get('type') == 'contextCompaction']
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]['params']['threadId'], thread)
                    self.assertEqual((await rpc.call('thread/loaded/list', {}))['data'], [thread])
                    probe, still_owner_pid = owner_socket(sock)
                    probe.close()
                    self.assertEqual(still_owner_pid, owner_pid)
                    await asyncio.sleep(1.2)
                    self.assertEqual(len(requests), normal_requests + 1, 'native compaction must not rearm itself')
                    hooks = [e['params']['run'] for e in notifications if e.get('method') == 'hook/completed'
                             and e.get('params', {}).get('run', {}).get('source') == 'plugin']
                    self.assertTrue(hooks, 'native plugin hooks must actually run')
                    for run in hooks:
                        self.assertEqual(run['status'], 'completed', run)
                        self.assertLess(run['durationMs'], 3000, run)
                    if os.environ.get('CODEX_IDLE_COMPACT_TEST_TIMINGS'):
                        print('Native hook milliseconds: ' + json.dumps([
                            (run['eventName'], run['durationMs']) for run in hooks]))
                finally:
                    seed_release.set()
                    release.set()
                    if rpc is not None:
                        await rpc.close()
                    if server.returncode is None:
                        server.terminate()
                        try:
                            await asyncio.wait_for(server.wait(), 5)
                        except asyncio.TimeoutError:
                            server.kill()
                            await server.wait()
                    # Detached watcher exits after its owner disconnects; do not leave test processes.
                    await asyncio.sleep(6)
        http.close()
        await http.wait_closed()
