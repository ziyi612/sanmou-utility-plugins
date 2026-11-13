"""Restricted stdio transport for the official Quark authorization-page POST.

Headers and payload stay in pipes. TLS verification is enabled and redirects
are refused. stdout is a private response envelope consumed by the Node preload.
"""
import base64
import json
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def run():
    source = json.loads(sys.stdin.read())
    url = urllib.parse.urlsplit(source['url'])
    if (url.scheme != 'https' or url.hostname != 'open-api-drive.quark.cn'
            or url.port not in (None, 443) or url.username or url.password
            or url.path != '/agent/v1/get_authorize_page_url'
            or source.get('method') != 'POST'):
        return {'transport_error': 'ENDPOINT_NOT_ALLOWED'}
    payload = base64.b64decode(source.get('body_base64', ''), validate=True)
    headers = source.get('headers', {})
    if not isinstance(headers, dict):
        return {'transport_error': 'INVALID_HEADERS'}
    timeout = min(max(float(source.get('timeout_seconds', 45)), 1), 45)
    request = urllib.request.Request(source['url'], data=payload, headers=headers, method='POST')
    opener = urllib.request.build_opener(NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    except (TimeoutError, socket.timeout):
        return {'transport_error': 'ETIMEDOUT'}
    except urllib.error.URLError as error:
        return {'transport_error': 'ETIMEDOUT' if isinstance(error.reason, (TimeoutError, socket.timeout)) else 'NETWORK_ERROR'}
    with response:
        return {'status': response.code, 'headers': list(response.headers.raw_items()),
                'body_base64': base64.b64encode(response.read()).decode('ascii')}


if __name__ == '__main__':
    try:
        result = run()
    except Exception:
        result = {'transport_error': 'BRIDGE_INPUT_OR_RESPONSE_ERROR'}
    print(json.dumps(result), flush=True)
