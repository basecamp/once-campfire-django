"""Native gzip framing with deterministic bytes and explicit quality exclusions."""

from django.middleware.gzip import GZipMiddleware
from django.utils.cache import patch_vary_headers


def accepts_gzip(header):
    qualities = {}
    for field in header.split(","):
        coding, *parameters = field.strip().lower().split(";")
        quality = 1.0
        for parameter in parameters:
            name, separator, value = parameter.strip().partition("=")
            if name == "q":
                try:
                    quality = float(value) if separator else 0.0
                except ValueError:
                    quality = 0.0
        coding = coding.strip()
        quality = quality if 0.0 <= quality <= 1.0 else 0.0
        qualities[coding] = min(qualities.get(coding, quality), quality)
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))
    return gzip_quality > 0.0 and gzip_quality >= qualities.get("identity", 0.0)


class CompressionMiddleware(GZipMiddleware):
    # Token-free HTML has no per-render CSRF secret requiring random gzip padding.
    max_random_bytes = 0

    def process_response(self, request, response):
        if not accepts_gzip(request.headers.get("Accept-Encoding", "")):
            if not response.has_header("Content-Encoding") and (
                response.streaming or len(response.content) >= 200
            ):
                patch_vary_headers(response, ["Accept-Encoding"])
            return response
        return super().process_response(request, response)
