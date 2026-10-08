"""Leased SQLite jobs survive restarts independently of the Rails application schema."""

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from django.conf import settings
from django.db import close_old_connections

from .cable import redis_client
from .push_transport import PinnedSession

_started = False
_stop = threading.Event()
_thread = None
_rates = {}
_lock = threading.Lock()


@contextmanager
def connect():
    path = settings.STORAGE_PATH / "db/jobs.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        'CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,available_at REAL NOT NULL,lease_until REAL,lease_token TEXT,status TEXT NOT NULL DEFAULT "ready",last_error TEXT)'
    )
    try:
        yield db
    finally:
        db.close()


def rate_limit(key, count, seconds):
    client = redis_client()
    if client:
        script = "local n=redis.call('INCR',KEYS[1]);if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end;return n"
        return client.eval(script, 1, "campfire:limit:" + key, seconds) <= count
    with _lock:
        times = [t for t in _rates.get(key, []) if t > time.time() - seconds]
        times.append(time.time())
        _rates[key] = times
        return len(times) <= count


def enqueue(kind, data):
    enqueue_many([(kind, data)])


def enqueue_many(jobs):
    available_at = time.time()
    rows = [
        (json.dumps({"kind": kind, "data": data}), available_at) for kind, data in jobs
    ]
    if not rows:
        return
    # Retain individual leased/retried jobs, with one durable queue transaction.
    with connect() as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("INSERT INTO jobs(payload,available_at) VALUES (?,?)", rows)


def claim():
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            'SELECT id,payload,attempts FROM jobs WHERE status="ready" AND available_at<=? AND (lease_until IS NULL OR lease_until<=?) ORDER BY id LIMIT 1',
            [time.time(), time.time()],
        ).fetchone()
        if not row:
            db.commit()
            return None
        id, payload, attempts = row
        token = secrets.token_hex(16)
        db.execute(
            "UPDATE jobs SET lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?",
            [time.time() + 120, token, id],
        )
        db.commit()
        return id, payload, attempts + 1, token


def finish(job, error=None):
    id, payload, attempts, token = job
    with connect() as db:
        if error is None:
            db.execute("DELETE FROM jobs WHERE id=? AND lease_token=?", [id, token])
        else:
            db.execute(
                "UPDATE jobs SET lease_until=NULL,lease_token=NULL,available_at=?,status=?,last_error=? WHERE id=? AND lease_token=?",
                [
                    time.time() + min(300, 2**attempts),
                    "dead" if attempts >= 5 else "ready",
                    str(error)[:1000],
                    id,
                    token,
                ],
            )


def start():
    global _started, _thread
    if _started:
        return
    _started = True
    _stop.clear()
    _thread = threading.Thread(target=work, name="campfire-jobs", daemon=True)
    _thread.start()


def stop():
    global _started
    _stop.set()
    if _thread:
        _thread.join(timeout=10)
    _started = False


def work():
    while not _stop.is_set():
        job = None
        try:
            job = claim()
            if not job:
                _stop.wait(0.25)
                continue
            close_old_connections()
            payload = json.loads(job[1])
            perform(payload["kind"], payload["data"])
            finish(job)
        except Exception as error:
            logging.exception("Campfire job failed")
            if job:
                finish(job, error)
            else:
                _stop.wait(1)
        finally:
            close_old_connections()


def permitted_endpoint(endpoint):
    from .push_transport import resolve

    try:
        resolve(endpoint)
        return True
    except (ValueError, TypeError, OSError):
        return False


def perform(kind, data):
    from .domain import create_message, delete_message
    from .models import Membership, Message, PushSubscription, RichText, Webhook
    from .richtext import plain_text

    if kind == "purge":
        from .storage import purge_blob

        purge_blob(data["blob_id"])
        return
    if kind == "ban-content":
        for message in Message.objects.filter(
            creator_id=data["user_id"]
        ).select_related("room", "creator"):
            delete_message(message)
        return
    message = (
        Message.objects.select_related("room", "creator")
        .filter(id=data["message_id"])
        .first()
    )
    if not message:
        return
    rich = RichText.objects.filter(record_type="Message", record_id=message.id).first()
    body = rich.body if rich else ""
    if kind == "webhook":
        import httpx

        webhook = (
            Webhook.objects.select_related("user")
            .filter(id=data["webhook_id"], user__status=0)
            .first()
        )
        if (
            not webhook
            or not Membership.objects.filter(
                user=webhook.user, room=message.room
            ).exists()
        ):
            return
        payload = {
            "user": {"id": message.creator_id, "name": message.creator.name},
            "room": {
                "id": message.room_id,
                "name": message.room.name,
                "path": f"/rooms/{message.room_id}/{webhook.user.id}-{webhook.user.bot_token}/messages",
            },
            "message": {
                "id": message.id,
                "body": {
                    "html": body,
                    "plain": plain_text(body)
                    .replace("@" + webhook.user.name, "")
                    .strip(),
                },
                "path": f"/rooms/{message.room_id}/@{message.id}",
            },
        }
        try:
            response = httpx.post(webhook.url, json=payload, timeout=7)
        except httpx.TimeoutException:
            create_message(
                webhook.user,
                message.room,
                "Failed to respond within 7 seconds",
                webhooks=False,
            )
            return
        content_type = response.headers.get("content-type", "").split(";")[0]
        if response.status_code == 200 and content_type in ("text/plain", "text/html"):
            create_message(webhook.user, message.room, response.text, webhooks=False)
        elif content_type and response.content:
            import mimetypes

            from django.core.files.uploadedfile import SimpleUploadedFile

            upload = SimpleUploadedFile(
                "attachment" + (mimetypes.guess_extension(content_type) or ".bin"),
                response.content,
                content_type,
            )
            create_message(
                webhook.user, message.room, attachment=upload, webhooks=False
            )
    elif kind == "push":
        private = os.environ.get("VAPID_PRIVATE_KEY")
        if not private:
            return
        from pywebpush import WebPushException, webpush

        payload = {
            "title": message.creator.name
            if message.room.type == "Rooms::Direct"
            else message.room.name,
            "body": plain_text(body)
            if message.room.type == "Rooms::Direct"
            else message.creator.name + ": " + plain_text(body),
            "path": f"/rooms/{message.room_id}",
            "badge": Membership.objects.filter(
                user_id=data["user_id"], unread_at__isnull=False
            ).count(),
        }
        for subscription in PushSubscription.objects.filter(user_id=data["user_id"]):
            if not permitted_endpoint(subscription.endpoint):
                continue
            try:
                webpush(
                    {
                        "endpoint": subscription.endpoint,
                        "keys": {
                            "p256dh": subscription.p256dh_key,
                            "auth": subscription.auth_key,
                        },
                    },
                    json.dumps(
                        {
                            "title": payload["title"],
                            "options": {
                                "body": payload["body"],
                                "data": {
                                    "path": payload["path"],
                                    "badge": payload["badge"],
                                },
                            },
                        }
                    ),
                    requests_session=PinnedSession(),
                    vapid_private_key=private,
                    vapid_claims={
                        "sub": os.environ.get(
                            "VAPID_SUBJECT", "mailto:campfire@example.com"
                        )
                    },
                    timeout=7,
                )
            except WebPushException as error:
                if error.response is not None and error.response.status_code in (
                    404,
                    410,
                ):
                    subscription.delete()
