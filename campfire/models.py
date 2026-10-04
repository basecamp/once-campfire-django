"""Unmanaged Django models retain the existing Rails installation schema."""

import os
from datetime import datetime

from django.db import models
from django.utils import timezone


def now():
    frozen = os.environ.get("CAMPFIRE_FROZEN_TIME")
    return (
        datetime.fromisoformat(frozen.replace("Z", "+00:00"))
        if frozen
        else timezone.now()
    )


class Record(models.Model):
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(default=now)

    class Meta:
        abstract = True
        managed = False

    def save(self, *args, **kwargs):
        self.updated_at = now()
        super().save(*args, **kwargs)


class User(Record):
    name = models.CharField(max_length=255)
    email_address = models.CharField(max_length=255, null=True, unique=True)
    password_digest = models.CharField(max_length=255, null=True)
    bio = models.TextField(null=True)
    bot_token = models.CharField(max_length=255, null=True, unique=True)
    role = models.IntegerField(default=0)
    status = models.IntegerField(default=0)

    class Meta(Record.Meta):
        db_table = "users"

    def can_administer(self, record=None):
        return self.role == 1 or (
            record is not None and getattr(record, "creator_id", None) == self.id
        )

    @property
    def title(self):
        return " – ".join(filter(None, [self.name, self.bio]))


class Account(Record):
    name = models.CharField(max_length=255)
    join_code = models.CharField(max_length=255)
    custom_styles = models.TextField(null=True)
    settings = models.JSONField(null=True, default=dict)
    singleton_guard = models.IntegerField(default=0)

    class Meta(Record.Meta):
        db_table = "accounts"


class Room(Record):
    name = models.CharField(max_length=255, null=True)
    type = models.CharField(max_length=255)
    creator = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, related_name="created_rooms"
    )

    class Meta(Record.Meta):
        db_table = "rooms"


class Membership(Record):
    room = models.ForeignKey(
        Room, on_delete=models.DO_NOTHING, related_name="memberships"
    )
    user = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, related_name="memberships"
    )
    involvement = models.CharField(max_length=255, default="mentions")
    connected_at = models.DateTimeField(null=True)
    connections = models.IntegerField(default=0)
    unread_at = models.DateTimeField(null=True)

    class Meta(Record.Meta):
        db_table = "memberships"


class Message(Record):
    room = models.ForeignKey(Room, on_delete=models.DO_NOTHING, related_name="messages")
    creator = models.ForeignKey(
        User, on_delete=models.DO_NOTHING, related_name="messages"
    )
    client_message_id = models.CharField(max_length=255)

    class Meta(Record.Meta):
        db_table = "messages"


class RichText(Record):
    name = models.CharField(max_length=255, default="body")
    body = models.TextField(null=True)
    record_id = models.BigIntegerField()
    record_type = models.CharField(max_length=255, default="Message")

    class Meta(Record.Meta):
        db_table = "action_text_rich_texts"


class Blob(models.Model):
    byte_size = models.BigIntegerField()
    checksum = models.CharField(max_length=255, null=True)
    content_type = models.CharField(max_length=255, null=True)
    created_at = models.DateTimeField(default=now)
    filename = models.CharField(max_length=255)
    key = models.CharField(max_length=255, unique=True)
    metadata = models.TextField(default="{}")
    service_name = models.CharField(max_length=255, default="local")

    class Meta:
        db_table = "active_storage_blobs"
        managed = False


class Attachment(models.Model):
    blob = models.ForeignKey(Blob, on_delete=models.DO_NOTHING)
    created_at = models.DateTimeField(default=now)
    name = models.CharField(max_length=255)
    record_id = models.BigIntegerField()
    record_type = models.CharField(max_length=255)

    class Meta:
        db_table = "active_storage_attachments"
        managed = False


class Variant(models.Model):
    blob = models.ForeignKey(Blob, on_delete=models.DO_NOTHING)
    variation_digest = models.CharField(max_length=255)

    class Meta:
        db_table = "active_storage_variant_records"
        managed = False


class Boost(Record):
    message = models.ForeignKey(
        Message, on_delete=models.DO_NOTHING, related_name="boosts"
    )
    booster = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    content = models.CharField(max_length=16)

    class Meta(Record.Meta):
        db_table = "boosts"


class Session(Record):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    token = models.CharField(max_length=255, unique=True)
    user_agent = models.CharField(max_length=255, null=True)
    ip_address = models.CharField(max_length=255, null=True)
    last_active_at = models.DateTimeField(default=now)

    class Meta(Record.Meta):
        db_table = "sessions"


class Search(Record):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    query = models.CharField(max_length=255)

    class Meta(Record.Meta):
        db_table = "searches"


class Ban(Record):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    ip_address = models.CharField(max_length=255)

    class Meta(Record.Meta):
        db_table = "bans"


class PushSubscription(Record):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    endpoint = models.TextField(null=True)
    p256dh_key = models.TextField(null=True)
    auth_key = models.TextField(null=True)
    user_agent = models.TextField(null=True)

    class Meta(Record.Meta):
        db_table = "push_subscriptions"


class Webhook(Record):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING)
    url = models.TextField(null=True)

    class Meta(Record.Meta):
        db_table = "webhooks"
