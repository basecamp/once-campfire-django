"""Exercise complete operations against a real temporary Rails-shaped SQLite database."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "campfire.settings")
os.environ.setdefault("SECRET_KEY_BASE", "tests-only")
import django

django.setup()
import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings
from PIL import Image

from campfire import jobs, rails
from campfire.domain import (
    create_message,
    create_user,
    grant_memberships,
    update_message,
)
from campfire.models import (
    Account,
    Attachment,
    Blob,
    Boost,
    Membership,
    Message,
    RichText,
    Room,
    User,
    Webhook,
    now,
)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        jobs._rates.clear()
        self.directory = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / "tmp"
        )
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "db/production.sqlite3"
        self.database.parent.mkdir()
        self.files = self.root / "files"
        self.files.mkdir()
        self.old_name = connection.settings_dict["NAME"]
        connection.close()
        connection.settings_dict["NAME"] = self.database
        self.addCleanup(self.restore_connection)
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript(
                (
                    Path(__file__).resolve().parents[1] / "campfire/schema.sql"
                ).read_text()
            )
        self.settings = override_settings(
            STORAGE_PATH=self.root, FILES_PATH=self.files, DATABASE_PATH=self.database
        )
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        Account.objects.create(name="Campfire", join_code="invitation")
        self.admin = create_user(
            name="Admin", email_address="admin@example.test", password="secret", role=1
        )
        self.member = create_user(
            name="Member", email_address="member@example.test", password="secret"
        )
        self.other = create_user(
            name="Other", email_address="other@example.test", password="secret"
        )
        self.room = Room.objects.create(
            name="All Talk", type="Rooms::Open", creator=self.admin
        )
        grant_memberships(self.room, [self.admin, self.member, self.other])
        self.private = Room.objects.create(
            name="Private", type="Rooms::Closed", creator=self.admin
        )
        grant_memberships(self.private, [self.admin])
        self.client = Client()
        self.token = self.login(self.client, self.admin)

    def restore_connection(self):
        connection.close()
        connection.settings_dict["NAME"] = self.old_name

    def login(self, client, user):
        response = client.get("/session/new")
        token = re.search(
            r'name="csrf-token" content="([^"]+)"', response.content.decode()
        )[1]
        response = client.post(
            "/session",
            {
                "email_address": user.email_address,
                "password": "secret",
                "authenticity_token": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        return token

    def post(self, path, data=None, method=None, client=None, token=None):
        data = dict(data or {})
        data["authenticity_token"] = token or self.token
        if method:
            data["_method"] = method
        return (client or self.client).post(path, data)

    def message(self, user=None, room=None, body="Coffee <strong>is good</strong>"):
        return create_message(user or self.admin, room or self.room, body)

    def test_populated_room_history_sidebar_search_are_real_pages(self):
        message = self.message()
        for path in [
            f"/rooms/{self.room.id}",
            f"/rooms/{self.room.id}/messages",
            "/searches?q=coffee",
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'data-message-id="{message.id}"'.encode(), response.content)
            self.assertIn(b"Coffee", response.content)
        self.assertIn(b"All Talk", self.client.get("/users/me/sidebar").content)

    def test_writes_update_fts_touch_room_and_mark_only_disconnected_members_unread(
        self,
    ):
        Membership.objects.filter(user=self.member, room=self.room).update(
            connected_at=now(), connections=1
        )
        response = self.post(
            f"/rooms/{self.room.id}/messages",
            {
                "message[body]": "fresh coffee",
                "message[client_message_id]": "from-browser",
            },
        )
        self.assertEqual(response.status_code, 200)
        message = Message.objects.get(client_message_id="from-browser")
        self.assertIn(b"message_from-browser", response.content)
        self.assertIsNotNone(
            Membership.objects.get(user=self.other, room=self.room).unread_at
        )
        self.assertIsNone(
            Membership.objects.get(user=self.member, room=self.room).unread_at
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT body FROM message_search_index WHERE rowid=%s", [message.id]
            )
            self.assertEqual(cursor.fetchone()[0], "fresh coffee")
        self.assertEqual(
            self.post(
                f"/rooms/{self.room.id}/messages/{message.id}",
                {"message[body]": "new tea"},
                "patch",
            ).status_code,
            302,
        )
        self.assertNotIn(b"fresh coffee", self.client.get("/searches?q=coffee").content)
        self.assertIn(b"new tea", self.client.get("/searches?q=tea").content)
        self.assertEqual(
            self.post(
                f"/rooms/{self.room.id}/messages/{message.id}", method="delete"
            ).status_code,
            200,
        )
        self.assertFalse(Message.objects.filter(id=message.id).exists())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM message_search_index WHERE rowid=%s", [message.id]
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_human_blank_message_allowed_and_scripts_sanitized(self):
        self.assertEqual(
            self.post(
                f"/rooms/{self.room.id}/messages", {"message[body]": ""}
            ).status_code,
            200,
        )
        message = self.message(
            body='<script>alert(1)</script><a href="javascript:alert(2)">safe</a>'
        )
        body = RichText.objects.get(record_id=message.id, record_type="Message").body
        self.assertNotIn("script", body)
        self.assertNotIn("javascript", body)

    def test_membership_required_even_for_open_rooms(self):
        Membership.objects.filter(room=self.room, user=self.member).delete()
        client = Client()
        token = self.login(client, self.member)
        self.assertEqual(client.get(f"/rooms/{self.room.id}").status_code, 302)
        self.assertEqual(
            self.post(
                f"/rooms/{self.room.id}/messages",
                {"message[body]": "intrusion"},
                client=client,
                token=token,
            ).status_code,
            302,
        )
        self.assertEqual(Message.objects.count(), 0)

    def test_search_and_sidebar_do_not_leak_closed_rooms(self):
        self.message(room=self.private, body="coffee confidential")
        client = Client()
        self.login(client, self.member)
        self.assertNotIn(b"confidential", client.get("/searches?q=coffee").content)
        self.assertNotIn(b"Private", client.get("/users/me/sidebar").content)

    def test_other_member_cannot_edit_message(self):
        message = self.message()
        client = Client()
        token = self.login(client, self.member)
        self.assertEqual(
            client.get(f"/rooms/{self.room.id}/messages/{message.id}/edit").status_code,
            403,
        )
        self.assertEqual(
            self.post(
                f"/rooms/{self.room.id}/messages/{message.id}",
                {"message[body]": "hacked"},
                "patch",
                client,
                token,
            ).status_code,
            403,
        )

    def test_direct_namespace_cannot_widen_room_privacy(self):
        direct = Room.objects.create(type="Rooms::Direct", creator=self.admin)
        grant_memberships(direct, [self.admin, self.member])
        self.assertEqual(
            self.client.get(f"/rooms/opens/{direct.id}/edit").status_code, 404
        )
        self.assertEqual(
            self.post(
                f"/rooms/opens/{direct.id}", {"room[name]": "exposed"}, "patch"
            ).status_code,
            404,
        )
        direct.refresh_from_db()
        self.assertEqual(direct.type, "Rooms::Direct")

    def test_boost_ownership_and_content_limit(self):
        message = self.message()
        self.assertEqual(
            self.post(
                f"/messages/{message.id}/boosts", {"boost[content]": "👍"}
            ).status_code,
            302,
        )
        boost = Boost.objects.get(message=message)
        client = Client()
        token = self.login(client, self.member)
        self.assertEqual(
            self.post(
                f"/messages/{message.id}/boosts/{boost.id}",
                method="delete",
                client=client,
                token=token,
            ).status_code,
            404,
        )
        self.assertEqual(
            self.post(
                f"/messages/{message.id}/boosts", {"boost[content]": "x" * 17}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.post(
                f"/messages/{message.id}/boosts/{boost.id}", method="delete"
            ).status_code,
            200,
        )

    def test_csrf_and_cross_origin_fail_closed(self):
        self.assertEqual(
            self.client.post(
                f"/rooms/{self.room.id}/messages", {"message[body]": "hacked"}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                f"/rooms/{self.room.id}/messages",
                {"message[body]": "hacked", "authenticity_token": self.token},
                HTTP_ORIGIN="https://attacker.test",
            ).status_code,
            422,
        )
        self.assertEqual(Message.objects.count(), 0)

    def test_bot_cannot_enter_browser_routes_and_real_raw_api(self):
        bot = create_user(name="Bot", role=2, bot_token="abcdefgh1234")
        grant_memberships(self.room, [bot])
        key = f"{bot.id}-{bot.bot_token}"
        client = Client()
        self.assertEqual(
            client.get(f"/rooms/{self.room.id}?bot_key={key}").status_code, 403
        )
        response = client.post(
            f"/rooms/{self.room.id}/{key}/messages",
            "plain webhook reply",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("/messages/", response["Location"])
        message = Message.objects.get(creator=bot)
        self.assertEqual(
            RichText.objects.get(record_id=message.id).body, "plain webhook reply"
        )
        self.assertEqual(
            client.post(
                f"/rooms/{self.private.id}/{key}/messages",
                "blocked",
                content_type="text/plain",
            ).status_code,
            404,
        )
        self.assertEqual(
            client.post(
                f"/rooms/{self.room.id}/wrong-key/messages",
                "no-auth",
                content_type="text/plain",
            ).status_code,
            422,
        )

    def test_image_upload_creates_real_thumb_metadata_and_range_download(self):
        output = io.BytesIO()
        Image.new("RGB", (1600, 1000), "red").save(output, "PNG")
        raw = output.getvalue()
        response = self.post(
            f"/rooms/{self.room.id}/messages",
            {
                "message[body]": "",
                "message[attachment]": SimpleUploadedFile(
                    "picture.png", raw, "image/png"
                ),
            },
        )
        self.assertEqual(response.status_code, 200)
        message = Message.objects.latest("id")
        original = Attachment.objects.get(
            record_type="Message", record_id=message.id
        ).blob
        self.assertEqual(json.loads(original.metadata)["width"], 1600)
        from campfire.media import representation_url, variant

        thumb = variant(original, [1200, 800])
        self.assertLess(thumb.byte_size, original.byte_size)
        self.assertEqual(json.loads(thumb.metadata)["width"], 1200)
        response = self.client.get(representation_url(original))
        self.assertEqual(response.status_code, 200)
        response.close()
        from campfire.storage import blob_url

        ranged = self.client.get(blob_url(original), HTTP_RANGE="bytes=0-9")
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, raw[:10])
        member = Client()
        self.login(member, self.member)
        Membership.objects.filter(user=self.member, room=self.room).delete()
        self.assertEqual(member.get(blob_url(original)).status_code, 403)

    def test_attachment_transaction_failure_cleans_files_and_rows(self):
        output = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(output, "PNG")
        with patch(
            "campfire.media.process_attachment",
            side_effect=RuntimeError("analysis failed"),
        ):
            with self.assertRaises(RuntimeError):
                create_message(
                    self.admin,
                    self.room,
                    attachment=SimpleUploadedFile(
                        "picture.png", output.getvalue(), "image/png"
                    ),
                )
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(Blob.objects.count(), 0)
        self.assertFalse(any(p.is_file() for p in self.files.rglob("*")))

    def test_new_user_granted_open_room_membership(self):
        user = create_user(
            name="Joined", email_address="joined@example.test", password="secret"
        )
        self.assertTrue(Membership.objects.filter(user=user, room=self.room).exists())
        self.assertFalse(
            Membership.objects.filter(user=user, room=self.private).exists()
        )

    def test_job_leases_recover_and_retry_is_bounded(self):
        jobs.enqueue("test", {"number": 1})
        job = jobs.claim()
        self.assertIsNotNone(job)
        self.assertIsNone(jobs.claim())
        with jobs.connect() as db:
            db.execute("UPDATE jobs SET lease_until=0")
        reclaimed = jobs.claim()
        self.assertEqual(reclaimed[0], job[0])
        self.assertNotEqual(reclaimed[3], job[3])
        jobs.finish(job)
        self.assertIsNone(jobs.claim())
        jobs.finish(reclaimed, RuntimeError("failed"))
        with jobs.connect() as db:
            db.execute("UPDATE jobs SET available_at=0,attempts=4")
        final = jobs.claim()
        jobs.finish(final, RuntimeError("last failure"))
        with jobs.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM jobs").fetchone()[0], "dead"
            )

    def test_push_endpoint_private_dns_and_lookalike_hosts_rejected(self):
        with patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]
        ):
            self.assertFalse(
                jobs.permitted_endpoint("https://fcm.googleapis.com/message")
            )
        self.assertFalse(
            jobs.permitted_endpoint("https://fcm.googleapis.com.attacker.test/message")
        )
        self.assertFalse(jobs.permitted_endpoint("http://fcm.googleapis.com/message"))

    def test_signed_inline_embeds_persist_and_mention_plaintext_is_indexed(self):
        from campfire.richtext import plain_text, render_body
        from campfire.storage import store_upload

        output = io.BytesIO()
        Image.new("RGB", (20, 20), "blue").save(output, "PNG")
        blob = store_upload(
            SimpleUploadedFile("inline.png", output.getvalue(), "image/png"),
            "User",
            self.admin.id,
            "unused",
        )
        body = (
            '<p>Hello <action-text-attachment sgid="'
            + rails.sgid("User", self.member.id)
            + '"></action-text-attachment> <action-text-attachment sgid="'
            + rails.sgid("ActiveStorage::Blob", blob.id)
            + '"></action-text-attachment></p>'
        )
        message = self.message(body=body)
        rich = RichText.objects.get(record_type="Message", record_id=message.id)
        self.assertTrue(
            Attachment.objects.filter(
                record_type="ActionText::RichText", record_id=rich.id, blob=blob
            ).exists()
        )
        self.assertIn("@Member", plain_text(body))
        self.assertIn("inline.png", render_body(body))
        self.assertIn('class="mention"', render_body(body))
        response = self.client.get("/searches?q=Member")
        self.assertIn(f'data-message-id="{message.id}"'.encode(), response.content)
        corrupted = rails.sgid("ActiveStorage::Blob", blob.id)[:-1] + "0"
        self.assertNotIn(
            "inline.png",
            render_body(
                '<action-text-attachment sgid="'
                + corrupted
                + '"></action-text-attachment>'
            ),
        )

    def test_opengraph_rebuilt_safely_and_plain_links_linked(self):
        from campfire.richtext import render_body

        html = render_body("<p>https://example.com/test.</p>")
        self.assertIn('href="https://example.com/test"', html)
        content = '<action-text-attachment content-type="application/vnd.actiontext.opengraph-embed" href="https://campfire.example.com/rooms/1" url="https://campfire.example.com/evil.png" filename="Title" caption="Description"></action-text-attachment>'
        result = render_body(content, "campfire.example.com")
        self.assertIn("Title", result)
        self.assertNotIn("href=", result)
        self.assertNotIn("<img", result)

    def test_admin_account_rows_unique_and_direct_room_all_members_can_delete(self):
        response = self.client.get("/account/edit")
        self.assertEqual(
            response.content.count(f'id="role_user_{self.admin.id}"'.encode()), 1
        )
        direct = Room.objects.create(type="Rooms::Direct", creator=self.admin)
        grant_memberships(direct, [self.admin, self.member])
        client = Client()
        token = self.login(client, self.member)
        self.assertEqual(
            self.post(
                f"/rooms/directs/{direct.id}",
                method="delete",
                client=client,
                token=token,
            ).status_code,
            302,
        )
        self.assertFalse(Room.objects.filter(id=direct.id).exists())

    def test_signed_direct_upload_and_tamper_rejection(self):
        import base64
        import hashlib
        from urllib.parse import urlsplit

        raw = b"a direct upload"
        response = self.client.post(
            "/rails/active_storage/direct_uploads",
            data=json.dumps(
                {
                    "blob": {
                        "filename": "document.txt",
                        "byte_size": len(raw),
                        "checksum": base64.b64encode(
                            hashlib.md5(raw).digest()
                        ).decode(),
                        "content_type": "text/plain",
                    }
                }
            ),
            content_type="application/json",
            HTTP_X_CSRF_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        path = urlsplit(data["direct_upload"]["url"]).path
        self.assertEqual(
            Client().put(path, data=raw, content_type="text/plain").status_code, 204
        )
        from campfire.storage import blob_url

        blob = Blob.objects.get(id=data["id"])
        draft = self.client.get(blob_url(blob))
        self.assertEqual(draft.status_code, 200)
        draft.close()
        other = Client()
        self.login(other, self.other)
        self.assertEqual(other.get(blob_url(blob)).status_code, 403)
        self.assertEqual(
            Client().put(path + "bad", data=raw, content_type="text/plain").status_code,
            422,
        )
        response = self.post(
            f"/rooms/{self.room.id}/messages",
            {"message[body]": "", "message[attachment]": data["signed_id"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Attachment.objects.filter(
                record_type="Message", blob_id=data["id"]
            ).count(),
            1,
        )

    def test_backup_and_restore_preserve_rows_and_queue(self):
        from django.core.management import call_command

        message = self.message(body="backed up")
        jobs.enqueue("purge-blob", {"id": 123})
        call_command("backup", stdout=io.StringIO())
        connection.close()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("DELETE FROM messages")
            db.commit()
        with jobs.connect() as db:
            db.execute("DELETE FROM jobs")
        call_command("restore", stdout=io.StringIO())
        self.assertTrue(Message.objects.filter(id=message.id).exists())
        self.assertIn(
            "backed up",
            RichText.objects.get(record_id=message.id, record_type="Message").body,
        )
        with jobs.connect() as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_pdf_video_audio_native_processing_and_presentation(self):
        import subprocess
        import wave

        from campfire.media import representation_url
        from campfire.rendering import message_data

        pdf = io.BytesIO()
        Image.new("RGB", (400, 300), "white").save(pdf, "PDF")
        video = self.root / "sample.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x240:d=0.1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
            timeout=20,
        )
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0" * 1600)
        for filename, raw, mime in [
            ("sample.pdf", pdf.getvalue(), "application/pdf"),
            ("sample.mp4", video.read_bytes(), "video/mp4"),
            ("sample.wav", audio.getvalue(), "audio/wav"),
        ]:
            with self.subTest(mime=mime):
                message = create_message(
                    self.admin,
                    self.room,
                    "",
                    attachment=SimpleUploadedFile(filename, raw, mime),
                )
                blob = Attachment.objects.get(
                    record_type="Message", record_id=message.id
                ).blob
                rendered = str(message_data([message])[0].HTML)
                if mime == "audio/wav":
                    self.assertIn("Download", rendered)
                    self.assertGreater(json.loads(blob.metadata)["duration"], 0)
                else:
                    self.assertTrue(
                        Attachment.objects.filter(
                            record_type="ActiveStorage::Blob",
                            record_id=blob.id,
                            name="preview_image",
                        ).exists()
                    )
                    response = self.client.get(representation_url(blob))
                    self.assertEqual(response.status_code, 200)
                    response.close()
                    self.assertIn(
                        "poster=", rendered
                    ) if mime == "video/mp4" else self.assertIn(
                        "lightbox#open", rendered
                    )

    def test_first_run_preserves_avatar_and_redirects_to_room(self):
        # Exercise the actual setup endpoint on an empty account, rather than a helper roundtrip.
        Account.objects.all().delete()
        client = Client()
        page = client.get("/first_run")
        token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.content)[
            1
        ].decode()
        image = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(image, "PNG")
        response = client.post(
            "/first_run",
            {
                "authenticity_token": token,
                "user[name]": "Founder",
                "user[email_address]": "founder@example.test",
                "user[password]": "secret",
                "user[avatar]": SimpleUploadedFile(
                    "avatar.png", image.getvalue(), "image/png"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        founder = User.objects.get(email_address="founder@example.test")
        self.assertTrue(
            Attachment.objects.filter(
                record_type="User", record_id=founder.id, name="avatar"
            ).exists()
        )
        self.assertRegex(client.get("/").url, r"^/rooms/\d+$")

    def test_independent_rails_editor_plain_text_vectors(self):
        from campfire.richtext import plain_text

        vectors = json.loads(
            (Path(__file__).resolve().parents[1] / "vectors/richtext.json").read_text()
        )
        for case in vectors["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(plain_text(case["body"]), case["plain_text"])

    def test_attachment_updates_preserve_body_index_and_rollback(self):
        from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

        from campfire.storage import path_for

        def image(filename):
            raw = io.BytesIO()
            Image.new("RGB", (30, 20), "red").save(raw, "PNG")
            return SimpleUploadedFile(filename, raw.getvalue(), "image/png")

        message = create_message(
            self.admin, self.room, "retained body", attachment=image("old.png")
        )
        old = Attachment.objects.get(record_type="Message", record_id=message.id).blob
        path = f"/rooms/{self.room.id}/messages/{message.id}"
        response = self.client.patch(
            path,
            data=encode_multipart(BOUNDARY, {"message[attachment]": image("new.png")}),
            content_type=MULTIPART_CONTENT,
            HTTP_X_CSRF_TOKEN=self.token,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            RichText.objects.get(record_id=message.id, record_type="Message").body,
            "retained body",
        )
        new = Attachment.objects.get(record_type="Message", record_id=message.id).blob
        self.assertNotEqual(old.id, new.id)
        self.assertTrue(path_for(old.key).exists())
        self.assertIn(
            f'data-message-id="{message.id}"'.encode(),
            self.client.get("/searches?q=retained").content,
        )
        before = set(self.files.rglob("*"))
        with patch(
            "campfire.media.process_attachment",
            side_effect=RuntimeError("analysis failed"),
        ):
            with self.assertRaises(RuntimeError):
                update_message(message, attachment=image("failure.png"))
        self.assertEqual(
            Attachment.objects.get(record_type="Message", record_id=message.id).blob_id,
            new.id,
        )
        self.assertEqual(
            {p for p in self.files.rglob("*") if p.is_file()},
            {p for p in before if p.is_file()},
        )
        response = self.post(
            path,
            {
                "message[body]": "",
                "message[attachment]": rails.signed_id(
                    "ActiveStorage::Blob", old.id, "blob_id"
                ),
            },
            "patch",
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"old.png", self.client.get("/searches?q=old").content)
        self.assertEqual(
            Attachment.objects.get(record_type="Message", record_id=message.id).blob_id,
            old.id,
        )

        response = self.post(path, {"message[attachment]": ""}, "patch")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Attachment.objects.filter(
                record_type="Message", record_id=message.id
            ).exists()
        )

    def test_bot_multipart_update_preserves_omitted_body(self):
        from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

        bot = create_user(name="Bot", role=2, bot_token="token")
        grant_memberships(self.room, [bot])
        message = create_message(bot, self.room, "bot body")
        response = Client().patch(
            f"/rooms/{self.room.id}/{bot.id}-token/messages/{message.id}",
            data=encode_multipart(
                BOUNDARY,
                {"attachment": SimpleUploadedFile("new.txt", b"file", "text/plain")},
            ),
            content_type=MULTIPART_CONTENT,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            RichText.objects.get(record_id=message.id, record_type="Message").body,
            "bot body",
        )
        self.assertEqual(
            Attachment.objects.get(
                record_type="Message", record_id=message.id
            ).blob.filename,
            "new.txt",
        )

    def test_webhook_reply_does_not_start_another_bot_webhook(self):
        import httpx

        one = create_user(name="First bot", role=2, bot_token="one")
        two = create_user(name="Second bot", role=2, bot_token="two")
        room = Room.objects.create(type="Rooms::Direct", creator=self.admin)
        grant_memberships(room, [self.admin, one, two])
        Membership.objects.filter(room=room, user=self.admin).update(
            involvement="everything"
        )
        first = Webhook.objects.create(user=one, url="http://localhost/one")
        Webhook.objects.create(user=two, url="http://localhost/two")
        message = create_message(self.admin, room, "hello")
        with jobs.connect() as db:
            db.execute("DELETE FROM jobs")
        response = httpx.Response(
            200, headers={"content-type": "text/plain"}, text="bot reply"
        )
        with patch("httpx.post", return_value=response):
            jobs.perform("webhook", {"webhook_id": first.id, "message_id": message.id})
        self.assertEqual(Message.objects.filter(room=room).count(), 2)
        with jobs.connect() as db:
            queued = [
                json.loads(row[0])["kind"]
                for row in db.execute("SELECT payload FROM jobs")
            ]
        self.assertNotIn("webhook", queued)
        self.assertIn("push", queued)

    def test_active_uploaded_content_downloads_as_binary_attachment(self):
        from campfire.storage import blob_url

        for mime, raw in [
            ("text/html", b"<script>alert(1)</script>"),
            (
                "image/svg+xml",
                b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            ),
        ]:
            with self.subTest(mime=mime):
                message = create_message(
                    self.admin,
                    self.room,
                    attachment=SimpleUploadedFile("active.txt", raw, mime),
                )
                blob = Attachment.objects.get(
                    record_type="Message", record_id=message.id
                ).blob
                response = self.client.get(blob_url(blob))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/octet-stream")
                self.assertTrue(
                    response["Content-Disposition"].startswith("attachment;")
                )
                response.close()
