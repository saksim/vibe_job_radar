import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from vibe_job_radar.guided.cdp_pipe import PipeTransport
from vibe_job_radar.guided.contracts import CrawlError


class Chunks:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read(self, _size):
        return next(self.chunks, b'')


class PipeTransportTests(unittest.TestCase):
    def transport(self, chunks):
        process = SimpleNamespace(stdin=io.BytesIO(), stdout=Chunks(chunks))
        transport = PipeTransport(process)
        self.addCleanup(transport.close)
        return transport

    def test_split_utf8_and_escaped_newline_or_null_stay_inside_one_message(self):
        source = json.dumps({'text': '架构\n\0师'}, ensure_ascii=False)
        raw = (source + '\n').encode('utf-8')
        transport = self.transport([raw[:11], raw[11:12], raw[12:15], raw[15:]])
        self.assertEqual(transport.recv(timeout=1), source)
        transport.reader.join(timeout=1)
        with self.assertRaises(CrawlError):
            transport.recv(timeout=.01)

    def test_several_responses_are_separate_and_remain_available_before_eof(self):
        transport = self.transport([b'{"id":1}\n{"id":2}\n'])
        self.assertEqual(json.loads(transport.recv(timeout=1)), {'id': 1})
        self.assertEqual(json.loads(transport.recv(timeout=1)), {'id': 2})
        self.assertEqual(transport.messages.maxsize, 16)
        self.assertEqual(transport.outgoing.maxsize, 32)

    def test_invalid_utf8_is_not_replaced_or_logged(self):
        transport = self.transport([b'\xffsecret-password\n'])
        transport.reader.join(timeout=1)
        with self.assertRaises(CrawlError) as caught:
            transport.recv(timeout=.01)
        self.assertEqual(str(caught.exception), 'browser_closed')

    def test_bridge_asset_is_packaged_and_uses_only_inherited_debugging_pipes(self):
        root = Path(__file__).resolve().parents[1]
        metadata = (root/'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('"guided/cdp_bridge.js"', metadata)
        source = (root/'src/vibe_job_radar/guided/cdp_bridge.js').read_text(encoding='utf-8')
        self.assertIn("stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe']", source)
        self.assertIn("args.some(x => x.startsWith('--remote-debugging-port'))", source)
        self.assertNotIn('console.log', source)
        self.assertNotIn('createServer', source)


if __name__ == '__main__':
    unittest.main()
