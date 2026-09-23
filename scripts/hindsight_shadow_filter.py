#!/usr/bin/env python3
"""Log-only reverse proxy in front of reviewer Hindsight recalls.

Forwards each request to the upstream and returns the upstream status,
headers, and body unchanged. Screens only a JSON-RPC ``tools/call`` for a
fixed tool set and appends one JSONL judgment. Passage text never enters
the log. Screening and logging are fail-open: the client still receives
the upstream bytes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

logger = logging.getLogger("hindsight_shadow_filter")

DEFAULT_LOG_PATH = Path.home() / ".omnigent" / "logs" / "hindsight-shadow-filter.jsonl"
MAX_SCREEN_BYTES = 2_000_000

SCREEN_TOOLS = frozenset(
    {
        "recall",
        "reflect",
        "search_knowledge_base",
        "list_memories",
        "get_memory",
        "get_document",
        "get_knowledge_page",
    }
)

_PASSAGE_KEYS = frozenset(
    {"text", "content", "passage", "memory", "description", "observation"}
)

_RECORD_KEYS = (
    "ts",
    "tool",
    "keep",
    "injection",
    "confidence",
    "duration_ms",
    "fail_open",
    "n",
)

# Case-insensitive phrase checks. Whitespace is flexible; the optional
# ignore-quantifier does not require a double space when it is absent.
_INJECTION_PATTERNS = (
    re.compile(
        r"\bignore(?:\s+(?:all|any|the))?\s+(?:previous|prior|above)\s+(?:instructions|prompts)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdisregard(?:\s+the)?\s+(?:ticket|diff|contract|review)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\b(?:must|should)\s+(?:pass|block)\b", re.IGNORECASE),
    re.compile(r"\bverdict:\s*(?:pass|block)\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+(?:report|block|flag)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions:", re.IGNORECASE),
    re.compile(r"\bas\s+the\s+reviewer,\s*you\s+must\b", re.IGNORECASE),
)

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
    }
)

_APPEND_LOCK = threading.Lock()
_UPSTREAM_TIMEOUT_S = 30


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def parse_listen(spec: str) -> tuple[str, int]:
    """Return ``(host, port)``. Only ``127.0.0.1`` is accepted."""
    host, sep, port_s = spec.rpartition(":")
    if sep != ":" or host != "127.0.0.1":
        raise SystemExit("listen address must be 127.0.0.1")
    try:
        port = int(port_s)
    except ValueError:
        raise SystemExit("listen port must be an integer") from None
    if port < 1 or port > 65535:
        raise SystemExit("listen port out of range")
    return host, port


def _upstream_endpoint(upstream: str) -> tuple[str, str, int, str]:
    parts = urlsplit(upstream)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SystemExit(f"unsupported upstream: {upstream}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host_header = parts.netloc or parts.hostname
    return parts.scheme, parts.hostname, port, host_header


def passage_is_injection(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


def score_passages(passages: list[str]) -> dict:
    """Judge passages. No passage text is copied into the result."""
    n = len(passages)
    if n == 0:
        return {"keep": True, "injection": False, "confidence": 0, "n": 0}
    injection = any(passage_is_injection(passage) for passage in passages)
    return {
        "keep": not injection,
        "injection": injection,
        "confidence": 0.9 if injection else 0.2,
        "n": n,
    }


def _take_passage(value, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item)
            else:
                _walk(item, out)
        return
    if isinstance(value, dict):
        _walk(value, out)


def _walk(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _PASSAGE_KEYS:
                _take_passage(value, out)
            else:
                _walk(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def _json_documents(body: bytes) -> list:
    # json.loads on bytes sniffs a UTF-16 BOM and raises UnicodeDecodeError
    # for binary that is not JSON. That is an empty walk, not a scorer failure.
    try:
        return [json.loads(body)]
    except (json.JSONDecodeError, UnicodeError):
        pass
    docs = []
    for line in body.splitlines():
        if not line.startswith(b"data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == b"[DONE]":
            continue
        try:
            docs.append(json.loads(raw))
        except (json.JSONDecodeError, UnicodeError):
            continue
    return docs


def extract_passages(body: bytes) -> list[str]:
    passages: list[str] = []
    for doc in _json_documents(body):
        _walk(doc, passages)
    return passages


def screen_response(body: bytes) -> dict:
    """Return the judgment fields. Does not include passage text.

    Bodies larger than ``MAX_SCREEN_BYTES`` are not walked.
    """
    if len(body) > MAX_SCREEN_BYTES:
        return {
            "keep": True,
            "injection": False,
            "confidence": 0,
            "n": 0,
            "fail_open": True,
        }
    judged = dict(score_passages(extract_passages(body)))
    judged["fail_open"] = False
    return judged


def screened_tool_name(body: bytes) -> str | None:
    """First screened ``tools/call`` name, or None when this request is not screened."""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeError):
        return None
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            continue
        params = message.get("params")
        if not isinstance(params, dict):
            continue
        name = params.get("name")
        if isinstance(name, str) and name in SCREEN_TOOLS:
            return name
    return None


def _record(tool: str, judged: dict, duration_ms: float) -> dict:
    return {
        "ts": _utc_now(),
        "tool": tool,
        "keep": bool(judged["keep"]),
        "injection": bool(judged["injection"]),
        "confidence": judged["confidence"],
        "duration_ms": duration_ms,
        "fail_open": bool(judged["fail_open"]),
        "n": int(judged["n"]),
    }


def _fail_open_record(tool: str, duration_ms: float) -> dict:
    return {
        "ts": _utc_now(),
        "tool": tool,
        "keep": True,
        "injection": False,
        "confidence": 0,
        "duration_ms": duration_ms,
        "fail_open": True,
        "n": 0,
    }


def append_record(log_path: Path, record: dict) -> None:
    """Append one JSON object. Only the judgment keys are written."""
    safe = {key: record[key] for key in _RECORD_KEYS}
    line = json.dumps(safe, separators=(",", ":"), ensure_ascii=True)
    if "\n" in line or "\r" in line:
        raise ValueError("shadow log record must be a single line")
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line + "\n").encode("utf-8")
    with _APPEND_LOCK:
        with path.open("ab") as handle:
            handle.write(data)
            handle.flush()


def _append_fail_open(log_path: Path, tool: str, duration_ms: float) -> None:
    try:
        append_record(log_path, _fail_open_record(tool, duration_ms))
    except Exception:
        logger.exception("hindsight shadow fail-open log append failed")


def _screen_and_log(log_path: Path, tool: str, body: bytes) -> None:
    started = time.perf_counter()
    try:
        judged = screen_response(body)
    except Exception:
        logger.exception("hindsight shadow screen failed")
        _append_fail_open(log_path, tool, _elapsed_ms(started))
        return
    record = _record(tool, judged, _elapsed_ms(started))
    try:
        append_record(log_path, record)
    except Exception:
        logger.exception("hindsight shadow log append failed")
        record["fail_open"] = True
        record["ts"] = _utc_now()
        try:
            append_record(log_path, record)
        except Exception:
            logger.exception("hindsight shadow fail-open log append failed")


def _header_items(headers) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if hasattr(headers, "items"):
        return [(str(key), str(value)) for key, value in headers.items()]
    return [(str(key), str(value)) for key, value in headers]


def _filter_headers(headers, *, drop_extra: Iterable[str] = ()) -> list[tuple[str, str]]:
    items = _header_items(headers)
    hop = set(_HOP_BY_HOP)
    hop.update(name.lower() for name in drop_extra)
    for key, value in items:
        if key.lower() == "connection":
            hop.update(part.strip().lower() for part in value.split(",") if part.strip())
    return [(key, value) for key, value in items if key.lower() not in hop]


def _request_headers(headers, host_header: str) -> dict[str, str]:
    forwarded = _filter_headers(headers, drop_extra=("host", "content-length"))
    out = {key: value for key, value in forwarded}
    out["Host"] = host_header
    return out


def _response_headers(headers, body: bytes) -> list[tuple[str, str]]:
    forwarded = _filter_headers(headers, drop_extra=("content-length",))
    forwarded.append(("Content-Length", str(len(body))))
    return forwarded


def forward_upstream(
    upstream: str,
    method: str,
    path: str,
    headers,
    body: bytes,
) -> tuple[int, str, list[tuple[str, str]], bytes]:
    """Forward one request. Raises on connection failure. Body is unread-modified."""
    scheme, hostname, port, host_header = _upstream_endpoint(upstream)
    connection_cls = HTTPSConnection if scheme == "https" else HTTPConnection
    connection = connection_cls(hostname, port, timeout=_UPSTREAM_TIMEOUT_S)
    try:
        connection.request(
            method,
            path,
            body=body,
            headers=_request_headers(headers, host_header),
        )
        response = connection.getresponse()
        resp_body = response.read()
        reason = response.reason or ""
        if isinstance(reason, bytes):
            reason = reason.decode("latin-1", "replace")
        resp_headers = _response_headers(response.getheaders(), resp_body)
        return response.status, str(reason), resp_headers, resp_body
    finally:
        connection.close()


def _connection_error_response(exc: BaseException) -> tuple[int, str, list[tuple[str, str]], bytes]:
    detail = f"{type(exc).__name__}: {exc}"[:300]
    payload = detail.encode("utf-8", "replace")
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(payload))),
    ]
    return 502, "Bad Gateway", headers, payload


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    raw = handler.headers.get("Content-Length")
    if raw is None or raw == "":
        return b""
    length = int(raw)
    if length < 0:
        raise ValueError("negative Content-Length")
    if length == 0:
        return b""
    return handler.rfile.read(length)


def _send(
    handler: BaseHTTPRequestHandler,
    status: int,
    reason: str,
    headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    handler.send_response(status, reason)
    for key, value in headers:
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def handle_proxied(handler: BaseHTTPRequestHandler, upstream: str, log_path: Path) -> None:
    """Proxy one request. Screening never changes the bytes sent to the client."""
    try:
        req_body = _read_request_body(handler)
    except Exception:
        logger.exception("hindsight shadow failed to read the request body")
        message = b"bad request"
        _send(
            handler,
            400,
            "Bad Request",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(message))),
            ],
            message,
        )
        return

    try:
        tool = screened_tool_name(req_body)
    except Exception:
        logger.exception("hindsight shadow failed to read the tool name")
        tool = None

    try:
        status, reason, resp_headers, resp_body = forward_upstream(
            upstream,
            handler.command,
            handler.path,
            handler.headers,
            req_body,
        )
    except (OSError, HTTPException, TimeoutError) as exc:
        logger.exception("hindsight shadow upstream connection failed")
        if tool is not None:
            _append_fail_open(log_path, tool, 0)
        status, reason, resp_headers, resp_body = _connection_error_response(exc)
        _send(handler, status, reason, resp_headers, resp_body)
        return
    except Exception as exc:
        logger.exception("hindsight shadow upstream forward failed")
        if tool is not None:
            _append_fail_open(log_path, tool, 0)
        status, reason, resp_headers, resp_body = _connection_error_response(exc)
        _send(handler, status, reason, resp_headers, resp_body)
        return

    if tool is not None:
        _screen_and_log(log_path, tool, resp_body)
    _send(handler, status, reason, resp_headers, resp_body)


class _ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _handler_class(upstream: str, log_path: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _proxy(self):
            handle_proxied(self, upstream, log_path)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _proxy

        def log_message(self, fmt, *args):
            logger.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def start_proxy(
    upstream: str,
    log_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[_ProxyServer, threading.Thread]:
    """Bind ``127.0.0.1`` and serve until ``httpd.shutdown()``."""
    if host != "127.0.0.1":
        raise ValueError("listen address must be 127.0.0.1")
    _upstream_endpoint(upstream)
    httpd = _ProxyServer((host, port), _handler_class(upstream, Path(log_path)))
    bound_host = httpd.server_address[0]
    if bound_host != "127.0.0.1":
        httpd.server_close()
        raise RuntimeError(f"refusing bind on {bound_host}")
    ready = threading.Event()

    def _run() -> None:
        ready.set()
        httpd.serve_forever(poll_interval=0.1)

    thread = threading.Thread(target=_run, name="hindsight-shadow-filter", daemon=True)
    thread.start()
    if not ready.wait(2):
        httpd.server_close()
        raise RuntimeError("shadow filter failed to start")
    return httpd, thread


def stop_proxy(httpd: _ProxyServer, thread: threading.Thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Log-only reverse proxy for reviewer Hindsight recalls.",
        epilog=(
            "Example: python3 scripts/hindsight_shadow_filter.py "
            "--listen 127.0.0.1:8899 --upstream http://127.0.0.1:8888 "
            "--log ~/.omnigent/logs/hindsight-shadow-filter.jsonl"
        ),
    )
    parser.add_argument("--listen", required=True, help="127.0.0.1:port")
    parser.add_argument("--upstream", required=True, help="origin, e.g. http://127.0.0.1:8888")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH), help="JSONL judgment path")
    args = parser.parse_args(argv)
    host, port = parse_listen(args.listen)
    log_path = Path(args.log).expanduser()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    httpd, thread = start_proxy(args.upstream, log_path, host, port)
    logger.info(
        "listening on 127.0.0.1:%s -> %s log %s",
        httpd.server_address[1],
        args.upstream,
        log_path,
    )
    try:
        thread.join()
    except KeyboardInterrupt:
        logger.info("hindsight shadow filter stopping")
    finally:
        stop_proxy(httpd, thread)


if __name__ == "__main__":
    main()
