import base64
import hashlib
import json
import mimetypes
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse

from . import rails
from .models import Attachment, Blob, Message, RichText, now

# Installed activestorage/lib/active_storage/engine.rb and Blob::Servable.
BINARY_TYPES = {
    "text/html",
    "image/svg+xml",
    "application/postscript",
    "application/x-shockwave-flash",
    "text/xml",
    "application/xml",
    "application/xhtml+xml",
    "application/mathml+xml",
    "text/cache-manifest",
}
INLINE_TYPES = {
    "image/webp",
    "image/avif",
    "image/png",
    "image/gif",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/vnd.adobe.photoshop",
    "image/vnd.microsoft.icon",
    "application/pdf",
}


def serving_attributes(content_type, disposition):
    content_type = (content_type or "application/octet-stream").split(";", 1)[0].lower()
    if content_type in BINARY_TYPES:
        return "application/octet-stream", "attachment"
    return content_type, disposition if content_type in INLINE_TYPES else "attachment"


def path_for(key):
    if not key.isalnum() or len(key) < 4:
        raise ValueError("invalid storage key")
    return settings.FILES_PATH / key[:2] / key[2:4] / key


def blob_url(blob):
    token = rails.signed_id("ActiveStorage::Blob", blob.id, "blob_id")
    return "/rails/active_storage/blobs/redirect/" + token + "/" + quote(blob.filename)


def store_upload(upload, record_type, record_id, name):
    raw = upload.read()
    key = secrets.token_urlsafe(21).replace("-", "a").replace("_", "b")
    filename = Path(upload.name).name
    blob = Blob.objects.create(
        key=key,
        filename=filename,
        content_type=upload.content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
        byte_size=len(raw),
        checksum=base64.b64encode(hashlib.md5(raw).digest()).decode(),
        metadata="{}",
    )
    target = path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    track_file(target)
    Attachment.objects.create(
        record_type=record_type, record_id=record_id, name=name, blob=blob
    )
    return blob


def attach_signed(token, record_type, record_id, name):
    id = rails.verify_id("ActiveStorage::Blob", token, "blob_id")
    blob = Blob.objects.get(id=id)
    if not path_for(blob.key).is_file():
        raise ValueError("upload missing")
    Attachment.objects.create(
        record_type=record_type, record_id=record_id, name=name, blob=blob
    )
    return blob


def direct_upload(request):
    if not request.current_user:
        return HttpResponse(status=401)
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        data = json.loads(request.body)["blob"]
        size = int(data["byte_size"])
        if size < 0 or size > 50 * 1024 * 1024:
            raise ValueError()
        blob = Blob.objects.create(
            key=secrets.token_hex(14),
            filename=Path(data["filename"]).name,
            byte_size=size,
            checksum=data["checksum"],
            content_type=data.get("content_type") or "application/octet-stream",
            metadata=json.dumps(
                {
                    **(
                        data.get("metadata")
                        if isinstance(data.get("metadata"), dict)
                        else {}
                    ),
                    "campfire_upload_user_id": request.current_user.id,
                }
            ),
        )
        token = rails.sign(
            {
                "key": blob.key,
                "content_type": blob.content_type,
                "content_length": size,
                "checksum": blob.checksum,
            },
            "ActiveStorage",
            "blob_token",
            now() + timedelta(minutes=5),
        )
        from django.http import JsonResponse

        return JsonResponse(
            {
                "id": blob.id,
                "key": blob.key,
                "filename": blob.filename,
                "content_type": blob.content_type,
                "byte_size": size,
                "checksum": blob.checksum,
                "signed_id": rails.signed_id("ActiveStorage::Blob", blob.id, "blob_id"),
                "attachable_sgid": rails.sgid("ActiveStorage::Blob", blob.id),
                "direct_upload": {
                    "url": f"{request.scheme}://{request.get_host()}/rails/active_storage/disk/{token}",
                    "headers": {"Content-Type": blob.content_type},
                },
            }
        )
    except (ValueError, KeyError):
        return HttpResponse(status=422)


def disk(request, token):
    try:
        purpose = "blob_token" if request.method == "PUT" else "blob_key"
        data = rails.verify(token, "ActiveStorage", purpose)
        path = path_for(data["key"])
        if request.method == "PUT":
            if request.current_session is None:
                return HttpResponse(status=401)
            blob = Blob.objects.filter(key=data["key"]).first()
            owner = (
                json.loads(blob.metadata or "{}").get("campfire_upload_user_id")
                if blob
                else None
            )
            if blob is None or (owner is not None and owner != request.current_user.id):
                return HttpResponse(status=403)
            raw = request.body
            if (
                len(raw) != data["content_length"]
                or base64.b64encode(hashlib.md5(raw).digest()).decode()
                != data["checksum"]
            ):
                return HttpResponse(status=422)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            return HttpResponse(status=204)
        mime, disposition = serving_attributes(
            data.get("content_type"), data.get("disposition", "inline")
        )
        return serve(request, path, mime, data.get("filename", "file"), disposition)
    except (ValueError, KeyError, FileNotFoundError):
        raise Http404


def blob_redirect(request, token, filename):
    if not request.current_user:
        return HttpResponse(status=401)
    try:
        blob = Blob.objects.get(
            id=rails.verify_id("ActiveStorage::Blob", token, "blob_id")
        )
    except (ValueError, Blob.DoesNotExist):
        raise Http404
    attachments = Attachment.objects.filter(blob=blob)

    allowed = False
    if not attachments.exists():
        # Preserve existing Rails draft signed URLs; native drafts record their uploader.
        owner = json.loads(blob.metadata or "{}").get("campfire_upload_user_id")
        allowed = owner is None or owner == request.current_user.id
    for a in attachments:
        if a.record_type == "Message":
            from .models import Message

            allowed = Message.objects.filter(
                id=a.record_id, room__memberships__user=request.current_user
            ).exists()
        elif a.record_type in ("User", "Account"):
            allowed = True
        elif a.record_type == "ActionText::RichText":
            from .models import Message, RichText

            rt = RichText.objects.filter(id=a.record_id, record_type="Message").first()
            allowed = bool(
                rt
                and Message.objects.filter(
                    id=rt.record_id, room__memberships__user=request.current_user
                ).exists()
            )
        if allowed:
            break
    if not allowed:
        return HttpResponse(status=403)
    mime, disposition = serving_attributes(
        blob.content_type, request.GET.get("disposition", "inline")
    )
    return serve(request, path_for(blob.key), mime, blob.filename, disposition)


def serve(request, path, content_type, filename, disposition="inline"):
    if not path.is_file():
        raise Http404
    size = path.stat().st_size
    range_header = request.headers.get("Range")
    if range_header:
        import re

        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        if not match:
            return HttpResponse(
                status=416, headers={"Content-Range": f"bytes */{size}"}
            )
        begin = int(match[1]) if match[1] else max(0, size - int(match[2] or 0))
        end = min(size - 1, int(match[2])) if match[1] and match[2] else size - 1
        if begin > end or begin >= size:
            return HttpResponse(
                status=416, headers={"Content-Range": f"bytes */{size}"}
            )
        with path.open("rb") as file:
            file.seek(begin)
            raw = file.read(end - begin + 1)
        response = HttpResponse(
            raw,
            status=206,
            content_type=content_type,
            headers={"Content-Range": f"bytes {begin}-{end}/{size}"},
        )
    else:
        response = FileResponse(path.open("rb"), content_type=content_type)
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = (
        ("attachment" if disposition == "attachment" else "inline")
        + '; filename="'
        + filename.replace('"', "").replace("\r", "").replace("\n", "")
        + '"'
    )
    return response


def representation(request, token, variation, filename):
    try:
        id = rails.verify_id("ActiveStorage::Blob", token, "blob_id")
        transforms = rails.verify(variation, "ActiveStorage", "variation")
        if not isinstance(transforms, dict) or set(transforms) - {
            "format",
            "resize_to_limit",
        }:
            raise ValueError("unsupported transformation")
        blob = Blob.objects.get(id=id)
        # Resolve access through the source blob's actual attachment rather than trusting a signed URL.
        if not request.current_user:
            return HttpResponse(status=401)
        allowed = Attachment.objects.filter(
            blob=blob,
            record_type="Message",
            record_id__in=Message.objects.filter(
                room__memberships__user=request.current_user
            ).values("id"),
        ).exists()
        if not allowed:
            allowed = Attachment.objects.filter(
                blob=blob,
                record_type="ActionText::RichText",
                record_id__in=RichText.objects.filter(
                    record_type="Message",
                    record_id__in=Message.objects.filter(
                        room__memberships__user=request.current_user
                    ).values("id"),
                ).values("id"),
            ).exists()
        if not allowed:
            return HttpResponse(status=403)
        from .media import preview, variant

        source = (
            preview(blob)
            if blob.content_type == "application/pdf"
            or (blob.content_type or "").startswith("video/")
            else blob
        )
        output = variant(
            source,
            transforms.get("resize_to_limit", [1200, 800]),
            transforms.get("format"),
        )
        return serve(
            request, path_for(output.key), output.content_type, output.filename
        )
    except (ValueError, Blob.DoesNotExist, FileNotFoundError):
        raise Http404


from contextlib import contextmanager
from contextvars import ContextVar

_staged_files = ContextVar("campfire_staged_files", default=None)


@contextmanager
def staged_files():
    parent = _staged_files.get()
    if parent is not None:
        yield parent
        return
    paths = []
    token = _staged_files.set(paths)
    try:
        yield paths
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    finally:
        _staged_files.reset(token)


def track_file(path):
    files = _staged_files.get()
    if files is not None:
        files.append(path)


def purge_blob(id):
    from .models import Variant

    if Attachment.objects.filter(blob_id=id).exists():
        return
    blob = Blob.objects.filter(id=id).first()
    if not blob:
        return
    children = []
    with transaction.atomic():
        for variant in Variant.objects.filter(blob=blob):
            children += list(
                Attachment.objects.filter(
                    record_type="ActiveStorage::VariantRecord", record_id=variant.id
                ).values_list("blob_id", flat=True)
            )
            Attachment.objects.filter(
                record_type="ActiveStorage::VariantRecord", record_id=variant.id
            ).delete()
            variant.delete()
        children += list(
            Attachment.objects.filter(
                record_type="ActiveStorage::Blob", record_id=id
            ).values_list("blob_id", flat=True)
        )
        Attachment.objects.filter(
            record_type="ActiveStorage::Blob", record_id=id
        ).delete()
        blob.delete()
    path_for(blob.key).unlink(missing_ok=True)
    for child in children:
        purge_blob(child)


def remove_attachment(record_type, record_id, name):
    from .jobs import enqueue

    query = Attachment.objects.filter(
        record_type=record_type, record_id=record_id, name=name
    )
    ids = list(query.values_list("blob_id", flat=True))
    query.delete()
    for id in ids:
        transaction.on_commit(lambda id=id: enqueue("purge", {"blob_id": id}))


def replace_attachment(upload, record_type, record_id, name):
    with staged_files(), transaction.atomic():
        remove_attachment(record_type, record_id, name)
        return store_upload(upload, record_type, record_id, name)
