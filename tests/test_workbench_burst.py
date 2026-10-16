"""Cold page resources must survive a bounded burst while accept is busy."""
import http.client
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import Workspace


class WorkbenchBurstTests(unittest.TestCase):
    def test_queued_cold_page_connections_deliver_all_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Fail deterministically if startup or resource serving asks the
            # system resolver for a reverse name; keep the real TCP requests.
            with patch('socket.getfqdn', side_effect=AssertionError('Unexpected reverse DNS')):
                server=LocalServer(Workspace(temporary),public_client=None)
            self.assertEqual(server.server_name, '127.0.0.1')
            self.assertEqual(server.server_port, server.server_address[1])
            connections=[];worker=None
            try:
                # Model a busy accept loop without sleeping or changing the
                # product's timeout. Each page can open several sockets at once.
                for _ in range(12):
                    connection=http.client.HTTPConnection(server.authority,timeout=2)
                    connections.append(connection);connection.connect()
                worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
                worker.start()
                resources=('/report-sources.js','/app.js','/network-settings.js','/release-updates.js')*3
                for connection,path in zip(connections,resources):
                    connection.request('GET',path)
                for connection in connections:
                    response=connection.getresponse()
                    self.assertEqual(response.status,200)
                    self.assertGreater(len(response.read()),0)
            finally:
                for connection in connections:connection.close()
                if worker is not None:server.shutdown();worker.join(5)
                server.server_close()
