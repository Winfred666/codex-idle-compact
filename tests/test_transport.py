import asyncio
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest

from codex_idle_compact import RPC, RpcError
from idle_plugin import owner_socket
from websockets.asyncio.server import unix_serve


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_websocket_protocol_and_peer_identity(self):
        requests, events = [], []

        async def server(connection):
            async for payload in connection:
                message = json.loads(payload)
                requests.append(message)
                if 'id' in message:
                    await connection.send(json.dumps({'id': message['id'], 'result': {}}))
                    if message['method'] == 'thread/compact/start':
                        await connection.send(json.dumps({'method': 'item/completed', 'params': {
                            'threadId': 'a', 'item': {'type': 'contextCompaction'}}}))

        with tempfile.TemporaryDirectory(prefix='cic-', dir='/tmp') as directory:
            sock = Path(directory) / 'owner.sock'
            logger = logging.getLogger('test.expected-peer-probes')
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
            async with unix_serve(server, str(sock), logger=logger):
                connection, pid = owner_socket(sock)
                connection.close()
                self.assertEqual(pid, os.getpid())
                with self.assertRaises(RpcError):
                    await RPC.open(sock, pid + 1, events.append)
                rpc = await RPC.open(sock, pid, events.append)
                try:
                    await rpc.call('thread/compact/start', {'threadId': 'a'})
                    for _ in range(20):
                        if events:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(events[0]['method'], 'item/completed')
                    self.assertEqual([r['method'] for r in requests], ['initialize', 'initialized', 'thread/compact/start'])
                finally:
                    await rpc.close()
