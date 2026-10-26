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
        executable = os.environ['CODEX_IDLE_COMPACT_TEST_CODEX']
        repository = Path(__file__).resolve().parents[1]
        requests, notifications = [], []

        async def provider(reader, writer):
            try:
                headers = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                              if line.lower().startswith(b'content-length:'))
                requests.append(await reader.readexactly(length))
                n = len(requests)
                tokens = 45000 if n == 1 else 200
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
            (home / 'codex-idle-compact.json').write_text(json.dumps({'idleMinutes': 0.01, 'socket': str(sock)}))
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
                    for _ in range(300):
                        logs = '\n'.join(p.read_text() for p in home.rglob('watcher.log'))
                        if 'compaction_completed' in logs:
                            break
                        await asyncio.sleep(0.1)
                    else:
                        signals = '\n'.join(p.read_text() for p in home.rglob('sessions/*.json'))
                        self.fail('No automatic compaction.\n' + logs + '\nSignals:\n' + signals
                                  + '\nNotifications:\n' + json.dumps(notifications)[-18000:]
                                  + '\nServer:\n' + (root / 'server.log').read_text()[-4000:])
                    self.assertEqual(len(requests), 2, 'one fixture reply and one native compaction only')
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
                    self.assertEqual(len(requests), 2, 'native compaction must not rearm itself')
                finally:
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
