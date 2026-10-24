import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from websockets.asyncio.server import unix_serve

from test_observer import event, usage


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_socket_timer_never_sends_compaction_and_disconnect_exits(self):
        received = []
        connected = asyncio.get_running_loop().create_future()

        async def server(connection):
            connected.set_result(connection)
            async for payload in connection:
                received.append(payload)
                await connection.send('{"id":1,"result":{}}')
                for message in [
                    event("turn/started", turn={"id": "t"}),
                    event("thread/tokenUsage/updated", turnId="t", tokenUsage=usage()),
                    event("turn/completed", turn={"id": "t", "status": "completed"}),
                    event("thread/status/changed", status={"type": "idle"}),
                ]:
                    await connection.send(json.dumps(message))

        with tempfile.TemporaryDirectory(prefix="cic-", dir="/tmp") as directory:
            socket = Path(directory) / "owner.sock"
            async with unix_serve(server, str(socket)):
                script = Path(__file__).resolve().parents[1] / "codex_idle_compact.py"
                process = await asyncio.create_subprocess_exec(
                    sys.executable, str(script), "proxy", "--socket", str(socket),
                    "--idle-minutes", "0.001", stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    request = '{ "id":1, "method":"initialize", "params":{"private":"DO_NOT_LOG"} }'
                    process.stdin.write(request.encode() + b"\n")
                    await process.stdin.drain()
                    stdout = [await asyncio.wait_for(process.stdout.readline(), 5) for _ in range(5)]
                    self.assertEqual(json.loads(stdout[0])["result"], {})
                    startup = await asyncio.wait_for(process.stderr.readline(), 5)
                    report = await asyncio.wait_for(process.stderr.readline(), 5)
                    self.assertEqual(json.loads(startup)["event"], "observer_started")
                    self.assertEqual(json.loads(report)["event"], "blocked")
                    self.assertFalse(json.loads(report)["compactionStarted"])
                    await asyncio.sleep(0.15)
                    self.assertEqual(received, [request])
                    self.assertNotIn(b"DO_NOT_LOG", startup + report)
                    await (await connected).close()
                    self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
                    self.assertEqual(await process.stderr.read(), b"")
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
