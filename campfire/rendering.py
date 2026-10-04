import html
import json
import os
import re
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponse
from jinja2 import ChainableUndefined, Environment, FileSystemLoader
from markupsafe import Markup

from . import rails
from .models import (
    Account,
    Attachment,
    Boost,
    RichText,
    User,
    now,
)


class Data(SimpleNamespace):
    def __getattr__(self, name):
        return ""


def user_data(user):
    if not user:
        return Data(ID=0, Role=0, Name="", UpdatedAt=now())
    return Data(
        ID=user.id,
        Role=user.role,
        Name=user.name,
        Email=user.email_address or "",
        Bio=user.bio or "",
        UpdatedAt=user.updated_at,
        Title=user.title,
        Status=user.status,
        BotKey=f"{user.id}-{user.bot_token}",
        WebhookURL="",
        Administer=user.role == 1,
    )


def room_data(room, user=None):
    members = (
        list(User.objects.filter(memberships__room=room).order_by("name"))
        if room.type == "Rooms::Direct"
        else []
    )
    others = [u for u in members if not user or u.id != user.id]
    name = (
        ", ".join(u.name for u in others) if room.type == "Rooms::Direct" else room.name
    )
    kind = room.type.split("::")[-1].lower()
    return Data(
        ID=room.id,
        Name=name or "",
        Type=room.type,
        UpdatedAt=room.updated_at,
        CreatorID=room.creator_id,
        DOM=lambda prefix: f"{prefix}_rooms_{kind}_{room.id}",
        Noun="ping" if kind == "direct" else "room",
        EditPath=f"/rooms/{kind}s/{room.id}/edit",
        Members=[user_data(u) for u in others],
        Label=", ".join(u.name.split()[0] for u in others),
    )


GENERATED = settings.BASE_DIR / "assets/generated"
MANIFEST = json.loads((GENERATED / "manifest.json").read_text())


def asset(name):
    return "/assets/" + MANIFEST.get(name, {}).get("digested_path", name)


def avatar(id, *updated):
    return (
        "/users/"
        + rails.signed_id("User", int(id), "avatar")
        + "/avatar"
        + ("?v=" + versionTime(updated[0]) if updated else "")
    )


def versionTime(t):
    return t.strftime("%Y%m%d%H%M%S") if isinstance(t, datetime) else ""


def epoch(t):
    return int(t.timestamp() * 1000) if isinstance(t, datetime) else 0


def iso(t):
    return (
        t.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if isinstance(t, datetime)
        else ""
    )


def allEmoji(s):
    return bool(s and not re.search(r"[\w]", s))


TRANSLATIONS = json.loads(
    (settings.BASE_DIR / "campfire/translations.json").read_text()
)


def translate(key):
    entries = "".join(
        "<dt>"
        + html.escape(flag)
        + '</dt><dd class="margin-none">'
        + html.escape(text)
        + "</dd>"
        for flag, text in TRANSLATIONS.get(key, [])
    )
    return Markup(
        '<details class="position-relative" data-controller="popup" data-action="keydown.esc-&gt;popup#close toggle-&gt;popup#toggle click@document-&gt;popup#closeOnClickOutside" data-popup-orientation-top-class="popup-orientation-top"><summary class="btn" tabindex="-1"><img width="20" height="20" aria-hidden="true" class="color-icon" src="'
        + asset("globe.svg")
        + '"><span class="for-screen-reader">Translate</span></summary><div class="language-list-menu shadow" data-popup-target="menu"><dl class="language-list">'
        + entries
        + "</dl></div></details>"
    )


def nextInvolvement(kind, value):
    order = (
        ["everything", "nothing"]
        if kind == "Rooms::Direct"
        else ["mentions", "everything", "nothing", "invisible"]
    )
    return order[(order.index(value) + 1) % len(order)] if value in order else order[0]


env = Environment(
    loader=FileSystemLoader(settings.BASE_DIR / "campfire/templates"),
    autoescape=True,
    undefined=ChainableUndefined,
)
env.globals.update(
    asset=asset,
    avatar=avatar,
    versionTime=versionTime,
    epoch=epoch,
    iso=iso,
    stylesheets=lambda: Markup((GENERATED / "stylesheets.html").read_text()),
    importmap=lambda: Markup((GENERATED / "importmap.html").read_text()),
    translate=translate,
    firstName=lambda s: s.split()[0] if s else "",
    lower=lambda s: s.lower(),
    len=len,
    get=lambda x, k: x.get(k, False),
    allEmoji=allEmoji,
    printf=lambda fmt, *a: fmt % a,
    qrpath=lambda s: "/qr_code/"
    + rails.b64(s.encode()).replace("/", "_").replace("+", "-"),
    humanInvolvement=lambda s: {
        "everything": "Notifying about all messages",
        "mentions": "Notifying about @ mentions",
        "nothing": "Notifications are off",
        "invisible": "Notifications are off and room invisible in sidebar",
    }.get(s, ""),
    nextInvolvement=nextInvolvement,
    reactions=lambda: [
        Data(Character=e, Title=t)
        for e, t in [
            ("👍", "Thumbs up"),
            ("👏", "Clapping"),
            ("👋", "Waving hand"),
            ("💪", "Muscle"),
            ("❤️", "Red heart"),
            ("😂", "Face with tears of joy"),
            ("🎉", "Party popper"),
            ("🔥", "Fire"),
        ]
    ],
    agent=lambda s: Data(Name=s, Platform="", Browser=s),
    helpMailto=lambda u: Markup('href="mailto:' + html.escape(u.Email) + '"'),
    botCommand=lambda origin,
    room,
    key,
    attach: f"curl -d 'Hello!' {origin}/rooms/{room}/{key}/messages",
)


def context(request, screen, **kwargs):
    account = Account.objects.first()
    account_data = (
        Data(
            ID=account.id,
            Name=account.name,
            JoinCode=account.join_code,
            UpdatedAt=account.updated_at,
            HasLogo=Attachment.objects.filter(
                record_type="Account", record_id=account.id, name="logo"
            ).exists(),
            RestrictRoomCreation=(account.settings or {}).get(
                "restrict_room_creation_to_administrators", False
            ),
            RestrictRooms=(account.settings or {}).get(
                "restrict_room_creation_to_administrators", False
            ),
        )
        if account
        else Data(UpdatedAt=now(), HasLogo=False)
    )
    data = Data(
        User=user_data(request.current_user),
        Account=account_data,
        Screen=screen,
        BodyClass="sidebar searches"
        if screen == "search"
        else "sidebar"
        if screen in ("room", "welcome")
        else screen,
        Title="Campfire",
        Frame=bool(request.headers.get("Turbo-Frame")),
        Origin=f"{request.scheme}://{request.get_host()}",
        CSRF=request.csrf_token,
        Version="once-campfire-django",
        VAPIDPublicKey=os.environ.get("VAPID_PUBLIC_KEY", ""),
        CustomStyles=Markup("<style>" + account.custom_styles + "</style>")
        if account and account.custom_styles
        else "",
        Messages=[],
        RecentSearches=[],
        RoomsStream=rails.sign_stream("rooms"),
        UserRoomsStream=rails.sign_stream(rails.decode64("Z2lk").decode())
        if False
        else "",
        Notice="",
        Error="",
        Reload=False,
        Chat=screen == "room",
        ReturnRoom=request.session_data.get("last_room_id", ""),
        Query="",
    )
    if request.current_user:
        usergid = rails.b64(
            f"gid://campfire/User/{request.current_user.id}".encode()
        ).rstrip("=")
        data.UserRoomsStream = rails.sign_stream(usergid + ":rooms")
        data.CanCreateRooms = (
            request.current_user.role == 1 or not account_data.RestrictRoomCreation
        )
    data.__dict__.update(kwargs)
    return data


def render_text(name, data):
    return str(
        getattr(env.get_template("pages.html").module, name.replace("-", "_"))(data)
    )


def page(request, name, **kwargs):
    data = context(request, name, **kwargs)
    body = render_text(name, data)
    meta = (
        '<meta name="csrf-param" content="authenticity_token"><meta name="csrf-token" content="'
        + html.escape(request.csrf_token)
        + '">'
    )
    body = body.replace("</head>", meta + "</head>")
    body = re.sub(
        r'(<form\b[^>]*\bmethod="post"[^>]*>)',
        lambda m: m[0]
        + '<input type="hidden" name="authenticity_token" value="'
        + html.escape(request.csrf_token)
        + '">',
        body,
        flags=re.I,
    )
    return HttpResponse(body)


def message_data(messages, origin=""):
    messages = list(messages)
    ids = [m.id for m in messages]
    bodies = {
        r.record_id: r.body or ""
        for r in RichText.objects.filter(
            record_type="Message", name="body", record_id__in=ids
        )
    }
    attachments = {
        a.record_id: a.blob
        for a in Attachment.objects.filter(
            record_type="Message", name="attachment", record_id__in=ids
        ).select_related("blob")
    }
    boosts = {}
    for b in (
        Boost.objects.filter(message_id__in=ids)
        .select_related("booster")
        .order_by("created_at")
    ):
        boosts.setdefault(b.message_id, []).append(
            Data(
                ID=b.id,
                MessageID=b.message_id,
                BoosterID=b.booster_id,
                Booster=b.booster.name,
                BoosterTitle=b.booster.title,
                BoosterUpdatedAt=b.booster.updated_at,
                Content=b.content,
            )
        )
    from .media import VARIABLE_TYPES, representation_url
    from .richtext import plain_text, render_body
    from .storage import blob_url

    result = []
    for m in messages:
        blob = attachments.get(m.id)
        body = render_body(
            bodies.get(m.id, ""),
            urlsplit(origin).hostname or "",
        )
        if blob:
            url = blob_url(blob)
            filename = html.escape(blob.filename)
            content_type = blob.content_type or ""
            download = url + "?disposition=attachment"
            if (
                content_type in VARIABLE_TYPES
                or content_type.startswith("video/")
                or content_type == "application/pdf"
            ):
                metadata = json.loads(blob.metadata or "{}")
                width, height = metadata.get("width"), metadata.get("height")
                style = ""
                if width and height:
                    factor = min(1, 1200 / width, 800 / height)
                    width, height = width * factor, height * factor
                    style = f' style="width: {width / 2}px; aspect-ratio: {width / height};"'
                if content_type.startswith("video/"):
                    media = f'<video src="{url}" poster="{representation_url(blob, format="webp")}" controls preload="none" width="100%" height="100%" class="message__attachment"></video>'
                else:
                    media = f'<a href="{url}" class="flex" data-lightbox-target="image" data-action="lightbox#open" data-lightbox-url-value="{download}"><img class="message__attachment" src="{representation_url(blob)}" alt="{filename}" loading="lazy"></a>'
                body = f'<div class="max-inline-size center flex overflow-clip"{style}>{media}</div>'
            else:
                body = f'<div class="flex-inline align-center gap-half"><img src="{asset("common-file-text.svg")}" width="22" height="22" class="colorize--black" aria-hidden="true"><span>{filename}</span><a class="btn message__action-btn hide-in-ios-pwa" href="{download}"><img src="{asset("download.svg")}" width="20" height="20" aria-hidden="true"><span class="for-screen-reader">Download {filename}</span></a><button class="btn message__action-btn" data-controller="web-share" data-action="web-share#share" data-web-share-files-value="{download}"><img src="{asset("share.svg")}" width="20" height="20" aria-hidden="true"><span class="for-screen-reader">Share {filename}</span></button></div>'
        result.append(
            Data(
                ID=m.id,
                ClientID=m.client_message_id,
                CreatorID=m.creator_id,
                Creator=m.creator.name,
                CreatorTitle=m.creator.title,
                CreatorUpdatedAt=m.creator.updated_at,
                RoomID=m.room_id,
                RoomName=m.room.name or "",
                CreatedAt=m.created_at,
                UpdatedAt=m.updated_at,
                HTML=Markup('<div class="lexxy-content">' + body + "</div>"),
                AllEmoji=allEmoji(plain_text(bodies.get(m.id, ""))),
                Boosts=boosts.get(m.id, []),
                Attachment=Data(Filename=blob.filename) if blob else None,
                DownloadURL=blob_url(blob) + "?disposition=attachment" if blob else "",
                BlobURL=blob_url(blob) if blob else "",
                Permalink=f"{origin}/rooms/{m.room_id}/@{m.id}",
            )
        )
    return result
