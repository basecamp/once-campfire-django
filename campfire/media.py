"""Native libvips/ffmpeg transforms with Rails variant-record storage ownership."""

import base64
import hashlib
import json
import secrets
import subprocess
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from django.db import transaction

from .models import Attachment, Blob, Variant
from .storage import path_for, track_file

VARIABLE_TYPES = {
    "image/png",
    "image/gif",
    "image/jpeg",
    "image/tiff",
    "image/webp",
    "image/avif",
    "image/heic",
    "image/heif",
}

_slots = threading.BoundedSemaphore(4)


class Symbol(str):
    pass


def marshal(value):
    symbols = []

    def number(n):
        if n == 0:
            return b"\x00"
        if 0 < n < 123:
            return bytes([n + 5])
        if -124 < n < 0:
            return bytes([(n - 5) & 255])
        length = max(1, (n.bit_length() + 7) // 8)
        return bytes([length]) + n.to_bytes(length, "little", signed=n < 0)

    def sym(s):
        if s in symbols:
            return b";" + number(symbols.index(s))
        symbols.append(s)
        encoded = s.encode()
        return b":" + number(len(encoded)) + encoded

    def pack(v):
        if v is None:
            return b"0"
        if v is True:
            return b"T"
        if v is False:
            return b"F"
        if isinstance(v, int):
            return b"i" + number(v)
        if isinstance(v, Symbol):
            return sym(v)
        if isinstance(v, str):
            raw = v.encode()
            return b'I"' + number(len(raw)) + raw + number(1) + sym("E") + b"T"
        if isinstance(v, list):
            return b"[" + number(len(v)) + b"".join(pack(x) for x in v)
        if isinstance(v, dict):
            return (
                b"{" + number(len(v)) + b"".join(sym(k) + pack(x) for k, x in v.items())
            )
        raise ValueError("unsupported transformation")

    return b"\x04\x08" + pack(value)


def default_format(blob):
    ext = Path(blob.filename).suffix.lstrip(".").lower()
    return (
        ext
        if ext in ["png", "jpeg", "jpg", "webp", "gif", "tiff", "avif", "heic", "heif"]
        else "png"
    )


def transformations(blob, size, format=None):
    return {
        "format": Symbol(format) if format else default_format(blob),
        "resize_to_limit": size,
    }


def variant(blob, size, format=None):
    transforms = transformations(blob, size, format)
    digest = base64.b64encode(hashlib.sha1(marshal(transforms)).digest()).decode()
    existing = Variant.objects.filter(blob=blob, variation_digest=digest).first()
    if existing:
        image = (
            Attachment.objects.filter(
                record_type="ActiveStorage::VariantRecord",
                record_id=existing.id,
                name="image",
            )
            .select_related("blob")
            .first()
        )
        if image and path_for(image.blob.key).is_file():
            return image.blob
    import pyvips

    pyvips.operation_block_set("VipsForeignLoadOpenslide", True)
    pyvips.block_untrusted_set(True)
    width, height = size
    if any(not isinstance(v, int) or v <= 0 or v > 16384 for v in size):
        raise ValueError("invalid transform dimensions")
    fmt = str(transforms["format"])
    with _slots:
        image = pyvips.Image.new_from_file(
            str(path_for(blob.key)), access="sequential"
        ).autorot()
        image = image.thumbnail_image(width, height=height, size="down", no_rotate=True)
        mask = pyvips.Image.new_from_array(
            [[-1, -1, -1], [-1, 32, -1], [-1, -1, -1]], scale=24, offset=0
        )
        image = image.conv(mask, precision="integer")
        raw = image.write_to_buffer("." + fmt)
    key = secrets.token_hex(14)
    target = path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with transaction.atomic():
            record, created = Variant.objects.get_or_create(
                blob=blob, variation_digest=digest
            )
            if not created:
                attached = (
                    Attachment.objects.filter(
                        record_type="ActiveStorage::VariantRecord",
                        record_id=record.id,
                        name="image",
                    )
                    .select_related("blob")
                    .first()
                )
                if attached:
                    return attached.blob
            output = Blob.objects.create(
                key=key,
                filename=Path(blob.filename).stem + "." + fmt,
                content_type="image/" + ("jpeg" if fmt == "jpg" else fmt),
                byte_size=len(raw),
                checksum=base64.b64encode(hashlib.md5(raw).digest()).decode(),
                metadata=json.dumps(
                    {
                        "identified": True,
                        "analyzed": True,
                        "width": image.width,
                        "height": image.height,
                    }
                ),
            )
            target.write_bytes(raw)
            track_file(target)
            Attachment.objects.create(
                record_type="ActiveStorage::VariantRecord",
                record_id=record.id,
                name="image",
                blob=output,
            )
        return output
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def analyze(blob):
    metadata = {"identified": True, "analyzed": True}
    content_type = blob.content_type or ""
    if content_type in VARIABLE_TYPES:
        import pyvips

        image = pyvips.Image.new_from_file(
            str(path_for(blob.key)), access="sequential"
        ).autorot()
        metadata.update(width=image.width, height=image.height)
    elif content_type.startswith(("audio/", "video/")):
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path_for(blob.key)),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        probe = json.loads(result.stdout)
        metadata["duration"] = float(probe.get("format", {}).get("duration", 0))
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "video":
                metadata.update(
                    width=stream.get("width"), height=stream.get("height"), angle=0
                )
    blob.metadata = json.dumps(metadata)
    blob.save(update_fields=["metadata"])
    return metadata


def preview(blob):
    attachment = (
        Attachment.objects.filter(
            record_type="ActiveStorage::Blob", record_id=blob.id, name="preview_image"
        )
        .select_related("blob")
        .first()
    )
    if attachment:
        return attachment.blob
    working = settings.STORAGE_PATH / "tmp"
    working.mkdir(parents=True, exist_ok=True)
    content_type = blob.content_type or ""
    with tempfile.TemporaryDirectory(dir=working) as directory:
        directory = Path(directory)
        output = directory / "preview.webp"
        if content_type.startswith("video/"):
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-i",
                    str(path_for(blob.key)),
                    "-y",
                    "-vframes",
                    "1",
                    "-vf",
                    "thumbnail,scale=1200:800:force_original_aspect_ratio=decrease",
                    str(output),
                ],
                check=True,
                timeout=60,
                capture_output=True,
            )
        elif content_type == "application/pdf":
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    "1",
                    "-singlefile",
                    "-png",
                    "-scale-to",
                    "1200",
                    str(path_for(blob.key)),
                    str(directory / "preview"),
                ],
                check=True,
                timeout=60,
                capture_output=True,
            )
            import pyvips

            pyvips.Image.new_from_file(str(directory / "preview.png")).write_to_file(
                str(output)
            )
        else:
            return None
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .storage import store_upload

        return store_upload(
            SimpleUploadedFile(
                Path(blob.filename).stem + ".webp", output.read_bytes(), "image/webp"
            ),
            "ActiveStorage::Blob",
            blob.id,
            "preview_image",
        )


def process_attachment(blob):
    analyze(blob)
    if blob.content_type in VARIABLE_TYPES:
        return variant(blob, [1200, 800])
    if (blob.content_type or "").startswith(
        "video/"
    ) or blob.content_type == "application/pdf":
        return preview(blob)
    return None


def representation_url(blob, size=None, format=None):
    from urllib.parse import quote

    from . import rails

    transforms = transformations(blob, size or [1200, 800], format)
    variation = rails.sign(transforms, "ActiveStorage", "variation")
    return (
        "/rails/active_storage/representations/redirect/"
        + rails.signed_id("ActiveStorage::Blob", blob.id, "blob_id")
        + "/"
        + variation
        + "/"
        + quote(blob.filename)
    )
