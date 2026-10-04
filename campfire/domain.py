"""Transaction owners keep persisted changes and publication order together."""

import uuid
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q

from . import rails
from .models import (
    Attachment,
    Boost,
    Membership,
    Message,
    RichText,
    Room,
    User,
    Webhook,
    now,
)
from .richtext import mention_ids, plain_text, reconcile_embeds, sanitize
from .storage import staged_files


def user_rooms(user):
    return Room.objects.filter(memberships__user=user)


def get_room(user, id):
    return user_rooms(user).filter(id=id).first()


def presentation_messages(query):
    return query.select_related("creator", "room").prefetch_related("boosts")


def grant_memberships(room, users):
    Membership.objects.bulk_create(
        [
            Membership(
                room=room,
                user=u,
                involvement="everything"
                if room.type == "Rooms::Direct"
                else "mentions",
            )
            for u in users
        ],
        ignore_conflicts=True,
    )


@transaction.atomic
def create_user(**attributes):
    password = attributes.pop("password", "")
    import bcrypt

    attributes["password_digest"] = bcrypt.hashpw(
        password.encode()[:72], bcrypt.gensalt()
    ).decode()
    user = User.objects.create(**attributes)
    for room in Room.objects.filter(type="Rooms::Open"):
        grant_memberships(room, [user])
    return user


def index_message(message, body, attachment=None):
    text = plain_text(body) or (attachment.filename if attachment else "")
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM message_search_index WHERE rowid=%s", [message.id])
        cursor.execute(
            "INSERT INTO message_search_index(rowid,body) VALUES (%s,%s)",
            [message.id, text],
        )


def create_message(
    user, room, body="", client_id=None, attachment=None, *, webhooks=True
):
    from .storage import attach_signed, store_upload

    with staged_files(), transaction.atomic():
        # Membership is rechecked inside the write transaction, including bot writes.
        if not Membership.objects.filter(user=user, room=room).exists():
            raise PermissionError("room membership required")
        message = Message.objects.create(
            room=room, creator=user, client_message_id=client_id or str(uuid.uuid4())
        )
        rich = RichText.objects.create(
            record_type="Message", record_id=message.id, body=sanitize(body)
        )
        blob = None
        if attachment:
            blob = (
                store_upload(attachment, "Message", message.id, "attachment")
                if hasattr(attachment, "read")
                else attach_signed(attachment, "Message", message.id, "attachment")
            )
        if blob:
            from .media import process_attachment

            process_attachment(blob)
        reconcile_embeds(rich)
        index_message(message, rich.body, blob)
        Room.objects.filter(id=room.id).update(updated_at=now())
        Membership.objects.filter(room=room).exclude(user=user).exclude(
            involvement="invisible"
        ).filter(
            Q(connected_at__isnull=True)
            | Q(connected_at__lt=now() - timedelta(seconds=60))
        ).update(unread_at=message.created_at, updated_at=now())
        transaction.on_commit(lambda: publish_message(message, "append"))
        transaction.on_commit(
            lambda: enqueue_notifications(message, rich.body, webhooks=webhooks)
        )
        return message


def update_message(message, body=None, attachment=None):
    from .storage import attach_signed, remove_attachment, store_upload

    with staged_files(), transaction.atomic():
        rich = RichText.objects.filter(
            record_type="Message", record_id=message.id, name="body"
        ).first()
        if body is not None:
            body = sanitize(body)
            rich, _ = RichText.objects.update_or_create(
                record_type="Message",
                record_id=message.id,
                name="body",
                defaults={"body": body},
            )
            reconcile_embeds(rich)
        else:
            body = rich.body if rich else ""
        if attachment is not None:
            remove_attachment("Message", message.id, "attachment")
            if attachment:
                blob = (
                    store_upload(attachment, "Message", message.id, "attachment")
                    if hasattr(attachment, "read")
                    else attach_signed(attachment, "Message", message.id, "attachment")
                )
                from .media import process_attachment

                process_attachment(blob)
        message.save()
        current = (
            Attachment.objects.filter(
                record_type="Message", record_id=message.id, name="attachment"
            )
            .select_related("blob")
            .first()
        )
        index_message(message, body, current.blob if current else None)
        Room.objects.filter(id=message.room_id).update(updated_at=now())
        transaction.on_commit(lambda: publish_message(message, "replace"))


def delete_message(message):
    with transaction.atomic():
        Boost.objects.filter(message=message).delete()
        rich_ids = list(
            RichText.objects.filter(
                record_type="Message", record_id=message.id
            ).values_list("id", flat=True)
        )
        attachments = Attachment.objects.filter(
            Q(record_type="Message", record_id=message.id)
            | Q(record_type="ActionText::RichText", record_id__in=rich_ids)
        )
        blob_ids = list(attachments.values_list("blob_id", flat=True))
        attachments.delete()
        from .jobs import enqueue

        for blob_id in blob_ids:
            transaction.on_commit(lambda id=blob_id: enqueue("purge", {"blob_id": id}))
        RichText.objects.filter(record_type="Message", record_id=message.id).delete()
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM message_search_index WHERE rowid=%s", [message.id]
            )
        Message.objects.filter(id=message.id).delete()
        Room.objects.filter(id=message.room_id).update(updated_at=now())
        transaction.on_commit(lambda: publish_message(message, "remove"))


def publish_message(message, action):
    from .cable import publish
    from .rendering import message_data, render_text

    if action == "remove":
        fragment = ""
        target = "message_" + message.client_message_id
    else:
        message = Message.objects.select_related("creator", "room").get(id=message.id)
        fragment = render_text("message", message_data([message])[0])
        target = (
            f"messages_rooms_{message.room.type.split('::')[-1].lower()}_{message.room_id}"
            if action == "append"
            else "message_" + message.client_message_id
        )
    content = f'<turbo-stream action="{action}" target="{target}" maintain_scroll="true"><template>{fragment}</template></turbo-stream>'
    publish(rails.stream(message.room), content)
    if action == "append":
        for user_id in Membership.objects.filter(room_id=message.room_id).values_list(
            "user_id", flat=True
        ):
            publish(f"user_{user_id}_unreads", {"roomId": message.room_id})


def enqueue_notifications(message, body, *, webhooks=True):
    from .jobs import enqueue

    mentions = mention_ids(body)
    bot_ids = (
        Membership.objects.filter(room=message.room, user__role=2, user__status=0)
        .exclude(user=message.creator)
        .values_list("user_id", flat=True)
    )
    if message.room.type != "Rooms::Direct":
        bot_ids = [id for id in bot_ids if id in mentions]
    if webhooks:
        for webhook in Webhook.objects.filter(user_id__in=bot_ids):
            enqueue("webhook", {"webhook_id": webhook.id, "message_id": message.id})
    for membership in (
        Membership.objects.filter(room=message.room, user__status=0)
        .exclude(user=message.creator)
        .filter(
            Q(connected_at__isnull=True)
            | Q(connected_at__lt=now() - timedelta(seconds=60))
        )
    ):
        if (
            membership.involvement == "everything"
            or membership.involvement == "mentions"
            and membership.user_id in mentions
        ):
            enqueue("push", {"user_id": membership.user_id, "message_id": message.id})


def delete_room(room):
    room_id = room.id
    kind = room.type.split("::")[-1].lower()
    with transaction.atomic():
        for message in Message.objects.filter(room=room).select_related(
            "room", "creator"
        ):
            delete_message(message)
        Membership.objects.filter(room=room).delete()
        room.delete()
        from .cable import publish

        transaction.on_commit(
            lambda: publish(
                "rooms",
                f'<turbo-stream action="remove" target="list_rooms_{kind}_{room_id}"></turbo-stream>',
            )
        )
