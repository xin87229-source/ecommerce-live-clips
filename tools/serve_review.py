"""Serve one local review folder read-only on loopback, with single byte ranges."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit


class ReviewHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.serve_file(True)

    def do_HEAD(self):
        self.serve_file(False)

    def reject_method(self):
        self.send_response(405)
        self.send_header('Allow', 'GET, HEAD')
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = reject_method

    def reject_range(self, size):
        self.send_response(416)
        self.send_header('Content-Range', f'bytes */{size}')
        self.send_header('Content-Length', '0')
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()

    def serve_file(self, send_body):
        try:
            url = urlsplit(self.path)
            path = unquote(url.path).replace('\\', '/')
            parts = PurePosixPath(path).parts
            if (url.scheme or url.netloc or not path.startswith('/') or '\x00' in path
                    or ':' in path or '..' in parts
                    or (len(parts) > 2 and not self.server.allow_nested)
                    or (path != '/' and path.endswith('/'))):
                self.send_error(403, 'Only files beside the review card are available')
                return
            target = (self.server.review_root / (path.lstrip('/') or 'index.html')).resolve()
            if (not target.is_relative_to(self.server.review_root) or target.is_dir()
                    or (target.parent != self.server.review_root and not self.server.allow_nested)):
                self.send_error(403, 'Directory listing and outside files are disabled')
                return
            if not target.is_file():
                self.send_error(404, 'File not found')
                return
            with target.open('rb') as source:
                size = os.fstat(source.fileno()).st_size
                start, end, status = 0, size - 1, 200
                ranges = self.headers.get_all('Range', [])
                if ranges:
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', ranges[0].strip())
                    if len(ranges) != 1 or not match or not any(match.groups()) or size == 0:
                        self.reject_range(size)
                        return
                    left, right = match.groups()
                    try:
                        if left:
                            start = int(left)
                            end = min(int(right), size - 1) if right else size - 1
                        else:
                            suffix = int(right)
                            if suffix <= 0:
                                raise ValueError('Empty suffix')
                            start, end = max(0, size - suffix), size - 1
                    except ValueError:
                        self.reject_range(size)
                        return
                    if start >= size or end < start:
                        self.reject_range(size)
                        return
                    status = 206
                length = max(0, end - start + 1)
                self.send_response(status)
                mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
                self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') else ''))
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'no-store')
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                if send_body:
                    source.seek(start)
                    remaining = length
                    while remaining:
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            self.close_connection = True
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError):
            self.send_error(404, 'File not available')


def create_server(card, port=8765, allow_nested=False):
    card = Path(card).expanduser()
    if not card.is_absolute() or card.name != 'index.html':
        raise ValueError('card须为现有index.html的绝对路径')
    card = card.resolve(strict=True)
    if not card.is_file() or card.name != 'index.html':
        raise ValueError('card须为现有index.html文件')
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('port无效')
    server = ThreadingHTTPServer(('127.0.0.1', port), ReviewHandler)
    server.review_root = card.parent
    server.allow_nested = bool(allow_nested)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--card', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--nested', action='store_true', help='允许本交付目录内的子审片卡文件，不开放目录列表或外部路径')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port须为1至65535')
    try:
        with create_server(args.card, args.port, allow_nested=args.nested) as server:
            print(f'本机只读审片：http://127.0.0.1:{server.server_port}/index.html', flush=True)
            scope = '本交付目录及子审片卡文件' if args.nested else '本卡同目录文件'
            print(f'仅提供{scope}；按Ctrl+C停止，不会上传或修改审核状态。', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except (OSError, ValueError) as error:
        parser.exit(1, f'无法启动审片服务：{error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
