"""Pin vendor push deliveries to a public DNS result while retaining TLS hostname checks."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

import requests


def resolve(endpoint):
    uri = urlsplit(endpoint)
    allowed = [
        "jmt17.google.com",
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
        "notify.windows.com",
    ]
    if (
        uri.scheme != "https"
        or uri.port not in (None, 443)
        or uri.username
        or uri.password
        or not uri.hostname
    ):
        raise ValueError("invalid endpoint")
    if not any(uri.hostname == h or uri.hostname.endswith("." + h) for h in allowed):
        raise ValueError("unpermitted endpoint")
    addresses = socket.getaddrinfo(uri.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or not all(
        ipaddress.ip_address(a[4][0]).is_global for a in addresses
    ):
        raise ValueError("private endpoint")
    return uri, addresses[0][4][0]


class PinnedSession:
    def post(self, endpoint, data=None, headers=None, timeout=7, **kwargs):
        uri, address = resolve(endpoint)
        # Direct TCP socket: proxy environment variables cannot cause a second DNS lookup.
        raw = socket.create_connection((address, 443), timeout=timeout)
        raw = ssl.create_default_context().wrap_socket(
            raw, server_hostname=uri.hostname
        )
        connection = http.client.HTTPConnection(uri.hostname, 443, timeout=timeout)
        connection.sock = raw
        try:
            headers = dict(headers or {})
            headers["Host"] = uri.hostname
            connection.request(
                "POST",
                uri.path + ("?" + uri.query if uri.query else "") or "/",
                body=data,
                headers=headers,
            )
            reply = connection.getresponse()
            response = requests.Response()
            response.status_code = reply.status
            response.reason = reply.reason
            response.headers = requests.structures.CaseInsensitiveDict(
                reply.getheaders()
            )
            response._content = reply.read(1024 * 1024)
            response.url = endpoint
            return response
        finally:
            connection.close()
