import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from raincheck_pipeline import http


def serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/x"


class Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_an_answer_cut_short_is_tried_again(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    calls = []

    class CutOnce(Quiet):
        def do_GET(self):
            calls.append(1)
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(b"0123456789" if len(calls) > 1 else b"012")  # the first answer stops after 3 bytes

    server, url = serve(CutOnce)
    try:
        assert http.request(url) == (200, b"0123456789")
        assert len(calls) == 2
    finally:
        server.shutdown()


def test_an_answer_always_cut_short_ends_as_a_network_error(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)

    class Cut(Quiet):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(b"012")

    server, url = serve(Cut)
    try:
        with pytest.raises(OSError):  # what the callers catch to move on to the next source
            http.request(url, tries=2)
    finally:
        server.shutdown()


def test_not_found_is_not_retried(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    calls = []

    class Missing(Quiet):
        def do_GET(self):
            calls.append(1)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server, url = serve(Missing)
    try:
        with pytest.raises(http.HttpError) as err:
            http.request(url)
        assert err.value.status == 404 and len(calls) == 1
    finally:
        server.shutdown()
