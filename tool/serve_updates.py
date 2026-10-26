"""Read-only release origin. Expose through an HTTPS tunnel, never the repository."""
import argparse
from io import BytesIO
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import urlsplit

from publish_update import DEFAULT_ROOT


class ReleaseHandler(SimpleHTTPRequestHandler):
    def send_response(self, code, message=None):
        self.response_status = code
        super().send_response(code, message)

    def send_head(self):
        path = urlsplit(self.path).path
        if path in {"/", "/healthz"}:
            body = b'Dumi Cut Room update server is running.\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return BytesIO(body)
        if path != "/latest.json" and not re.fullmatch(r"/releases/Dumi-Cut-Room-\d+\.\d+\.\d+\.exe", path):
            self.send_error(404)
            return None
        root = Path(self.directory).resolve()
        target = (root / path.lstrip("/")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            self.send_error(404)
            return None
        return super().send_head()

    def end_headers(self):
        cacheable = (getattr(self, "response_status", None) == 200
                     and urlsplit(self.path).path.startswith("/releases/"))
        self.send_header("Cache-Control", "public, max-age=86400, immutable" if cacheable else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(ReleaseHandler, directory=str(root)))
    print(f"Update server: http://127.0.0.1:{args.port}; files: {root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
