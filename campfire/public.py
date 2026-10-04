import html
import io
import mimetypes
import re
import zlib

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse

from . import rails
from .models import Account, Attachment, User
from .rendering import asset
from .storage import path_for, serve


def static(request, path):
    root = (settings.BASE_DIR / "assets/generated/public").resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise Http404
    response = serve(
        request,
        target,
        mimetypes.guess_type(target)[0] or "application/octet-stream",
        target.name,
    )
    response["Cache-Control"] = (
        "public, max-age=31536000, immutable"
        if path.startswith("assets/")
        else "public, max-age=3600"
    )
    return response


def health(request):
    return HttpResponse(
        '<!doctype html><html><body style="background-color: green"></body></html>'
    )


def avatar(request, user_id):
    if request.method == "DELETE":
        if not request.current_user:
            return HttpResponse(status=401)
        from .storage import remove_attachment

        remove_attachment("User", request.current_user.id, "avatar")
        return HttpResponseRedirect("/users/me/profile")
    try:
        user = User.objects.get(id=rails.verify_id("User", user_id, "avatar"))
    except (ValueError, User.DoesNotExist):
        raise Http404
    attachment = (
        Attachment.objects.filter(record_type="User", record_id=user.id, name="avatar")
        .select_related("blob")
        .first()
    )
    if attachment:
        from .media import variant

        blob = variant(attachment.blob, [512, 512], "webp")
        return serve(request, path_for(blob.key), "image/webp", blob.filename)
    if user.role == 2:
        return static(request, asset("default-bot-avatar.svg").lstrip("/"))
    colors = [
        "#AF2E1B",
        "#CC6324",
        "#3B4B59",
        "#BFA07A",
        "#ED8008",
        "#ED3F1C",
        "#BF1B1B",
        "#736B1E",
        "#D07B53",
        "#736356",
        "#AD1D1D",
        "#BF7C2A",
        "#C09C6F",
        "#698F9C",
        "#7C956B",
        "#5D618F",
        "#3B3633",
        "#67695E",
    ]
    initials = "".join(re.findall(r"\b\w", user.name))
    color = colors[zlib.crc32(str(user.id).encode()) % len(colors)]
    source = (
        settings.BASE_DIR / "reference/app/views/users/avatars/show.svg.erb"
    ).read_text()
    source = (
        source.replace("<%= avatar_background_color(@user) %>", color)
        .replace("<%= @user.initials %>", html.escape(initials))
        .replace(
            '<%=raw \'textLength="85%" lengthAdjust="spacingAndGlyphs"\' if @user.initials.size >= 3 %>',
            'textLength="85%" lengthAdjust="spacingAndGlyphs"'
            if len(initials) >= 3
            else "",
        )
    )
    response = HttpResponse(source, content_type="image/svg+xml")
    response["Cache-Control"] = "public, max-age=1800, stale-while-revalidate=604800"
    return response


def logo(request):
    account = Account.objects.first()
    if request.method == "DELETE":
        if not request.current_user or request.current_user.role != 1:
            return HttpResponse(status=403)
        if account:
            from .storage import remove_attachment

            remove_attachment("Account", account.id, "logo")
        return HttpResponseRedirect("/account/edit")
    small = request.GET.get("size") == "small"
    attachment = (
        Attachment.objects.filter(
            record_type="Account", record_id=account.id, name="logo"
        )
        .select_related("blob")
        .first()
        if account
        else None
    )
    if attachment:
        from .media import variant

        blob = variant(attachment.blob, [192, 192] if small else [512, 512], "png")
        return serve(request, path_for(blob.key), "image/png", blob.filename)
    return static(
        request,
        asset("logos/app-icon-192.png" if small else "logos/app-icon.png").lstrip("/"),
    )


def manifest(request):
    account = Account.objects.first()
    name = account.name if account else "Campfire"
    return JsonResponse(
        {
            "name": name,
            "icons": [
                {
                    "src": "/account/logo?size=small",
                    "type": "image/png",
                    "sizes": "192x192",
                },
                {"src": "/account/logo", "type": "image/png", "sizes": "512x512"},
                {
                    "src": "/account/logo",
                    "type": "image/png",
                    "sizes": "512x512",
                    "purpose": "maskable",
                },
            ],
            "start_url": "/",
            "display": "standalone",
            "scope": "/",
            "description": "A chat app from the makers of Basecamp and HEY.",
            "categories": ["social", "business", "productivity"],
            "theme_color": "#ffffff",
            "background_color": "#ffffff",
            "shortcuts": [
                {"name": "New chat room", "url": "/rooms/opens/new"},
                {"name": "My profile", "url": "/users/me/profile"},
            ],
        },
        content_type="application/manifest+json",
    )


def service_worker(request):
    return HttpResponse(
        (settings.BASE_DIR / "reference/app/views/pwa/service_worker.js").read_text(),
        content_type="application/javascript",
    )


def qr(request, id):
    import qrcode

    try:
        value = rails.decode64(id).decode()
    except (ValueError, UnicodeDecodeError):
        raise Http404
    stream = io.BytesIO()
    qrcode.make(value).save(stream, format="PNG")
    return HttpResponse(stream.getvalue(), content_type="image/png")
