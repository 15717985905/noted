import unittest
import tempfile
import os
import sys
import json
import socket
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from noted.hub import (
    ALLOWED_HOSTS,
    validate_host,
    validate_origin,
    get_notes_dir,
    load_index,
    save_index,
    is_summary_file,
    simple_markdown,
    escape_html,
    Handler,
    get_port,
    run_server,
    _get_server_port,
    _generate_discover_token,
    _validate_discover_token,
    _cleanup_discover_tokens,
    _DISCOVER_TOKEN_LOCK,
    _DISCOVER_TOKEN_CACHE,
)


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestHostValidation(unittest.TestCase):
    def test_valid_localhost_with_port(self):
        class FakeSelf:
            class Headers:
                def get(self, key, default=""):
                    return "localhost:8765"
            headers = Headers()
        self.assertTrue(validate_host(FakeSelf()))

    def test_valid_127_with_port(self):
        class FakeSelf:
            class Headers:
                def get(self, key, default=""):
                    return "127.0.0.1:8765"
            headers = Headers()
        self.assertTrue(validate_host(FakeSelf()))

    def test_valid_ipv6_with_port(self):
        class FakeSelf:
            class Headers:
                def get(self, key, default=""):
                    return "[::1]:8765"
            headers = Headers()
        self.assertTrue(validate_host(FakeSelf()))

    def test_invalid_host_rejected(self):
        class FakeSelf:
            class Headers:
                def get(self, key, default=""):
                    return "evil.com:8765"
            headers = Headers()
        self.assertFalse(validate_host(FakeSelf()))

    def test_invalid_port_zero_rejected(self):
        class FakeSelf:
            class Headers:
                def get(self, key, default=""):
                    return "localhost:0"
            headers = Headers()
        self.assertFalse(validate_host(FakeSelf()))

    def test_invalid_port_too_high_rejected(self):
        class FakeSelf:
            class Headers:
                def get(self, key, default=""):
                    return "localhost:70000"
            headers = Headers()
        self.assertFalse(validate_host(FakeSelf()))

    def test_localhost_without_port_accepted(self):
        class FakeSelf:
            class Headers:
                def get(self, key, default=""):
                    return "localhost"
            headers = Headers()
        self.assertTrue(validate_host(FakeSelf()))


class TestOriginValidation(unittest.TestCase):
    def _make_handler(self, origin_header, port=8765):
        class FakeSelf:
            class Server:
                server_address = ("127.0.0.1", port)
            class Headers:
                def get(self, key, default=""):
                    if key == "Origin":
                        return origin_header
                    if key == "Host":
                        return f"localhost:{port}"
                    if key == "Content-Length":
                        return "0"
                    return default
            server = Server()
            headers = Headers()
            requestline = "POST /api/star HTTP/1.1"
            command = "POST"
            path = "/api/star"
            wfile = type("W", (), {"write": lambda *a, **k: None})()
            rfile = type("R", (), {"read": lambda *a, **k: b""})()
            def send_error(self, code):
                raise AssertionError(f"send_error called with {code}")
            send_response = lambda self, *a: None
            send_header = lambda self, *a: None
            end_headers = lambda self: None
        return FakeSelf()

    def test_no_origin_accepted(self):
        h = self._make_handler("")
        self.assertTrue(validate_origin(h))

    def test_null_origin_rejected(self):
        h = self._make_handler("null")
        self.assertFalse(validate_origin(h))

    def test_http_localhost_wrong_port_rejected(self):
        h = self._make_handler("http://localhost:1234", port=8765)
        self.assertFalse(validate_origin(h))

    def test_http_127_wrong_port_rejected(self):
        h = self._make_handler("http://127.0.0.1:9999", port=8765)
        self.assertFalse(validate_origin(h))

    def test_https_origin_rejected(self):
        h = self._make_handler("https://localhost:8765")
        self.assertFalse(validate_origin(h))

    def test_evil_host_rejected(self):
        h = self._make_handler("http://evil.com:8765")
        self.assertFalse(validate_origin(h))

    def test_localhost_exact_port_accepted(self):
        h = self._make_handler("http://localhost:8765", port=8765)
        self.assertTrue(validate_origin(h))

    def test_127_exact_port_accepted(self):
        h = self._make_handler("http://127.0.0.1:8765", port=8765)
        self.assertTrue(validate_origin(h))


class TestPathTraversal(unittest.TestCase):
    def test_read_rejects_dotdot(self):
        handler = Handler.__new__(Handler)
        handler.server = type("S", (), {"server_address": ("127.0.0.1", 8765)})()
        handler.headers = type("H", (), {"get": lambda *a, **k: "localhost:8765"})()
        handler.requestline = "GET /api/read?file=../../etc/passwd HTTP/1.1"
        handler.command = "GET"
        handler.path = "/api/read?file=../../etc/passwd"
        handler.wfile = type("W", (), {"write": lambda *a, **k: None})()
        handler.rfile = type("R", (), {"read": lambda *a, **k: b""})()
        called = []
        def fake_send_error(code):
            called.append(code)
            raise AssertionError(f"send_error called with {code}")
        handler.send_error = fake_send_error
        handler.send_response = lambda self, *a: None
        handler.send_header = lambda self, *a: None
        handler.end_headers = lambda self: None
        try:
            handler.send_read("../../etc/passwd")
        except AssertionError as e:
            self.assertIn("400", str(e))

    def test_read_rejects_slash(self):
        handler = Handler.__new__(Handler)
        handler.server = type("S", (), {"server_address": ("127.0.0.1", 8765)})()
        handler.headers = type("H", (), {"get": lambda *a, **k: "localhost:8765"})()
        handler.requestline = "GET /api/read?file=/etc/passwd HTTP/1.1"
        handler.command = "GET"
        handler.path = "/api/read?file=/etc/passwd"
        handler.wfile = type("W", (), {"write": lambda *a, **k: None})()
        handler.rfile = type("R", (), {"read": lambda *a, **k: b""})()
        def fake_send_error(code):
            raise AssertionError(f"send_error called with {code}")
        handler.send_error = fake_send_error
        handler.send_response = lambda self, *a: None
        handler.send_header = lambda self, *a: None
        handler.end_headers = lambda self: None
        try:
            handler.send_read("/etc/passwd")
        except AssertionError as e:
            self.assertIn("400", str(e))


class TestMarkdownXSS(unittest.TestCase):
    def test_simple_markdown_escapes_script(self):
        md = "<script>alert(1)</script>"
        html = simple_markdown(md)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_simple_markdown_escapes_img_onerror(self):
        md = '<img src="x" onerror="alert(1)">'
        html = simple_markdown(md)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_simple_markdown_escapes_javascript_link(self):
        md = "[click](javascript:alert(1))"
        html = simple_markdown(md)
        self.assertNotIn("javascript:", html)
        self.assertIn("<a href=", html)

    def test_simple_markdown_escapes_data_link(self):
        md = "[data](data:text/html,<script>alert(1)</script>)"
        html = simple_markdown(md)
        self.assertNotIn("data:", html)


class _HTTPServerFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["NOTED_HOME"] = self.tmpdir
        self.port = _find_free_port()
        self.server_thread = None
        self.httpd = None

    def tearDown(self):
        os.environ.pop("NOTED_HOME", None)
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            if self.server_thread and self.server_thread.is_alive():
                self.server_thread.join(timeout=3)
            try:
                self.httpd.server_close()
            except Exception:
                pass

    def _start_server(self):
        import noted.hub as hub
        old_get_port = hub.get_port
        hub.get_port = lambda: self.port
        try:
            self.httpd = hub.ServerClass(("127.0.0.1", self.port), hub.Handler)
            self.server_thread = threading.Thread(target=self.httpd.serve_forever)
            self.server_thread.daemon = True
            self.server_thread.start()
            time.sleep(0.5)
        finally:
            hub.get_port = old_get_port

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _request(self, path, headers=None, data=None, method="GET"):
        req = urllib.request.Request(self._url(path), data=data, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8")
        except Exception as e:
            return None, None, str(e)


class TestRealHTTPIntegration(_HTTPServerFixture):
    def test_list_returns_json(self):
        with open(os.path.join(self.tmpdir, "note1.md"), "w", encoding="utf-8") as f:
            f.write("# Note 1\n\ntags: a\n\nbody")
        with open(os.path.join(self.tmpdir, "note2.md"), "w", encoding="utf-8") as f:
            f.write("# Note 2\n\ntags: b\n\nbody")
        self._start_server()
        status, headers, body = self._request("/api/list")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data), 2)
        self.assertIn("file", data[0])
        self.assertNotIn("source", data[0])

    def test_search_returns_results(self):
        with open(os.path.join(self.tmpdir, "alpha.md"), "w", encoding="utf-8") as f:
            f.write("# Alpha\n\ntags: x\n\nalpha content here")
        with open(os.path.join(self.tmpdir, "beta.md"), "w", encoding="utf-8") as f:
            f.write("# Beta\n\ntags: y\n\nbeta content")
        self._start_server()
        status, headers, body = self._request("/api/search?q=alpha")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["file"], "alpha.md")

    def test_search_matches_filename(self):
        with open(os.path.join(self.tmpdir, "MDM-experiment-summary.md"), "w", encoding="utf-8") as f:
            f.write("# 中文标题不在查询词内\n\ntags: x\n\n正文")
        self._start_server()
        status, headers, body = self._request("/api/search?q=MDM")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["file"], "MDM-experiment-summary.md")

    def test_same_origin_post_star_succeeds(self):
        with open(os.path.join(self.tmpdir, "star.md"), "w", encoding="utf-8") as f:
            f.write("# Star\n\ntags: test\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "star.md", "starred": True}).encode("utf-8")
        status, headers, body = self._request(
            "/api/star",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))

    def test_malicious_host_get_returns_403(self):
        with open(os.path.join(self.tmpdir, "x.md"), "w", encoding="utf-8") as f:
            f.write("# X\n\nbody")
        self._start_server()
        req = urllib.request.Request(self._url("/api/list"))
        req.add_header("Host", "evil.com")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8")
                self.fail(f"Expected 403 but got {resp.status}: {body[:100]}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_null_origin_post_returns_403(self):
        with open(os.path.join(self.tmpdir, "y.md"), "w", encoding="utf-8") as f:
            f.write("# Y\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "y.md", "starred": True}).encode("utf-8")
        status, _, _ = self._request(
            "/api/star",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": "null",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 403)

    def test_path_traversal_returns_400(self):
        self._start_server()
        status, _, _ = self._request("/api/read?file=../../etc/passwd")
        self.assertEqual(status, 400)

    def test_security_headers_present(self):
        with open(os.path.join(self.tmpdir, "h.md"), "w", encoding="utf-8") as f:
            f.write("# H\n\nbody")
        self._start_server()
        _, headers, _ = self._request("/api/list")
        self.assertIn("X-Content-Type-Options", headers)
        self.assertIn("X-Frame-Options", headers)
        self.assertIn("Referrer-Policy", headers)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_no_absolute_source_path_in_api(self):
        target = os.path.join(self.tmpdir, "real.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("# Real\n\nbody")
        link = os.path.join(self.tmpdir, "link.md")
        os.symlink(target, link)
        self._start_server()
        status, headers, body = self._request("/api/list")
        self.assertEqual(status, 200)
        data = json.loads(body)
        link_note = next((n for n in data if n["file"] == "link.md"), None)
        self.assertIsNotNone(link_note)
        self.assertNotIn("source", link_note)
        self.assertIn("source_label", link_note)

    def test_view_save_and_list_roundtrip(self):
        self._start_server()
        payload = json.dumps({"name": "rt-view", "filters": {"search": "alpha"}}).encode("utf-8")
        status, _, body = self._request(
            "/api/views/save",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        status, _, body = self._request("/api/views")
        views = json.loads(body)
        self.assertIn("rt-view", views)
        self.assertEqual(views["rt-view"]["filters"], {"search": "alpha"})

    def test_delete_response_has_no_absolute_path(self):
        self._start_server()
        with open(os.path.join(self.tmpdir, "del.md"), "w", encoding="utf-8") as f:
            f.write("# Del\n\ntags: t\n\nbody")
        payload = json.dumps({"file": "del.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/delete",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        self.assertNotIn("/", json.loads(body).get("trash", ""))
        self.assertNotIn("tmp", body)

    def test_discover_returns_no_absolute_path(self):
        d = os.path.join(self.tmpdir, "discover_src")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "candidate.md"), "w", encoding="utf-8") as f:
            f.write("# Candidate\n\ntags: test\n\nbody")
        sync_paths_file = os.path.join(self.tmpdir, ".sync-paths")
        with open(sync_paths_file, "w") as f:
            f.write(d + "\n")
        self._start_server()
        status, headers, body = self._request("/api/discover")
        self.assertEqual(status, 200)
        data = json.loads(body)
        if data:
            self.assertNotIn("path", data[0])
            self.assertIn("candidate_id", data[0])

    def test_discover_add_requires_candidate_id(self):
        self._start_server()
        payload = json.dumps({"candidate_id": "invalid"}).encode("utf-8")
        status, _, _ = self._request(
            "/api/discover/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 404)


class TestPortConflict(unittest.TestCase):
    def test_port_in_use_exits(self):
        port = _find_free_port()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        try:
            with self.assertRaises(SystemExit):
                run_server(port=port, notes_dir=tempfile.mkdtemp())
        finally:
            s.close()


class TestMarkdownRenderer(unittest.TestCase):
    def test_simple_markdown(self):
        md = "# Title\n\n**bold** and *italic*\n\n- item1\n- item2\n"
        html = simple_markdown(md)
        self.assertIn("<h1>", html)
        self.assertIn("<strong>", html)
        self.assertIn("<em>", html)
        self.assertIn("<li>", html)

    def test_escape_html(self):
        self.assertEqual(escape_html("<script>"), "&lt;script&gt;")


class TestSecurityHeadersMethod(unittest.TestCase):
    def test_security_headers_method_exists(self):
        self.assertTrue(hasattr(Handler, "send_security_headers"))


class TestDiscoverPrivacy(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.environ["NOTED_HOME"] = self.tmpdir

    def tearDown(self):
        os.environ.pop("NOTED_HOME", None)

    def test_discover_response_has_no_absolute_path(self):
        with open(os.path.join(self.tmpdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("# Note\n\ntags: test\n\nbody")
        handler = Handler.__new__(Handler)
        handler.server = type("S", (), {"server_address": ("127.0.0.1", 8765)})()
        handler.headers = type("H", (), {"get": lambda *a, **k: "localhost:8765"})()
        handler.requestline = "GET /api/discover HTTP/1.1"
        handler.command = "GET"
        handler.path = "/api/discover"
        handler.wfile = type("W", (), {"write": lambda *a, **k: None})()
        handler.send_json = mock.Mock()
        handler.send_discover()
        call_args = handler.send_json.call_args[0][0]
        if call_args:
            candidate = call_args[0]
            self.assertNotIn("path", candidate)
            self.assertIn("candidate_id", candidate)
            self.assertIn("source_label", candidate)


class TestFrontendInjectionStatic(unittest.TestCase):
    def test_no_onclick_in_html(self):
        from noted.hub import HTML
        self.assertNotIn("onclick=", HTML)

    def test_no_marked_parse_in_html(self):
        from noted.hub import HTML
        self.assertNotIn("marked.parse", HTML)

    def test_no_cdn_marked_in_html(self):
        from noted.hub import HTML
        self.assertNotIn("cdn.jsdelivr.net/npm/marked", HTML)


class TestMarkdownLinkWhitelist(unittest.TestCase):
    def test_simple_markdown_rejects_vbscript(self):
        md = "[x](vbscript:alert(1))"
        html = simple_markdown(md)
        self.assertNotIn("vbscript:", html)

    def test_simple_markdown_allows_https(self):
        md = "[link](https://example.com)"
        html = simple_markdown(md)
        self.assertIn('href="https://example.com"', html)

    def test_simple_markdown_allows_mailto(self):
        md = "[mail](mailto:test@example.com)"
        html = simple_markdown(md)
        self.assertIn('href="mailto:test@example.com"', html)

    def test_simple_markdown_escapes_raw_html(self):
        md = "<img src=x onerror=alert(1)>"
        html = simple_markdown(md)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)


class TestCLI(unittest.TestCase):
    def test_serve_parser_help(self):
        from noted.cli import main
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit):
                sys.argv = ["noted", "serve", "--help"]
                main()
        finally:
            sys.stdout = old_stdout

    def test_discover_uses_base_url(self):
        from noted.cli import get_base_url, get_port
        os.environ["NOTED_PORT"] = "9999"
        try:
            self.assertEqual(get_base_url(), "http://localhost:9999")
        finally:
            os.environ.pop("NOTED_PORT", None)


class TestDiscoverTokenBoundary(_HTTPServerFixture):
    def test_token_is_opaque_random_string(self):
        tmpdir = tempfile.mkdtemp()
        token = _generate_discover_token(tmpdir, os.path.join(tmpdir, "x.md"))
        self.assertNotIn("/", token)
        self.assertNotIn("Users", token)
        self.assertNotIn("home", token)
        self.assertNotIn("~", token)
        self.assertNotIn(os.path.basename(tmpdir), token)

    def test_token_validates_notes_dir_and_file(self):
        tmpdir = tempfile.mkdtemp()
        target = os.path.join(tmpdir, "real.md")
        with open(target, "w") as f:
            f.write("# Real\n\nbody")
        token = _generate_discover_token(tmpdir, target)
        entry = _validate_discover_token(token, tmpdir)
        self.assertTrue(entry)
        self.assertEqual(entry["real_path"], os.path.realpath(target))

        other_dir = tempfile.mkdtemp()
        self.assertFalse(_validate_discover_token(token, other_dir))

    def test_token_rejects_unknown_token(self):
        tmpdir = tempfile.mkdtemp()
        self.assertFalse(_validate_discover_token("unknown-token-xyz", tmpdir))

    def test_token_rejects_after_expiry(self):
        import datetime as dt
        tmpdir = tempfile.mkdtemp()
        target = os.path.join(tmpdir, "real.md")
        with open(target, "w") as f:
            f.write("# Real\n\nbody")
        with _DISCOVER_TOKEN_LOCK:
            _DISCOVER_TOKEN_CACHE["expired-token"] = {
                "notes_dir": os.path.realpath(tmpdir),
                "real_path": os.path.realpath(target),
                "expires_at": datetime.now() - timedelta(seconds=1),
            }
        self.assertFalse(_validate_discover_token("expired-token", tmpdir))

    def test_real_http_discover_returns_no_path_and_validates_token(self):
        d = os.path.join(self.tmpdir, "discover_src")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "candidate.md"), "w", encoding="utf-8") as f:
            f.write("# Candidate\n\ntags: test\n\nbody")
        sync_paths_file = os.path.join(self.tmpdir, ".sync-paths")
        with open(sync_paths_file, "w") as f:
            f.write(d + "\n")
        self._start_server()
        status, headers, body = self._request("/api/discover")
        self.assertEqual(status, 200)
        data = json.loads(body)
        if data:
            candidate = data[0]
            self.assertNotIn("path", candidate)
            self.assertIn("candidate_id", candidate)
            token = candidate["candidate_id"]
            self.assertNotIn("/", token)
            payload = json.dumps({"candidate_id": "invalid-token"}).encode("utf-8")
            status2, _, _ = self._request(
                "/api/discover/add",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status2, 404)


class TestMarkdownLinkWhitelist(unittest.TestCase):
    def test_simple_markdown_rejects_vbscript(self):
        md = "[x](vbscript:alert(1))"
        html = simple_markdown(md)
        self.assertNotIn("vbscript:", html)

    def test_simple_markdown_allows_https(self):
        md = "[link](https://example.com)"
        html = simple_markdown(md)
        self.assertIn('href="https://example.com"', html)

    def test_simple_markdown_allows_mailto(self):
        md = "[mail](mailto:test@example.com)"
        html = simple_markdown(md)
        self.assertIn('href="mailto:test@example.com"', html)

    def test_simple_markdown_escapes_raw_html(self):
        md = "<img src=x onerror=alert(1)>"
        html = simple_markdown(md)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_simple_markdown_rejects_javascript(self):
        md = "[x](javascript:alert(1))"
        html = simple_markdown(md)
        self.assertNotIn("javascript:", html)

    def test_simple_markdown_rejects_data(self):
        md = "[x](data:text/html,<script>alert(1)</script>)"
        html = simple_markdown(md)
        self.assertNotIn("data:", html)

    def test_simple_markdown_rejects_protocol_relative(self):
        md = "[x](//evil.com)"
        html = simple_markdown(md)
        self.assertNotIn("//evil.com", html)

    def test_simple_markdown_allows_relative(self):
        md = "[x](relative/path)"
        html = simple_markdown(md)
        self.assertIn('href="relative/path"', html)


class TestFrontendStatic(unittest.TestCase):
    def test_no_onclick_in_html(self):
        from noted.hub import HTML
        self.assertNotIn("onclick=", HTML)

    def test_no_marked_parse_in_html(self):
        from noted.hub import HTML
        self.assertNotIn("marked.parse", HTML)

    def test_no_cdn_marked_in_html(self):
        from noted.hub import HTML
        self.assertNotIn("cdn.jsdelivr.net/npm/marked", HTML)


class TestCLIDoctor(unittest.TestCase):
    def test_doctor_reports_healthy(self):
        import noted.cli
        from noted.cli import cmd_doctor
        import noted.hub as hub
        tmpdir = tempfile.mkdtemp()
        old = os.environ.get("NOTED_HOME")
        os.environ["NOTED_HOME"] = tmpdir
        old_port = hub.get_port
        hub.get_port = lambda: _find_free_port()
        old_base = None
        server = None
        thread = None
        try:
            server = hub.ServerClass(("127.0.0.1", hub.get_port()), hub.Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            time.sleep(0.3)
            old_base = noted.cli.get_base_url
            noted.cli.get_base_url = lambda: f"http://127.0.0.1:{hub.get_port()}"
            cmd_doctor()
        finally:
            if old_base is not None:
                noted.cli.get_base_url = old_base
            if server:
                try:
                    server.shutdown()
                except Exception:
                    pass
                if thread and thread.is_alive():
                    thread.join(timeout=2)
                try:
                    server.server_close()
                except Exception:
                    pass
            if old is None:
                os.environ.pop("NOTED_HOME", None)
            else:
                os.environ["NOTED_HOME"] = old
            hub.get_port = old_port

    def test_doctor_reports_missing_dir(self):
        from noted.cli import cmd_doctor
        tmpdir = tempfile.mkdtemp()
        target = os.path.join(tmpdir, "missing")
        old = os.environ.get("NOTED_HOME")
        os.environ["NOTED_HOME"] = target
        try:
            with self.assertRaises(SystemExit) as cm:
                cmd_doctor()
            self.assertEqual(cm.exception.code, 1)
        finally:
            if old is None:
                os.environ.pop("NOTED_HOME", None)
            else:
                os.environ["NOTED_HOME"] = old

    def test_doctor_reports_broken_symlink(self):
        from noted.cli import cmd_doctor
        tmpdir = tempfile.mkdtemp()
        link = os.path.join(tmpdir, "broken.md")
        os.symlink("/nonexistent/path", link)
        old = os.environ.get("NOTED_HOME")
        os.environ["NOTED_HOME"] = tmpdir
        try:
            with self.assertRaises(SystemExit) as cm:
                cmd_doctor()
            self.assertEqual(cm.exception.code, 1)
        finally:
            if old is None:
                os.environ.pop("NOTED_HOME", None)
            else:
                os.environ["NOTED_HOME"] = old

    def test_doctor_reports_bad_sync_path(self):
        from noted.cli import cmd_doctor
        tmpdir = tempfile.mkdtemp()
        sync_file = os.path.join(tmpdir, ".sync-paths")
        with open(sync_file, "w") as f:
            f.write("/nonexistent/sync/path\n")
        old = os.environ.get("NOTED_HOME")
        os.environ["NOTED_HOME"] = tmpdir
        try:
            with self.assertRaises(SystemExit) as cm:
                cmd_doctor()
            self.assertEqual(cm.exception.code, 1)
        finally:
            if old is None:
                os.environ.pop("NOTED_HOME", None)
            else:
                os.environ["NOTED_HOME"] = old


class TestRename(_HTTPServerFixture):
    def test_rename_real_file_success(self):
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\ntags: a\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": "new.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["file"], "new.md")
        self.assertFalse(os.path.isfile(os.path.join(self.tmpdir, "old.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmpdir, "new.md")))
        with open(os.path.join(self.tmpdir, "new.md"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "# Old\n\ntags: a\n\nbody")

    def test_rename_symlink_only_link_name(self):
        target = os.path.join(self.tmpdir, "real.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("# Real\n\nbody")
        link = os.path.join(self.tmpdir, "link.md")
        os.symlink(target, link)
        original_target = os.readlink(link)
        self._start_server()
        payload = json.dumps({"file": "link.md", "new_name": "newlink.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["file"], "newlink.md")
        self.assertFalse(os.path.exists(link))
        self.assertTrue(os.path.islink(os.path.join(self.tmpdir, "newlink.md")))
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(os.readlink(os.path.join(self.tmpdir, "newlink.md")), original_target)

    def test_rename_symlink_propagate(self):
        target = os.path.join(self.tmpdir, "real.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("# Real\n\nbody")
        link = os.path.join(self.tmpdir, "link.md")
        os.symlink(target, link)
        self._start_server()
        payload = json.dumps({"file": "link.md", "new_name": "new.md", "propagate": True}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["propagated"])
        self.assertFalse(os.path.exists(link))
        self.assertTrue(os.path.isfile(os.path.join(self.tmpdir, "new.md")))
        self.assertFalse(os.path.isfile(target))

    def test_rename_propagate_non_symlink_rejected(self):
        with open(os.path.join(self.tmpdir, "real.md"), "w", encoding="utf-8") as f:
            f.write("# Real\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "real.md", "new_name": "new.md", "propagate": True}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data.get("ok"))
        self.assertIn("软链接", data.get("error", ""))

    def test_rename_propagate_shared_realpath_rejected(self):
        target = os.path.join(self.tmpdir, "real.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("# Real\n\nbody")
        link1 = os.path.join(self.tmpdir, "link1.md")
        link2 = os.path.join(self.tmpdir, "link2.md")
        os.symlink(target, link1)
        os.symlink(target, link2)
        self._start_server()
        payload = json.dumps({"file": "link1.md", "new_name": "new.md", "propagate": True}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data.get("ok"))
        self.assertIn("链接", data.get("error", ""))

    def test_rename_same_name_conflict(self):
        with open(os.path.join(self.tmpdir, "exist.md"), "w", encoding="utf-8") as f:
            f.write("# Exist\n\nbody")
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": "exist.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data.get("ok"))

    def test_rename_traversal_rejected(self):
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": "../escape.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 400)

    def test_rename_reserved_name_rejected(self):
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": ".index.json"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data.get("ok"))

    def test_rename_migrates_star_and_tags(self):
        index = {"old.md": {"tags": ["a", "b"], "starred": True, "note": "note1"}}
        with open(os.path.join(self.tmpdir, ".index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f)
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\ntags: x\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": "new.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        with open(os.path.join(self.tmpdir, ".index.json"), "r", encoding="utf-8") as f:
            idx = json.load(f)
        self.assertIn("new.md", idx)
        self.assertEqual(idx["new.md"]["tags"], ["a", "b"])
        self.assertTrue(idx["new.md"]["starred"])
        self.assertEqual(idx["new.md"]["note"], "note1")
        self.assertNotIn("old.md", idx)

    def test_rename_update_h1(self):
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": "new.md", "update_h1": True}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["h1_updated"])
        with open(os.path.join(self.tmpdir, "new.md"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "# new\n\nbody")

    def test_rename_update_h1_no_h1(self):
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("no heading\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": "new.md", "update_h1": True}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertFalse(data["h1_updated"])

    def test_rename_then_sync_no_duplicate(self):
        target = os.path.join(self.tmpdir, "real.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("# Real\n\nbody")
        link = os.path.join(self.tmpdir, "link.md")
        os.symlink(target, link)
        self._start_server()
        payload = json.dumps({"file": "link.md", "new_name": "newlink.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        _, _, sync_body = self._request("/api/sync")
        sync_data = json.loads(sync_body)
        self.assertEqual(sync_data["count"], 0)

    def test_rename_malicious_host_rejected(self):
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\nbody")
        self._start_server()
        payload = json.dumps({"file": "old.md", "new_name": "new.md"}).encode("utf-8")
        req = urllib.request.Request(self._url("/api/rename"), data=payload, method="POST")
        req.add_header("Host", "evil.com")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(payload)))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8")
                self.fail(f"Expected 403 but got {resp.status}: {body[:100]}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_rename_missing_file_404(self):
        self._start_server()
        payload = json.dumps({"file": "missing.md", "new_name": "new.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 404)


class TestFileDetail(_HTTPServerFixture):
    def test_detail_local_file(self):
        with open(os.path.join(self.tmpdir, "local.md"), "w", encoding="utf-8") as f:
            f.write("# Local\n\nbody")
        self._start_server()
        status, _, body = self._request("/api/detail?file=local.md")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["file"], "local.md")
        self.assertEqual(data["kind"], "local")
        self.assertTrue(data["path"].startswith(self.tmpdir))
        self.assertEqual(data["source_label"], "notes")
        self.assertTrue(data["exists"])

    def test_detail_symlink(self):
        target = os.path.join(self.tmpdir, "real.md")
        with open(target, "w", encoding="utf-8") as f:
            f.write("# Real\n\nbody")
        link = os.path.join(self.tmpdir, "link.md")
        os.symlink(target, link)
        self._start_server()
        status, _, body = self._request("/api/detail?file=link.md")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["file"], "link.md")
        self.assertEqual(data["kind"], "symlink")
        self.assertEqual(data["path"], os.path.realpath(target))
        self.assertTrue(data["exists"])

    def test_detail_broken_symlink(self):
        target = os.path.join(self.tmpdir, "ghost.md")
        link = os.path.join(self.tmpdir, "link.md")
        os.symlink(target, link)
        self._start_server()
        status, _, body = self._request("/api/detail?file=link.md")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["kind"], "symlink")
        self.assertFalse(data["exists"])

    def test_detail_missing_404(self):
        self._start_server()
        status, _, _ = self._request("/api/detail?file=missing.md")
        self.assertEqual(status, 404)

    def test_detail_traversal_400(self):
        self._start_server()
        status, _, _ = self._request("/api/detail?file=../outside.md")
        self.assertEqual(status, 400)

    def test_detail_malicious_host_403(self):
        with open(os.path.join(self.tmpdir, "x.md"), "w", encoding="utf-8") as f:
            f.write("# X\n\nbody")
        self._start_server()
        req = urllib.request.Request(self._url("/api/detail?file=x.md"))
        req.add_header("Host", "evil.com")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.fail(f"Expected 403 but got {resp.status}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_reveal_success(self):
        with open(os.path.join(self.tmpdir, "local.md"), "w", encoding="utf-8") as f:
            f.write("# Local\n\nbody")
        self._start_server()
        with mock.patch("subprocess.run") as mock_run:
            payload = json.dumps({"file": "local.md"}).encode("utf-8")
            status, _, body = self._request(
                "/api/reveal",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            mock_run.assert_called_once()

    def test_open_success(self):
        with open(os.path.join(self.tmpdir, "local.md"), "w", encoding="utf-8") as f:
            f.write("# Local\n\nbody")
        self._start_server()
        with mock.patch("subprocess.Popen") as mock_popen:
            payload = json.dumps({"file": "local.md"}).encode("utf-8")
            status, _, body = self._request(
                "/api/open",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            mock_popen.assert_called_once()

    def test_open_with_editor_env(self):
        with open(os.path.join(self.tmpdir, "local.md"), "w", encoding="utf-8") as f:
            f.write("# Local\n\nbody")
        self._start_server()
        with mock.patch.dict(os.environ, {"NOTED_EDITOR": "vim"}):
            with mock.patch("subprocess.Popen") as mock_popen:
                payload = json.dumps({"file": "local.md"}).encode("utf-8")
                status, _, body = self._request(
                    "/api/open",
                    method="POST",
                    headers={
                        "Host": f"localhost:{self.port}",
                        "Origin": f"http://localhost:{self.port}",
                        "Content-Type": "application/json",
                        "Content-Length": str(len(payload)),
                    },
                    data=payload,
                )
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertTrue(data["ok"])
                args, _ = mock_popen.call_args
                self.assertEqual(args[0][0], "vim")

    def test_reveal_open_missing_file_404(self):
        self._start_server()
        payload = json.dumps({"file": "missing.md"}).encode("utf-8")
        status, _, _ = self._request(
            "/api/reveal",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 404)

        status, _, _ = self._request(
            "/api/open",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 404)

    def test_list_has_no_absolute_path(self):
        with open(os.path.join(self.tmpdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("# Note\n\nbody")
        self._start_server()
        status, _, body = self._request("/api/list")
        self.assertEqual(status, 200)
        self.assertNotIn("/Users/", body)
        self.assertNotIn("/home/", body)

    def test_responses_have_no_cors_header(self):
        with open(os.path.join(self.tmpdir, "note.md"), "w", encoding="utf-8") as f:
            f.write("# Note\n\nbody")
        self._start_server()
        _, headers, _ = self._request("/api/list")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        _, headers, _ = self._request("/api/detail?file=note.md")
        self.assertNotIn("Access-Control-Allow-Origin", headers)


class TestGroups(_HTTPServerFixture):
    def test_group_create_success(self):
        self._start_server()
        payload = json.dumps({"name": "工作"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["group"], "工作")

    def test_group_create_duplicate(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        payload = json.dumps({"name": "工作"}).encode("utf-8")
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        status, _, body = self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("已存在", data["error"])

    def test_group_create_invalid_name(self):
        self._start_server()
        for bad in ["", "   ", "_test", "a" * 21, "工作/测试"]:
            payload = json.dumps({"name": bad}).encode("utf-8")
            status, _, body = self._request(
                "/api/group/create",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertFalse(data["ok"])

    def test_group_add_file(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        payload = json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        self.assertEqual(len(groups), 1)
        self.assertIn("a.md", groups[0]["files"])

    def test_group_add_auto_remove_from_old_group(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        for g in ["旧组", "新组"]:
            self._request(
                "/api/group/create",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(json.dumps({"name": g}).encode("utf-8"))),
                },
                data=json.dumps({"name": g}).encode("utf-8"),
            )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "旧组", "file": "a.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "旧组", "file": "a.md"}).encode("utf-8"),
        )
        payload = json.dumps({"group": "新组", "file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        new_group = next(g for g in groups if g["name"] == "新组")
        self.assertIn("a.md", new_group["files"])
        old_group = next(g for g in groups if g["name"] == "旧组")
        self.assertNotIn("a.md", old_group["files"])

    def test_group_add_idempotent(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        payload = json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8")
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        status, _, _ = self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        self.assertEqual(groups[0]["count"], 1)

    def test_group_remove_file(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"),
        )
        payload = json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/remove",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        _, _, list_body = self._request("/api/list")
        notes = json.loads(list_body)
        self.assertEqual(notes[0]["group"], "")

    def test_empty_group_persists(self):
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "空组"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "空组"}).encode("utf-8"),
        )
        status, _, body = self._request("/api/groups")
        self.assertEqual(status, 200)
        groups = json.loads(body)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "空组")
        self.assertEqual(groups[0]["count"], 0)

    def test_group_remove_file_only_no_group(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        for path, obj in [
            ("/api/group/create", {"name": "工作"}),
            ("/api/group/add", {"group": "工作", "file": "a.md"}),
        ]:
            payload = json.dumps(obj).encode("utf-8")
            self._request(
                path,
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
        payload = json.dumps({"file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/remove",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        self.assertEqual(groups[0]["files"], [])
        payload = json.dumps({"file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/remove",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("不在任何分组", data["error"])

    def test_group_rename(self):
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "旧组"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "旧组"}).encode("utf-8"),
        )
        payload = json.dumps({"old_name": "旧组", "new_name": "新组"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["name"], "新组")
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "新组")

    def test_group_disband(self):
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        payload = json.dumps({"name": "工作"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/disband",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        self.assertEqual(len(groups), 0)

    def test_group_toggle_collapsed(self):
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        payload = json.dumps({"name": "工作", "collapsed": True}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/toggle",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["collapsed"])

    def test_group_list_returns_all(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        for name in ["A", "B"]:
            self._request(
                "/api/group/create",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(json.dumps({"name": name}).encode("utf-8"))),
                },
                data=json.dumps({"name": name}).encode("utf-8"),
            )
        status, _, body = self._request("/api/groups")
        self.assertEqual(status, 200)
        groups = json.loads(body)
        self.assertEqual(len(groups), 2)

    def test_group_with_search_filter(self):
        with open(os.path.join(self.tmpdir, "alpha.md"), "w", encoding="utf-8") as f:
            f.write("# Alpha\n\ntags: x\n\nbody")
        with open(os.path.join(self.tmpdir, "beta.md"), "w", encoding="utf-8") as f:
            f.write("# Beta\n\ntags: y\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "工作", "file": "alpha.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "工作", "file": "alpha.md"}).encode("utf-8"),
        )
        status, _, body = self._request("/api/search?q=beta")
        self.assertEqual(status, 200)
        notes = json.loads(body)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["file"], "beta.md")
        self.assertEqual(notes[0]["group"], "")

    def test_group_survives_file_rename(self):
        with open(os.path.join(self.tmpdir, "old.md"), "w", encoding="utf-8") as f:
            f.write("# Old\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "工作", "file": "old.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "工作", "file": "old.md"}).encode("utf-8"),
        )
        payload = json.dumps({"file": "old.md", "new_name": "new.md"}).encode("utf-8")
        status, _, _ = self._request(
            "/api/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        self.assertEqual(len(groups), 1)
        self.assertIn("new.md", groups[0]["files"])
        self.assertNotIn("old.md", groups[0]["files"])

    def test_group_survives_file_delete(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"),
        )
        payload = json.dumps({"file": "a.md"}).encode("utf-8")
        status, _, _ = self._request(
            "/api/delete",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["files"], [])
        self.assertEqual(groups[0]["count"], 0)

    def test_group_malicious_host_rejected(self):
        self._start_server()
        payload = json.dumps({"name": "工作"}).encode("utf-8")
        req = urllib.request.Request(self._url("/api/group/create"), data=payload, method="POST")
        req.add_header("Host", "evil.com")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(payload)))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.fail(f"Expected 403 but got {resp.status}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_group_origin_rejected(self):
        self._start_server()
        payload = json.dumps({"name": "工作"}).encode("utf-8")
        req = urllib.request.Request(self._url("/api/group/create"), data=payload, method="POST")
        req.add_header("Host", f"localhost:{self.port}")
        req.add_header("Origin", "http://evil.com")
        req.add_header("Content-Type", "application/json")
        req.add_header("Content-Length", str(len(payload)))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.fail(f"Expected 403 but got {resp.status}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_group_no_cors_header(self):
        self._start_server()
        payload = json.dumps({"name": "工作"}).encode("utf-8")
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        _, headers, _ = self._request("/api/groups")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_drag_out_semantics_group_field_clears(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"),
        )
        payload = json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/remove",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        _, _, list_body = self._request("/api/list")
        notes = json.loads(list_body)
        self.assertEqual(notes[0]["group"], "")

    def test_list_has_group_field(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8"),
        )
        status, _, body = self._request("/api/list")
        self.assertEqual(status, 200)
        notes = json.loads(body)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["group"], "工作")
        self.assertNotIn("/Users/", body)

    def test_search_has_group_field(self):
        with open(os.path.join(self.tmpdir, "alpha.md"), "w", encoding="utf-8") as f:
            f.write("# Alpha\n\ntags: x\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "工作", "file": "alpha.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "工作", "file": "alpha.md"}).encode("utf-8"),
        )
        status, _, body = self._request("/api/search?q=alpha")
        self.assertEqual(status, 200)
        notes = json.loads(body)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["group"], "工作")

    def test_group_create_non_string_name_soft_error(self):
        self._start_server()
        for bad in [None, [], {}]:
            payload = json.dumps({"name": bad}).encode("utf-8")
            status, _, body = self._request(
                "/api/group/create",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertFalse(data["ok"])

    def test_group_add_non_string_params_soft_error(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        for bad_group in [123, None, [], {}]:
            payload = json.dumps({"group": bad_group, "file": "a.md"}).encode("utf-8")
            status, _, body = self._request(
                "/api/group/add",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertFalse(data["ok"])
        for bad_file in [123, None, [], {}]:
            payload = json.dumps({"group": "工作", "file": bad_file}).encode("utf-8")
            status, _, body = self._request(
                "/api/group/add",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertFalse(data["ok"])

    def test_group_name_stripped(self):
        self._start_server()
        payload = json.dumps({"name": " 工作 "}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["group"], "工作")
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        payload = json.dumps({"group": " 工作 ", "file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        _, _, list_body = self._request("/api/list")
        notes = json.loads(list_body)
        self.assertEqual(notes[0]["group"], "工作")

    def test_group_add_cleans_all_old_groups(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        for g in ["旧组1", "旧组2"]:
            self._request(
                "/api/group/create",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(json.dumps({"name": g}).encode("utf-8"))),
                },
                data=json.dumps({"name": g}).encode("utf-8"),
            )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "旧组1", "file": "a.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "旧组1", "file": "a.md"}).encode("utf-8"),
        )
        self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"group": "旧组2", "file": "a.md"}).encode("utf-8"))),
            },
            data=json.dumps({"group": "旧组2", "file": "a.md"}).encode("utf-8"),
        )
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "新组"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "新组"}).encode("utf-8"),
        )
        payload = json.dumps({"group": "新组", "file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/add",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        new_group = next(g for g in groups if g["name"] == "新组")
        self.assertIn("a.md", new_group["files"])
        for old_name in ["旧组1", "旧组2"]:
            old_group = next(g for g in groups if g["name"] == old_name)
            self.assertNotIn("a.md", old_group["files"])

    def test_groups_safe_with_corrupted_index(self):
        index = {
            "_group_好": {"name": "好", "files": ["a.md", 123, None, {"a": "b"}], "collapsed": False, "created_at": "", "updated_at": ""},
            "_group_坏": {"name": "坏", "files": "../../etc/passwd", "collapsed": "yes", "created_at": "", "updated_at": ""},
        }
        with open(os.path.join(self.tmpdir, ".index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f)
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        status, _, body = self._request("/api/groups")
        self.assertEqual(status, 200)
        groups = json.loads(body)
        good = next(g for g in groups if g["name"] == "好")
        self.assertEqual(good["files"], ["a.md"])
        self.assertFalse(good["collapsed"])
        bad = next(g for g in groups if g["name"] == "坏")
        self.assertEqual(bad["files"], [])
        self.assertFalse(bad["collapsed"])
        self.assertNotIn("/Users/", body)
        self.assertNotIn("/etc/", body)

    def test_group_toggle_non_bool_collapsed_rejected(self):
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        for bad in ["true", 1, 0, []]:
            payload = json.dumps({"name": "工作", "collapsed": bad}).encode("utf-8")
            status, _, body = self._request(
                "/api/group/toggle",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertFalse(data["ok"])

    def test_group_remove_null_group_rejected(self):
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "工作"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "工作"}).encode("utf-8"),
        )
        payload = json.dumps({"group": None, "file": "a.md"}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/remove",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["ok"])

    def test_group_ops_safe_with_corrupted_target_group(self):
        index = {
            "_group_工作": {"name": "工作", "files": "broken", "collapsed": "yes", "created_at": "", "updated_at": ""},
        }
        with open(os.path.join(self.tmpdir, ".index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f)
        with open(os.path.join(self.tmpdir, "a.md"), "w", encoding="utf-8") as f:
            f.write("# A\n\nbody")
        self._start_server()
        post_headers = {
            "Host": f"localhost:{self.port}",
            "Origin": f"http://localhost:{self.port}",
            "Content-Type": "application/json",
        }
        payload = json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8")
        post_headers["Content-Length"] = str(len(payload))
        status, _, body = self._request(
            "/api/group/add",
            method="POST",
            headers=post_headers,
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        _, _, groups_body = self._request("/api/groups")
        groups = json.loads(groups_body)
        work = next(g for g in groups if g["name"] == "工作")
        self.assertEqual(work["files"], ["a.md"])
        payload = json.dumps({"group": "工作", "file": "a.md"}).encode("utf-8")
        post_headers["Content-Length"] = str(len(payload))
        status, _, body = self._request(
            "/api/group/remove",
            method="POST",
            headers=post_headers,
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        payload = json.dumps({"name": "工作", "collapsed": True}).encode("utf-8")
        post_headers["Content-Length"] = str(len(payload))
        status, _, body = self._request(
            "/api/group/toggle",
            method="POST",
            headers=post_headers,
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["collapsed"])

    def test_group_rename_non_string_params_soft_error(self):
        self._start_server()
        self._request(
            "/api/group/create",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(json.dumps({"name": "旧组"}).encode("utf-8"))),
            },
            data=json.dumps({"name": "旧组"}).encode("utf-8"),
        )
        for bad in [123, None, [], {}]:
            payload = json.dumps({"old_name": bad, "new_name": "新组"}).encode("utf-8")
            status, _, body = self._request(
                "/api/group/rename",
                method="POST",
                headers={
                    "Host": f"localhost:{self.port}",
                    "Origin": f"http://localhost:{self.port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                },
                data=payload,
            )
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertFalse(data["ok"])
        payload = json.dumps({"old_name": "旧组", "new_name": []}).encode("utf-8")
        status, _, body = self._request(
            "/api/group/rename",
            method="POST",
            headers={
                "Host": f"localhost:{self.port}",
                "Origin": f"http://localhost:{self.port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
            data=payload,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()