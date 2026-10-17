"""Real loopback HTTP requests against synthetic files; no browser or upload."""
import http.client
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote


MODULE = Path(__file__).resolve().parents[1] / 'tools/serve_review.py'


class ServeReviewTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.is_file(), 'The standalone review HTTP server is not implemented')
        spec = importlib.util.spec_from_file_location('serve_review_test', MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / 'card'
        self.folder.mkdir()
        self.card = self.folder / 'index.html'
        self.card.write_bytes(b'<video src="review.mp4" controls></video>')
        self.media = bytes(range(100))
        (self.folder / 'review.mp4').write_bytes(self.media)
        (self.folder / 'empty.mp4').write_bytes(b'')
        (self.folder / 'sample name.txt').write_bytes(b'spaced name')
        (self.folder / 'subdirectory').mkdir()
        (self.folder / 'subdirectory/hidden.txt').write_bytes(b'private nested note')
        (self.root / 'outside.txt').write_bytes(b'private outside note')
        self.server = self.module.create_server(self.card, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if hasattr(self, 'server'):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        if hasattr(self, 'temp'):
            assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
            self.temp.cleanup()

    def request(self, method='GET', path='/review.mp4', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_loopback_get_head_and_root_page(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        for path in ('/', '/index.html?reload=1'):
            status, headers, body = self.request(path=path)
            self.assertEqual(status, 200)
            self.assertEqual(body, self.card.read_bytes())
            self.assertIn('text/html', headers['Content-Type'])
        status, headers, body = self.request('HEAD')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Length'], '100')
        self.assertEqual(headers['Accept-Ranges'], 'bytes')
        self.assertEqual(body, b'')
        self.assertEqual(self.request(path='/' + quote('sample name.txt'))[2], b'spaced name')

    def test_single_ranges_include_open_end_suffix_and_clamped_end(self):
        for value, expected, content_range in (
                ('bytes=0-0', self.media[:1], 'bytes 0-0/100'),
                ('bytes=99-99', self.media[-1:], 'bytes 99-99/100'),
                ('bytes=10-19', self.media[10:20], 'bytes 10-19/100'),
                ('bytes=90-', self.media[90:], 'bytes 90-99/100'),
                ('bytes=-8', self.media[-8:], 'bytes 92-99/100'),
                ('bytes=-200', self.media, 'bytes 0-99/100'),
                ('bytes=95-200', self.media[95:], 'bytes 95-99/100')):
            with self.subTest(value=value):
                status, headers, body = self.request(headers={'Range': value})
                self.assertEqual(status, 206)
                self.assertEqual(headers['Content-Range'], content_range)
                self.assertEqual(int(headers['Content-Length']), len(expected))
                self.assertEqual(body, expected)
        status, headers, body = self.request('HEAD', headers={'Range': 'bytes=10-19'})
        self.assertEqual(status, 206)
        self.assertEqual(headers['Content-Length'], '10')
        self.assertEqual(body, b'')

    def test_invalid_multiple_and_unsatisfiable_ranges_return_416(self):
        for value in ('bytes=100-', 'bytes=8-7', 'bytes=-0', 'bytes=-',
                      'items=0-1', 'bytes=0-1,4-5', 'bytes=NaN-5'):
            with self.subTest(value=value):
                status, headers, body = self.request(headers={'Range': value})
                self.assertEqual(status, 416)
                self.assertEqual(headers['Content-Range'], 'bytes */100')
                self.assertEqual(body, b'')
        status, headers, body = self.request('HEAD', headers={'Range': 'bytes=100-'})
        self.assertEqual((status, headers['Content-Range'], body), (416, 'bytes */100', b''))
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.putrequest('GET', '/review.mp4')
            connection.putheader('Range', 'bytes=0-1')
            connection.putheader('Range', 'bytes=4-5')
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 416)
            response.read()
        finally:
            connection.close()

    def test_empty_file_and_empty_range(self):
        status, headers, body = self.request(path='/empty.mp4')
        self.assertEqual((status, headers['Content-Length'], body), (200, '0', b''))
        status, headers, body = self.request(path='/empty.mp4', headers={'Range': 'bytes=0-'})
        self.assertEqual((status, headers['Content-Range'], body), (416, 'bytes */0', b''))

    def test_traversal_subdirectories_and_absolute_urls_are_rejected(self):
        for path in ('/../outside.txt', '/%2e%2e/outside.txt', '/..%5coutside.txt',
                     '/%00', '/C:/outside.txt', '/subdirectory/', '/subdirectory/hidden.txt',
                     'http://example.invalid/index.html'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 403)
        self.assertEqual(self.request(path='/missing.mp4')[0], 404)

    def test_write_methods_cannot_create_or_change_files(self):
        before = {p: p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT'):
            with self.subTest(method=method):
                status, headers, body = self.request(method, '/new.txt', body=b'change')
                self.assertEqual(status, 405)
                self.assertEqual(headers['Allow'], 'GET, HEAD')
        self.assertEqual({p: p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}, before)

    def test_opt_in_nested_cards_keep_ranges_and_root_boundary(self):
        self.server.allow_nested = True
        nested = self.folder / 'subdirectory'
        (nested / 'index.html').write_bytes(b'question card')
        (nested / 'original.mp4').write_bytes(self.media)
        self.assertEqual(self.request(path='/subdirectory/index.html')[2], b'question card')
        status, headers, body = self.request(path='/subdirectory/original.mp4', headers={'Range': 'bytes=10-19'})
        self.assertEqual((status, body), (206, self.media[10:20]))
        for path in ('/subdirectory/', '/subdirectory/../../outside.txt',
                     '/subdirectory/%2e%2e/%2e%2e/outside.txt', '/C:/outside.txt'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 403)
        self.assertEqual(self.request('POST', '/subdirectory/new.txt', body=b'change')[0], 405)

    def test_card_requires_an_absolute_existing_index_file(self):
        for card in (Path('index.html'), self.folder, self.folder / 'missing/index.html',
                     self.folder / 'sample name.txt'):
            with self.subTest(card=card), self.assertRaises((ValueError, OSError)):
                self.module.create_server(card, 0)

    def test_symlink_cannot_escape_card_directory(self):
        link = self.folder / 'linked.txt'
        try:
            link.symlink_to(self.root / 'outside.txt')
        except OSError:
            self.skipTest('Symlink creation unavailable; traversal tests still run')
        self.assertEqual(self.request(path='/linked.txt')[0], 403)

    def test_standalone_cli_starts_and_rejects_invalid_arguments(self):
        for arguments in (['--card', 'index.html'], ['--card', str(self.card), '--port', '0'],
                          ['--card', str(self.card), '--port', '65536']):
            result = subprocess.run([sys.executable, '-B', str(MODULE), *arguments],
                                    capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-B', str(MODULE), '--card', str(self.card),
                                    '--port', str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                try:
                    connection.request('GET', '/review.mp4', headers={'Range': 'bytes=30-39'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), self.media[30:40])
                    break
                except ConnectionRefusedError:
                    if process.poll() is not None:
                        self.fail('Standalone server exited before accepting requests')
                    time.sleep(.02)
                finally:
                    connection.close()
            else:
                self.fail('Standalone server did not start within five seconds')
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
