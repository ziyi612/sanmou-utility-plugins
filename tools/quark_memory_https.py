"""Restricted HTTPS transport over private pipes; never writes report data.

The official SDK still owns URLs, signatures, headers, account state and API
semantics. This helper changes only the TLS/HTTP implementation for allowlisted
HTTPS:443 requests. It verifies certificates and refuses automatic redirects.
"""
from __future__ import annotations

import base64
import errno
import http.client
import json
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 64 * 1024 * 1024
MAX_ENVELOPE = MAX_BYTES * 4 // 3 + 2 * 1024 * 1024


def phase(name, **values):
    """Private pipe metadata only; never include request or response values."""
    record = {"kind": "quark-https-phase", "phase": name}
    for key in ("status", "content_length", "bytes"):
        value = values.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_ENVELOPE:
            record[key] = value
    sys.stderr.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def allowed(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        return (parsed.scheme == "https" and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and (host == "open-api-drive.quark.cn" or host.endswith(".open-drive.quark.cn")))
    except (ValueError, TypeError):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class VerifiedHTTPS(urllib.request.HTTPSHandler):
    """Keep explicit SDK header values, including Connection, unchanged."""

    def __init__(self, context):
        super().__init__(context=context)
        self.context = context

    def https_open(self, request):
        connection = http.client.HTTPSConnection(request.host, timeout=request.timeout, context=self.context)
        headers = getattr(request, "_quark_headers", request.header_items())
        names = {name.lower() for name, _ in headers}
        body = request.data
        try:
            phase("request-started")
            connection.putrequest(request.get_method(), request.selector,
                                  skip_host="host" in names, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            if body is not None and "content-length" not in names and "transfer-encoding" not in names:
                connection.putheader("Content-Length", str(len(body)))
            chunked = any(name.lower() == "transfer-encoding" and "chunked" in value.lower() for name, value in headers)
            connection.endheaders(body, encode_chunked=chunked)
            phase("request-sent", bytes=len(body) if body is not None else 0)
            response = connection.getresponse()
            response.url = request.full_url
            response.msg = response.reason
            response._quark_connection = connection
            return response
        except BaseException:
            connection.close()
            raise


def category(error):
    if isinstance(error, urllib.error.URLError):
        return category(error.reason)
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS_VERIFICATION_FAILED"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "ETIMEDOUT"
    if isinstance(error, ConnectionResetError):
        return "ECONNRESET"
    if isinstance(error, ConnectionAbortedError):
        return "ECONNABORTED"
    if isinstance(error, socket.gaierror):
        return "EAI_AGAIN" if error.errno == getattr(socket, "EAI_AGAIN", None) else "DNS_ERROR"
    if isinstance(error, ssl.SSLError):
        return "EPROTO"
    if isinstance(error, OSError):
        return {errno.ECONNRESET: "ECONNRESET", errno.ECONNABORTED: "ECONNABORTED", errno.ETIMEDOUT: "ETIMEDOUT"}.get(error.errno, "NETWORK_ERROR")
    return "NETWORK_ERROR"


def exchange(source):
    url = source.get("url")
    if not isinstance(url, str) or not allowed(url):
        return {"transport_error": "ENDPOINT_NOT_ALLOWED"}
    method = source.get("method", "GET")
    if method not in {"GET", "POST", "PUT", "HEAD", "DELETE", "PATCH", "OPTIONS"}:
        return {"transport_error": "METHOD_NOT_ALLOWED"}
    headers = source.get("headers", {})
    if not isinstance(headers, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()):
        return {"transport_error": "INVALID_HEADERS"}
    try:
        payload = base64.b64decode(source.get("body_base64", ""), validate=True)
    except (ValueError, TypeError):
        return {"transport_error": "INVALID_BODY"}
    header_bytes = sum(len(key.encode()) + len(value.encode()) for key, value in headers.items())
    if len(payload) + header_bytes + len(url.encode()) > MAX_BYTES:
        return {"transport_error": "MEMORY_LIMIT"}
    try:
        timeout = min(max(float(source.get("timeout_seconds", 60)), 0.001), 900)
        context = ssl.create_default_context()
        # Do not inherit a proxy behind the official SDK's back. The selected
        # transport targets precisely the allowlisted HTTPS host directly.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), VerifiedHTTPS(context))
        opener.addheaders = []
        body = payload if source.get("has_body", False) or payload else None
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        request._quark_headers = list(headers.items())
        try:
            phase("opening")
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        try:
            pairs = list(response.headers.raw_items())
            response_headers = sum(len(key.encode()) + len(value.encode()) for key, value in pairs)
            maximum = MAX_BYTES - response_headers
            if maximum < 0:
                return {"transport_error": "MEMORY_LIMIT"}
            declared = response.headers.get("Content-Length")
            phase("response-headers", status=response.code,
                  content_length=int(declared) if declared and declared.isdigit() else None)
            if declared is not None and declared.isdigit() and int(declared) > maximum:
                return {"transport_error": "MEMORY_LIMIT"}
            received = response.read(maximum + 1)
            phase("response-body", bytes=len(received))
            if len(received) > maximum:
                return {"transport_error": "MEMORY_LIMIT"}
            return {"status": response.code, "status_message": str(getattr(response, "reason", "")),
                    "headers": pairs, "body_base64": base64.b64encode(received).decode("ascii")}
        finally:
            response.close()
            connection = getattr(response, "_quark_connection", None)
            if connection is not None:
                connection.close()
    except Exception as error:
        return {"transport_error": category(error)}


def main():
    phase("stdin-reading")
    raw = sys.stdin.buffer.read(MAX_ENVELOPE + 1)
    phase("stdin-complete", bytes=len(raw))
    if len(raw) > MAX_ENVELOPE:
        result = {"transport_error": "MEMORY_LIMIT"}
    else:
        try:
            result = exchange(json.loads(raw))
        except Exception:
            result = {"transport_error": "INVALID_REQUEST"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.flush()
    phase("stdout-complete")


if __name__ == "__main__":
    main()
