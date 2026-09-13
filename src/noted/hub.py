#!/usr/bin/env python3
# ~/Projects/noted/src/noted/hub.py
# 落笔 · Noted — 本地网页服务 + 自动软链接同步 + 全文搜索 + 标签管理
# 零依赖，macOS 自带 Python 运行

import base64
import http.server
import os
import json
import re
import sys
import subprocess
import shlex
import urllib.parse
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional

try:
    from http.server import ThreadingHTTPServer
    ServerClass = ThreadingHTTPServer
except ImportError:
    from socketserver import TCPServer
    ServerClass = TCPServer

DEFAULT_PORT = 8765
DEFAULT_NOTES_DIR = os.path.expanduser("~/ai-notes")

EXCLUDE_FILES = {
    "README.md", "CHANGELOG.md", "LICENSE.md", "CONTRIBUTING.md",
    "AGENTS.md", "AGENTS_RULE.md", ".sync-paths", ".index.json",
}

EXCLUDE_DIRS = {
    "gpt备份", "node_modules", ".git", "__pycache__", ".trash", "._assets_",
}

SKIP_DIRS = {
    "Library", "ai-notes", "build", "dist", "Pods",
    "node_modules", "gpt备份", "__pycache__", "venv", ".venv", ".next",
    ".claude-science", ".claude", ".mirasim", ".aweskill", ".nvm",
    ".cursor", ".opencode", ".codex",
    "._assets_",
}

REFRESH_INTERVAL = 5000

_DISCOVER_TOKEN_CACHE = {}
_DISCOVER_TOKEN_LOCK = threading.Lock()
_DISCOVER_TOKEN_TTL = timedelta(hours=24)


def get_notes_dir() -> str:
    notes_dir = os.environ.get("NOTED_HOME", "")
    if not notes_dir:
        notes_dir = DEFAULT_NOTES_DIR
    return os.path.abspath(os.path.expanduser(notes_dir))


def get_port() -> int:
    port_str = os.environ.get("NOTED_PORT", "")
    if port_str.isdigit():
        return int(port_str)
    return DEFAULT_PORT


def load_index(notes_dir: str):
    index_file = os.path.join(notes_dir, ".index.json")
    if os.path.isfile(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                index = json.load(f)
            return index if isinstance(index, dict) else {}
        except Exception:
            pass
    return {}


def save_index(notes_dir: str, index):
    index_file = os.path.join(notes_dir, ".index.json")
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


_GROUP_NAME_RE = re.compile(r'^[\u4e00-\u9fa5a-zA-Z0-9_\- ]{1,20}$')


def _validate_group_name(name: str):
    if not isinstance(name, str):
        return "组名必须为字符串"
    if not name:
        return "组名不能为空"
    stripped = name.strip()
    if not stripped:
        return "组名不能为空"
    if len(stripped) > 20:
        return "组名长度不能超过 20 个字符"
    if stripped.startswith("_"):
        return "组名不能以 _ 开头"
    if not _GROUP_NAME_RE.match(stripped):
        return "组名只能包含中文、英文、数字、下划线、连字符和空格"
    return None


def _is_safe_group_file(filename):
    return (
        isinstance(filename, str)
        and bool(filename)
        and "/" not in filename
        and "\\" not in filename
        and ".." not in filename
    )


def _group_files(entry):
    if not isinstance(entry, dict):
        return []
    raw_files = entry.get("files", [])
    if not isinstance(raw_files, list):
        return []
    files = []
    seen = set()
    for filename in raw_files:
        if not _is_safe_group_file(filename) or filename in seen:
            continue
        seen.add(filename)
        files.append(filename)
    return files


def _safe_group_entry(entry: dict, key: str) -> dict:
    if not isinstance(entry, dict) or not isinstance(key, str):
        return None
    suffix = key[len("_group_"):] if key.startswith("_group_") else ""
    name = entry.get("name", suffix)
    if not isinstance(name, str):
        name = suffix
    name = name.strip()
    if not name or _validate_group_name(name):
        return None
    collapsed = entry.get("collapsed", False)
    if not isinstance(collapsed, bool):
        collapsed = False
    return {
        "name": name,
        "files": _group_files(entry),
        "collapsed": collapsed,
        "created_at": entry.get("created_at", "") if isinstance(entry.get("created_at"), str) else "",
        "updated_at": entry.get("updated_at", "") if isinstance(entry.get("updated_at"), str) else "",
    }


def _get_groups(index: dict):
    if not isinstance(index, dict):
        return {}
    groups = {}
    for key, entry in index.items():
        if not isinstance(key, str) or not key.startswith("_group_"):
            continue
        safe = _safe_group_entry(entry, key)
        if safe:
            groups[key] = safe
    return groups


def _set_groups(index: dict, groups: dict):
    for key in list(index.keys()):
        if isinstance(key, str) and key.startswith("_group_"):
            del index[key]
    for key, group in groups.items():
        index[key] = group


def _find_group_for_file(index: dict, filename: str) -> str:
    if not _is_safe_group_file(filename):
        return ""
    for key, entry in _get_groups(index).items():
        if filename in entry["files"]:
            return entry["name"]
    return ""


def _remove_file_from_all_groups(index: dict, filename: str):
    if not isinstance(index, dict):
        return
    for key, entry in index.items():
        if not isinstance(key, str) or not key.startswith("_group_") or not isinstance(entry, dict):
            continue
        files = _group_files(entry)
        if filename in files:
            files.remove(filename)
            entry["files"] = files
            entry["updated_at"] = datetime.now().isoformat()


def load_sync_paths(notes_dir: str):
    sync_paths_file = os.path.join(notes_dir, ".sync-paths")
    paths = []
    if os.path.isfile(sync_paths_file):
        with open(sync_paths_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    expanded = os.path.expanduser(line)
                    if os.path.isdir(expanded):
                        paths.append(expanded)
    return paths


def is_summary_file(filepath, filename):
    if filename in EXCLUDE_FILES:
        return False
    if not filename.endswith(".md"):
        return False
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            size = len(content)
            if size < 100:
                return False
            first_lines = content.strip().split("\n")[:10]
            for line in first_lines:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return True
                if stripped.startswith("tags:") or stripped.startswith("标签:"):
                    return True
                if stripped.startswith("---") and size > 500:
                    return True
            if size > 500:
                return True
            return False
    except Exception:
        return False


def is_discover_candidate(filepath, filename):
    if filename in EXCLUDE_FILES:
        return False
    if not filename.endswith(".md"):
        return False
    try:
        stat = os.stat(filepath)
        mtime = datetime.fromtimestamp(stat.st_mtime)
        if (datetime.now() - mtime).days > 30:
            return False
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            size = len(content)
            if size < 100:
                return False
            raw_first = content.split("\n", 1)[0]
            first_lines = content.strip().split("\n")[:15]
            reasons = []
            for line in first_lines:
                stripped = line.strip()
                if stripped.startswith("# "):
                    reasons.append("H1 标题")
                    break
            for line in first_lines:
                stripped = line.strip()
                if stripped.startswith("tags:") or stripped.startswith("标签:"):
                    reasons.append("标签行")
                    break
            if raw_first.strip() == "---":
                reasons.append("YAML frontmatter")
            if reasons:
                return reasons
            return False
    except Exception:
        return False


def get_ignored_paths(notes_dir: str):
    ignored = set()
    for key, entry in load_index(notes_dir).items():
        if isinstance(entry, dict) and entry.get("discover_ignored"):
            discover_path = entry.get("discover_path", key)
            if isinstance(discover_path, str):
                ignored.add(discover_path)
    return ignored


def create_link_for(notes_dir: str, filepath, linked_targets, new_links):
    real_path = os.path.realpath(filepath)
    if real_path in linked_targets:
        return
    link_name = os.path.basename(filepath).strip()
    if not link_name:
        return
    link_path = os.path.join(notes_dir, link_name)
    if os.path.exists(link_path):
        base, ext = os.path.splitext(link_name)
        parent = os.path.basename(os.path.dirname(filepath))
        counter = 1
        while os.path.exists(link_path):
            link_name = f"{parent}-{base}-{counter}{ext}"
            link_path = os.path.join(notes_dir, link_name)
            counter += 1
    try:
        rel_path = os.path.relpath(filepath, notes_dir)
        os.symlink(rel_path, link_path)
        new_links.append(link_name)
        linked_targets.add(real_path)
    except OSError as e:
        print(f"软链接创建失败 {link_name}: {e}", file=sys.stderr)


def scan_dir_for_md(notes_dir: str, scan_dir, max_depth, time_limited, linked_targets, ignored, new_links):
    if not os.path.isdir(scan_dir):
        return
    for root, dirs, files in os.walk(scan_dir):
        depth = root.count(os.sep) - scan_dir.count(os.sep)
        if max_depth is not None and depth >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d not in EXCLUDE_DIRS and d not in SKIP_DIRS]
        for filename in files:
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(root, filename)
            if os.path.realpath(filepath) in ignored:
                continue
            if time_limited:
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                except OSError:
                    continue
                if (datetime.now() - mtime).days > 30:
                    continue
                if not is_discover_candidate(filepath, filename):
                    continue
            else:
                if not is_summary_file(filepath, filename):
                    continue
            create_link_for(notes_dir, filepath, linked_targets, new_links)


def agent_working_dirs():
    dirs = set()
    codex_sess = os.path.expanduser("~/.codex/sessions")
    if os.path.isdir(codex_sess):
        for root, _, files in os.walk(codex_sess):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                try:
                    with open(os.path.join(root, fn), "r", encoding="utf-8", errors="replace") as f:
                        head = f.read(4096)
                    m = re.search(r'"cwd"\s*:\s*"([^"]+)"', head)
                    if m:
                        dirs.add(m.group(1))
                except Exception:
                    pass
    db = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if os.path.isfile(db):
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = conn.execute("SELECT DISTINCT directory FROM session").fetchall()
            conn.close()
            for (d,) in rows:
                if d:
                    dirs.add(d)
        except Exception:
            pass
    SYSTEM_DIRS = {"/", "/tmp", "/private", "/private/tmp", "/private/var",
                   "/var", "/etc", "/usr", "/opt", "/Users", "/Library"}
    return {d for d in dirs
            if os.path.isdir(d) and os.path.realpath(d) not in SYSTEM_DIRS}


def sync_links(notes_dir: str):
    new_links = []

    for f in os.listdir(notes_dir):
        full = os.path.join(notes_dir, f)
        if f.endswith(".md") and os.path.islink(full) and not os.path.exists(full):
            os.remove(full)

    linked_targets = set()
    for f in os.listdir(notes_dir):
        full = os.path.join(notes_dir, f)
        if f.endswith(".md"):
            if os.path.islink(full):
                try:
                    resolved = os.path.join(notes_dir, os.readlink(full))
                    linked_targets.add(os.path.realpath(resolved))
                except Exception:
                    pass
            else:
                linked_targets.add(os.path.realpath(full))

    ignored = get_ignored_paths(notes_dir)

    for scan_dir in load_sync_paths(notes_dir):
        scan_dir_for_md(notes_dir, scan_dir, None, False, linked_targets, ignored, new_links)

    return new_links


ASSET_DIRNAME = "._assets_"

IMAGE_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

IMAGE_CHAR_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}

IMAGE_MAX_SIZE = 5 * 1024 * 1024

MAX_PREVIEW_CHARS = 1000000

MAX_SAVE_CHARS = 2000000

MAX_IMAGE_BASE64_CHARS = int(IMAGE_MAX_SIZE * 4 / 3) + 8192

MAX_UPLOAD_BODY_BYTES = 12 * 1024 * 1024

DEFAULT_POST_BODY_BYTES = 1 * 1024 * 1024

POST_BODY_CAPS = {
    "/api/upload": MAX_UPLOAD_BODY_BYTES,
    "/api/preview": MAX_UPLOAD_BODY_BYTES,
    "/api/save": MAX_UPLOAD_BODY_BYTES,
}

ASSET_FILE_MAX_CHARS = 2048


def is_valid_note_name(filename):
    return (
        isinstance(filename, str)
        and bool(filename)
        and "/" not in filename
        and "\\" not in filename
        and ".." not in filename
        and "\0" not in filename
        and len(filename) <= 200
    )


def is_note_file(filename):
    return is_valid_note_name(filename) and filename.lower().endswith(".md")


def is_safe_note_ref(filename):
    return (
        isinstance(filename, str)
        and bool(filename)
        and "/" not in filename
        and "\\" not in filename
        and ".." not in filename
        and "\0" not in filename
    )


def _file_revision(path):
    st = os.stat(path)
    return f"{st.st_mtime_ns}:{st.st_size}"


def _atomic_write_file(path, text):
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp")
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _image_magic_ok(raw, ext):
    if ext == "png":
        return raw[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in ("jpg", "jpeg"):
        return raw[:3] == b"\xff\xd8\xff"
    if ext == "gif":
        return raw[:4] in (b"GIF87a", b"GIF89a")
    if ext == "webp":
        return len(raw) > 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    return False


def _url_has_attr_risk(url):
    if not url or len(url) > 2048:
        return True
    if '"' in url or "<" in url or ">" in url:
        return True
    return any(ord(c) < 32 for c in url)


def _safe_link_href(url):
    u = url.strip()
    if _url_has_attr_risk(u):
        return "#"
    try:
        parsed = urllib.parse.urlparse(u)
    except Exception:
        return "#"
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https", "mailto"):
        return u
    if scheme:
        return "#"
    if u.startswith("//"):
        return "#"
    return u


def _image_web_url(url):
    u = url.strip()
    if _url_has_attr_risk(u):
        return ""
    try:
        parsed = urllib.parse.urlparse(u)
    except Exception:
        return ""
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https"):
        return u
    if scheme:
        return ""
    if u.startswith("/") or u.startswith("//"):
        return ""
    return "/api/asset?file=" + urllib.parse.quote(u, safe="/")


def _is_table_sep(line):
    s = line.strip()
    if not s or "-" not in s:
        return False
    inner = s.strip("|")
    cells = [c.strip() for c in inner.split("|")]
    if not any(cells):
        return False
    return all(re.fullmatch(r":?-+:?", c) for c in cells)


def _split_table_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _inline_html(s):
    s = re.sub(r'`([^`]+)`', lambda m: f"<code>{m.group(1)}</code>", s)
    def img_repl(m):
        web = _image_web_url(m.group(2))
        if not web:
            return m.group(1)
        return f'<img src="{web}" alt="{m.group(1)}">'
    s = re.sub(r'!\[([^\]]*)\]\(((?:\([^()]*\)|[^()])+)\)', img_repl, s)
    s = re.sub(r'\*\*(.+?)\*\*', lambda m: f"<strong>{m.group(1)}</strong>", s)
    s = re.sub(r'\*(.+?)\*', lambda m: f"<em>{m.group(1)}</em>", s)
    s = re.sub(r'\[([^\]]+)\]\(((?:\([^()]*\)|[^()])+)\)',
               lambda m: f'<a href="{_safe_link_href(m.group(2))}">{m.group(1)}</a>', s)
    return s


def simple_markdown(text):
    if not isinstance(text, str):
        return ""
    lines = text.split("\n")
    html = []
    in_code = False
    in_list = False
    list_kind = "ul"
    in_quote = False
    i = 0
    n = len(lines)

    def close_list():
        nonlocal in_list
        if in_list:
            html.append("</" + list_kind + ">")
            in_list = False

    def close_quote():
        nonlocal in_quote
        if in_quote:
            html.append("</blockquote>")
            in_quote = False

    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            close_list()
            close_quote()
            if in_code:
                html.append("</code></pre>")
                in_code = False
            else:
                html.append("<pre><code>")
                in_code = True
            i += 1
            continue
        if in_code:
            html.append(escape_html(stripped))
            i += 1
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if heading:
            close_list()
            close_quote()
            level = len(heading.group(1))
            html.append(f"<h{level}>{escape_html(heading.group(2).strip())}</h{level}>")
            i += 1
            continue
        if (("|" in stripped and i + 1 < n and _is_table_sep(lines[i + 1])
             and not stripped.startswith(("#", ">", "-", "*", "+")))):
            close_list()
            close_quote()
            header_cells = _split_table_row(stripped)
            k = i + 2
            body_rows = []
            while k < n:
                s2 = lines[k].strip()
                if not s2 or "|" not in s2:
                    break
                body_rows.append(_split_table_row(lines[k]))
                k += 1
            out = ["<table><thead><tr>"]
            for cell in header_cells:
                out.append(f"<th>{_inline_html(escape_html(cell))}</th>")
            out.append("</tr></thead>")
            width = len(header_cells)
            if body_rows:
                out.append("<tbody>")
                for row in body_rows:
                    cells = list(row) + [""] * max(0, width - len(row))
                    out.append("<tr>" + "".join(
                        f"<td>{_inline_html(escape_html(c))}</td>" for c in cells[:width]
                    ) + "</tr>")
                out.append("</tbody>")
            out.append("</table>")
            html.extend(out)
            i = k
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            close_quote()
            if not in_list:
                list_kind = "ul"
                html.append("<ul>")
                in_list = True
            content = escape_html(stripped[2:])
            task = re.match(r'^\[([ xX])\]\s*(.*)$', content)
            if task:
                checked = " checked" if task.group(1).lower() == "x" else ""
                checkbox = f'<input type="checkbox" disabled{checked}> '
                html.append(f'<li class="task"><label>{checkbox}{_inline_html(task.group(2))}</label></li>')
            else:
                html.append(f"<li>{_inline_html(content)}</li>")
            i += 1
            continue
        ordered = re.match(r'^\d+\.\s+(.*)$', stripped)
        if ordered:
            close_quote()
            if not in_list:
                list_kind = "ol"
                html.append("<ol>")
                in_list = True
            html.append(f"<li>{_inline_html(escape_html(ordered.group(1)))}</li>")
            i += 1
            continue
        if stripped == ">" or stripped.startswith("> "):
            close_list()
            if not in_quote:
                html.append("<blockquote>")
                in_quote = True
            quote_text = stripped[1:].lstrip()
            html.append(f"<p>{_inline_html(escape_html(quote_text))}</p>")
            i += 1
            continue
        close_list()
        close_quote()
        if not stripped:
            i += 1
            continue
        html.append(f"<p>{_inline_html(escape_html(stripped))}</p>")
        i += 1
    if in_code:
        html.append("</code></pre>")
    close_list()
    close_quote()
    return "\n".join(html)


def escape_html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


def _generate_discover_token(notes_dir: str, real_path: str) -> str:
    token = secrets.token_urlsafe(16)
    with _DISCOVER_TOKEN_LOCK:
        _DISCOVER_TOKEN_CACHE[token] = {
            "notes_dir": os.path.realpath(notes_dir),
            "real_path": os.path.realpath(real_path),
            "expires_at": datetime.now() + _DISCOVER_TOKEN_TTL,
        }
    return token


def _validate_discover_token(token: str, notes_dir: str, require_file: bool = True) -> dict:
    with _DISCOVER_TOKEN_LOCK:
        entry = _DISCOVER_TOKEN_CACHE.get(token)
    if not entry:
        return {}
    if entry.get("notes_dir") != os.path.realpath(notes_dir):
        return {}
    if datetime.now() > entry.get("expires_at", datetime.min):
        with _DISCOVER_TOKEN_LOCK:
            _DISCOVER_TOKEN_CACHE.pop(token, None)
        return {}
    real_path = entry.get("real_path", "")
    if require_file and not os.path.isfile(real_path):
        return {}
    return entry


def _cleanup_discover_tokens():
    now = datetime.now()
    with _DISCOVER_TOKEN_LOCK:
        expired = [k for k, v in _DISCOVER_TOKEN_CACHE.items() if now > v.get("expires_at", datetime.min)]
        for k in expired:
            _DISCOVER_TOKEN_CACHE.pop(k, None)


ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_host(self):
    host_header = self.headers.get("Host", "")
    if not host_header:
        return False

    host = ""
    port = None

    if host_header.startswith("["):
        end = host_header.find("]")
        if end == -1:
            return False
        host = host_header[1:end]
        rest = host_header[end + 1:]
        if rest.startswith(":"):
            port_str = rest[1:]
        elif rest == "":
            port_str = ""
        else:
            return False
    else:
        if ":" in host_header:
            host, _, port_str = host_header.rpartition(":")
        else:
            host = host_header
            port_str = ""

    if port_str and not port_str.isdigit():
        return False

    if port_str:
        port = int(port_str)
        if host in ALLOWED_HOSTS and 1 <= port <= 65535:
            return True
    elif host in ALLOWED_HOSTS:
        return True

    return False


def _get_server_port(self):
    try:
        return self.server.server_address[1]
    except Exception:
        return None


def validate_origin(self):
    origin = self.headers.get("Origin", "")
    if not origin:
        return True

    if origin == "null":
        return False

    if not origin.startswith("http://"):
        return False

    try:
        parsed = urllib.parse.urlparse(origin)
    except Exception:
        return False

    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    host = parsed.hostname or ""
    if host not in allowed_hosts:
        return False

    server_port = _get_server_port(self)
    if server_port is None:
        return True

    origin_port = parsed.port
    if origin_port is None:
        if parsed.scheme == "http":
            origin_port = 80
        else:
            return False

    if origin_port != server_port:
        return False

    return True


class Handler(http.server.SimpleHTTPRequestHandler):
    def send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def do_GET(self):
        if not validate_host(self):
            self.send_error(403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self.send_index()
        elif path == "/api/list":
            self.send_list()
        elif path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            term = qs.get("q", [""])[0]
            self.send_search(term)
        elif path == "/api/read":
            qs = urllib.parse.parse_qs(parsed.query)
            filename = qs.get("file", [""])[0]
            self.send_read(filename)
        elif path == "/api/read/html":
            qs = urllib.parse.parse_qs(parsed.query)
            filename = qs.get("file", [""])[0]
            self.send_read_html(filename)
        elif path == "/api/sync":
            self.send_sync()
        elif path == "/api/views":
            self.send_views()
        elif path == "/api/discover":
            self.send_discover()
        elif path == "/api/detail":
            qs = urllib.parse.parse_qs(parsed.query)
            filename = qs.get("file", [""])[0]
            self.send_detail(filename)
        elif path == "/api/groups":
            self.send_groups()
        elif path == "/api/edit":
            qs = urllib.parse.parse_qs(parsed.query)
            self.send_edit(qs.get("file", [""])[0])
        elif path == "/api/asset":
            qs = urllib.parse.parse_qs(parsed.query)
            self.send_asset(qs.get("file", [""])[0])
        else:
            self.send_error(404)

    def _read_json_body(self, limit):
        val = self.headers.get("Content-Length")
        if val is None:
            length = 0
        else:
            token = val.strip()
            if not re.fullmatch(r"[0-9]{1,10}", token):
                self.close_connection = True
                self.send_error(400)
                return None
            length = int(token)
            if length > limit:
                self.close_connection = True
                self.send_error(400)
                return None
        if length == 0:
            body = b""
        else:
            body = self.rfile.read(length)
            self.close_connection = True
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self.send_error(400)
            return None
        if not isinstance(data, dict):
            self.send_error(400)
            return None
        return data

    def do_POST(self):
        if not validate_host(self):
            self.send_error(403)
            return
        if not validate_origin(self):
            self.send_error(403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        data = self._read_json_body(POST_BODY_CAPS.get(path, DEFAULT_POST_BODY_BYTES))
        if data is None:
            return
        if path == "/api/tags":
            self.send_tags(data)
        elif path == "/api/star":
            self.send_star(data)
        elif path == "/api/delete":
            self.send_delete(data)
        elif path == "/api/rename":
            self.send_rename(data)
        elif path == "/api/views/save":
            self.send_save_view(data)
        elif path == "/api/discover/ignore":
            self.send_discover_ignore(data)
        elif path == "/api/discover/add":
            self.send_discover_add(data)
        elif path == "/api/reveal":
            self.send_reveal(data)
        elif path == "/api/open":
            self.send_open(data)
        elif path == "/api/group/create":
            self.send_group_create(data)
        elif path == "/api/group/add":
            self.send_group_add(data)
        elif path == "/api/group/remove":
            self.send_group_remove(data)
        elif path == "/api/group/rename":
            self.send_group_rename(data)
        elif path == "/api/group/disband":
            self.send_group_disband(data)
        elif path == "/api/group/toggle":
            self.send_group_toggle(data)
        elif path == "/api/preview":
            self.send_preview(data)
        elif path == "/api/save":
            self.send_save(data)
        elif path == "/api/upload":
            self.send_upload(data)
        else:
            self.send_error(404)

    def send_index(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))

    def send_json(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _get_index_entry(self, notes_dir: str, filename):
        index = load_index(notes_dir)
        entry = index.get(filename, {})
        return entry if isinstance(entry, dict) else {}

    def _parse_note(self, notes_dir: str, filename, path, full_content=False):
        try:
            stat = os.stat(path)
            mtime = datetime.fromtimestamp(stat.st_mtime)
            date = mtime.strftime("%Y-%m-%d")
            time_str = mtime.strftime("%H:%M")
            title = filename.replace(".md", "")
            tags = []
            excerpt = ""
            content = ""

            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()

            for i, line in enumerate(lines):
                line = line.strip()
                if line.startswith("# ") and title == filename.replace(".md", ""):
                    title = line[2:].strip()
                elif line.startswith("标签:") or line.startswith("tags:"):
                    tag_str = line.split(":", 1)[1]
                    tags = [t.strip() for t in re.split(r"[,，]", tag_str) if t.strip()]
                    continue
                if line and not line.startswith("---") and not line.startswith("#") and not excerpt:
                    excerpt = line[:160]

            if full_content:
                content = "".join(lines)

            idx = self._get_index_entry(notes_dir, filename)
            raw_tags = idx.get("tags")
            if isinstance(raw_tags, list):
                tags = [t for t in raw_tags if isinstance(t, str)]
            else:
                tags = []
            starred = idx.get("starred") is True
            note = idx.get("note", "")
            if not isinstance(note, str):
                note = ""

            is_symlink = os.path.islink(path)
            source = ""
            source_label = ""
            if is_symlink:
                try:
                    target = os.readlink(path)
                    source = os.path.normpath(os.path.join(notes_dir, target))
                    source_label = os.path.basename(os.path.dirname(source))
                    if not source_label or source_label == notes_dir:
                        source_label = "notes"
                except Exception:
                    source_label = ""

            result = {
                "file": filename,
                "title": title,
                "date": date,
                "time": time_str,
                "tags": tags,
                "excerpt": excerpt,
                "content": content,
                "is_symlink": is_symlink,
                "starred": starred,
                "note": note,
            }
            if source_label:
                result["source_label"] = source_label
            return result
        except Exception:
            return None

    def send_list(self):
        notes_dir = get_notes_dir()
        notes = []
        index = load_index(notes_dir)
        for f in os.listdir(notes_dir):
            if not f.endswith(".md") or f in EXCLUDE_FILES:
                continue
            path = os.path.join(notes_dir, f)
            if not os.path.isfile(path):
                continue
            info = self._parse_note(notes_dir, f, path)
            if info:
                info["group"] = _find_group_for_file(index, f)
                notes.append(info)
        notes.sort(key=lambda x: x["date"], reverse=True)
        self.send_json(notes)

    def send_search(self, term):
        notes_dir = get_notes_dir()
        term_lower = term.lower()
        results = []
        index = load_index(notes_dir)
        for f in os.listdir(notes_dir):
            if not f.endswith(".md") or f in EXCLUDE_FILES:
                continue
            path = os.path.join(notes_dir, f)
            if not os.path.isfile(path):
                continue
            info = self._parse_note(notes_dir, f, path, full_content=True)
            if not info:
                continue
            haystack = f"{info['file']} {info['title']} {' '.join(info['tags'])} {info['excerpt']} {info['content']}".lower()
            if term_lower in haystack:
                info["_highlight"] = True
                info["group"] = _find_group_for_file(index, f)
                results.append(info)
        results.sort(key=lambda x: x["date"], reverse=True)
        self.send_json(results)

    def send_read(self, filename):
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def send_read_html(self, filename):
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        html = simple_markdown(content)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def send_edit(self, filename):
        if not is_note_file(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            self.send_error(404)
            return
        is_symlink = os.path.islink(path)
        source_label = ""
        if is_symlink:
            try:
                real = os.path.realpath(path)
                source_label = os.path.basename(os.path.dirname(real)) or "notes"
            except Exception:
                source_label = "notes"
        self.send_json({
            "file": filename,
            "content": content,
            "revision": _file_revision(path),
            "is_symlink": is_symlink,
            "source_label": source_label,
        })

    def send_preview(self, data):
        content = data.get("content")
        if not isinstance(content, str):
            self.send_error(400)
            return
        if len(content) > MAX_PREVIEW_CHARS:
            self.send_error(400)
            return
        self.send_json({"ok": True, "html": simple_markdown(content)})

    def send_save(self, data):
        filename = data.get("file")
        content = data.get("content")
        revision = data.get("revision")
        if not isinstance(filename, str) or not isinstance(content, str) or not isinstance(revision, str):
            self.send_error(400)
            return
        if not is_note_file(filename):
            self.send_error(400)
            return
        if len(content) > MAX_SAVE_CHARS:
            self.send_json({"ok": False, "error": "内容过长"})
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        is_symlink = os.path.islink(path)
        current_revision = _file_revision(path)
        if revision != current_revision:
            self.send_json({"ok": False, "conflict": True, "revision": current_revision})
            return
        propagate = data.get("propagate") is True
        write_path = path
        if is_symlink:
            if not propagate:
                self.send_json({"ok": False, "symlink": True, "requires_propagate": True})
                return
            write_path = os.path.realpath(path)
            if not os.path.isfile(write_path):
                self.send_error(404)
                return
            linked = []
            for f in os.listdir(notes_dir):
                p = os.path.join(notes_dir, f)
                if f != filename and f.endswith(".md") and os.path.islink(p):
                    try:
                        linked.append(os.path.realpath(p))
                    except Exception:
                        pass
            if write_path in linked:
                self.send_json({"ok": False, "symlink": True, "error": "有多个链接指向同一源文件，无法安全写入"})
                return
        try:
            _atomic_write_file(write_path, content)
        except Exception as e:
            self.send_json({"ok": False, "error": f"保存失败: {e}"})
            return
        try:
            new_revision = _file_revision(write_path)
        except Exception:
            new_revision = revision
        self.send_json({"ok": True, "revision": new_revision, "propagated": is_symlink and propagate})

    def send_upload(self, data):
        filename = data.get("file")
        name = data.get("filename")
        mime = data.get("mime")
        payload = data.get("data")
        if not is_note_file(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        if not isinstance(payload, str) or not payload:
            self.send_json({"ok": False, "error": "data 必须为 base64 字符串"})
            return
        if not isinstance(mime, str) or not mime:
            self.send_json({"ok": False, "error": "mime 必须为字符串"})
            return
        mime_norm = mime.strip().lower()
        target_ext = None
        for m, ext in IMAGE_MIME_EXT.items():
            if mime_norm == m or (m == "image/jpeg" and mime_norm in ("image/jpg", "jpg", "jpeg")):
                target_ext = ext
        if not target_ext:
            self.send_json({"ok": False, "error": "不支持的图片类型"})
            return
        if isinstance(name, str) and name:
            dotted = os.path.splitext(name)[1].lstrip(".").lower()
            if dotted and dotted not in IMAGE_CHAR_EXTS:
                self.send_json({"ok": False, "error": "文件扩展名不符合图片类型"})
                return
        if len(payload) > MAX_IMAGE_BASE64_CHARS:
            self.send_json({"ok": False, "error": "图片数据超限"})
            return
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            self.send_json({"ok": False, "error": "图片数据无效"})
            return
        if not raw:
            self.send_json({"ok": False, "error": "图片为空"})
            return
        if len(raw) > IMAGE_MAX_SIZE:
            self.send_json({"ok": False, "error": "图片大小超限"})
            return
        if not _image_magic_ok(raw, target_ext):
            self.send_json({"ok": False, "error": "文件内容与类型不符"})
            return
        stem = filename[: -len(".md")] if filename.lower().endswith(".md") else filename
        asset_dir = os.path.join(notes_dir, ASSET_DIRNAME, stem)
        new_name = f"{secrets.token_hex(10)}.{target_ext}"
        target_path = os.path.join(asset_dir, new_name)
        tmp_path = os.path.join(asset_dir, f".{new_name}.{secrets.token_hex(4)}.tmp")
        try:
            os.makedirs(asset_dir, exist_ok=True)
            with open(tmp_path, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, target_path)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            self.send_json({"ok": False, "error": f"保存图片失败: {e}"})
            return
        url = f"{stem}/{new_name}"
        self.send_json({"ok": True, "url": url, "name": new_name})

    ASSET_EXT_MIME = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }

    def send_asset(self, name):
        if not isinstance(name, str):
            self.send_error(400)
            return
        name = name.strip()
        if not name or len(name) > ASSET_FILE_MAX_CHARS or "\0" in name:
            self.send_error(400)
            return
        if name.startswith("/") or any(part in ("", ".", "..") for part in name.replace("\\", "/").split("/")):
            self.send_error(400)
            return
        base_dir = os.path.realpath(os.path.join(get_notes_dir(), ASSET_DIRNAME))
        target = os.path.realpath(os.path.join(base_dir, name))
        if not target.startswith(base_dir + os.sep):
            self.send_error(400)
            return
        ext = os.path.splitext(target)[1].lstrip(".").lower()
        content_type = self.ASSET_EXT_MIME.get(ext)
        if not content_type:
            self.send_error(404)
            return
        if not os.path.isfile(target):
            self.send_error(404)
            return
        with open(target, "rb") as f:
            payload = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def send_sync(self):
        notes_dir = get_notes_dir()
        new_links = sync_links(notes_dir)
        self.send_json({"new_links": new_links, "count": len(new_links)})

    def send_discover(self):
        notes_dir = get_notes_dir()
        ignored = set()
        index = load_index(notes_dir)
        for key, entry in index.items():
            if isinstance(entry, dict) and entry.get("discover_ignored"):
                discover_path = entry.get("discover_path", key)
                if isinstance(discover_path, str):
                    ignored.add(discover_path)

        _cleanup_discover_tokens()

        candidates = []
        scan_roots = [(d, 3) for d in load_sync_paths(notes_dir) if os.path.isdir(d)]

        agent_dirs = agent_working_dirs()
        for d in agent_dirs:
            if os.path.isdir(d) and os.path.realpath(d) not in ignored:
                scan_roots.append((d, 1))

        for scan_dir, max_depth in scan_roots:
            if not os.path.isdir(scan_dir):
                continue
            for root, dirs, files in os.walk(scan_dir):
                depth = root.count(os.sep) - scan_dir.count(os.sep)
                if max_depth is not None and depth >= max_depth:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in EXCLUDE_DIRS and d not in SKIP_DIRS]
                for filename in files:
                    if not filename.endswith(".md"):
                        continue
                    filepath = os.path.join(root, filename)
                    real_path = os.path.realpath(filepath)
                    if real_path in ignored:
                        continue
                    link_name = os.path.basename(filepath)
                    existing_link = os.path.join(notes_dir, link_name)
                    if os.path.exists(existing_link):
                        continue
                    try:
                        reasons = is_discover_candidate(filepath, filename)
                        if reasons:
                            if (scan_dir, max_depth) in [(d, 1) for d in agent_dirs]:
                                reasons = list(reasons)
                                reasons.append("来自 agent 会话目录")
                            token = _generate_discover_token(notes_dir, real_path)
                            candidates.append({
                                "candidate_id": token,
                                "filename": filename,
                                "source_label": os.path.basename(os.path.dirname(filepath)),
                                "size": os.path.getsize(filepath),
                                "modified": datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d %H:%M"),
                                "reasons": reasons,
                            })
                    except Exception:
                        pass
        candidates.sort(key=lambda x: x["modified"], reverse=True)
        seen = set()
        unique = []
        for c in candidates:
            if c["candidate_id"] not in seen:
                seen.add(c["candidate_id"])
                unique.append(c)
        self.send_json(unique[:50])

    def send_discover_ignore(self, data):
        candidate_id = data.get("candidate_id", "")
        if not isinstance(candidate_id, str) or not candidate_id:
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        entry = _validate_discover_token(candidate_id, notes_dir, require_file=False)
        if not entry:
            self.send_error(404)
            return
        index = load_index(notes_dir)
        key = f"_ignore_{candidate_id}"
        index[key] = {
            "discover_ignored": True,
            "discover_path": entry["real_path"],
            "updated_at": datetime.now().isoformat(),
        }
        save_index(notes_dir, index)
        self.send_json({"ok": True})

    def send_discover_add(self, data):
        candidate_id = data.get("candidate_id", "")
        if not isinstance(candidate_id, str) or not candidate_id:
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        entry = _validate_discover_token(candidate_id, notes_dir, require_file=True)
        if not entry:
            self.send_error(404)
            return
        real_path = entry["real_path"]
        filename = os.path.basename(real_path).strip()
        link_path = os.path.join(notes_dir, filename)
        if os.path.exists(link_path):
            base, ext = os.path.splitext(filename)
            parent = os.path.basename(os.path.dirname(real_path))
            counter = 1
            while os.path.exists(link_path):
                link_name = f"{parent}-{base}-{counter}{ext}"
                link_path = os.path.join(notes_dir, link_name)
                counter += 1
        rel_path = os.path.relpath(real_path, notes_dir)
        try:
            os.symlink(rel_path, link_path)
            self.send_json({"ok": True, "link": os.path.basename(link_path)})
        except OSError as e:
            self.send_error(500)

    def send_views(self):
        notes_dir = get_notes_dir()
        index = load_index(notes_dir)
        views = {}
        for _, entry in index.items():
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if entry.get("view") and isinstance(name, str) and name:
                filters = entry.get("filters", {})
                if not isinstance(filters, dict):
                    filters = {}
                views[name] = {
                    "name": name,
                    "filters": filters,
                    "updated_at": entry.get("updated_at", "") if isinstance(entry.get("updated_at"), str) else "",
                }
        self.send_json(views)

    def send_detail(self, filename):
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path) and not os.path.islink(path):
            self.send_error(404)
            return
        is_symlink = os.path.islink(path)
        kind = "symlink" if is_symlink else "local"
        source_label = ""
        real_path = ""
        exists = True
        if is_symlink:
            try:
                real_path = os.path.realpath(path)
                source_label = os.path.basename(os.path.dirname(real_path))
                if not source_label or source_label == notes_dir:
                    source_label = "notes"
                exists = os.path.isfile(real_path)
            except Exception:
                exists = False
        else:
            real_path = os.path.abspath(path)
            source_label = "notes"
        self.send_json({
            "file": filename,
            "kind": kind,
            "path": real_path,
            "source_label": source_label,
            "exists": exists,
        })

    def send_reveal(self, data):
        filename = data.get("file", "")
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        real_path = os.path.realpath(path)
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", real_path], check=False)
            else:
                subprocess.run(["xdg-open", os.path.dirname(real_path)], check=False)
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def send_open(self, data):
        filename = data.get("file", "")
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        real_path = os.path.realpath(path)
        try:
            editor = os.environ.get("NOTED_EDITOR")
            if editor:
                subprocess.Popen(shlex.split(editor) + [real_path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", real_path])
            else:
                subprocess.Popen(["xdg-open", real_path])
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def send_groups(self):
        notes_dir = get_notes_dir()
        index = load_index(notes_dir)
        groups = _get_groups(index)
        result = []
        for key, group in groups.items():
            safe_files = []
            for f in group.get("files", []):
                if not isinstance(f, str):
                    continue
                f = f.strip()
                if not f or "/" in f or "\\" in f or ".." in f:
                    continue
                full = os.path.join(notes_dir, f)
                if os.path.isfile(full) or os.path.islink(full):
                    safe_files.append(f)
            result.append({
                "name": group.get("name", ""),
                "files": safe_files,
                "collapsed": group.get("collapsed", False),
                "count": len(safe_files),
            })
        result.sort(key=lambda x: x["name"])
        self.send_json(result)

    def send_group_create(self, data):
        name = data.get("name", "")
        if not isinstance(name, str):
            self.send_json({"ok": False, "error": "组名必须为字符串"})
            return
        err = _validate_group_name(name)
        if err:
            self.send_json({"ok": False, "error": err})
            return
        stripped = name.strip()
        notes_dir = get_notes_dir()
        index = load_index(notes_dir)
        groups = _get_groups(index)
        for key, group in groups.items():
            if group.get("name") == stripped:
                self.send_json({"ok": False, "error": "组名已存在"})
                return
        key = f"_group_{stripped}"
        index[key] = {
            "name": stripped,
            "files": [],
            "collapsed": False,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        save_index(notes_dir, index)
        self.send_json({"ok": True, "group": stripped})

    def send_group_add(self, data):
        group_name = data.get("group", "")
        if not isinstance(group_name, str):
            self.send_json({"ok": False, "error": "组名必须为字符串"})
            return
        filename = data.get("file", "")
        if not isinstance(filename, str):
            self.send_json({"ok": False, "error": "文件名必须为字符串"})
            return
        err = _validate_group_name(group_name)
        if err:
            self.send_json({"ok": False, "error": err})
            return
        group_name = group_name.strip()
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            self.send_json({"ok": False, "error": "文件名不合法"})
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path) and not os.path.islink(path):
            self.send_json({"ok": False, "error": "文件不存在"})
            return
        index = load_index(notes_dir)
        groups = _get_groups(index)
        target_key = None
        for key, group in groups.items():
            if group.get("name") == group_name:
                target_key = key
                break
        if not target_key:
            self.send_json({"ok": False, "error": "组不存在"})
            return
        for key, entry in index.items():
            if isinstance(key, str) and key.startswith("_group_") and isinstance(entry, dict):
                files = _group_files(entry)
                if filename in files:
                    files.remove(filename)
                    entry["files"] = files
                    entry["updated_at"] = datetime.now().isoformat()
        entry = index[target_key]
        if not isinstance(entry, dict):
            self.send_json({"ok": False, "error": "组不存在"})
            return
        files = _group_files(entry)
        if filename not in files:
            files.append(filename)
        entry["files"] = files
        entry["updated_at"] = datetime.now().isoformat()
        save_index(notes_dir, index)
        self.send_json({"ok": True, "group": group_name})

    def send_group_remove(self, data):
        if "group" in data:
            group_name = data["group"]
            if not isinstance(group_name, str):
                self.send_json({"ok": False, "error": "组名必须为字符串"})
                return
        else:
            group_name = ""
        filename = data.get("file", "")
        if not isinstance(filename, str):
            self.send_json({"ok": False, "error": "文件名必须为字符串"})
            return
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            self.send_json({"ok": False, "error": "文件名不合法"})
            return
        notes_dir = get_notes_dir()
        index = load_index(notes_dir)
        groups = _get_groups(index)
        if group_name:
            err = _validate_group_name(group_name)
            if err:
                self.send_json({"ok": False, "error": err})
                return
            group_name = group_name.strip()
            target_key = None
            for key, group in groups.items():
                if group.get("name") == group_name:
                    target_key = key
                    break
            if not target_key:
                self.send_json({"ok": False, "error": "组不存在"})
                return
        else:
            target_key = None
            for key, group in groups.items():
                if filename in group.get("files", []):
                    target_key = key
                    break
            if not target_key:
                self.send_json({"ok": False, "error": "文件不在任何分组中"})
                return
        files = _group_files(index[target_key])
        if filename not in files:
            self.send_json({"ok": False, "error": "文件不在该组内"})
            return
        files.remove(filename)
        index[target_key]["files"] = files
        index[target_key]["updated_at"] = datetime.now().isoformat()
        save_index(notes_dir, index)
        self.send_json({"ok": True})

    def send_group_rename(self, data):
        old_name = data.get("old_name", "")
        new_name = data.get("new_name", "")
        if not isinstance(old_name, str):
            self.send_json({"ok": False, "error": "原组名必须为字符串"})
            return
        if not isinstance(new_name, str):
            self.send_json({"ok": False, "error": "新组名必须为字符串"})
            return
        err = _validate_group_name(new_name)
        if err:
            self.send_json({"ok": False, "error": err})
            return
        if not old_name:
            self.send_json({"ok": False, "error": "原组名不能为空"})
            return
        notes_dir = get_notes_dir()
        stripped_old = old_name.strip()
        if not stripped_old:
            self.send_json({"ok": False, "error": "原组名不能为空"})
            return
        stripped_new = new_name.strip()
        index = load_index(notes_dir)
        old_key = None
        new_key = f"_group_{stripped_new}"
        for key, entry in index.items():
            if not isinstance(key, str) or not key.startswith("_group_"):
                continue
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", key[len("_group_"):])
            if isinstance(name, str) and name == stripped_old:
                old_key = key
                break
        if not old_key:
            self.send_json({"ok": False, "error": "组不存在"})
            return
        if new_key in index:
            self.send_json({"ok": False, "error": "组名已存在"})
            return
        normalized = _safe_group_entry(index[old_key], old_key)
        if normalized:
            normalized["name"] = stripped_new
            normalized["updated_at"] = datetime.now().isoformat()
            index.pop(old_key)
            index[new_key] = normalized
        else:
            self.send_json({"ok": False, "error": "组不存在"})
            return
        save_index(notes_dir, index)
        self.send_json({"ok": True, "name": stripped_new})

    def send_group_disband(self, data):
        name = data.get("name", "")
        if not isinstance(name, str):
            self.send_json({"ok": False, "error": "组名必须为字符串"})
            return
        err = _validate_group_name(name)
        if err:
            self.send_json({"ok": False, "error": err})
            return
        name = name.strip()
        notes_dir = get_notes_dir()
        index = load_index(notes_dir)
        key = None
        for k, entry in index.items():
            if not k.startswith("_group_") or not isinstance(entry, dict):
                continue
            if entry.get("name") == name:
                key = k
                break
        if not key:
            self.send_json({"ok": False, "error": "组不存在"})
            return
        del index[key]
        save_index(notes_dir, index)
        self.send_json({"ok": True})

    def send_group_toggle(self, data):
        name = data.get("name", "")
        collapsed = data.get("collapsed")
        if not isinstance(name, str):
            self.send_json({"ok": False, "error": "组名必须为字符串"})
            return
        if collapsed is not None and not isinstance(collapsed, bool):
            self.send_json({"ok": False, "error": "collapsed 必须为布尔值"})
            return
        err = _validate_group_name(name)
        if err:
            self.send_json({"ok": False, "error": err})
            return
        name = name.strip()
        notes_dir = get_notes_dir()
        index = load_index(notes_dir)
        key = None
        for k, entry in index.items():
            if not k.startswith("_group_") or not isinstance(entry, dict):
                continue
            if entry.get("name") == name:
                key = k
                break
        if not key:
            self.send_json({"ok": False, "error": "组不存在"})
            return
        if collapsed is None:
            index[key]["collapsed"] = not index[key].get("collapsed", False)
        else:
            index[key]["collapsed"] = collapsed
        index[key]["updated_at"] = datetime.now().isoformat()
        save_index(notes_dir, index)
        self.send_json({"ok": True, "collapsed": index[key]["collapsed"]})

    def send_tags(self, data):
        filename = data.get("file", "")
        tags = data.get("tags", [])
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        safe_tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        index = load_index(notes_dir)
        if filename not in index:
            index[filename] = {}
        index[filename]["tags"] = safe_tags
        index[filename]["updated_at"] = datetime.now().isoformat()
        save_index(notes_dir, index)
        self.send_json({"ok": True})

    def send_star(self, data):
        filename = data.get("file", "")
        starred = data.get("starred", False)
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        index = load_index(notes_dir)
        if filename not in index:
            index[filename] = {}
        index[filename]["starred"] = starred is True
        index[filename]["updated_at"] = datetime.now().isoformat()
        save_index(notes_dir, index)
        self.send_json({"ok": True})

    def send_delete(self, data):
        filename = data.get("file", "")
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        trash_dir = os.path.join(notes_dir, ".trash")
        path = os.path.join(notes_dir, filename)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        os.makedirs(trash_dir, exist_ok=True)
        trash_path = os.path.join(trash_dir, filename)
        if os.path.exists(trash_path):
            base, ext = os.path.splitext(filename)
            trash_path = os.path.join(trash_dir, f"{base}-{datetime.now().strftime('%H%M%S')}{ext}")
        if os.path.islink(path):
            real = os.path.realpath(path)
            index = load_index(notes_dir)
            index[f"_ignore_{real}"] = {
                "discover_ignored": True,
                "discover_path": real,
                "updated_at": datetime.now().isoformat(),
            }
            _remove_file_from_all_groups(index, filename)
            save_index(notes_dir, index)
            os.remove(path)
        else:
            index = load_index(notes_dir)
            _remove_file_from_all_groups(index, filename)
            save_index(notes_dir, index)
            os.rename(path, trash_path)
        self.send_json({"ok": True, "trash": os.path.basename(trash_path)})

    def send_rename(self, data):
        filename = data.get("file", "")
        new_name = data.get("new_name", "")
        update_h1 = bool(data.get("update_h1", False))
        propagate = bool(data.get("propagate", False))

        if not isinstance(filename, str):
            self.send_error(400)
            return
        if not is_safe_note_ref(filename):
            self.send_error(400)
            return
        if not isinstance(new_name, str) or not new_name:
            self.send_error(400)
            return

        notes_dir = get_notes_dir()
        old_path = os.path.join(notes_dir, filename)
        if not os.path.isfile(old_path):
            self.send_error(404)
            return

        base, ext = os.path.splitext(new_name.strip())
        if not base:
            self.send_error(400)
            return
        if len(new_name.strip()) > 200:
            self.send_error(400)
            return
        if any(c in new_name for c in "/\\\0"):
            self.send_error(400)
            return
        if ".." in new_name:
            self.send_error(400)
            return
        if base.startswith("."):
            self.send_json({"ok": False, "error": "不能以点开头的隐藏文件名"})
            return
        if ext.lower() != ".md":
            new_name = f"{base}.md"
        else:
            new_name = f"{base}{ext}"

        if new_name in EXCLUDE_FILES:
            self.send_json({"ok": False, "error": "该文件名被保留，请更换"})
            return
        if new_name == filename:
            self.send_json({"ok": False, "error": "新文件名与原名相同"})
            return

        new_path = os.path.join(notes_dir, new_name)
        if os.path.exists(new_path):
            self.send_json({"ok": False, "error": f"已存在同名文件: {new_name}"})
            return

        is_symlink = os.path.islink(old_path)
        propagated = False
        h1_updated = False

        if propagate and not is_symlink:
            self.send_json({"ok": False, "error": "仅软链接支持同步重命名源文件"})
            return

        if propagate and is_symlink:
            real_path = os.path.realpath(old_path)
            if not os.path.isfile(real_path):
                self.send_json({"ok": False, "error": "源文件不存在"})
                return
            real_dir = os.path.dirname(real_path)
            new_target_name = f"{base}{ext}"
            new_target_path = os.path.join(real_dir, new_target_name)
            if os.path.exists(new_target_path):
                self.send_json({"ok": False, "error": "源文件目标名已存在"})
                return
            linked_targets = []
            for f in os.listdir(notes_dir):
                if f.endswith(".md") and f != filename and os.path.islink(os.path.join(notes_dir, f)):
                    try:
                        linked_targets.append(os.path.realpath(os.path.join(notes_dir, f)))
                    except Exception:
                        pass
            if real_path in linked_targets:
                self.send_json({"ok": False, "error": "有其他链接指向同一源文件，请先解除"})
                return

            try:
                os.rename(real_path, new_target_path)
                os.remove(old_path)
                if os.path.realpath(new_target_path) != os.path.realpath(new_path):
                    rel = os.path.relpath(new_target_path, os.path.realpath(notes_dir))
                    os.symlink(rel, new_path)
                propagated = True
            except Exception as e:
                try:
                    if os.path.islink(new_path):
                        os.remove(new_path)
                except Exception:
                    pass
                try:
                    if not os.path.exists(real_path) and os.path.exists(new_target_path):
                        os.rename(new_target_path, real_path)
                except Exception:
                    pass
                self.send_json({"ok": False, "error": f"同步重命名失败: {e}"})
                return

        if not propagated:
            try:
                os.rename(old_path, new_path)
            except Exception as e:
                self.send_json({"ok": False, "error": f"重命名失败: {e}"})
                return

        index = load_index(notes_dir)
        if filename in index:
            index[new_name] = index.pop(filename)
        note_key_old = f"_note_{filename}"
        note_key_new = f"_note_{new_name}"
        if note_key_old in index:
            index[note_key_new] = index.pop(note_key_old)
        for key, entry in index.items():
            if key.startswith("_group_") and isinstance(entry, dict):
                files = entry.get("files", [])
                if filename in files:
                    files.remove(filename)
                    files.append(new_name)
                    entry["files"] = files
                    entry["updated_at"] = datetime.now().isoformat()
        save_index(notes_dir, index)

        if update_h1:
            target_path = new_path
            if propagated:
                target_path = new_target_path
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for i, line in enumerate(lines[:50]):
                    if line.strip().startswith("# "):
                        lines[i] = f"# {base}\n"
                        with open(target_path, "w", encoding="utf-8") as f:
                            f.writelines(lines)
                        h1_updated = True
                        break
            except Exception:
                pass

        self.send_json({"ok": True, "file": new_name, "h1_updated": h1_updated, "propagated": propagated})

    def send_save_view(self, data):
        name = data.get("name", "")
        filters = data.get("filters", {})
        if not isinstance(name, str):
            self.send_error(400)
            return
        if not name or len(name) > 64 or any(c in name for c in "\\/:\0"):
            self.send_error(400)
            return
        if not isinstance(filters, dict):
            self.send_error(400)
            return
        notes_dir = get_notes_dir()
        index = load_index(notes_dir)
        view_key = f"_view_{name}"
        index[view_key] = {
            "name": name,
            "view": True,
            "filters": filters,
            "updated_at": datetime.now().isoformat(),
        }
        save_index(notes_dir, index)
        self.send_json({"ok": True})

    def log_message(self, format, *args):
        pass


HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>落笔 · Noted</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: grid;
    grid-template-columns: 260px 1fr;
    min-height: 100vh;
    background: #f5f5f7;
    color: #1d1d1f;
  }
  .sidebar {
    background: #fff;
    border-right: 1px solid #e5e5e5;
    padding: 24px 20px;
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
  }
  .sidebar-title {
    font-size: 20px;
    font-weight: 700;
    margin-bottom: 4px;
  }
  .sidebar-subtitle {
    font-size: 12px;
    color: #86868b;
    margin-bottom: 20px;
  }
  .sidebar-section { margin-bottom: 24px; }
  .sidebar-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #86868b;
    margin-bottom: 8px;
  }
  .tag-cloud { display: flex; flex-wrap: wrap; gap: 6px; }
  .tag {
    display: inline-flex;
    align-items: center;
    padding: 3px 10px;
    background: #f5f5f7;
    color: #1d1d1f;
    border-radius: 12px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
    user-select: none;
  }
  .tag:hover { background: #e8e8ed; }
  .tag.active { background: #007aff; color: #fff; }
  .tag-count {
    font-size: 10px;
    color: #86868b;
    margin-left: 4px;
  }
  .tag.active .tag-count { color: rgba(255,255,255,0.8); }
  .sync-btn {
    width: 100%;
    padding: 8px;
    background: #007aff;
    color: #fff;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    cursor: pointer;
    margin-bottom: 6px;
  }
  .sync-btn:hover { background: #0062cc; }
  .sync-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .sync-status {
    font-size: 11px;
    color: #86868b;
    text-align: center;
  }
  .main {
    padding: 24px 32px;
    max-width: 900px;
    width: 100%;
  }
  .toolbar {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
    flex-wrap: wrap;
    align-items: center;
  }
  .search-bar {
    flex: 1;
    min-width: 200px;
    padding: 10px 14px;
    font-size: 15px;
    border: 1px solid #d2d2d7;
    border-radius: 10px;
    outline: none;
    background: #fff;
    transition: border-color 0.2s;
  }
  .search-bar:focus { border-color: #007aff; }
  .search-bar::placeholder { color: #86868b; }
  .btn {
    padding: 8px 14px;
    border: 1px solid #d2d2d7;
    background: #fff;
    border-radius: 8px;
    font-size: 13px;
    cursor: pointer;
    color: #1d1d1f;
  }
  .btn:hover { background: #f5f5f7; }
  .btn-primary {
    background: #007aff;
    color: #fff;
    border-color: #007aff;
  }
  .btn-primary:hover { background: #0062cc; }
  .btn-danger {
    background: #ff3b30;
    color: #fff;
    border-color: #ff3b30;
  }
  .btn-danger:hover { background: #d63328; }
  .note-count {
    font-size: 13px;
    color: #86868b;
    margin-bottom: 12px;
  }
  .note {
    background: #fff;
    padding: 16px 18px;
    border-radius: 12px;
    border: 1px solid #e5e5e5;
    margin-bottom: 12px;
    cursor: pointer;
    transition: all 0.15s;
    position: relative;
  }
  .note:hover {
    border-color: #007aff;
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(0,0,0,0.06);
  }
  .note.selected {
    border-color: #007aff;
    background: #f0f8ff;
  }
  .note-title {
    font-size: 17px;
    font-weight: 600;
    margin-bottom: 6px;
    color: #1d1d1f;
    padding-right: 40px;
  }
  .note-star {
    position: absolute;
    top: 14px;
    right: 14px;
    font-size: 18px;
    cursor: pointer;
    opacity: 0.4;
    transition: opacity 0.15s;
    user-select: none;
    background: none;
    border: none;
    padding: 4px;
  }
  .note-star:hover { opacity: 0.8; }
  .note-star.starred { opacity: 1; }
  .note-meta {
    font-size: 12px;
    color: #86868b;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .note-source {
    font-size: 11px;
    color: #007aff;
    background: #eef3ff;
    padding: 1px 6px;
    border-radius: 4px;
    font-family: "SF Mono", monospace;
    max-width: 400px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .note-excerpt {
    font-size: 14px;
    color: #515154;
    line-height: 1.5;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .note-actions {
    display: none;
    gap: 6px;
    margin-top: 10px;
  }
  .note.selected .note-actions {
    display: flex;
  }
  .note-actions .btn {
    padding: 4px 10px;
    font-size: 12px;
  }
  .reader {
    display: none;
    background: #fff;
    padding: 32px 40px;
    border-radius: 12px;
    border: 1px solid #e5e5e5;
  }
  .reader-back {
    display: inline-flex;
    align-items: center;
    font-size: 14px;
    color: #007aff;
    cursor: pointer;
    margin-bottom: 20px;
    padding: 4px 0;
  }
  .reader-back:hover { text-decoration: underline; }
  .reader-title {
    font-size: 28px;
    font-weight: 700;
    margin-bottom: 12px;
    line-height: 1.3;
  }
  .reader-meta {
    font-size: 13px;
    color: #86868b;
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 1px solid #e5e5e5;
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .reader-body { line-height: 1.8; font-size: 16px; color: #1d1d1f; }
  .reader-body h1, .reader-body h2 { margin-top: 28px; margin-bottom: 12px; }
  .reader-body h3 { margin-top: 20px; margin-bottom: 8px; }
  .reader-body p { margin: 12px 0; }
  .reader-body pre {
    background: #f5f5f7;
    padding: 16px;
    border-radius: 8px;
    overflow-x: auto;
    font-size: 14px;
  }
  .reader-body code {
    background: #f5f5f7;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 14px;
    font-family: "SF Mono", monospace;
  }
  .reader-body blockquote {
    border-left: 3px solid #007aff;
    padding-left: 16px;
    margin: 16px 0;
    color: #515154;
  }
  .reader-body ul, .reader-body ol { margin: 12px 0; padding-left: 24px; }
  .reader-body li { margin: 4px 0; }
  .empty {
    text-align: center;
    padding: 80px 20px;
    color: #86868b;
  }
  .empty-icon { font-size: 48px; margin-bottom: 16px; }
  .empty-title {
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 8px;
    color: #1d1d1f;
  }
  .empty-desc { font-size: 14px; line-height: 1.5; }
  .modal-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.4);
    z-index: 1000;
    align-items: center;
    justify-content: center;
  }
  .modal {
    background: #fff;
    padding: 24px;
    border-radius: 12px;
    width: 400px;
    max-width: 90%;
    box-shadow: 0 20px 60px rgba(0,0,0,0.2);
  }
  .modal-title {
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 12px;
  }
  .modal-body {
    font-size: 14px;
    color: #515154;
    margin-bottom: 20px;
    line-height: 1.5;
  }
  .modal-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }
  .tag-input-row {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }
  .tag-input {
    flex: 1;
    padding: 6px 10px;
    border: 1px solid #d2d2d7;
    border-radius: 6px;
    font-size: 13px;
  }
  .tag-input:focus { outline: none; border-color: #007aff; }
  .batch-bar {
    display: none;
    background: #fff;
    padding: 12px 16px;
    border-radius: 10px;
    border: 1px solid #e5e5e5;
    margin-bottom: 12px;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .batch-bar.show { display: flex; }
  .view-bar {
    display: flex;
    gap: 6px;
    margin-top: 8px;
    flex-wrap: wrap;
  }
  .view-chip {
    padding: 4px 10px;
    background: #eef3ff;
    color: #1a56db;
    border-radius: 10px;
    font-size: 12px;
    cursor: pointer;
  }
  .view-chip:hover { background: #dbe6ff; }
  .mark-highlight { background: #ffeb3b; padding: 0 2px; border-radius: 2px; }
  .favorites-section {
    background: #fff;
    border-radius: 12px;
    border: 1px solid #e5e5e5;
    padding: 16px 18px;
    margin-bottom: 16px;
  }
  .favorites-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    user-select: none;
  }
  .favorites-title {
    font-size: 16px;
    font-weight: 600;
    color: #1d1d1f;
  }
  .favorites-count {
    font-size: 12px;
    color: #86868b;
    background: #f5f5f7;
    padding: 2px 8px;
    border-radius: 10px;
  }
  .favorites-empty {
    font-size: 13px;
    color: #86868b;
    padding: 12px 0;
    text-align: center;
  }
  .view-modes {
    display: flex;
    gap: 6px;
    margin-bottom: 12px;
  }
  .view-mode-btn {
    padding: 6px 12px;
    border: 1px solid #d2d2d7;
    background: #fff;
    border-radius: 8px;
    font-size: 12px;
    cursor: pointer;
    color: #1d1d1f;
  }
  .view-mode-btn.active {
    background: #007aff;
    color: #fff;
    border-color: #007aff;
  }
  .related-section {
    background: #fff;
    border-radius: 12px;
    border: 1px solid #e5e5e5;
    padding: 16px 18px;
    margin-top: 20px;
  }
  .related-title {
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 12px;
    color: #1d1d1f;
  }
  .related-item {
    padding: 8px 0;
    border-bottom: 1px solid #f5f5f7;
    cursor: pointer;
    font-size: 14px;
    color: #007aff;
  }
  .related-item:last-child { border-bottom: none; }
  .related-item:hover { text-decoration: underline; }
  .mobile-menu-btn {
    display: none;
    position: fixed;
    top: 12px;
    left: 12px;
    z-index: 900;
    width: 40px;
    height: 40px;
    background: #fff;
    border: 1px solid #e5e5e5;
    border-radius: 8px;
    font-size: 20px;
    cursor: pointer;
    align-items: center;
    justify-content: center;
  }
  .sidebar-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.3);
    z-index: 800;
  }
  .loading-state {
    text-align: center;
    padding: 40px;
    color: #86868b;
  }
  .error-state {
    text-align: center;
    padding: 40px;
    color: #ff3b30;
  }
  @media (max-width: 768px) {
    body { grid-template-columns: 1fr; }
    .sidebar {
      position: fixed;
      top: 0; left: 0; bottom: 0;
      width: 260px;
      z-index: 850;
      transform: translateX(-100%);
      transition: transform 0.25s ease;
    }
    .sidebar.open { transform: translateX(0); }
    .sidebar-overlay.show { display: block; }
    .mobile-menu-btn { display: flex; }
    .main { padding: 16px; }
    .reader { padding: 20px; }
  }
  .group-header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    background: #f5f5f7;
    border-radius: 8px;
    margin: 12px 0 6px;
    cursor: pointer;
    user-select: none;
  }
  .group-header .group-arrow {
    width: 16px;
    text-align: center;
    font-size: 12px;
    color: #86868b;
  }
  .group-header .group-name {
    flex: 1;
    font-weight: 600;
    font-size: 14px;
    color: #1d1d1f;
  }
  .group-header .group-count {
    font-size: 12px;
    color: #86868b;
    margin-right: 8px;
  }
  .group-header .group-actions {
    display: flex;
    gap: 4px;
  }
  .group-header .group-actions button {
    padding: 2px 8px;
    font-size: 12px;
  }
  .group-files {
    margin-left: 24px;
    border-left: 2px solid #e5e5e5;
    padding-left: 8px;
  }
  .group-file {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
    font-size: 14px;
  }
  .group-file .file-name {
    flex: 1;
    color: #1d1d1f;
  }
  .group-file .file-remove {
    color: #ff3b30;
    cursor: pointer;
    font-size: 12px;
    padding: 2px 6px;
  }
  .ungrouped-zone {
    border: 2px dashed #d2d2d7;
    border-radius: 8px;
    padding: 12px;
    margin: 12px 0;
    text-align: center;
    color: #86868b;
    font-size: 13px;
  }
  .ungrouped-zone.drag-over {
    border-color: #007aff;
    background: #f0f8ff;
  }
  .note.dragging {
    opacity: 0.5;
  }
  .group-header.drag-over {
    border: 2px dashed #007aff;
    background: #f0f8ff;
  }
  .group-header.drag-invalid {
    border: 2px dashed #ff3b30;
    background: #fff0f0;
  }
  .create-group-zone {
    border: 2px dashed #d2d2d7;
    border-radius: 8px;
    padding: 16px;
    margin: 12px 0;
    text-align: center;
    color: #86868b;
    font-size: 14px;
    cursor: pointer;
  }
  .create-group-zone.drag-over {
    border-color: #007aff;
    background: #f0f8ff;
  }
  .add-to-group-select {
    max-height: 200px;
    overflow-y: auto;
    border: 1px solid #d2d2d7;
    border-radius: 8px;
    padding: 8px;
  }
  .add-to-group-select label {
    display: block;
    padding: 6px 8px;
    cursor: pointer;
  }
  .add-to-group-select label:hover {
    background: #f5f5f7;
  }
  .editor-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 8px;
  }
  .editor-toolbar .btn {
    padding: 4px 10px;
    font-size: 12px;
  }
  .editor-panes {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .editor-pane-edit, .editor-pane-preview { min-width: 0; }
  #editor-text {
    width: 100%;
    min-height: 420px;
    font-family: "SF Mono", Menlo, monospace;
    font-size: 13px;
    line-height: 1.6;
    border: 1px solid #d2d2d7;
    border-radius: 8px;
    padding: 12px;
    resize: vertical;
    background: #fff;
    color: #1d1d1f;
    outline: none;
  }
  #editor-text:focus { border-color: #007aff; }
  #editor-preview {
    background: #f5f5f7;
    border-radius: 8px;
    padding: 16px;
    min-height: 420px;
    max-height: 70vh;
    overflow: auto;
    font-size: 14px;
    line-height: 1.7;
  }
  #editor-preview h1, #editor-preview h2 { margin-top: 20px; margin-bottom: 10px; }
  #editor-preview h3, #editor-preview h4, #editor-preview h5, #editor-preview h6 { margin-top: 16px; margin-bottom: 8px; }
  #editor-preview p { margin: 10px 0; }
  #editor-preview pre {
    background: #fff;
    padding: 12px;
    border-radius: 8px;
    overflow-x: auto;
    font-size: 13px;
  }
  #editor-preview code {
    background: #e5e5e5;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
    font-family: "SF Mono", monospace;
  }
  #editor-preview pre code { background: none; padding: 0; }
  #editor-preview blockquote {
    border-left: 3px solid #007aff;
    padding-left: 14px;
    margin: 12px 0;
    color: #515154;
  }
  #editor-preview ul, #editor-preview ol { margin: 10px 0; padding-left: 22px; }
  #editor-preview li { margin: 3px 0; }
  #editor-preview li.task { list-style: none; margin-left: -20px; }
  #editor-preview li.task label { cursor: default; }
  #editor-preview input[type="checkbox"] { margin-right: 6px; }
  #editor-preview table {
    border-collapse: collapse;
    margin: 12px 0;
    width: 100%;
    font-size: 14px;
  }
  #editor-preview th, #editor-preview td {
    border: 1px solid #d2d2d7;
    padding: 6px 10px;
    text-align: left;
  }
  #editor-preview th { background: #fff; }
  #editor-preview img { max-width: 100%; border-radius: 8px; }
  .editor-status-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 10px;
    flex-wrap: wrap;
  }
  #editor-save-state {
    font-size: 12px;
    color: #86868b;
    flex: 1;
  }
  #editor-save-state.dirty { color: #ff9500; }
  #editor-save-state.saved { color: #34c759; }
  #editor-save-state.error { color: #ff3b30; }
  .editor-conflict {
    display: none;
    background: #fff8e1;
    border: 1px solid #f0c040;
    color: #7a5b00;
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 13px;
    margin-top: 10px;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .editor-conflict.show { display: flex; }
  .editor-meta-note {
    font-size: 12px;
    color: #ff9500;
    background: #fff8e1;
    border-radius: 8px;
    padding: 8px 12px;
    margin: 8px 0;
  }
  #editor-mode {
    display: none;
    gap: 6px;
    margin-bottom: 8px;
  }
  #editor-mode .btn.active {
    background: #007aff;
    color: #fff;
    border-color: #007aff;
  }
  @media (max-width: 768px) {
    .editor-panes { grid-template-columns: 1fr; }
    #editor-text { min-height: 50vh; }
    #editor-preview { min-height: auto; max-height: 60vh; }
    #editor-mode { display: flex; }
    .editor-pane-preview { display: none; }
    .editor-pane-preview.active { display: block; }
    .editor-pane-edit { display: block; }
    .editor-pane-edit.inactive { display: none; }
  }
</style>
</head>
<body>
  <button class="mobile-menu-btn" data-action="toggleSidebar">☰</button>
  <div class="sidebar-overlay" id="sidebar-overlay"></div>
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-title">落笔 · Noted</div>
    <div class="sidebar-subtitle">本地阅读，自动同步</div>
    <div class="sidebar-section">
     <button class="sync-btn" data-action="sync">立即同步</button>
     <button class="sync-btn" data-action="discover" style="margin-top:6px;background:#f5f5f7;color:#1d1d1f;border:1px solid #d2d2d7;">发现未入库总结</button>
      <div class="sync-status" id="sync-status">上次同步: --</div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">标签</div>
      <div class="tag-cloud" id="tag-cloud"></div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">视图</div>
      <div class="view-bar" id="view-bar"></div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">关于</div>
      <div style="font-size: 12px; color: #86868b; line-height: 1.5;">
        自动收纳 opencode / codex / Claude Code 等 agent 对话产生的总结。<br>
        软链接模式，原始文件不动。
      </div>
    </div>
  </aside>
  <main class="main">
    <div class="toolbar">
      <input type="text" class="search-bar" id="search" placeholder="搜索标题、标签、内容…">
      <button class="btn" data-action="toggleBatch">批量选择</button>
      <button class="btn btn-primary" data-action="saveCurrentView">保存当前视图</button>
    </div>
    <div class="view-modes" id="view-modes">
      <button class="view-mode-btn active" data-view-mode="flat">平铺</button>
      <button class="view-mode-btn" data-view-mode="source">按来源</button>
      <button class="view-mode-btn" data-view-mode="tag">按标签</button>
      <button class="view-mode-btn" data-view-mode="group">分组</button>
    </div>
    <div class="batch-bar" id="batch-bar">
      <span>已选 <strong id="selected-count">0</strong> 条</span>
      <button class="btn btn-primary" data-action="batchTag">批量改标签</button>
      <button class="btn btn-primary" data-action="batchAddToGroup">加入分组</button>
      <button class="btn btn-danger" data-action="batchDelete">批量删除</button>
      <button class="btn" data-action="clearSelection">取消选择</button>
    </div>
    <div class="favorites-section" id="favorites-section" style="display:none;">
      <div class="favorites-header" data-action="toggleFavorites">
        <span class="favorites-title">⭐ 收藏</span>
        <span class="favorites-count" id="favorites-count">0</span>
      </div>
      <div id="favorites-list"></div>
    </div>
    <div class="note-count" id="note-count"></div>
    <div id="note-list"></div>
    <div id="reader" class="reader" data-file="">
      <span class="reader-back" data-action="showList">← 返回列表</span>
      <button class="btn" data-action="renameReaderNote" id="reader-rename-btn" style="display:none;margin-left:8px;">重命名</button>
      <button class="btn btn-primary" data-action="editNote" id="reader-edit-btn" style="display:none;margin-left:8px;">编辑</button>
      <div id="reader-meta" style="display:none;padding:8px 12px;background:#f5f5f7;border-radius:8px;margin:8px 0;font-size:13px;">
        <span id="reader-kind" style="display:inline-block;padding:2px 8px;border-radius:4px;background:#e5e5e5;margin-right:8px;"></span>
        <span id="reader-path" style="font-family:monospace;color:#86868b;word-break:break-all;"></span>
        <button class="btn" data-action="copyReaderPath" style="margin-left:8px;padding:2px 8px;font-size:12px;">复制路径</button>
        <button class="btn" data-action="revealReaderFile" style="margin-left:4px;padding:2px 8px;font-size:12px;">在 Finder 中显示</button>
        <button class="btn" data-action="openReaderFile" style="margin-left:4px;padding:2px 8px;font-size:12px;">打开</button>
        <span id="reader-feedback" style="margin-left:8px;color:#007aff;font-size:12px;display:none;"></span>
      </div>
      <div id="reader-content"></div>
      <div class="related-section" id="related-section" style="display:none;">
        <div class="related-title">相关笔记</div>
        <div id="related-list"></div>
      </div>
    </div>
    <div id="editor" class="reader" style="display:none;">
      <div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;">
        <span class="reader-back" data-edit="back">← 返回</span>
        <span id="editor-title" style="font-size:18px;font-weight:700;"></span>
      </div>
      <div id="editor-meta" style="display:none;padding:8px 12px;background:#f5f5f7;border-radius:8px;margin:8px 0;font-size:13px;">
        <span id="editor-kind" style="display:inline-block;padding:2px 8px;border-radius:4px;background:#e5e5e5;margin-right:8px;"></span>
        <span id="editor-propagate-row" style="display:none;color:#ff9500;">保存会修改源文件，继续前将请求确认</span>
      </div>
      <div id="editor-mode">
        <button class="btn active" data-edit="mode-edit">编辑</button>
        <button class="btn" data-edit="mode-preview">预览</button>
      </div>
      <div class="editor-toolbar" id="editor-toolbar">
        <button class="btn" data-edit="heading">H</button>
        <button class="btn" data-edit="bold" style="font-weight:700;">B</button>
        <button class="btn" data-edit="italic" style="font-style:italic;">I</button>
        <button class="btn" data-edit="link">链接</button>
        <button class="btn" data-edit="quote">引用</button>
        <button class="btn" data-edit="codeblock">代码块</button>
        <button class="btn" data-edit="ul">列表</button>
        <button class="btn" data-edit="task">任务</button>
        <button class="btn" data-edit="table">表格</button>
        <button class="btn" data-edit="image">图片</button>
        <input type="file" id="editor-image-input" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none;" />
      </div>
      <div class="editor-conflict" id="editor-conflict">
        <span>文件已被外部修改，继续保存将覆盖最新内容</span>
        <button class="btn btn-primary" data-edit="reload">重新加载</button>
        <button class="btn btn-danger" data-edit="overwrite">强制覆盖</button>
        <button class="btn" data-edit="close-conflict">关闭</button>
      </div>
      <div class="editor-panes" id="editor-panes">
        <div class="editor-pane-edit" id="editor-pane-edit">
          <textarea id="editor-text" wrap="soft" placeholder="# 开始写 Markdown…"></textarea>
        </div>
        <div class="editor-pane-preview active" id="editor-pane-preview">
          <div id="editor-preview" class="reader-body"></div>
        </div>
      </div>
      <div class="editor-status-bar">
        <span id="editor-save-state">未保存</span>
        <button class="btn" data-edit="cancel">取消</button>
        <button class="btn btn-primary" data-edit="save">保存</button>
      </div>
    </div>
    <div id="empty" class="empty" style="display:none;">
      <div class="empty-icon">📝</div>
      <div class="empty-title">落笔 · Noted</div>
      <div class="empty-desc">在 opencode / codex 中对话后，总结会自动同步到这里</div>
    </div>
    <div id="discover-panel" style="display:none;background:#fff;padding:24px;border-radius:12px;border:1px solid #e5e5e5;margin-top:12px;">
      <div style="font-size:18px;font-weight:600;margin-bottom:12px;">发现未入库总结</div>
      <div id="discover-list"></div>
      <div id="discover-empty" style="text-align:center;padding:40px;color:#86868b;display:none;">没有发现候选文件</div>
    </div>
  </main>
  <div class="modal-overlay" id="modal-overlay" data-action="closeModal">
    <div class="modal" id="modal" data-action="stopPropagation">
      <div class="modal-title" id="modal-title">标题</div>
      <div class="modal-body" id="modal-body">内容</div>
      <div class="modal-actions" id="modal-actions"></div>
    </div>
  </div>
  <script>
    let allNotes = [];
    let activeTag = null;
    let searchQuery = "";
    let selectedFiles = new Set();
    let batchMode = false;
    let userInteracting = false;
    let interactionTimer = null;
    let lastListSig = 0;

    function escapeHtml(s) {
      return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    function highlightText(text, query) {
      if (!query) return escapeHtml(text);
      const escaped = query.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
      const regex = new RegExp(`(${escaped})`, 'gi');
      return escapeHtml(text).replace(regex, '<span class="mark-highlight">$1</span>');
    }
    function renderTags(notes) {
      const tagCount = {};
      notes.forEach(n => { n.tags.forEach(t => { tagCount[t] = (tagCount[t] || 0) + 1; }); });
      const el = document.getElementById('tag-cloud');
      const sorted = Object.entries(tagCount).sort((a, b) => b[1] - a[1]);
      el.innerHTML = sorted.map(([tag, count]) => `
        <span class="tag ${activeTag === tag ? 'active' : ''}" data-tag="${escapeHtml(tag)}">
          ${escapeHtml(tag)}<span class="tag-count">${count}</span>
        </span>
      `).join('');
    }
    function renderViews() {
      const el = document.getElementById('view-bar');
      fetch('/api/views').then(r => r.json()).then(views => {
        const items = Object.entries(views).map(([key, v]) => `
          <span class="view-chip" data-view="${escapeHtml(v.name)}" data-filters="${escapeHtml(JSON.stringify(v.filters))}">
            ${escapeHtml(v.name)}
          </span>
        `).join('');
        el.innerHTML = items;
      }).catch(() => {});
    }
    function applyView(name, filters) {
      searchQuery = filters.search || "";
      activeTag = filters.tag || null;
      document.getElementById('search').value = searchQuery;
      renderList(allNotes);
      renderTags(allNotes);
    }
    function renderList(notes) {
      const el = document.getElementById('note-list');
      const empty = document.getElementById('empty');
      const countEl = document.getElementById('note-count');
      const filtered = notes.filter(n => {
        if (activeTag && !n.tags.includes(activeTag)) return false;
        if (searchQuery) {
          const q = searchQuery.toLowerCase();
          const text = `${n.title} ${n.tags.join(' ')} ${n.excerpt} ${n.content}`.toLowerCase();
          return text.includes(q);
        }
        return true;
      });
      countEl.textContent = `共 ${filtered.length} 条总结`;
      if (!filtered.length) { el.innerHTML = ''; empty.style.display = 'block'; return; }
      empty.style.display = 'none';
      el.innerHTML = filtered.map(n => `
        <div class="note ${selectedFiles.has(n.file) ? 'selected' : ''}" data-file="${escapeHtml(n.file)}">
          <button class="note-star ${n.starred ? 'starred' : ''}" data-action="toggleStar" data-file="${escapeHtml(n.file)}">${n.starred ? '★' : '☆'}</button>
          <div class="note-title">${highlightText(n.title, searchQuery)}</div>
          <div class="note-meta">
            <span>${n.date} ${n.time}</span>
            ${n.is_symlink ? `<span class="note-source" title="${escapeHtml(n.source || '')}">📎 ${escapeHtml(n.source_label || '')}</span>` : ''}
          </div>
          <div class="note-meta">
            ${n.tags.map(t => `<span class="tag">${highlightText(t, searchQuery)}</span>`).join('')}
          </div>
          <div class="note-excerpt">${highlightText(n.excerpt, searchQuery)}</div>
          <div class="note-actions">
            <button class="btn btn-primary" data-action="editTags" data-file="${escapeHtml(n.file)}">标签</button>
            <button class="btn" data-action="addNote" data-file="${escapeHtml(n.file)}">备注</button>
            <button class="btn btn-danger" data-action="deleteNote" data-file="${escapeHtml(n.file)}">删除</button>
          </div>
        </div>
      `).join('');
    }
    document.getElementById('note-list').addEventListener('click', e => {
      const note = e.target.closest('.note');
      if (!note || e.target.closest('button')) return;
      if (batchMode) {
        const file = note.dataset.file;
        if (selectedFiles.has(file)) {
          selectedFiles.delete(file);
        } else {
          selectedFiles.add(file);
        }
        document.getElementById('selected-count').textContent = selectedFiles.size;
        renderList(allNotes);
      } else {
        openNote(note.dataset.file);
      }
    });
    function computeListSig(notes) {
      return notes.map(n => n.file + '|' + (n.tags || []).join(',')).join('##');
    }
    async function loadList() {
      try {
        const res = await fetch('/api/list');
        allNotes = await res.json();
        const newSig = computeListSig(allNotes);
        if (newSig !== lastListSig) {
          lastListSig = newSig;
          renderTags(allNotes);
          renderFavorites(allNotes);
          renderList(allNotes);
          renderViews();
        }
      } catch (e) { console.error(e); }
    }
    let viewMode = 'flat';
    let favoritesOpen = true;

    function renderFavorites(notes) {
      const favs = notes.filter(n => n.starred);
      const section = document.getElementById('favorites-section');
      const list = document.getElementById('favorites-list');
      const count = document.getElementById('favorites-count');
      count.textContent = favs.length;
      if (!favs.length) {
        section.style.display = 'none';
        return;
      }
      section.style.display = 'block';
      list.innerHTML = favs.map(n => `
        <div class="note" data-file="${escapeHtml(n.file)}">
          <div class="note-title">${escapeHtml(n.title)}</div>
          <div class="note-meta">${n.date} ${n.time}</div>
        </div>
      `).join('');
    }

    function toggleFavorites() {
      favoritesOpen = !favoritesOpen;
      const list = document.getElementById('favorites-list');
      list.style.display = favoritesOpen ? 'block' : 'none';
    }

    function setViewMode(mode) {
      viewMode = mode;
      document.querySelectorAll('.view-mode-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.viewMode === mode);
      });
      if (mode === 'group') {
        loadGroups();
      } else {
        renderList(allNotes);
      }
    }

    let groupsCache = [];
    async function loadGroups() {
      try {
        const res = await fetch('/api/groups');
        groupsCache = await res.json();
        renderList(allNotes);
      } catch (e) { console.error(e); }
    }

    function renderGroups(notes) {
      const el = document.getElementById('note-list');
      const empty = document.getElementById('empty');
      const countEl = document.getElementById('note-count');
      const q = (searchQuery || '').toLowerCase();
      const tagFilter = activeTag;
      let html = '';
      let total = 0;
      const noteMap = {};
      notes.forEach(n => { noteMap[n.file] = n; });
      const rendered = new Set();
      for (const group of groupsCache) {
        let files = group.files || [];
        if (tagFilter) {
          files = files.filter(f => {
            const n = noteMap[f];
            return n && n.tags && n.tags.includes(tagFilter);
          });
        }
        if (q) {
          files = files.filter(f => {
            const n = noteMap[f];
            if (!n) return false;
            const text = `${n.title} ${n.tags.join(' ')} ${n.excerpt} ${n.content}`.toLowerCase();
            return text.includes(q);
          });
        }
        const validFiles = files.filter(f => noteMap[f]);
        total += validFiles.length;
        html += `<div class="group-header" data-group="${escapeHtml(group.name)}" draggable="true">
          <span class="group-arrow">${group.collapsed ? '▸' : '▾'}</span>
          <span class="group-name">${escapeHtml(group.name)}</span>
          <span class="group-count">${validFiles.length}</span>
          <div class="group-actions">
            <button class="btn" data-action="renameGroup" data-group="${escapeHtml(group.name)}">重命名</button>
            <button class="btn btn-danger" data-action="disbandGroup" data-group="${escapeHtml(group.name)}">解散</button>
          </div>
        </div>`;
        if (!group.collapsed && validFiles.length) {
          html += `<div class="group-files">`;
          for (const f of validFiles) {
            const n = noteMap[f];
            html += `<div class="group-file" data-file="${escapeHtml(n.file)}" draggable="true">
              <span class="file-name">${highlightText(n.title, searchQuery)}</span>
              <button class="btn" data-action="removeFromGroup" data-group="${escapeHtml(group.name)}" data-file="${escapeHtml(n.file)}">移出</button>
            </div>`;
          }
          html += `</div>`;
        }
        if (!group.collapsed && !validFiles.length && files.length) {
          html += `<div class="group-files" style="color:#86868b;font-size:13px;">无匹配结果</div>`;
        }
        if (!group.collapsed && !files.length) {
          html += `<div class="group-files" style="color:#86868b;font-size:13px;">空组，拖入文件或勾选添加</div>`;
        }
      }
      html += `<div class="create-group-zone" data-action="createGroup">+ 新建分组</div>`;
      html += `<div class="ungrouped-zone" data-action="ungroupedZone">未分组（拖入文件到此区域）</div>`;
      const ungrouped = notes.filter(n => !n.group && (tagFilter ? n.tags && n.tags.includes(tagFilter) : true) && (q ? `${n.title} ${n.tags.join(' ')} ${n.excerpt} ${n.content}`.toLowerCase().includes(q) : true));
      total += ungrouped.length;
      if (ungrouped.length) {
        html += `<div style="font-size:13px;font-weight:600;color:#86868b;margin:16px 0 8px;">未分组</div>`;
        html += `<div class="group-files">`;
        for (const n of ungrouped) {
          html += `<div class="note ${selectedFiles.has(n.file) ? 'selected' : ''}" data-file="${escapeHtml(n.file)}" draggable="true">
            <button class="note-star ${n.starred ? 'starred' : ''}" data-action="toggleStar" data-file="${escapeHtml(n.file)}">${n.starred ? '★' : '☆'}</button>
            <div class="note-title">${highlightText(n.title, searchQuery)}</div>
            <div class="note-meta"><span>${n.date} ${n.time}</span></div>
            <div class="note-meta">${n.tags.map(t => `<span class="tag">${highlightText(t, searchQuery)}</span>`).join('')}</div>
            <div class="note-actions">
              <button class="btn" data-action="addToGroup" data-file="${escapeHtml(n.file)}">加入分组</button>
            </div>
          </div>`;
        }
        html += `</div>`;
      }
      countEl.textContent = `共 ${total} 条总结`;
      if (!total && !groupsCache.length) { el.innerHTML = ''; empty.style.display = 'block'; return; }
      empty.style.display = 'none';
      el.innerHTML = html;
      setupGroupDnD();
    }

    function setupGroupDnD() {
      const el = document.getElementById('note-list');
      let draggedFile = null;
      el.querySelectorAll('.note[draggable="true"], .group-file[draggable="true"]').forEach(item => {
        item.addEventListener('dragstart', e => {
          draggedFile = item.dataset.file;
          item.classList.add('dragging');
          e.dataTransfer.setData('text/plain', draggedFile);
          e.dataTransfer.effectAllowed = 'move';
        });
        item.addEventListener('dragend', () => {
          item.classList.remove('dragging');
          draggedFile = null;
          el.querySelectorAll('.drag-over, .drag-invalid').forEach(x => {
            x.classList.remove('drag-over', 'drag-invalid');
          });
        });
      });
      el.querySelectorAll('.group-header[draggable="true"]').forEach(header => {
        header.addEventListener('dragover', e => {
          e.preventDefault();
          if (!draggedFile) return;
          header.classList.add('drag-over');
          header.classList.remove('drag-invalid');
        });
        header.addEventListener('dragleave', () => {
          header.classList.remove('drag-over', 'drag-invalid');
        });
        header.addEventListener('drop', e => {
          e.preventDefault();
          header.classList.remove('drag-over', 'drag-invalid');
          if (!draggedFile) return;
          const groupName = header.dataset.group;
          addToGroup(groupName, draggedFile);
        });
      });
      const ungrouped = el.querySelector('.ungrouped-zone');
      if (ungrouped) {
        ungrouped.addEventListener('dragover', e => {
          e.preventDefault();
          if (!draggedFile) return;
          ungrouped.classList.add('drag-over');
        });
        ungrouped.addEventListener('dragleave', () => {
          ungrouped.classList.remove('drag-over');
        });
        ungrouped.addEventListener('drop', e => {
          e.preventDefault();
          ungrouped.classList.remove('drag-over');
          if (!draggedFile) return;
          removeFromGroup(draggedFile);
        });
      }
      const createZone = el.querySelector('.create-group-zone');
      if (createZone) {
        createZone.addEventListener('dragover', e => {
          e.preventDefault();
          if (!draggedFile) return;
          createZone.classList.add('drag-over');
        });
        createZone.addEventListener('dragleave', () => {
          createZone.classList.remove('drag-over');
        });
        createZone.addEventListener('drop', e => {
          e.preventDefault();
          createZone.classList.remove('drag-over');
          if (!draggedFile) return;
          const name = prompt('新建分组名称：');
          if (!name) return;
          createGroup(name).then(() => {
            addToGroup(name, draggedFile);
          });
        });
      }
    }

    async function createGroup(name) {
      const res = await fetch('/api/group/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name}),
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || '创建失败');
        return null;
      }
      await loadGroups();
      return data.group;
    }

    async function addToGroup(groupName, file) {
      const res = await fetch('/api/group/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({group: groupName, file}),
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || '添加失败');
        return;
      }
      await loadGroups();
    }

    async function removeFromGroup(file) {
      const res = await fetch('/api/group/remove', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({file}),
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || '移出失败');
        return;
      }
      await loadGroups();
    }

    async function renameGroup(oldName) {
      const newName = prompt('新分组名称：', oldName);
      if (!newName || newName === oldName) return;
      const res = await fetch('/api/group/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({old_name: oldName, new_name: newName}),
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || '重命名失败');
        return;
      }
      await loadGroups();
    }

    async function disbandGroup(name) {
      if (!confirm(`确定解散分组「${name}」？文件将回到未分组。`)) return;
      const res = await fetch('/api/group/disband', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name}),
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || '解散失败');
        return;
      }
      await loadGroups();
    }

    async function toggleGroup(name, collapsed) {
      const res = await fetch('/api/group/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, collapsed}),
      });
      const data = await res.json();
      if (!data.ok) {
        alert(data.error || '操作失败');
        return;
      }
      await loadGroups();
    }

    function groupBySource(notes) {
      const groups = {};
      notes.forEach(n => {
        const key = n.source_label || '未分类';
        if (!groups[key]) groups[key] = [];
        groups[key].push(n);
      });
      return groups;
    }

    function groupByTag(notes) {
      const groups = {};
      notes.forEach(n => {
        const tags = n.tags && n.tags.length ? n.tags : ['未标签'];
        tags.forEach(t => {
          if (!groups[t]) groups[t] = [];
          groups[t].push(n);
        });
      });
      return groups;
    }

    function renderGroupedNotes(groups) {
      const el = document.getElementById('note-list');
      const empty = document.getElementById('empty');
      const countEl = document.getElementById('note-count');
      let html = '';
      let total = 0;
      for (const [group, notes] of Object.entries(groups)) {
        total += notes.length;
        html += `<div style="font-size:13px;font-weight:600;color:#86868b;margin:16px 0 8px;">${escapeHtml(group)}</div>`;
        html += notes.map(n => `
          <div class="note ${selectedFiles.has(n.file) ? 'selected' : ''}" data-file="${escapeHtml(n.file)}">
            <button class="note-star ${n.starred ? 'starred' : ''}" data-action="toggleStar" data-file="${escapeHtml(n.file)}">${n.starred ? '★' : '☆'}</button>
            <div class="note-title">${highlightText(n.title, searchQuery)}</div>
            <div class="note-meta"><span>${n.date} ${n.time}</span></div>
            <div class="note-excerpt">${highlightText(n.excerpt, searchQuery)}</div>
          </div>
        `).join('');
      }
      countEl.textContent = `共 ${total} 条总结`;
      if (!total) { el.innerHTML = ''; empty.style.display = 'block'; return; }
      empty.style.display = 'none';
      el.innerHTML = html;
    }

    function renderList(notes) {
      if (viewMode === 'source') {
        renderGroupedNotes(groupBySource(notes));
        return;
      }
      if (viewMode === 'tag') {
        renderGroupedNotes(groupByTag(notes));
        return;
      }
      if (viewMode === 'group') {
        renderGroups(notes);
        return;
      }
      const el = document.getElementById('note-list');
      const empty = document.getElementById('empty');
      const countEl = document.getElementById('note-count');
      const filtered = notes.filter(n => {
        if (activeTag && !n.tags.includes(activeTag)) return false;
        if (searchQuery) {
          const q = searchQuery.toLowerCase();
          const text = `${n.title} ${n.tags.join(' ')} ${n.excerpt} ${n.content}`.toLowerCase();
          return text.includes(q);
        }
        return true;
      });
      countEl.textContent = `共 ${filtered.length} 条总结`;
      if (!filtered.length) { el.innerHTML = ''; empty.style.display = 'block'; return; }
      empty.style.display = 'none';
      el.innerHTML = filtered.map(n => `
        <div class="note ${selectedFiles.has(n.file) ? 'selected' : ''}" data-file="${escapeHtml(n.file)}">
          <button class="note-star ${n.starred ? 'starred' : ''}" data-action="toggleStar" data-file="${escapeHtml(n.file)}">${n.starred ? '★' : '☆'}</button>
          <div class="note-title">${highlightText(n.title, searchQuery)}</div>
          <div class="note-meta">
            <span>${n.date} ${n.time}</span>
            ${n.is_symlink ? `<span class="note-source" title="${escapeHtml(n.source || '')}">📎 ${escapeHtml(n.source_label || '')}</span>` : ''}
          </div>
          <div class="note-meta">
            ${n.tags.map(t => `<span class="tag">${highlightText(t, searchQuery)}</span>`).join('')}
          </div>
          <div class="note-excerpt">${highlightText(n.excerpt, searchQuery)}</div>
          <div class="note-actions">
            <button class="btn btn-primary" data-action="editTags" data-file="${escapeHtml(n.file)}">标签</button>
            <button class="btn" data-action="addNote" data-file="${escapeHtml(n.file)}">备注</button>
            <button class="btn" data-action="renameNote" data-file="${escapeHtml(n.file)}" data-symlink="${n.is_symlink ? '1' : '0'}" data-source-label="${escapeHtml(n.source_label || '')}">重命名</button>
            <button class="btn" data-action="revealNote" data-file="${escapeHtml(n.file)}" title="在 Finder 中显示">📂</button>
            <button class="btn btn-danger" data-action="deleteNote" data-file="${escapeHtml(n.file)}">删除</button>
            <button class="btn" data-action="addToGroup" data-file="${escapeHtml(n.file)}">加入分组</button>
          </div>
        </div>
      `).join('');
    }

    async function openNote(file) {
      if (!closeEditorIfDirty()) return;
      try {
        const res = await fetch('/api/read/html?file=' + encodeURIComponent(file));
        const html = await res.text();
        document.getElementById('reader-content').innerHTML = html;
        document.getElementById('note-list').style.display = 'none';
        document.getElementById('empty').style.display = 'none';
        document.getElementById('discover-panel').style.display = 'none';
        document.getElementById('editor').style.display = 'none';
        editorOpen = false;
        document.getElementById('reader').style.display = 'block';
        document.getElementById('reader').dataset.file = file;
        document.getElementById('reader-rename-btn').style.display = 'inline-block';
        document.getElementById('reader-edit-btn').style.display = 'inline-block';
        window.scrollTo(0, 0);
        renderRelatedNotes(file);
        loadReaderDetail(file);
      } catch (e) { console.error(e); }
    }

    async function loadReaderDetail(file) {
      try {
        const res = await fetch('/api/detail?file=' + encodeURIComponent(file));
        if (!res.ok) return;
        const data = await res.json();
        const meta = document.getElementById('reader-meta');
        const kindEl = document.getElementById('reader-kind');
        const pathEl = document.getElementById('reader-path');
        const revealBtn = document.querySelector('[data-action="revealReaderFile"]');
        const openBtn = document.querySelector('[data-action="openReaderFile"]');
        meta.style.display = 'block';
        kindEl.textContent = data.kind === 'symlink' ? `软链接 · ${escapeHtml(data.source_label || '')}` : '本地文件';
        pathEl.textContent = data.path || '';
        if (!data.exists) {
          kindEl.textContent = '断链';
          kindEl.style.background = '#ff3b30';
          kindEl.style.color = '#fff';
          if (revealBtn) revealBtn.disabled = true;
          if (openBtn) openBtn.disabled = true;
        } else {
          kindEl.style.background = '#e5e5e5';
          kindEl.style.color = '#1d1d1f';
          if (revealBtn) revealBtn.disabled = false;
          if (openBtn) openBtn.disabled = false;
        }
      } catch (e) { console.error(e); }
    }

    function flashFeedback(text) {
      const el = document.getElementById('reader-feedback');
      if (!el) return;
      el.textContent = text;
      el.style.display = 'inline';
      setTimeout(() => { el.style.display = 'none'; }, 1500);
    }

    async function copyReaderPath() {
      const text = document.getElementById('reader-path').textContent;
      if (!text) return;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement('textarea');
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        flashFeedback('已复制');
      } catch (e) {
        flashFeedback('复制失败');
      }
    }

    async function revealFile(file) {
      file = file || (document.getElementById('reader').dataset.file || '');
      if (!file) return;
      try {
        const res = await fetch('/api/reveal', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({file}),
        });
        const data = await res.json();
        if (data.ok) flashFeedback('已打开');
        else flashFeedback('失败');
      } catch (e) {
        flashFeedback('失败');
      }
    }

    async function revealReaderFile() {
      await revealFile('');
    }

    async function openReaderFile() {
      const file = document.getElementById('reader').dataset.file || '';
      if (!file) return;
      try {
        const res = await fetch('/api/open', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({file}),
        });
        const data = await res.json();
        if (data.ok) flashFeedback('已打开');
        else flashFeedback('失败');
      } catch (e) {
        flashFeedback('失败');
      }
    }

    async function renderRelatedNotes(currentFile) {
      const current = allNotes.find(n => n.file === currentFile);
      if (!current) return;
      const scored = allNotes
        .filter(n => n.file !== currentFile)
        .map(n => {
          const overlap = n.tags.filter(t => current.tags.includes(t)).length;
          return { note: n, score: overlap };
        })
        .filter(x => x.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, 5);
      const section = document.getElementById('related-section');
      const list = document.getElementById('related-list');
      if (!scored.length) { section.style.display = 'none'; return; }
      section.style.display = 'block';
      list.innerHTML = scored.map(x => `
        <div class="related-item" data-file="${escapeHtml(x.note.file)}">${escapeHtml(x.note.title)}</div>
      `).join('');
      list.onclick = (e) => {
        const item = e.target.closest('.related-item');
        if (item) openNote(item.dataset.file);
      };
    }
    function showList() {
      if (!closeEditorIfDirty()) return;
      document.getElementById('reader').style.display = 'none';
      document.getElementById('note-list').style.display = 'block';
    }
    function toggleTag(tag) {
      activeTag = activeTag === tag ? null : tag;
      renderList(allNotes);
      renderTags(allNotes);
    }
    document.getElementById('search').addEventListener('input', e => {
      searchQuery = e.target.value.trim();
      renderList(allNotes);
    });
    document.getElementById('search').addEventListener('focus', () => {
      userInteracting = true;
    });
    document.getElementById('search').addEventListener('blur', () => {
      interactionTimer = setTimeout(() => { userInteracting = false; }, 1000);
    });
    document.addEventListener('mousedown', () => {
      userInteracting = true;
      if (interactionTimer) clearTimeout(interactionTimer);
      interactionTimer = setTimeout(() => { userInteracting = false; }, 2000);
    });
    async function doSync() {
      const btn = document.querySelector('.sync-btn');
      btn.textContent = '同步中…';
      btn.disabled = true;
      try {
        const res = await fetch('/api/sync');
        const data = await res.json();
        document.getElementById('sync-status').textContent =
          `上次同步: ${new Date().toLocaleTimeString()} · 新增 ${data.count} 个`;
        await loadList();
      } catch (e) {
        document.getElementById('sync-status').textContent = '同步失败';
      } finally {
        btn.textContent = '立即同步';
        btn.disabled = false;
      }
    }
    async function toggleStar(file) {
      const note = allNotes.find(n => n.file === file);
      if (!note) return;
      const newStar = !note.starred;
      await fetch('/api/star', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({file, starred: newStar})
      });
      note.starred = newStar;
      renderList(allNotes);
    }
    function editTags(file) {
      const note = allNotes.find(n => n.file === file);
      if (!note) return;
      const current = (note.tags || []).join(', ');
      const input = prompt('编辑标签（逗号分隔）：\\n' + note.title, current);
      if (input === null) return;
      const tags = input.split(/[,，]/).map(t => t.trim()).filter(Boolean);
      saveTags(file, tags);
    }
    async function saveTags(file, tags) {
      await fetch('/api/tags', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({file, tags})
      });
      const note = allNotes.find(n => n.file === file);
      if (note) note.tags = tags;
      renderList(allNotes);
      renderTags(allNotes);
    }
    function addNote(file) {
      const note = allNotes.find(n => n.file === file);
      if (!note) return;
      const input = prompt('添加备注：\\n' + note.title, note.note || '');
      if (input === null) return;
      saveNote(file, input);
    }
    async function saveNote(file, text) {
      const index = await fetch('/api/views').then(r => r.json());
      const key = `_note_${file}`;
      if (!index[key]) {
        await fetch('/api/views/save', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: key, filters: {}})
        });
      }
      await fetch('/api/tags', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({file, tags: allNotes.find(n => n.file === file)?.tags || []})
      });
      const idx = await (await fetch('/api/views')).json();
      const entry = idx[key] || {};
      entry.note = text;
      entry.updated_at = new Date().toISOString();
      await fetch('/api/views/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: key, filters: entry})
      });
    }
    function deleteNote(file) {
      showModal('确认删除', '确定要删除这条总结吗？原始文件不会被删除（软链接仅移除链接）。', [
        {text: '取消', class: 'btn', action: closeModal},
        {text: '删除', class: 'btn btn-danger', action: () => confirmDelete(file)}
      ]);
    }
    async function confirmDelete(file) {
      await fetch('/api/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({file})
      });
      allNotes = allNotes.filter(n => n.file !== file);
      renderList(allNotes);
      renderTags(allNotes);
      closeModal();
    }
    function toggleBatch() {
      batchMode = !batchMode;
      selectedFiles.clear();
      document.getElementById('batch-bar').classList.toggle('show', batchMode);
      renderList(allNotes);
    }
    function clearSelection() {
      selectedFiles.clear();
      document.getElementById('selected-count').textContent = '0';
      renderList(allNotes);
    }
    function batchTag() {
      if (!selectedFiles.size) return;
      const input = prompt(`为 ${selectedFiles.size} 条总结批量设置标签（逗号分隔）：`);
      if (input === null) return;
      const tags = input.split(/[,，]/).map(t => t.trim()).filter(Boolean);
      Promise.all(Array.from(selectedFiles).map(f => saveTags(f, tags))).then(() => {
        selectedFiles.clear();
        document.getElementById('selected-count').textContent = '0';
        renderList(allNotes);
      });
    }
    function batchAddToGroup() {
      if (!selectedFiles.size) return;
      const existing = groupsCache.map(g => g.name).join('、');
      const name = prompt(`将 ${selectedFiles.size} 条总结加入分组（${existing ? '现有分组: ' + existing + '；' : ''}输入新名称可自动创建）：`);
      if (!name) return;
      (async () => {
        const res = await fetch('/api/group/create', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name}),
        });
        const cdata = await res.json();
        if (!cdata.ok && !(cdata.error || '').includes('已存在')) {
          alert(cdata.error || '创建失败');
          return;
        }
        for (const f of Array.from(selectedFiles)) {
          await addToGroup(name, f);
        }
        selectedFiles.clear();
        document.getElementById('selected-count').textContent = '0';
        await loadList();
        if (viewMode === 'group') await loadGroups(); else renderList(allNotes);
      })();
    }
    function batchDelete() {
      if (!selectedFiles.size) return;
      showModal('批量删除', `确定要删除选中的 ${selectedFiles.size} 条总结吗？`, [
        {text: '取消', class: 'btn', action: closeModal},
        {text: '删除', class: 'btn btn-danger', action: async () => {
          await Promise.all(Array.from(selectedFiles).map(f => fetch('/api/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({file: f})
          })));
          allNotes = allNotes.filter(n => !selectedFiles.has(n.file));
          selectedFiles.clear();
          document.getElementById('selected-count').textContent = '0';
          renderList(allNotes);
          renderTags(allNotes);
          closeModal();
        }}
      ]);
    }
    function saveCurrentView() {
      const name = prompt('视图名称：');
      if (!name) return;
      fetch('/api/views/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          name,
          filters: {search: searchQuery, tag: activeTag}
        })
      }).then(() => { renderViews(); alert('视图已保存'); });
    }
    function showModal(title, body, actions) {
      document.getElementById('modal-title').textContent = title;
      document.getElementById('modal-body').textContent = body;
      const actionsEl = document.getElementById('modal-actions');
      actionsEl.innerHTML = actions.map((a, idx) =>
        `<button class="${a.class || 'btn'}" data-modal-action="${idx}">${a.text}</button>`
      ).join('');
      actionsEl._actions = actions;
      document.getElementById('modal-overlay').style.display = 'flex';
    }
    function closeModal() {
      document.getElementById('modal-overlay').style.display = 'none';
    }
    let renameCurrentFile = '';
    function showRenameModal(file, isSymlink, sourceLabel) {
      renameCurrentFile = file;
      const base = file.replace(/[.]md$/i, '');
      const modal = document.getElementById('modal');
      const body = document.getElementById('modal-body');
      const actions = document.getElementById('modal-actions');
      document.getElementById('modal-title').textContent = '重命名';
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;">
          <div>
            <label style="display:block;font-size:13px;color:#86868b;margin-bottom:4px;">文件名</label>
            <input id="rename-input" type="text" value="${escapeHtml(base)}" style="width:100%;padding:8px 12px;border:1px solid #d2d2d7;border-radius:8px;font-size:15px;" />
            <div id="rename-error" style="color:#ff3b30;font-size:13px;margin-top:4px;display:none;"></div>
          </div>
          <label style="display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;">
            <input id="rename-update-h1" type="checkbox" checked /> 同时更新正文 H1 标题
          </label>
          ${isSymlink ? `<label style="display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;">
            <input id="rename-propagate" type="checkbox" /> 同步重命名源文件（来源: ${escapeHtml(sourceLabel || '')}）
          </label>` : ''}
        </div>
      `;
      actions.innerHTML = `
        <button class="btn" data-modal-action="0">取消</button>
        <button class="btn btn-primary" data-modal-action="1">确认</button>
      `;
      actions._actions = [
        {text: '取消', class: 'btn', action: closeModal},
        {text: '确认', class: 'btn btn-primary', action: submitRename},
      ];
      document.getElementById('modal-overlay').style.display = 'flex';
      const input = document.getElementById('rename-input');
      input.focus();
      input.select();
      input.onkeydown = (e) => { if (e.key === 'Enter') submitRename(); };
    }
    async function submitRename() {
      const input = document.getElementById('rename-input');
      const errorEl = document.getElementById('rename-error');
      const updateH1 = document.getElementById('rename-update-h1').checked;
      const propagate = document.getElementById('rename-propagate') ? document.getElementById('rename-propagate').checked : false;
      const newName = input.value.trim();
      if (!newName) {
        errorEl.textContent = '文件名不能为空';
        errorEl.style.display = 'block';
        return;
      }
      try {
        const res = await fetch('/api/rename', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({file: renameCurrentFile, new_name: newName, update_h1: updateH1, propagate: propagate}),
        });
        const data = await res.json();
        if (!data.ok) {
          errorEl.textContent = data.error || '重命名失败';
          errorEl.style.display = 'block';
          return;
        }
        closeModal();
        loadList();
        const currentFile = document.getElementById('reader').dataset.file || '';
        if (currentFile && (currentFile === renameCurrentFile || currentFile === data.file)) {
          openNote(data.file);
        }
      } catch (e) {
        errorEl.textContent = '请求失败';
        errorEl.style.display = 'block';
      }
    }
    async function doDiscover() {
      userInteracting = true;
      const panel = document.getElementById('discover-panel');
      const list = document.getElementById('discover-list');
      const empty = document.getElementById('discover-empty');
      panel.style.display = 'block';
      list.innerHTML = '<div style="text-align:center;padding:40px;color:#86868b;">扫描中…</div>';
      empty.style.display = 'none';
      try {
        const res = await fetch('/api/discover');
        const candidates = await res.json();
        if (!candidates.length) {
          list.innerHTML = '';
          empty.style.display = 'block';
          return;
        }
        empty.style.display = 'none';
        list.innerHTML = candidates.map((c, i) => `
          <div class="note" style="cursor:default;">
            <div class="note-title">${escapeHtml(c.filename)}</div>
            <div class="note-meta">${c.modified} · ${(c.size / 1024).toFixed(1)} KB · ${escapeHtml(c.source_label || '')}</div>
            <div class="note-meta">判定: ${c.reasons.map(r => '<span class="tag">' + escapeHtml(r) + '</span>').join(' ')}</div>
            <div style="display:flex;gap:8px;margin-top:8px;">
              <button class="btn btn-primary" data-action="discoverAdd" data-candidate-id="${escapeHtml(c.candidate_id)}">入库</button>
              <button class="btn" data-action="discoverIgnore" data-candidate-id="${escapeHtml(c.candidate_id)}" data-index="${i}">跳过</button>
            </div>
          </div>
        `).join('');
      } catch (e) {
        list.innerHTML = '<div style="text-align:center;padding:40px;color:#ff3b30;">扫描失败</div>';
      }
    }
    async function discoverAdd(candidateId) {
      await fetch('/api/discover/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({candidate_id: candidateId})
      });
      alert('已入库');
      document.getElementById('discover-panel').style.display = 'none';
      await loadList();
    }
    async function discoverIgnore(candidateId, index) {
      await fetch('/api/discover/ignore', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({candidate_id: candidateId})
      });
      const items = document.querySelectorAll('#discover-list .note');
      if (items[index]) items[index].remove();
      if (!document.querySelectorAll('#discover-list .note').length) {
        document.getElementById('discover-empty').style.display = 'block';
      }
    }
    let editorOpen = false;
    let editorFile = '';
    let editorRevision = '';
    let editorDirty = false;
    let editorSymlink = false;
    let editorConflictRevision = '';
    let previewSeq = 0;
    let previewTimer = null;
    const EDITOR_IMG_TYPES = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'};

    function editorTa() {
      return document.getElementById('editor-text');
    }
    function setEditorSaveState(cls, text) {
      const el = document.getElementById('editor-save-state');
      el.className = cls || '';
      el.textContent = text;
    }
    function markEditorDirty(dirty) {
      editorDirty = dirty;
      if (dirty) setEditorSaveState('dirty', '有未保存的更改');
      else setEditorSaveState('saved', '已保存');
    }
    function hideEditorConflict() {
      document.getElementById('editor-conflict').classList.remove('show');
      editorConflictRevision = '';
    }
    function closeEditorIfDirty() {
      if (!editorOpen) return true;
      if (editorDirty && !confirm('当前笔记有未保存的更改，离开将放弃这些更改。确定离开？')) {
        return false;
      }
      closeEditorSilent();
      return true;
    }
    function closeEditorSilent() {
      if (!editorOpen) return;
      editorOpen = false;
      if (previewTimer) {
        clearTimeout(previewTimer);
        previewTimer = null;
      }
      previewSeq++;
      document.getElementById('editor').style.display = 'none';
      hideEditorConflict();
      document.getElementById('editor-text').value = '';
      document.getElementById('editor-preview').innerHTML = '';
    }
    function exitToBack() {
      if (!closeEditorIfDirty()) return;
      showList();
    }
    async function openEditor(file) {
      if (editorOpen && editorDirty && !confirm('当前笔记有未保存更改，打开其他笔记将放弃这些更改')) {
        return;
      }
      const res = await fetch('/api/edit?file=' + encodeURIComponent(file));
      if (!res.ok) {
        alert('打开编辑失败');
        return;
      }
      const data = await res.json();
      closeEditorSilent();
      editorFile = file;
      editorRevision = data.revision || '';
      editorSymlink = !!data.is_symlink;
      editorConflictRevision = '';
      editorTa().value = data.content || '';
      editorTa().focus();
      document.getElementById('editor-title').textContent = data.file;
      const meta = document.getElementById('editor-meta');
      meta.style.display = 'block';
      const kind = document.getElementById('editor-kind');
      kind.textContent = editorSymlink ? '软链接 · ' + (data.source_label || '未知来源') : '本地文件';
      kind.style.background = editorSymlink ? '#ff9500' : '#e5e5e5';
      kind.style.color = editorSymlink ? '#fff' : '#1d1d1f';
      document.getElementById('editor-propagate-row').style.display = editorSymlink ? 'inline' : 'none';
      document.getElementById('reader').style.display = 'none';
      document.getElementById('note-list').style.display = 'none';
      document.getElementById('empty').style.display = 'none';
      document.getElementById('discover-panel').style.display = 'none';
      document.getElementById('editor').style.display = 'block';
      editorOpen = true;
      markEditorDirty(false);
      setEditorSaveState('', '已加载');
      const isMobile = window.matchMedia && window.matchMedia('(max-width: 768px)').matches;
      setEditorMode(isMobile ? 'edit' : 'both');
      requestPreview();
      window.scrollTo(0, 0);
    }
    function setEditorMode(mode) {
      const editPane = document.getElementById('editor-pane-edit');
      const previewPane = document.getElementById('editor-pane-preview');
      document.querySelectorAll('#editor-mode .btn').forEach(b => {
        b.classList.toggle('active', b.dataset.edit === 'mode-' + mode);
      });
      if (mode === 'edit') {
        editPane.classList.remove('inactive');
        previewPane.classList.remove('active');
      } else if (mode === 'preview') {
        editPane.classList.add('inactive');
        previewPane.classList.add('active');
        requestPreview();
      } else {
        editPane.classList.remove('inactive');
        previewPane.classList.add('active');
      }
    }
    function schedulePreview() {
      if (previewTimer) {
        clearTimeout(previewTimer);
        previewTimer = null;
      }
      previewTimer = setTimeout(requestPreview, 400);
    }
    async function requestPreview() {
      const text = editorTa().value;
      const seq = ++previewSeq;
      try {
        const res = await fetch('/api/preview', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({content: text}),
        });
        const data = await res.json();
        if (seq !== previewSeq) return;
        document.getElementById('editor-preview').innerHTML = data.ok ? (data.html || '') : '';
      } catch (e) {
        if (seq === previewSeq) document.getElementById('editor-preview').textContent = '预览失败';
      }
    }
    function applySaveSuccess(data) {
      editorRevision = data.revision || '';
      hideEditorConflict();
      markEditorDirty(false);
      setEditorSaveState('saved', '已保存 ' + new Date().toLocaleTimeString());
      loadList();
    }
    async function saveEditor(propagateConfirmed) {
      if (!editorOpen) return;
      if (editorSymlink && propagateConfirmed !== true) {
        const srcText = document.getElementById('editor-kind').textContent;
        showModal('确认写入源文件', '「' + srcText + '」为软链接，保存将直接修改源文件内容。是否继续？', [
          {text: '取消', class: 'btn', action: closeModal},
          {text: '继续保存', class: 'btn btn-primary', action: async function() {
            closeModal();
            await saveEditor(true);
          }},
        ]);
        return;
      }
      const text = editorTa().value;
      const propagate = editorSymlink && propagateConfirmed === true;
      const revision = editorConflictRevision || editorRevision;
      try {
        const res = await fetch('/api/save', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({file: editorFile, content: text, revision: revision, propagate: propagate}),
        });
        const data = await res.json();
        if (!data.ok) {
          if (data.conflict) {
            editorConflictRevision = data.revision || '';
            document.getElementById('editor-conflict').classList.add('show');
            setEditorSaveState('error', '保存冲突：文件已被外部修改');
            return;
          }
          setEditorSaveState('error', data.error || '保存失败');
          return;
        }
        applySaveSuccess(data);
      } catch (e) {
        setEditorSaveState('error', '保存失败：网络错误');
      }
    }
    function cancelEdit() {
      if (!editorDirty) {
        doCancelEdit();
        return;
      }
      showModal('放弃更改', '存在未保存的更改，放弃后无法恢复。', [
        {text: '继续编辑', class: 'btn', action: closeModal},
        {text: '放弃', class: 'btn btn-danger', action: doCancelEdit},
      ]);
    }
    function doCancelEdit() {
      const file = editorFile;
      closeModal();
      closeEditorSilent();
      if (file) openNote(file);
      else showList();
    }
    function commitEditorEdit() {
      markEditorDirty(true);
      schedulePreview();
    }
    function wrapSelection(pre, post) {
      const ta = editorTa();
      const s = ta.selectionStart;
      const e = ta.selectionEnd;
      const sel = ta.value.slice(s, e);
      ta.value = ta.value.slice(0, s) + pre + sel + post + ta.value.slice(e);
      ta.selectionStart = s + pre.length;
      ta.selectionEnd = s + pre.length + sel.length;
      ta.focus();
      commitEditorEdit();
    }
    function toggleLinePrefix(prefix) {
      const ta = editorTa();
      const v = ta.value;
      const s = ta.selectionStart;
      const e = ta.selectionEnd;
      const ls = v.lastIndexOf('\\n', s - 1) + 1;
      let le = v.indexOf('\\n', e);
      if (le === -1) le = v.length;
      const lines = v.slice(ls, le).split('\\n');
      const all = lines.every(l => l.startsWith(prefix));
      const updated = lines.map(l => all ? l.slice(prefix.length) : (l.startsWith(prefix) ? l : prefix + l)).join('\\n');
      ta.value = v.slice(0, ls) + updated + v.slice(le);
      ta.selectionStart = ls;
      ta.selectionEnd = ls + updated.length;
      ta.focus();
      commitEditorEdit();
    }
    function insertLink() {
      const ta = editorTa();
      const s = ta.selectionStart;
      const e = ta.selectionEnd;
      const sel = ta.value.slice(s, e);
      if (sel) {
        ta.value = ta.value.slice(0, s) + '[' + sel + '](https://)' + ta.value.slice(e);
        ta.selectionStart = s + sel.length + 3;
        ta.selectionEnd = ta.selectionStart + 8;
      } else {
        ta.value = ta.value.slice(0, s) + '[链接](https://)' + ta.value.slice(e);
        ta.selectionStart = s + 1;
        ta.selectionEnd = s + 4;
      }
      ta.focus();
      commitEditorEdit();
    }
    function toggleCodeBlock() {
      const ta = editorTa();
      const s = ta.selectionStart;
      const e = ta.selectionEnd;
      const sel = ta.value.slice(s, e);
      if (!sel) {
        const pre = (s > 0 && ta.value[s - 1] !== '\\n') ? '\\n' : '';
        const t = pre + '```\\n\\n```\\n';
        ta.value = ta.value.slice(0, s) + t + ta.value.slice(e);
        ta.selectionStart = s + pre.length + 4;
        ta.selectionEnd = ta.selectionStart;
      } else {
        const pre = (s > 0 && ta.value[s - 1] !== '\\n') ? '\\n' : '';
        const post = ta.value.slice(e) && ta.value[e] !== '\\n' ? '\\n' : '';
        const t = pre + '```\\n' + sel + '\\n```' + post;
        ta.value = ta.value.slice(0, s) + t + ta.value.slice(e);
        ta.selectionStart = s + t.length;
        ta.selectionEnd = s + t.length;
      }
      ta.focus();
      commitEditorEdit();
    }
    function insertBlock(block) {
      const ta = editorTa();
      const s = ta.selectionStart;
      const e = ta.selectionEnd;
      const pre = (s > 0 && ta.value[s - 1] !== '\\n') ? '\\n' : '';
      const t = pre + block;
      ta.value = ta.value.slice(0, s) + t + ta.value.slice(e);
      ta.selectionStart = s + t.length;
      ta.selectionEnd = s + t.length;
      ta.focus();
      commitEditorEdit();
    }
    const EDITOR_TOOLBAR = {
      heading: function() { toggleLinePrefix('## '); },
      bold: function() { wrapSelection('**', '**'); },
      italic: function() { wrapSelection('*', '*'); },
      link: function() { insertLink(); },
      quote: function() { toggleLinePrefix('> '); },
      codeblock: function() { toggleCodeBlock(); },
      ul: function() { toggleLinePrefix('- '); },
      task: function() { toggleLinePrefix('- [ ] '); },
      table: function() { insertBlock('| 列一 | 列二 | 列三 |\\n| --- | --- | --- |\\n|  |  |  |'); },
      image: function() { document.getElementById('editor-image-input').click(); },
    };
    async function uploadAndInsertImage(file) {
      if (!file || !editorOpen) return;
      const mimeOk = !!EDITOR_IMG_TYPES[file.type];
      const fileName = String(file.name || '');
      const dot = fileName.lastIndexOf('.');
      const ext = dot >= 0 ? fileName.slice(dot + 1).toLowerCase() : '';
      const extMimes = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp'};
      const mime = mimeOk ? file.type : (extMimes[ext] || '');
      if (!mime) {
        setEditorSaveState('error', '不支持的图片类型：' + (fileName || '未命名'));
        return;
      }
      var dataUrl = '';
      try {
        dataUrl = await new Promise(function(resolve) {
          const reader = new FileReader();
          reader.onload = function() { resolve(reader.result); };
          reader.onerror = function() { resolve(''); };
          reader.readAsDataURL(file);
        });
      } catch (e) {
        dataUrl = '';
      }
      if (!dataUrl || dataUrl.indexOf('base64,') === -1) {
        setEditorSaveState('error', '读取图片失败');
        return;
      }
      const b64 = dataUrl.split('base64,')[1];
      try {
        const res = await fetch('/api/upload', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({file: editorFile, data: b64, mime: mime, filename: file.name || ''}),
        });
        const data = await res.json();
        if (!data.ok) {
          setEditorSaveState('error', '图片上传失败：' + (data.error || '未知错误'));
          return;
        }
        const alt = fileName.split('[').join('').split(']').join('').split('(').join('').split(')').join('').trim() || '图片';
        insertBlock('![' + alt + '](' + data.url + ')');
        requestPreview();
      } catch (e) {
        setEditorSaveState('error', '图片上传失败：网络错误');
      }
    }
    document.getElementById('editor-text').addEventListener('input', () => {
      commitEditorEdit();
    });
    document.getElementById('editor-text').addEventListener('paste', e => {
      const items = e.clipboardData && e.clipboardData.items ? e.clipboardData.items : [];
      const files = [];
      for (const item of items) {
        if (item.kind === 'file') {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (!files.length) return;
      e.preventDefault();
      files.forEach(f => uploadAndInsertImage(f));
    });
    document.getElementById('editor').addEventListener('click', e => {
      const btn = e.target.closest('[data-edit]');
      if (!btn) return;
      const action = btn.dataset.edit;
      if (action === 'back') exitToBack();
      else if (action === 'save') saveEditor();
      else if (action === 'cancel') cancelEdit();
      else if (action === 'reload') { if (editorFile) openEditor(editorFile); }
      else if (action === 'overwrite') saveEditor();
      else if (action === 'close-conflict') {
        hideEditorConflict();
        setEditorSaveState('dirty', '有未保存的更改');
      }
      else if (action === 'mode-edit') setEditorMode('edit');
      else if (action === 'mode-preview') setEditorMode('preview');
      else if (EDITOR_TOOLBAR[action]) EDITOR_TOOLBAR[action]();
    });
    document.getElementById('editor-image-input').addEventListener('change', e => {
      const files = Array.from(e.target.files || []);
      files.forEach(f => uploadAndInsertImage(f));
      e.target.value = '';
    });
    document.addEventListener('keydown', e => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 's' || e.key === 'S')) {
        if (editorOpen) {
          e.preventDefault();
          saveEditor();
        }
      }
    });
    window.addEventListener('beforeunload', e => {
      if (editorOpen && editorDirty) {
        e.preventDefault();
        e.returnValue = '';
      }
    });
    loadList();
    setInterval(() => {
      if (!userInteracting) {
        loadList();
      }
    }, 5000);
    document.getElementById('sync-status').textContent = '上次同步: 页面加载时';

    document.getElementById('note-list').addEventListener('click', e => {
      const btn = e.target.closest('button[data-action]');
      if (btn) {
        const action = btn.dataset.action;
        const file = btn.dataset.file;
        if (action === 'toggleStar') toggleStar(file);
        else if (action === 'editTags') editTags(file);
        else if (action === 'addNote') addNote(file);
        else if (action === 'renameNote') showRenameModal(file, btn.dataset.symlink === '1', btn.dataset.sourceLabel || '');
        else if (action === 'revealNote') revealFile(btn.dataset.file);
        else if (action === 'deleteNote') deleteNote(file);
        else if (action === 'renameGroup') renameGroup(btn.dataset.group);
        else if (action === 'disbandGroup') disbandGroup(btn.dataset.group);
        else if (action === 'removeFromGroup') removeFromGroup(btn.dataset.file);
        else if (action === 'addToGroup') {
          const groupName = prompt('请输入分组名称：');
          if (groupName) addToGroup(groupName, btn.dataset.file);
        }
        else if (action === 'createGroup') {
          const name = prompt('新建分组名称：');
          if (name) createGroup(name);
        }
        return;
      }
      const groupHeader = e.target.closest('.group-header');
      if (groupHeader && e.target.closest('.group-arrow')) {
        const name = groupHeader.dataset.group;
        const collapsed = groupHeader.querySelector('.group-arrow').textContent === '▾';
        toggleGroup(name, !collapsed);
        return;
      }
      const note = e.target.closest('.note');
      if (!note) return;
      if (batchMode) {
        const file = note.dataset.file;
        if (selectedFiles.has(file)) {
          selectedFiles.delete(file);
        } else {
          selectedFiles.add(file);
        }
        document.getElementById('selected-count').textContent = selectedFiles.size;
        renderList(allNotes);
      } else {
        openNote(note.dataset.file);
      }
    });

    document.getElementById('tag-cloud').addEventListener('click', e => {
      const tag = e.target.closest('.tag');
      if (tag) toggleTag(tag.dataset.tag);
    });

    document.getElementById('view-bar').addEventListener('click', e => {
      const chip = e.target.closest('.view-chip');
      if (chip) {
        try {
          const filters = JSON.parse(chip.dataset.filters);
          applyView(chip.dataset.view, filters);
        } catch (err) {}
      }
    });

    document.querySelector('.sidebar').addEventListener('click', e => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === 'sync') doSync();
      else if (action === 'discover') doDiscover();
    });

    document.querySelector('.toolbar').addEventListener('click', e => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === 'toggleBatch') toggleBatch();
      else if (action === 'saveCurrentView') saveCurrentView();
    });

    document.querySelector('.batch-bar').addEventListener('click', e => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      if (action === 'batchTag') batchTag();
      else if (action === 'batchAddToGroup') batchAddToGroup();
      else if (action === 'batchDelete') batchDelete();
      else if (action === 'clearSelection') clearSelection();
    });

    document.querySelector('.reader').addEventListener('click', e => {
      const back = e.target.closest('[data-action="showList"]');
      if (back) showList();
      const renameBtn = e.target.closest('[data-action="renameReaderNote"]');
      if (renameBtn) {
        const file = document.getElementById('reader').dataset.file || '';
        if (file) {
          const note = allNotes.find(n => n.file === file);
          showRenameModal(file, !!(note && note.is_symlink), note ? (note.source_label || '') : '');
        }
      }
      const copyBtn = e.target.closest('[data-action="copyReaderPath"]');
      if (copyBtn) copyReaderPath();
      const revealBtn = e.target.closest('[data-action="revealReaderFile"]');
      if (revealBtn) revealReaderFile();
      const openBtn = e.target.closest('[data-action="openReaderFile"]');
      if (openBtn) openReaderFile();
      const editBtn = e.target.closest('[data-action="editNote"]');
      if (editBtn) {
        const file = document.getElementById('reader').dataset.file || '';
        if (file) openEditor(file);
      }
    });

    document.getElementById('modal-overlay').addEventListener('click', e => {
      if (e.target.id === 'modal-overlay') closeModal();
    });
    document.getElementById('modal').addEventListener('click', e => {
      e.stopPropagation();
    });

    document.getElementById('discover-list').addEventListener('click', e => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      const candidateId = btn.dataset.candidateId;
      if (action === 'discoverAdd') discoverAdd(candidateId);
      else if (action === 'discoverIgnore') discoverIgnore(candidateId, btn.dataset.index);
    });

    document.getElementById('view-modes').addEventListener('click', e => {
      const btn = e.target.closest('.view-mode-btn');
      if (!btn) return;
      setViewMode(btn.dataset.viewMode);
    });

    document.getElementById('favorites-section').addEventListener('click', e => {
      const header = e.target.closest('[data-action="toggleFavorites"]');
      if (header) toggleFavorites();
    });

    document.querySelector('.mobile-menu-btn').addEventListener('click', () => {
      document.getElementById('sidebar').classList.add('open');
      document.getElementById('sidebar-overlay').classList.add('show');
    });

    document.getElementById('sidebar-overlay').addEventListener('click', () => {
      document.getElementById('sidebar').classList.remove('open');
      document.getElementById('sidebar-overlay').classList.remove('show');
    });

    document.getElementById('modal-actions').addEventListener('click', e => {
      const btn = e.target.closest('button[data-modal-action]');
      if (!btn) return;
      const idx = parseInt(btn.dataset.modalAction, 10);
      const actions = document.getElementById('modal-actions')._actions;
      if (actions && actions[idx]) {
        actions[idx].action();
      }
    });
  </script>
</body>
</html>"""


def run_server(port: Optional[int] = None, notes_dir: Optional[str] = None):
    if notes_dir:
        os.environ["NOTED_HOME"] = notes_dir
    if port:
        os.environ["NOTED_PORT"] = str(port)

    notes_dir = get_notes_dir()
    port = get_port()

    os.makedirs(notes_dir, exist_ok=True)
    trash_dir = os.path.join(notes_dir, ".trash")
    os.makedirs(trash_dir, exist_ok=True)

    try:
        sync_links(notes_dir)
    except Exception:
        pass

    try:
        with ServerClass(("127.0.0.1", port), Handler) as httpd:
            print(f"落笔 · Noted 已启动：http://localhost:{port}")
            print(f"笔记目录：{notes_dir}")
            print("Ctrl+C 停止服务")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n服务已停止")
    except OSError as e:
        if e.errno == 48:
            print(f"错误：端口 {port} 已被占用，请更换端口或停止占用进程。", file=sys.stderr)
            sys.exit(1)
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="落笔 · Noted 本地网页服务")
    parser.add_argument("--notes-dir", help="笔记目录路径，默认 ~/ai-notes", default=None)
    parser.add_argument("--port", type=int, help=f"服务端口，默认 {DEFAULT_PORT}", default=None)
    args = parser.parse_args()
    run_server(port=args.port, notes_dir=args.notes_dir)