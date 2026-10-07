import html
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlencode

import bcrypt
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.expressions import RawSQL
from django.db.models.functions import Lower
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from markupsafe import Markup

from . import rails
from .domain import (
    create_message,
    create_user,
    delete_message,
    delete_room,
    get_room,
    grant_memberships,
    presentation_messages,
    search_message_ids,
    update_message,
    user_rooms,
)
from .models import (
    Account,
    Attachment,
    Ban,
    Blob,
    Boost,
    Membership,
    Message,
    PushSubscription,
    RichText,
    Room,
    Search,
    Session,
    User,
    Webhook,
    now,
)
from .rendering import (
    Data,
    avatar,
    message_data,
    page,
    render_text,
    room_data,
    user_data,
)
from .response_cache import cached_page
from .richtext import plain_text
from .storage import staged_files


def params(request):
    if request.content_type == "application/json":
        try:
            return json.loads(request.body)
        except ValueError:
            return {}
    if request.method in ("PATCH", "PUT", "DELETE") and not hasattr(request, "_post"):
        from django.http import QueryDict

        return QueryDict(request.body)
    return request.POST if request.method != "GET" else request.GET


def value(request, group, name, default=""):
    data = params(request)
    return (
        data.get(group, {}).get(name, default)
        if isinstance(data.get(group), dict)
        else data.get(f"{group}[{name}]", default)
    )


def message_attachment(request):
    for key in ("message[attachment]", "attachment"):
        if key in request.FILES:
            return request.FILES[key]
    attachment = value(request, "message", "attachment", None)
    return params(request).get("attachment") if attachment is None else attachment


def login_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.current_user:
            request.session_data["return_to_after_authenticating"] = (
                request.get_full_path()
            )
            return HttpResponseRedirect("/session/new")
        if request.authenticated_by_bot:
            return HttpResponse(status=403)
        return view(request, *args, **kwargs)

    return wrapped


def start_session(request, user):
    session = Session.objects.create(
        user=user,
        token=secrets.token_urlsafe(18),
        user_agent=request.headers.get("User-Agent", ""),
        ip_address=request.META.get("REMOTE_ADDR", ""),
    )
    request.current_session, request.current_user = session, user
    request.new_session_token = session.token


def welcome(request):
    if not Account.objects.exists():
        return HttpResponseRedirect("/first_run")
    if not request.current_user:
        return HttpResponseRedirect("/session/new")
    last = request.COOKIES.get("last_room") or request.session_data.get("last_room_id")
    room = (
        user_rooms(request.current_user).filter(id=last).first()
        if str(last or "").isdigit()
        else None
    )
    room = room or user_rooms(request.current_user).order_by("created_at").first()
    return (
        HttpResponseRedirect(f"/rooms/{room.id}") if room else page(request, "welcome")
    )


def session(request):
    if request.method == "GET":
        if not User.objects.exists():
            return HttpResponseRedirect("/first_run")
        return page(request, "login", Email=request.GET.get("email_address", ""))
    if request.method == "DELETE":
        if request.current_session:
            request.current_session.delete()
        if request.current_user:
            PushSubscription.objects.filter(
                user=request.current_user,
                endpoint=params(request).get("push_subscription_endpoint", ""),
            ).delete()
        request.session_data.clear()
        response = HttpResponseRedirect("/")
        response.delete_cookie("session_token")
        return response
    if request.method != "POST":
        return HttpResponse(status=405)
    data = params(request)
    from .jobs import rate_limit

    if not rate_limit("login:" + request.META.get("REMOTE_ADDR", ""), 10, 180):
        return HttpResponse("Too many requests or unauthorized.", status=429)
    user = User.objects.filter(
        status=0, email_address=data.get("email_address")
    ).first()
    valid = False
    if user and user.password_digest:
        try:
            valid = bcrypt.checkpw(
                data.get("password", "").encode()[:72], user.password_digest.encode()
            )
        except ValueError:
            pass
    if not valid:
        response = page(
            request,
            "login",
            Error="Too many requests or unauthorized.",
            Email=data.get("email_address", ""),
        )
        response.status_code = 401
        return response
    start_session(request, user)
    redirect = request.session_data.pop("return_to_after_authenticating", "/")
    return HttpResponseRedirect(
        redirect if redirect.startswith("/") and not redirect.startswith("//") else "/"
    )


def first_run(request):
    if Account.objects.exists():
        return HttpResponseRedirect("/")
    if request.method == "GET":
        return page(request, "first-run")
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        with staged_files(), transaction.atomic():
            Account.objects.create(name="Campfire", join_code=secrets.token_urlsafe(18))
            user = create_user(
                name=value(request, "user", "name"),
                email_address=value(request, "user", "email_address"),
                password=value(request, "user", "password"),
                role=1,
            )
            room = Room.objects.create(
                name="All Talk", type="Rooms::Open", creator=user
            )
            grant_memberships(room, [user])
            if request.FILES.get("user[avatar]"):
                from .storage import replace_attachment

                replace_attachment(
                    request.FILES["user[avatar]"], "User", user.id, "avatar"
                )
        start_session(request, user)
        return HttpResponseRedirect("/")
    except IntegrityError:
        return HttpResponseRedirect("/")


def join(request, code):
    account = Account.objects.first()
    if not account or account.join_code != code:
        raise Http404
    if request.current_user:
        return HttpResponseRedirect("/")
    if request.method == "GET":
        return page(request, "join", JoinCode=code)
    try:
        with staged_files(), transaction.atomic():
            user = create_user(
                name=value(request, "user", "name"),
                email_address=value(request, "user", "email_address"),
                password=value(request, "user", "password"),
            )
            if request.FILES.get("user[avatar]"):
                from .storage import replace_attachment

                replace_attachment(
                    request.FILES["user[avatar]"], "User", user.id, "avatar"
                )
        start_session(request, user)
        return HttpResponseRedirect("/")
    except IntegrityError:
        return HttpResponseRedirect("/session/new")


@login_required
@cached_page("room")
def room(request, id=None, message_id=None):
    if id is None:
        last = user_rooms(request.current_user).last()
        return HttpResponseRedirect(f"/rooms/{last.id}" if last else "/")
    obj = get_room(request.current_user, id)
    if not obj:
        return HttpResponseRedirect("/")
    if request.method == "DELETE":
        if not request.current_user.can_administer(obj):
            return HttpResponse(status=403)
        delete_room(obj)
        return HttpResponseRedirect("/")
    if request.method != "GET":
        return HttpResponse(status=405)
    query = presentation_messages(obj.messages.all()).order_by("created_at")
    selected = obj.messages.filter(id=message_id).first() if message_id else None
    if selected:
        before = list(
            query.filter(created_at__lt=selected.created_at).order_by("-created_at")[
                :40
            ]
        )
        before.reverse()
        messages = (
            before
            + [Message.objects.select_related("creator", "room").get(id=selected.id)]
            + list(query.filter(created_at__gt=selected.created_at)[:40])
        )
    else:
        messages = list(query.order_by("-created_at")[:40])
        messages.reverse()
    request.last_room = obj.id
    membership = Membership.objects.get(room=obj, user=request.current_user)
    return page(
        request,
        "room",
        Room=room_data(obj, request.current_user),
        Messages=message_data(messages, f"{request.scheme}://{request.get_host()}"),
        LoadedAt=int(obj.updated_at.timestamp() * 1000),
        Stream=rails.sign_stream(rails.stream(obj)),
        Involvement=membership.involvement,
        Invitation=False,
    )


def serialize_message(m, request=None):
    body = RichText.objects.filter(
        record_type="Message", record_id=m.id, name="body"
    ).first()
    origin = f"{request.scheme}://{request.get_host()}" if request else ""
    return {
        "id": m.id,
        "created_at": m.created_at.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "body": {
            "plain_text": plain_text(body.body) if body else "",
            "html": '<div class="lexxy-content">'
            + (body.body if body else "")
            + "</div>",
        },
        "creator": {
            "id": m.creator_id,
            "name": m.creator.name,
            "role": ["member", "administrator", "bot"][m.creator.role],
            "avatar_url": origin + avatar(m.creator_id, m.creator.updated_at),
        },
        "room": {"id": m.room_id},
        "url": f"{origin}/rooms/{m.room_id}/messages/{m.id}",
    }


@cached_page("messages")
def messages(request, room_id=None, id=None, edit=False, bot_key=None):
    if bot_key:
        try:
            bot_id, token = bot_key.strip().split("-", 1)
            user = User.objects.get(id=bot_id, bot_token=token, role=2, status=0)
        except (ValueError, User.DoesNotExist):
            return HttpResponse(status=401)
    else:
        user = request.current_user
        if not user:
            return HttpResponseRedirect("/session/new")
        if request.authenticated_by_bot:
            return HttpResponse(status=403)
    if room_id is None and id:
        m = Message.objects.filter(id=id).first()
        room_id = m.room_id if m else None
    obj = get_room(user, room_id)
    if not obj:
        return HttpResponse(status=404) if bot_key else HttpResponseRedirect("/")
    m = (
        Message.objects.select_related("creator", "room")
        .filter(room=obj, id=id)
        .first()
        if id
        else None
    )
    if id and not m:
        raise Http404
    wants_json = bool(
        bot_key
        or request.path.endswith(".json")
        or "application/json" in request.headers.get("Accept", "")
    )
    if request.method == "GET":
        if edit:
            if not user.can_administer(m):
                return HttpResponse(status=403)
            body = RichText.objects.filter(
                record_type="Message", record_id=m.id, name="body"
            ).first()
            return page(
                request,
                "edit-message",
                Messages=message_data([m]),
                Room=room_data(obj, user),
                Body=body.body if body else "",
            )
        query = presentation_messages(obj.messages.all())
        if m:
            rows = [m]
        else:
            for direction, operator in [("before", "lt"), ("after", "gt")]:
                anchor = request.GET.get(direction)
                if anchor:
                    pivot = obj.messages.filter(id=anchor).first()
                    if not pivot:
                        raise Http404
                    query = query.filter(
                        **{"created_at__" + operator: pivot.created_at}
                    )
            rows = list(
                query.order_by(
                    "created_at" if request.GET.get("after") else "-created_at"
                )[:40]
            )
            if not request.GET.get("after"):
                rows.reverse()
        if not rows:
            return HttpResponse(status=204)
        if wants_json:
            response = JsonResponse(
                serialize_message(m, request)
                if m
                else [serialize_message(x, request) for x in rows],
                safe=bool(m),
            )
            if bot_key:
                response["X-Total-Count"] = str(obj.messages.count())
                if rows:
                    direction = "after" if request.GET.get("after") else "before"
                    anchor = rows[-1] if direction == "after" else rows[0]
                    exists = obj.messages.filter(
                        **{
                            "created_at__gt"
                            if direction == "after"
                            else "created_at__lt": anchor.created_at
                        }
                    ).exists()
                    if exists:
                        response["Link"] = (
                            f'<{request.scheme}://{request.get_host()}/rooms/{obj.id}/{bot_key}/messages?{direction}={anchor.id}>; rel="next"'
                        )
            return response
        return (
            page(request, "show-message", Messages=message_data(rows))
            if m
            else HttpResponse(
                render_text("messages", Data(Messages=message_data(rows))),
                content_type="text/html",
            )
        )
    if id and not user.can_administer(m):
        return HttpResponse(status=403)
    if request.method == "POST":
        body = (
            request.body.decode(errors="replace")
            if bot_key and request.content_type != "multipart/form-data"
            else value(request, "message", "body")
        )
        if bot_key and request.FILES.get("attachment"):
            body = ""
        attachment = message_attachment(request)
        if bot_key and not body and not attachment:
            return HttpResponse(status=422)
        try:
            m = create_message(
                user,
                obj,
                body,
                value(request, "message", "client_message_id") or None,
                attachment,
            )
        except (ValueError, PermissionError):
            return HttpResponse(status=422)
        if bot_key:
            return HttpResponse(
                status=201,
                headers={
                    "Location": f"{request.scheme}://{request.get_host()}/rooms/{obj.id}/messages/{m.id}"
                },
            )
        if wants_json:
            return JsonResponse(serialize_message(m, request), status=201)
        fragment = render_text("message", message_data([m])[0])
        return HttpResponse(
            f'<turbo-stream action="append" target="messages_rooms_{obj.type.split("::")[-1].lower()}_{obj.id}"><template>{fragment}</template></turbo-stream>',
            content_type="text/vnd.turbo-stream.html",
        )
    if request.method in ("PATCH", "PUT"):
        attachment = message_attachment(request)
        body = (
            None
            if bot_key and attachment is not None
            else request.body.decode(errors="replace")
            if bot_key
            else value(request, "message", "body", None)
        )
        try:
            update_message(m, body, attachment)
        except (ValueError, Blob.DoesNotExist):
            return HttpResponse(status=422)
        return (
            JsonResponse(serialize_message(m, request))
            if wants_json
            else HttpResponseRedirect(f"/rooms/{obj.id}/messages/{m.id}")
        )
    if request.method == "DELETE":
        delete_message(m)
        return (
            HttpResponse(status=204)
            if wants_json
            else HttpResponse(
                f'<turbo-stream action="remove" target="message_{m.client_message_id}"></turbo-stream>',
                content_type="text/vnd.turbo-stream.html",
            )
        )
    return HttpResponse(status=405)


@login_required
@cached_page("sidebar")
def sidebar(request):
    memberships = list(
        Membership.objects.filter(user=request.current_user)
        .exclude(involvement="invisible")
        .select_related("room")
        .order_by(Lower("room__name"))
    )
    directs = [m for m in memberships if m.room.type == "Rooms::Direct"]
    directs.sort(key=lambda m: m.room.updated_at, reverse=True)
    rooms = []
    for m in directs + [m for m in memberships if m.room.type != "Rooms::Direct"]:
        dto = room_data(m.room, request.current_user)
        dto.Unread = bool(m.unread_at)
        rooms.append(dto)
    return page(request, "sidebar", SidebarRooms=rooms, Placeholders=[])


@login_required
@cached_page("search")
def searches(request):
    query = re.sub(r"[^\w]", " ", params(request).get("q", "")).strip()
    if request.method == "DELETE":
        Search.objects.filter(user=request.current_user).delete()
        return HttpResponseRedirect("/searches")
    if request.method == "POST":
        if query:
            Search.objects.update_or_create(
                user=request.current_user, query=query, defaults={"updated_at": now()}
            )
            stale = list(
                Search.objects.filter(user=request.current_user)
                .order_by("-updated_at")
                .values_list("id", flat=True)[10:]
            )
            Search.objects.filter(id__in=stale).delete()
        return HttpResponseRedirect("/searches?" + urlencode({"q": query}))
    rows = []
    if query:
        ids = search_message_ids(request.current_user, query)
        rows = list(
            presentation_messages(
                Message.objects.filter(
                    id__in=ids,
                    room__memberships__user=request.current_user,
                )
            ).order_by("id")
        )
    return page(
        request,
        "search",
        Messages=message_data(rows),
        Query=query,
        RecentSearches=list(
            Search.objects.filter(user=request.current_user)
            .order_by("-updated_at")
            .values_list("query", flat=True)[:10]
        ),
    )


@login_required
def refresh(request, room_id):
    obj = get_room(request.current_user, room_id)
    if not obj:
        raise Http404
    try:
        loaded = datetime.fromtimestamp(
            float(request.GET.get("since", 0)) / 1000,
            tz=timezone.utc,
        )
    except ValueError:
        loaded = now()
    new_rows = list(
        presentation_messages(
            Message.objects.filter(room=obj, created_at__gt=loaded)
        ).order_by("created_at")[:40]
    )
    updated_rows = list(
        presentation_messages(
            Message.objects.filter(room=obj, updated_at__gt=loaded).exclude(
                id__in=[m.id for m in new_rows]
            )
        ).order_by(RawSQL("+messages.created_at", []).desc())[:40]
    )
    rows = new_rows + list(reversed(updated_rows))
    streams = []
    for m, dto in zip(rows, message_data(rows)):
        action = "append" if m.created_at > loaded else "replace"
        target = (
            f"messages_rooms_{obj.type.split('::')[-1].lower()}_{obj.id}"
            if action == "append"
            else "message_" + m.client_message_id
        )
        streams.append(
            f'<turbo-stream action="{action}" target="{target}"><template>{render_text("message", dto)}</template></turbo-stream>'
        )
    return HttpResponse("".join(streams), content_type="text/vnd.turbo-stream.html")


@login_required
def involvement(request, room_id):
    obj = get_room(request.current_user, room_id)
    if not obj:
        raise Http404
    membership = Membership.objects.get(room=obj, user=request.current_user)
    if request.method in ("PUT", "PATCH"):
        choice = params(request).get("involvement") or request.GET.get("involvement")
        allowed = (
            ["everything", "nothing"]
            if obj.type == "Rooms::Direct"
            else ["mentions", "everything", "nothing", "invisible"]
        )
        if choice not in allowed:
            return HttpResponse(status=422)
        membership.involvement = choice
        membership.save()
    return page(
        request,
        "involvement",
        Room=room_data(obj, request.current_user),
        Involvement=membership.involvement,
    )


@login_required
def room_form(request, kind, id=None, edit=False, new=False):
    type_name = "Rooms::" + kind[:-1].capitalize()
    obj = get_room(request.current_user, id) if id else None
    if id and (not obj or (obj.type == "Rooms::Direct") != (kind == "directs")):
        raise Http404
    if obj and request.method == "DELETE":
        if kind != "directs" and not request.current_user.can_administer(obj):
            return HttpResponse(status=403)
        delete_room(obj)
        return HttpResponseRedirect("/")
    if obj and not edit and request.method == "GET":
        return HttpResponseRedirect(f"/rooms/{obj.id}")
    account = Account.objects.first()
    if (
        not obj
        and kind != "directs"
        and (account.settings or {}).get("restrict_room_creation_to_administrators")
        and request.current_user.role != 1
    ):
        return HttpResponse(status=403)
    if request.method == "GET":
        display = (
            room_data(obj, request.current_user)
            if obj
            else Data(
                ID=0,
                Name="New room",
                Type=type_name,
                EditPath="",
                DOM=lambda prefix: "",
            )
        )
        display.Type = type_name
        users = list(User.objects.filter(status=0).order_by(Lower("name")))
        selected = (
            set(Membership.objects.filter(room=obj).values_list("user_id", flat=True))
            if obj
            else {request.current_user.id}
        )
        users.sort(key=lambda u: u.id not in selected)
        return page(
            request,
            "room-form",
            Room=display,
            Users=[user_data(u) for u in users],
            Selected={id: True for id in selected},
            UserDivider=len(selected),
            CanAdminister=not obj
            or request.current_user.can_administer(obj)
            or kind == "directs",
            BackPath=f"/rooms/{obj.id}" if obj else "/",
        )
    if request.method not in ("POST", "PATCH", "PUT"):
        return HttpResponse(status=405)
    if obj and not request.current_user.can_administer(obj):
        return HttpResponse(status=403)
    data = params(request)
    ids = (
        data.getlist("user_ids[]")
        if hasattr(data, "getlist")
        else data.get("user_ids", [])
    )
    ids = {int(id) for id in ids if str(id).isdigit()}
    if kind == "directs":
        ids.add(request.current_user.id)
        users = list(User.objects.filter(id__in=ids))
        ids = {u.id for u in users}
        with staged_files(), transaction.atomic():
            candidate = (
                Room.objects.filter(type="Rooms::Direct")
                .annotate(
                    member_count=Count("memberships"),
                    selected_count=Count(
                        "memberships", filter=Q(memberships__user_id__in=ids)
                    ),
                )
                .filter(member_count=len(ids), selected_count=len(ids))
                .order_by("id")
                .first()
            )
            if candidate:
                return HttpResponseRedirect(f"/rooms/{candidate.id}")
            obj = Room.objects.create(
                type=type_name, name=None, creator=request.current_user
            )
            grant_memberships(obj, users)
    else:
        with staged_files(), transaction.atomic():
            if obj:
                obj.name = value(request, "room", "name")
                obj.type = type_name
                obj.save()
            else:
                obj = Room.objects.create(
                    type=type_name,
                    name=value(request, "room", "name"),
                    creator=request.current_user,
                )
            users = (
                User.objects.filter(status=0)
                if kind == "opens"
                else User.objects.filter(id__in=ids)
            )
            Membership.objects.filter(room=obj).exclude(user__in=users).delete()
            grant_memberships(obj, users)
    from .cable import publish

    for membership in Membership.objects.filter(room=obj).select_related("user"):
        dto = room_data(obj, membership.user)
        dto.Unread = False
        stream = (
            "rooms"
            if kind == "opens"
            else rails.b64(f"gid://campfire/User/{membership.user_id}".encode()).rstrip(
                "="
            )
            + ":rooms"
        )
        target = "direct_rooms" if kind == "directs" else "shared_rooms"
        fragment = render_text(
            "sidebar-direct" if kind == "directs" else "sidebar-shared", dto
        )
        publish(
            stream,
            f'<turbo-stream action="prepend" target="{target}"><template>{fragment}</template></turbo-stream>',
        )
    return HttpResponseRedirect(f"/rooms/{obj.id}")


@login_required
def boosts(request, message_id, id=None, new=False):
    message = (
        Message.objects.select_related("room", "creator")
        .filter(id=message_id, room__memberships__user=request.current_user)
        .first()
    )
    if not message:
        raise Http404
    from .cable import publish

    if request.method == "POST":
        content = value(request, "boost", "content")
        if not content.strip() or len(content) > 16:
            return HttpResponse(status=422)
        with staged_files(), transaction.atomic():
            boost = Boost.objects.create(
                message=message, booster=request.current_user, content=content
            )
            Message.objects.filter(id=message.id).update(updated_at=now())
        dto = message_data([message])[0]
        fragment = render_text("boost", next(b for b in dto.Boosts if b.ID == boost.id))
        publish(
            rails.stream(message.room),
            f'<turbo-stream action="append" target="boosts_message_{message.client_message_id}" maintain_scroll="true"><template>{fragment}</template></turbo-stream>',
        )
        return HttpResponseRedirect(f"/messages/{message.id}/boosts")
    if request.method == "DELETE":
        boost = Boost.objects.filter(
            id=id, message=message, booster=request.current_user
        ).first()
        if not boost:
            raise Http404
        boost.delete()
        Message.objects.filter(id=message.id).update(updated_at=now())
        publish(
            rails.stream(message.room),
            f'<turbo-stream action="remove" target="boost_{id}"></turbo-stream>',
        )
        return HttpResponse(
            f'<turbo-stream action="remove" target="boost_{id}"></turbo-stream>',
            content_type="text/vnd.turbo-stream.html",
        )
    return page(
        request,
        "new-boost" if new else "boosts-index",
        Messages=message_data([message]),
    )


@login_required
def autocomplete(request):
    users = User.objects.filter(status=0).order_by(Lower("name"))
    if request.GET.get("room_id"):
        room = get_room(request.current_user, request.GET["room_id"])
        if not room:
            raise Http404
        users = users.filter(memberships__room=room)
    query = request.GET.get("filter") or request.GET.get("query")
    if query:
        users = users.filter(name__icontains=query)
    users = list(users[:20])
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(
            [
                {
                    "id": u.id,
                    "name": html.escape(u.name),
                    "label": u.name,
                    "avatar": avatar(u.id, u.updated_at),
                    "avatar_url": avatar(u.id, u.updated_at),
                    "sgid": rails.sgid("User", u.id),
                    "value": u.id,
                }
                for u in users
            ],
            safe=False,
        )
    fragments = []
    for u in users:
        data = Data(
            Mention=Data(
                Name=u.name,
                Title=u.title,
                SGID=rails.sgid("User", u.id),
                Path=f"/users/{u.id}",
                Avatar=avatar(u.id, u.updated_at),
            ),
            HTML=Markup(
                '<span class="mention" data-user-id="'
                + str(u.id)
                + '">'
                + html.escape(u.name)
                + "</span>"
            ),
        )
        fragments.append(render_text("prompt-item", data))
    return HttpResponse("".join(fragments))


@login_required
def profile(request, id="me"):
    user = request.current_user
    if request.method in ("PATCH", "PUT"):
        for name in ["name", "bio", "email_address"]:
            new = value(request, "user", name, None)
            if new is not None:
                setattr(user, name, new)
        password = value(request, "user", "password")
        if password:
            user.password_digest = bcrypt.hashpw(
                password.encode()[:72], bcrypt.gensalt()
            ).decode()
        with staged_files(), transaction.atomic():
            user.save()
            if request.FILES.get("user[avatar]"):
                from .storage import replace_attachment

                replace_attachment(
                    request.FILES["user[avatar]"], "User", user.id, "avatar"
                )
        return HttpResponseRedirect("/users/me/profile")
    memberships = list(
        Membership.objects.filter(user=user)
        .select_related("room")
        .order_by(Lower("room__name"))
    )
    shared = []
    direct = []
    for membership in memberships:
        dto = room_data(membership.room, user)
        dto.Involvement = membership.involvement
        dto = Data(Room=dto, Involvement=membership.involvement, ID=membership.id)
        (direct if membership.room.type == "Rooms::Direct" else shared).append(dto)
    transfer = rails.signed_id("User", user.id, "transfer", now() + timedelta(hours=4))
    return page(
        request,
        "profile",
        DirectMemberships=direct,
        SharedMemberships=shared,
        Memberships=direct + shared,
        AvatarAttached=Attachment.objects.filter(
            record_type="User", record_id=user.id, name="avatar"
        ).exists(),
        Transfer=f"{request.scheme}://{request.get_host()}/session/transfers/{transfer}",
        Platform=Data(Chrome=True, Firefox=False, IOS=False, Android=False),
        CanAdminister=user.role == 1,
    )


@login_required
def user_show(request, id):
    user = User.objects.filter(id=id).first()
    if not user:
        raise Http404
    return page(
        request,
        "user",
        Subject=user_data(user),
        CanAdminister=request.current_user.role == 1,
        AvatarURL=avatar(user.id, user.updated_at),
    )


@login_required
def account(request):
    obj = Account.objects.first()
    if request.method in ("PATCH", "PUT"):
        if request.current_user.role != 1:
            return HttpResponse(status=403)
        name = value(request, "account", "name", None)
        if name is not None:
            obj.name = name
        data = params(request)
        setting = value(request, "account", "settings", None)
        if isinstance(setting, dict):
            obj.settings = setting
        elif "account[settings][restrict_room_creation_to_administrators]" in data:
            obj.settings = {
                "restrict_room_creation_to_administrators": data[
                    "account[settings][restrict_room_creation_to_administrators]"
                ]
                in ["1", "true", "on"]
            }
        with staged_files(), transaction.atomic():
            obj.save()
            if request.FILES.get("account[logo]"):
                from .storage import replace_attachment

                replace_attachment(
                    request.FILES["account[logo]"], "Account", obj.id, "logo"
                )
        return HttpResponseRedirect("/account/edit")
    users = (
        User.objects.exclude(role=2)
        .filter(status__in=[0, 2] if request.current_user.role == 1 else [0])
        .order_by(Lower("name"))
    )
    users = list(users[:500])
    users.sort(key=lambda u: u.role != 1)
    return page(
        request,
        "account",
        Users=[user_data(u) for u in users if u.role != 1],
        UserDivider=0,
        CanAdminister=request.current_user.role == 1,
        JoinURL=f"{request.scheme}://{request.get_host()}/join/{obj.join_code}",
        Administrators=[user_data(u) for u in users if u.role == 1],
        Members=[user_data(u) for u in users if u.role != 1],
    )


@login_required
def account_users(request, id=None):
    if request.method == "GET":
        return account(request)
    if request.current_user.role != 1:
        return HttpResponse(status=403)
    user = User.objects.filter(id=id, status=0).first()
    if not user:
        raise Http404
    if request.method == "DELETE":
        deactivate(user)
    elif request.method in ("PUT", "PATCH"):
        user.role = 1 if value(request, "user", "role") == "administrator" else 0
        user.save()
    else:
        return HttpResponse(status=405)
    return HttpResponseRedirect("/account/edit")


def deactivate(user):
    with staged_files(), transaction.atomic():
        Membership.objects.filter(user=user).exclude(
            room__type="Rooms::Direct"
        ).delete()
        PushSubscription.objects.filter(user=user).delete()
        Search.objects.filter(user=user).delete()
        Session.objects.filter(user=user).delete()
        user.status = 1
        if user.email_address:
            user.email_address = user.email_address.replace(
                "@", "-deactivated-" + secrets.token_hex(8) + "@"
            )
        user.save()


@login_required
def bots(request, id=None, new=False, edit=False, key=False):
    if request.current_user.role != 1:
        return HttpResponse(status=403)
    bot = User.objects.filter(id=id, role=2, status=0).first() if id else None
    if id and not bot:
        raise Http404
    if request.method == "DELETE":
        deactivate(bot)
        return HttpResponseRedirect("/account/bots")
    if key and request.method == "PUT":
        bot.bot_token = secrets.token_hex(6)
        bot.save()
        return HttpResponseRedirect(f"/account/bots/{bot.id}/edit")
    if request.method in ("POST", "PATCH", "PUT"):
        with staged_files(), transaction.atomic():
            if bot:
                bot.name = value(request, "user", "name")
                bot.save()
            else:
                bot = create_user(
                    name=value(request, "user", "name"),
                    role=2,
                    bot_token=secrets.token_hex(6),
                )
            url = value(request, "user", "webhook_url")
            if url:
                Webhook.objects.update_or_create(user=bot, defaults={"url": url})
            else:
                Webhook.objects.filter(user=bot).delete()
            if request.FILES.get("user[avatar]"):
                from .storage import replace_attachment

                replace_attachment(
                    request.FILES["user[avatar]"], "User", bot.id, "avatar"
                )
        return HttpResponseRedirect("/account/bots")
    if new or edit:
        webhook = Webhook.objects.filter(user=bot).first() if bot else None
        return page(
            request,
            "bot-form",
            Subject=user_data(bot),
            Webhook=webhook.url if webhook else "",
            AvatarURL=avatar(bot.id, bot.updated_at) if bot else "",
        )
    data = []
    for bot in User.objects.filter(role=2, status=0).order_by(Lower("name")):
        data.append(
            Data(
                User=user_data(bot), Rooms=[room_data(r, bot) for r in user_rooms(bot)]
            )
        )
    return page(request, "bots", Bots=data)


@login_required
def custom_styles(request):
    if request.current_user.role != 1:
        return HttpResponse(status=403)
    obj = Account.objects.first()
    if request.method in ("PATCH", "PUT"):
        obj.custom_styles = value(request, "account", "custom_styles")
        obj.save()
        return HttpResponseRedirect("/account/custom_styles/edit")
    return page(
        request,
        "custom-styles",
        Styles=obj.custom_styles or "",
        CustomStylesBody=obj.custom_styles or "",
    )


@login_required
def join_code(request):
    if request.current_user.role != 1:
        return HttpResponse(status=403)
    if request.method != "POST":
        return HttpResponse(status=405)
    account = Account.objects.first()
    account.join_code = secrets.token_urlsafe(18)
    account.save()
    return HttpResponseRedirect("/account/edit")


@login_required
def ban(request, user_id):
    if request.current_user.role != 1:
        return HttpResponse(status=403)
    user = User.objects.filter(id=user_id).first()
    if not user:
        raise Http404
    with staged_files(), transaction.atomic():
        if request.method == "DELETE":
            Ban.objects.filter(user=user).delete()
            user.status = 0
            user.save()
        elif request.method == "POST":
            for ip in set(
                Session.objects.filter(user=user)
                .exclude(ip_address__isnull=True)
                .exclude(ip_address="")
                .values_list("ip_address", flat=True)
            ):
                Ban.objects.create(user=user, ip_address=ip)
            Session.objects.filter(user=user).delete()
            user.status = 2
            user.save()
            from .jobs import enqueue

            transaction.on_commit(lambda: enqueue("ban-content", {"user_id": user.id}))
        else:
            return HttpResponse(status=405)
    return HttpResponseRedirect(f"/users/{user.id}")


def transfer(request, id):
    if request.method == "GET":
        return page(request, "transfer", Transfer=id)
    if request.method not in ("PUT", "PATCH"):
        return HttpResponse(status=405)
    try:
        user = User.objects.get(id=rails.verify_id("User", id, "transfer"), status=0)
    except (ValueError, User.DoesNotExist):
        return HttpResponse(status=400)
    start_session(request, user)
    return HttpResponseRedirect("/")


@login_required
def push_subscriptions(request, id=None, test=False):
    query = PushSubscription.objects.filter(user=request.current_user)
    if test:
        subscription = query.filter(id=id).first()
        if not subscription:
            raise Http404

        # Delivery shares endpoint policy and native Web Push implementation.
        from .notifications import test_push

        test_push(subscription)
        return HttpResponseRedirect("/users/me/push_subscriptions")
    if request.method == "POST":
        from .jobs import permitted_endpoint

        endpoint = value(request, "push_subscription", "endpoint")
        if not permitted_endpoint(endpoint):
            return HttpResponse(status=422)
        query.update_or_create(
            endpoint=endpoint,
            p256dh_key=value(request, "push_subscription", "p256dh_key"),
            auth_key=value(request, "push_subscription", "auth_key"),
            defaults={
                "user_agent": request.headers.get("User-Agent", ""),
                "updated_at": now(),
            },
        )
        return HttpResponse(status=200)
    if request.method == "DELETE":
        query.filter(id=id).delete()
        return HttpResponseRedirect("/users/me/push_subscriptions")
    return page(
        request,
        "push-subscriptions",
        Subscriptions=[
            Data(
                ID=p.id,
                Endpoint=p.endpoint,
                UserAgent=p.user_agent or "",
                UpdatedAt=p.updated_at,
            )
            for p in query
        ],
    )


def bot_boosts(request, room_id, bot_key, message_id, id=None):
    try:
        bot_id, token = bot_key.strip().split("-", 1)
        user = User.objects.get(id=bot_id, bot_token=token, role=2, status=0)
    except (ValueError, User.DoesNotExist):
        return HttpResponse(status=401)
    message = (
        Message.objects.select_related("room", "creator")
        .filter(id=message_id, room_id=room_id, room__memberships__user=user)
        .first()
    )
    if not message:
        raise Http404
    from .cable import publish

    if request.method == "POST":
        content = request.body.decode(errors="replace")
        if not content.strip() or len(content) > 16:
            return HttpResponse(status=422)
        boost = Boost.objects.create(message=message, booster=user, content=content)
        Message.objects.filter(id=message.id).update(updated_at=now())
        dto = message_data([message])[0]
        fragment = render_text("boost", next(b for b in dto.Boosts if b.ID == boost.id))
        publish(
            rails.stream(message.room),
            f'<turbo-stream action="append" target="boosts_message_{message.client_message_id}"><template>{fragment}</template></turbo-stream>',
        )
        origin = f"{request.scheme}://{request.get_host()}"
        return JsonResponse(
            {
                "id": boost.id,
                "content": content,
                "created_at": boost.created_at.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "booster": {
                    "id": user.id,
                    "name": user.name,
                    "role": "bot",
                    "avatar_url": origin + avatar(user.id, user.updated_at),
                },
                "message": {
                    "id": message.id,
                    "url": f"{origin}/rooms/{room_id}/messages/{message.id}",
                },
            },
            status=201,
        )
    if request.method == "DELETE":
        boost = Boost.objects.filter(id=id, message=message, booster=user).first()
        if not boost:
            raise Http404
        boost.delete()
        publish(
            rails.stream(message.room),
            f'<turbo-stream action="remove" target="boost_{id}"></turbo-stream>',
        )
        return HttpResponse(status=204)
    return HttpResponse(status=405)
