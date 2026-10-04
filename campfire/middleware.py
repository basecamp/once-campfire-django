import secrets
from datetime import timedelta
from io import BytesIO

from django.http import HttpResponse

from . import rails
from .context import request_host
from .models import Ban, Session, User, now


class SessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.session_data = {}
        encrypted = request.COOKIES.get("_campfire_session")
        if encrypted:
            try:
                request.session_data = rails.decrypt_cookie(
                    "_campfire_session", encrypted
                )
            except Exception:
                pass
        if not isinstance(request.session_data, dict):
            request.session_data = {}
        try:
            if (
                "_csrf_token" in request.session_data
                and len(rails.decode64(request.session_data["_csrf_token"])) != 32
            ):
                request.session_data.pop("_csrf_token")
        except (ValueError, TypeError):
            request.session_data.pop("_csrf_token", None)
        request.session_original = request.session_data.copy()
        request.session_data.setdefault("session_id", secrets.token_hex(16))
        request.session_data.setdefault(
            "_csrf_token", rails.b64(secrets.token_bytes(32))
        )
        request.current_user = request.current_session = None
        raw = request.COOKIES.get("session_token")
        if raw:
            try:
                token = rails.verify_cookie("session_token", raw)
                session = (
                    Session.objects.select_related("user")
                    .filter(token=token, user__status=0)
                    .first()
                )
                if session:
                    request.current_session, request.current_user = (
                        session,
                        session.user,
                    )
                    if session.last_active_at < now() - timedelta(hours=1):
                        Session.objects.filter(id=session.id).update(
                            last_active_at=now(),
                            updated_at=now(),
                            user_agent=request.headers.get("User-Agent", ""),
                            ip_address=request.META.get("REMOTE_ADDR"),
                        )
            except (ValueError, TypeError):
                pass
        request.authenticated_by_bot = False
        pieces = request.path.strip("/").split("/")
        bot_key = request.GET.get("bot_key") or (
            pieces[2]
            if len(pieces) > 3 and pieces[0] == "rooms" and pieces[3] == "messages"
            else ""
        )
        if bot_key and not request.current_user:
            try:
                bot_id, token = bot_key.strip().split("-", 1)
                bot = User.objects.filter(
                    id=bot_id, bot_token=token, status=0, role=2
                ).first()
                if bot:
                    request.current_user = bot
                    request.authenticated_by_bot = True
            except (ValueError, TypeError):
                pass
        request.csrf_token = rails.mask_csrf(
            rails.decode64(request.session_data["_csrf_token"])
        )
        host_token = request_host.set(request.get_host().split(":")[0])
        try:
            response = self.get_response(request)
        finally:
            request_host.reset(host_token)
        if request.session_data != request.session_original:
            response.set_cookie(
                "_campfire_session",
                rails.encrypt_cookie(
                    "_campfire_session",
                    request.session_data,
                    now() + timedelta(days=20 * 365),
                ),
                max_age=20 * 365 * 86400,
                httponly=True,
                samesite="Lax",
                secure=request.is_secure(),
            )
        if hasattr(request, "new_session_token"):
            response.set_cookie(
                "session_token",
                rails.sign_cookie(
                    "session_token",
                    request.new_session_token,
                    now() + timedelta(days=20 * 365),
                ),
                max_age=20 * 365 * 86400,
                httponly=True,
                samesite="Lax",
                secure=request.is_secure(),
            )
        if hasattr(request, "last_room"):
            response.set_cookie(
                "last_room",
                request.last_room,
                max_age=20 * 365 * 86400,
                samesite="Lax",
                secure=request.is_secure(),
            )
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "SAMEORIGIN"
        response["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


class SecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if Ban.objects.filter(ip_address=request.META.get("REMOTE_ADDR", "")).exists():
            return HttpResponse(status=403)
        if (
            request.method in ("PATCH", "PUT")
            and request.content_type == "multipart/form-data"
        ):
            from django.http.multipartparser import MultiPartParser

            request._post, request._files = MultiPartParser(
                request.META,
                BytesIO(request.body),
                request.upload_handlers,
                request.encoding,
            ).parse()
        if (
            not request.path.startswith(("/assets/", "/rails/active_storage/"))
            and request.path.endswith((".json", ".turbo_stream"))
            and not request.path.startswith("/webmanifest")
        ):
            suffix = request.path.rsplit(".", 1)[1]
            request.path_info = request.path_info[: -(len(suffix) + 1)]
        if request.method == "POST" and request.POST.get("_method", "").upper() in (
            "PATCH",
            "PUT",
            "DELETE",
        ):
            request.method = request.POST["_method"].upper()
        pieces = request.path.strip("/").split("/")
        bot_route = (
            request.authenticated_by_bot
            and len(pieces) >= 4
            and pieces[0] == "rooms"
            and pieces[3] == "messages"
        )
        disk_upload = False
        if request.method == "PUT" and request.path.startswith(
            "/rails/active_storage/disk/"
        ):
            try:
                payload = rails.verify(
                    request.path.rsplit("/", 1)[1], "ActiveStorage", "blob_token"
                )
                disk_upload = isinstance(payload, dict) and "key" in payload
            except (ValueError, TypeError):
                pass
        # Active Storage disk writes authenticate their expiring, purpose-bound token.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not (
            bot_route or disk_upload
        ):
            token = request.headers.get("X-CSRF-Token") or request.POST.get(
                "authenticity_token"
            )
            origin = request.headers.get("Origin")
            if origin and origin != f"{request.scheme}://{request.get_host()}":
                return HttpResponse(status=422)
            if not rails.valid_csrf(
                rails.decode64(request.session_data["_csrf_token"]),
                token,
                request.path,
                request.method,
            ):
                return HttpResponse(status=422)
        return self.get_response(request)
