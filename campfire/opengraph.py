"""Fetch public HTTP metadata using a pinned, validated IP on every redirect."""

import http.client
import ipaddress
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from django.http import HttpResponse, JsonResponse

from .views import login_required, params


class Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = {}
        self.title = False
        self.title_text = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = attrs.get("property") or attrs.get("name")
            if name and attrs.get("content"):
                self.metadata[name] = attrs["content"]
        if tag == "title":
            self.title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.title = False

    def handle_data(self, data):
        if self.title:
            self.title_text += data


def fetch(url):
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("invalid URL")
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if not addresses or not all(
            ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise ValueError("private address")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        raw = socket.create_connection((addresses[0][4][0], port), timeout=5)
        if parsed.scheme == "https":
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=parsed.hostname
            )
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=5)
        connection.sock = raw
        try:
            connection.request(
                "GET",
                parsed.path + ("?" + parsed.query if parsed.query else "") or "/",
                headers={
                    "Host": parsed.netloc,
                    "User-Agent": "Campfire",
                    "Accept": "text/html",
                },
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader("Location", ""))
                continue
            if response.status != 200 or "text/html" not in response.getheader(
                "Content-Type", ""
            ):
                raise ValueError("no metadata")
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("metadata too large")
            document = Document()
            document.feed(raw.decode("utf-8", errors="replace"))
            title = document.metadata.get("og:title") or document.title_text
            description = document.metadata.get(
                "og:description"
            ) or document.metadata.get("description", "")
            return {
                "url": url,
                "title": title,
                "description": description,
                "image": urljoin(url, document.metadata["og:image"])
                if document.metadata.get("og:image")
                else "",
                "site_name": document.metadata.get("og:site_name", parsed.hostname),
            }
        finally:
            connection.close()
    raise ValueError("too many redirects")


@login_required
def unfurl(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        data = fetch(params(request).get("url", ""))
        from .richtext import plain_text

        data["title"] = plain_text(data["title"])
        data["description"] = plain_text(data["description"])
        if not data["title"] or not data["description"]:
            return HttpResponse(status=204)
        return JsonResponse(data)
    except (ValueError, OSError, http.client.HTTPException):
        return HttpResponse(status=204)
