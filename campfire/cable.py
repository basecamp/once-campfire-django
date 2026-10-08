"""Action Cable wire protocol with per-publication access checks and shared Redis pub/sub."""

import asyncio
import json
import os
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from django.db import close_old_connections, transaction

from . import rails
from .models import Membership, Room, Session, now

_clients = set()
_loop = None
_redis = None


def redis_client():
    global _redis
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    if _redis is None:
        import redis

        _redis = redis.Redis.from_url(url)
    return _redis


def publish(stream, value):
    publish_many([(stream, value)])


def publish_many(publications):
    payloads = [
        json.dumps(
            {"stream": stream, "message": value},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for stream, value in publications
    ]
    if not payloads:
        return
    client = redis_client()
    if client:
        # Send ordered individual publications in one Redis round trip.
        with client.pipeline(transaction=False) as pipeline:
            for payload in payloads:
                pipeline.publish("campfire:cable", payload)
            pipeline.execute()
    elif _loop:
        for payload in payloads:
            _loop.call_soon_threadsafe(deliver, payload)


def deliver(payload):
    for client in list(_clients):
        try:
            client.queue.put_nowait(payload)
        except asyncio.QueueFull:
            client.closed = True


async def relay():
    import redis.asyncio as redis

    client = redis.Redis.from_url(os.environ["REDIS_URL"])
    async with client.pubsub() as sub:
        await sub.subscribe("campfire:cable")
        async for event in sub.listen():
            if event["type"] == "message":
                deliver(event["data"])


@sync_to_async
def authenticate(raw):
    close_old_connections()
    try:
        cookies = SimpleCookie(raw)
        token = rails.verify_cookie("session_token", cookies["session_token"].value)
        session = (
            Session.objects.select_related("user")
            .filter(token=token, user__status=0)
            .first()
        )
        return (
            (session.id, session.user_id, session.user.name)
            if session and session.user.role != 2
            else None
        )
    except (ValueError, KeyError):
        return None
    finally:
        close_old_connections()


@sync_to_async
def authorize(session_id, user_id, identifier):
    close_old_connections()
    try:
        if not Session.objects.filter(
            id=session_id, user_id=user_id, user__status=0
        ).exists():
            return None
        params = json.loads(identifier)
        if not isinstance(params, dict):
            return None
        channel = params.get("channel")
        room_id = 0
        stream = ""
        if channel in ("ApplicationCable::Channel", "HeartbeatChannel"):
            pass
        elif channel in ("ReadRoomsChannel", "UnreadRoomsChannel"):
            stream = f"user_{user_id}_" + (
                "reads" if channel == "ReadRoomsChannel" else "unreads"
            )
        elif channel == "RoomMessagesChannel":
            stream = rails.verify_stream(params.get("signed_stream_name", ""))
            if not isinstance(stream, str):
                return None
            gid, suffix = stream.split(":", 1)
            if suffix != "messages":
                return None
            parts = rails.decode64(gid).decode().split("/")
            if (
                len(parts) != 5
                or parts[:3] != ["gid:", "", "campfire"]
                or parts[3]
                not in ["Room", "Rooms::Open", "Rooms::Closed", "Rooms::Direct"]
            ):
                return None
            room_id = int(parts[4])
            room = Room.objects.filter(id=room_id, memberships__user_id=user_id).first()
            if not room or parts[3] not in ["Room", room.type]:
                return None
        elif channel in (
            "RoomChannel",
            "PresenceChannel",
            "TypingNotificationsChannel",
        ):
            room_id = int(params.get("room_id", 0))
            if not Membership.objects.filter(room_id=room_id, user_id=user_id).exists():
                return None
            stream = f"{channel}:{room_id}"
        elif channel == "Turbo::StreamsChannel":
            stream = rails.verify_stream(params.get("signed_stream_name", ""))
            if not isinstance(stream, str):
                return None
            own = (
                rails.b64(f"gid://campfire/User/{user_id}".encode()).rstrip("=")
                + ":rooms"
            )
            if stream not in ["rooms", own]:
                return None
        else:
            return None
        return {"channel": channel, "room": room_id, "stream": stream}
    except (ValueError, TypeError, KeyError):
        return None
    finally:
        close_old_connections()


@sync_to_async
def presence(user_id, room_id, action):
    from datetime import timedelta

    close_old_connections()
    try:
        with transaction.atomic():
            membership = Membership.objects.filter(
                room_id=room_id, user_id=user_id
            ).first()
            if not membership:
                return
            active = (
                membership.connected_at
                and membership.connected_at >= now() - timedelta(seconds=60)
            )
            if action in ("present", "refresh"):
                count = (
                    (
                        membership.connections + 1
                        if action == "present"
                        else membership.connections
                    )
                    if active
                    else 1
                )
                Membership.objects.filter(id=membership.id).update(
                    connections=count,
                    connected_at=now(),
                    unread_at=None,
                    updated_at=now(),
                )
                if action == "present":
                    transaction.on_commit(
                        lambda: publish(f"user_{user_id}_reads", {"room_id": room_id})
                    )
            else:
                count = max(0, membership.connections - 1) if active else 0
                Membership.objects.filter(id=membership.id).update(
                    connections=count,
                    connected_at=membership.connected_at if count else None,
                    updated_at=now(),
                )
    finally:
        close_old_connections()


@sync_to_async
def session_alive(session_id, user_id):
    close_old_connections()
    try:
        return Session.objects.filter(
            id=session_id, user_id=user_id, user__status=0
        ).exists()
    finally:
        close_old_connections()


class Client:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=256)
        self.closed = False


async def websocket(scope, receive, send):
    global _loop
    _loop = asyncio.get_running_loop()
    headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
    origin = headers.get("origin")
    host = headers.get("host", "")
    if origin and (
        urlsplit(origin).netloc != host
        or urlsplit(origin).scheme
        != ("https" if scope.get("scheme") == "wss" else "http")
    ):
        await send({"type": "websocket.close", "code": 1008})
        return
    identity = await authenticate(headers.get("cookie", ""))
    if not identity:
        await send({"type": "websocket.close", "code": 1008})
        return
    session_id, user_id, user_name = identity
    await receive()
    protocols = scope.get("subprotocols", [])
    if "actioncable-v1-json" not in protocols:
        await send({"type": "websocket.close", "code": 1002})
        return
    await send({"type": "websocket.accept", "subprotocol": "actioncable-v1-json"})
    client = Client()
    _clients.add(client)
    subscriptions = {}

    async def frame(value):
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            }
        )

    await frame({"type": "welcome"})

    async def pump():
        while not client.closed:
            try:
                raw = await asyncio.wait_for(client.queue.get(), timeout=3)
            except asyncio.TimeoutError:
                if not await session_alive(session_id, user_id):
                    await frame(
                        {
                            "type": "disconnect",
                            "reason": "unauthorized",
                            "reconnect": False,
                        }
                    )
                    client.closed = True
                    break
                await frame({"type": "ping", "message": int(time.time())})
                continue
            event = json.loads(raw)
            for identifier, sub in list(subscriptions.items()):
                if sub["stream"] != event["stream"]:
                    continue
                if not await authorize(session_id, user_id, identifier):
                    subscriptions.pop(identifier, None)
                    await frame(
                        {"type": "reject_subscription", "identifier": identifier}
                    )
                    continue
                await frame({"identifier": identifier, "message": event["message"]})
        await send({"type": "websocket.close", "code": 1008})

    task = asyncio.create_task(pump())
    try:
        while not client.closed:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                break
            raw = message.get("text")
            if raw is None or len(raw) > 65536:
                break
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    break
                identifier = data.get("identifier", "")
                command = data.get("command")
                if not isinstance(identifier, str) or len(identifier) > 8192:
                    break
            except ValueError:
                break
            if command == "subscribe":
                sub = await authorize(session_id, user_id, identifier)
                if sub:
                    if (
                        identifier not in subscriptions
                        and sub["channel"] == "PresenceChannel"
                    ):
                        await presence(user_id, sub["room"], "present")
                    subscriptions[identifier] = sub
                    await frame(
                        {"type": "confirm_subscription", "identifier": identifier}
                    )
                else:
                    await frame(
                        {"type": "reject_subscription", "identifier": identifier}
                    )
            elif command == "unsubscribe":
                sub = subscriptions.pop(identifier, None)
                if sub and sub["channel"] == "PresenceChannel":
                    await presence(user_id, sub["room"], "absent")
            elif command == "message" and identifier in subscriptions:
                sub = await authorize(session_id, user_id, identifier)
                if not sub:
                    continue
                try:
                    body = json.loads(data["data"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not isinstance(body, dict):
                    continue
                action = body.get("action")
                if sub["channel"] == "PresenceChannel" and action == "refresh":
                    await presence(user_id, sub["room"], "refresh")
                elif sub["channel"] == "TypingNotificationsChannel" and action in (
                    "start",
                    "stop",
                ):
                    await sync_to_async(publish)(
                        sub["stream"],
                        {"action": action, "user": {"id": user_id, "name": user_name}},
                    )
    finally:
        task.cancel()
        _clients.discard(client)
        for sub in subscriptions.values():
            if sub["channel"] == "PresenceChannel":
                await presence(user_id, sub["room"], "absent")
