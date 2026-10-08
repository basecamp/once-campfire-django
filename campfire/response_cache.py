"""Bounded immutable responses after fresh authorization and SQLite epoch checks."""

import gzip
import json
import sqlite3
import threading
from collections import OrderedDict
from copy import deepcopy
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.middleware.gzip import re_accepts_gzip
from django.utils.cache import patch_vary_headers

from .domain import get_room


class ResponseCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.bytes = 0
        self.observer = None
        self.database = None
        self.version = None

    def clear(self):
        with self.lock:
            self._clear()
            if self.observer is not None:
                self.observer.close()
            self.observer = self.database = self.version = None

    def _clear(self):
        self.entries.clear()
        self.bytes = 0

    def _version(self):
        database = str(connection.settings_dict["NAME"])
        # PRAGMA data_version is comparable only on one persistent connection.
        # That connection never writes, so it sees local and foreign commits.
        if database != self.database or self.observer is None:
            self._clear()
            if self.observer is not None:
                self.observer.close()
            self.observer = None
            self.observer = sqlite3.connect(
                Path(database).resolve().as_uri() + "?mode=ro",
                uri=True,
                check_same_thread=False,
                timeout=0,
            )
            self.database = database
            self.version = None
        version = self.observer.execute("PRAGMA data_version").fetchone()[0]
        if version != self.version:
            self._clear()
            self.version = version
        return database, version

    def get(self, key):
        with self.lock:
            try:
                version = self._version()
            except sqlite3.Error:
                self._clear()
                return None, None
            return version, self.entries.get(key)

    def version_for_request(self):
        with self.lock:
            try:
                return self._version()
            except sqlite3.Error:
                self._clear()
                return None

    def put(self, key, version, entry, budget):
        size = (
            len(key)
            + len(entry[0])
            + sum(len(name) + len(value) for name, value in entry[1])
            + 256
        )
        if size > min(budget, 1024 * 1024):
            return
        with self.lock:
            try:
                if self._version() != version:
                    return
            except sqlite3.Error:
                self._clear()
                return
            if key in self.entries:
                return
            while self.entries and self.bytes + size > budget:
                _, (_, old_size) = self.entries.popitem(last=False)
                self.bytes -= old_size
            self.entries[key] = entry, size
            self.bytes += size


cache = ResponseCache()


def cached_page(scope):
    """Cache only the four audited read actions, after fresh room authorization."""

    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            budget = settings.RESPONSE_CACHE_BYTES
            if (
                budget <= 0
                or request.method != "GET"
                or request.META.get("REQUEST_METHOD") != "GET"
                or request.current_session is None
                or request.authenticated_by_bot
                or request.session_data.get("flash")
                or connection.in_atomic_block
                or (scope == "messages" and (kwargs.get("id") or kwargs.get("bot_key")))
            ):
                return view(request, *args, **kwargs)

            room = None
            if scope in ("room", "messages"):
                room_id = kwargs.get("id" if scope == "room" else "room_id")
                if room_id is None:
                    return view(request, *args, **kwargs)
                room = get_room(request.current_user, room_id)
                if room is None:
                    return view(request, *args, **kwargs)

            key = json.dumps(
                [
                    scope,
                    request.current_user.id,
                    request.current_session.token,
                    request.session_data,
                    request.scheme,
                    request.get_host(),
                    request.get_full_path(),
                    request.COOKIES,
                    request.headers.get("Accept", ""),
                    request.headers.get("User-Agent", ""),
                    request.headers.get("Turbo-Frame", ""),
                    request.headers.get("Accept-Encoding", ""),
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if len(key) > 2048:
                return view(request, *args, **kwargs)

            version, stored = cache.get(key)
            # Authentication captured model fields before reaching this view.
            # A commit during that interval must not admit them under a newer epoch.
            if version != getattr(request, "response_cache_version", None):
                return view(request, *args, **kwargs)
            if stored is not None:
                (body, headers), _ = stored
                if scope == "room":
                    request.last_room = room.id
                response = HttpResponse(
                    body,
                    headers=dict(headers),
                )
                return response

            original_session = deepcopy(request.session_data)
            response = view(request, *args, **kwargs)
            if response.streaming:
                return response
            # Store the selected complete representation once; the outer gzip
            # middleware leaves already encoded responses unchanged.
            if len(response.content) >= 200 and not response.has_header(
                "Content-Encoding"
            ):
                patch_vary_headers(response, ["Accept-Encoding"])
                if re_accepts_gzip.search(request.headers.get("Accept-Encoding", "")):
                    encoded = gzip.compress(response.content, compresslevel=6, mtime=0)
                    if len(encoded) < len(response.content):
                        response.content = encoded
                        response["Content-Encoding"] = "gzip"
                        response["Content-Length"] = len(encoded)
                        if response.has_header("ETag") and not response[
                            "ETag"
                        ].startswith("W/"):
                            response["ETag"] = "W/" + response["ETag"]
            if (
                version is not None
                and response.status_code == 200
                and response.get("Content-Type", "").startswith("text/html")
                and not response.cookies
                and request.session_data == original_session
            ):
                entry = response.content, tuple(response.items())
                cache.put(key, version, entry, budget)
            return response

        return wrapped

    return decorate
