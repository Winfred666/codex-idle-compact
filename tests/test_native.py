"""Opt-in transport smoke test; creates an empty server, never a model turn."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("CODEX_IDLE_COMPACT_TEST_CODEX"),
                     "set CODEX_IDLE_COMPACT_TEST_CODEX to test an installed native binary")
class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_connects_to_existing_owner_without_loading_sessions(self):
        executable = os.environ["CODEX_IDLE_COMPACT_TEST_CODEX"]
        with tempfile.TemporaryDirectory(prefix="cic-", dir="/tmp") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            socket = root / "owner.sock"
            env = dict(os.environ, CODEX_HOME=str(home))
            server = await asyncio.create_subprocess_exec(
                executable, "app-server", "--listen", "unix://" + str(socket),
                env=env, cwd=root, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            observer = None
            try:
                for _ in range(200):
                    if socket.exists() or server.returncode is not None:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(socket.exists(), "test App Server did not create its socket")
                script = Path(__file__).resolve().parents[1] / "codex_idle_compact.py"
                observer = await asyncio.create_subprocess_exec(
                    sys.executable, str(script), "proxy",
                    "--socket", str(socket), env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)

                async def send(message):
                    observer.stdin.write(json.dumps(message).encode() + b"\n")
                    await observer.stdin.drain()

                async def response(request_id):
                    while True:
                        line = await asyncio.wait_for(observer.stdout.readline(), 15)
                        self.assertTrue(line, "proxy closed before the response")
                        message = json.loads(line)
                        if message.get("id") == request_id:
                            self.assertNotIn("error", message)
                            return message["result"]

                await send({"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "codex_idle_compact_test", "version": "0.1.0"}}})
                self.assertIn("userAgent", await response(1))
                await send({"method": "initialized", "params": {}})
                await send({"id": 2, "method": "thread/loaded/list", "params": {}})
                self.assertEqual((await response(2))["data"], [])
                startup = json.loads(await asyncio.wait_for(observer.stderr.readline(), 5))
                self.assertFalse(startup["automaticCompaction"])
            finally:
                for process in (observer, server):
                    if process is not None and process.returncode is None:
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 5)
                        except asyncio.TimeoutError:
                            process.kill()
                            await process.wait()
