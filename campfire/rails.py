"""Rails wire formats, checked against independently generated Rails vectors."""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import unquote, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings


def encode(value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for character, escaped in [("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")]:
        text = text.replace(character, escaped)
    return text.encode()


def b64(value):
    return base64.b64encode(value).decode()


def decode64(value):
    if not isinstance(value, str):
        raise ValueError("invalid base64")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, UnicodeError) as error:
        raise ValueError("invalid base64") from error


@lru_cache(maxsize=64)
def _derived_key(secret, salt, length):
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 1000, length)


def key(salt, length=64):
    return _derived_key(settings.SECRET_KEY, salt, length)


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _expiry(expiry):
    return (
        expiry.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _load(raw):
    # Rails 7-era signed messages contained Marshal strings. Read only that scalar;
    # never instantiate classes or deserialize arbitrary Ruby objects.
    if raw.startswith(b"\x04\x08"):
        body = raw[2:]
        if body.startswith(b"I"):
            body = body[1:]
        if len(body) < 2 or body[0] != ord('"'):
            raise ValueError("unsupported Marshal value")
        first = int.from_bytes(body[1:2], "little", signed=True)
        offset = 2
        if first == 0:
            length = 0
        elif first > 4:
            length = first - 5
        elif 1 <= first <= 4 and len(body) >= offset + first:
            length = int.from_bytes(body[offset : offset + first], "little")
            offset += first
        else:
            raise ValueError("invalid Marshal string length")
        if len(body) < offset + length:
            raise ValueError("truncated Marshal string")
        return body[offset : offset + length].decode()
    return json.loads(raw)


def unpack(raw, purpose=None):
    value = _load(raw)
    if isinstance(value, dict) and "_rails" in value:
        metadata = value["_rails"]
        if not isinstance(metadata, dict) or (metadata.get("pur") or "") != (
            purpose or ""
        ):
            raise ValueError("invalid purpose")
        if metadata.get("exp") is not None:
            from .models import now

            if now() >= _time(metadata["exp"]):
                raise ValueError("expired message")
        return (
            _load(decode64(metadata["message"]))
            if "message" in metadata
            else metadata.get("data")
        )
    if purpose:
        raise ValueError("missing purpose")
    return value


def sign(
    value,
    salt,
    purpose=None,
    expiry=None,
    algorithm="sha1",
    urlsafe=False,
    padded=True,
    *,
    plain_json=False,
):
    metadata = {"data": value}
    if expiry:
        metadata["exp"] = _expiry(expiry)
    if purpose:
        metadata["pur"] = purpose
    value = {"_rails": metadata} if purpose or expiry else value
    raw = (
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        if plain_json
        else encode(value)
    )
    payload = (
        base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)
    ).decode()
    if not padded:
        payload = payload.rstrip("=")
    return payload + "--" + hmac.new(key(salt), payload.encode(), algorithm).hexdigest()


def verify(raw, salt, purpose=None, algorithm="sha1"):
    try:
        payload, mac = raw.rsplit("--", 1)
        expected = hmac.new(key(salt), payload.encode(), algorithm).hexdigest()
        if not hmac.compare_digest(expected, mac):
            raise ValueError("invalid signature")
        return unpack(decode64(payload), purpose)
    except (AttributeError, TypeError, UnicodeError, KeyError) as error:
        raise ValueError("invalid message") from error


def cookie_envelope(name, value, expiry=None):
    return encode(
        {
            "_rails": {
                "message": b64(encode(value)),
                "exp": _expiry(expiry) if expiry else None,
                "pur": "cookie." + name,
            }
        }
    )


def sign_cookie(name, value, expiry=None):
    payload = b64(cookie_envelope(name, value, expiry))
    return (
        payload
        + "--"
        + hmac.new(key("signed cookie"), payload.encode(), "sha1").hexdigest()
    )


def _cookie_value(plaintext, name):
    # Cookie jars use NullSerializer: only the legacy message envelope is metadata.
    # Before Rails 5.2, raw JSON was accepted without a cookie-name purpose.
    if plaintext.startswith(b'{"_rails":{"message":"'):
        envelope = json.loads(plaintext)
        metadata = envelope["_rails"]
        if metadata.get("pur") not in (None, "", "cookie." + name):
            raise ValueError("invalid cookie purpose")
        if metadata.get("exp"):
            from .models import now

            if now() >= _time(metadata["exp"]):
                raise ValueError("expired cookie")
        plaintext = decode64(metadata["message"])
    # :json cookie jars explicitly reject historical Marshal values.
    return json.loads(plaintext)


def verify_cookie(name, raw):
    payload, mac = unquote(raw).rsplit("--", 1)
    if not hmac.compare_digest(
        hmac.new(key("signed cookie"), payload.encode(), "sha1").hexdigest(), mac
    ):
        raise ValueError("invalid cookie signature")
    return _cookie_value(decode64(payload), name)


def encrypt_cookie(name, value, expiry=None, *, nonce=None):
    nonce = nonce if nonce is not None else secrets.token_bytes(12)
    encrypted = AESGCM(key("authenticated encrypted cookie", 32)).encrypt(
        nonce, cookie_envelope(name, value, expiry), b""
    )
    return "--".join(map(b64, [encrypted[:-16], nonce, encrypted[-16:]]))


def decrypt_cookie(name, raw):
    try:
        ciphertext, nonce, tag = map(decode64, unquote(raw).split("--"))
        if len(nonce) != 12 or len(tag) != 16:
            raise ValueError("invalid encrypted cookie")
        plaintext = AESGCM(key("authenticated encrypted cookie", 32)).decrypt(
            nonce, ciphertext + tag, b""
        )
        return _cookie_value(plaintext, name)
    except (InvalidTag, TypeError, UnicodeError, KeyError) as error:
        raise ValueError("invalid encrypted cookie") from error


def _model_purpose(model, purpose):
    model = "Room" if model.startswith("Rooms::") else model
    underscored = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", model.replace("::", "/"))
    underscored = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", underscored).lower()
    return underscored + ("/" + purpose if purpose else "")


def signed_id(model, id, purpose="", expiry=None):
    # ActiveStorage::Blob overrides both verifier and purpose for old installations.
    if model == "ActiveStorage::Blob":
        return sign(id, "ActiveStorage", purpose or "blob_id", expiry)
    return sign(
        id,
        "active_record/signed_id",
        _model_purpose(model, purpose),
        expiry,
        "sha256",
        True,
        False,
        plain_json=True,
    )


def verify_id(model, raw, purpose=""):
    if model == "ActiveStorage::Blob":
        value = verify(raw, "ActiveStorage", purpose or "blob_id")
    else:
        combined = _model_purpose(model, purpose)
        try:
            value = verify(raw, "active_record/signed_id", combined, "sha256")
        except ValueError:
            value = verify(raw, "active_record/signed_id", combined, "sha1")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("invalid model id")
    if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value):
        raise ValueError("invalid model id")
    return int(value)


def stream(room):
    return (
        base64.urlsafe_b64encode(f"gid://campfire/{room.type}/{room.id}".encode())
        .decode()
        .rstrip("=")
        + ":messages"
    )


def sign_stream(name):
    return sign(
        name, "turbo/signed_stream_verifier_key", algorithm="sha256", plain_json=True
    )


def verify_stream(raw):
    value = verify(raw, "turbo/signed_stream_verifier_key", algorithm="sha256")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid stream")
    return value


def sgid(model, id):
    return sign(
        f"gid://campfire/{model}/{id}?expires_in",
        "signed_global_ids",
        "attachable",
        urlsafe=True,
    )


def verify_sgid(raw, purpose="attachable"):
    try:
        value = verify(raw, "signed_global_ids", purpose)
    except ValueError:
        value = verify(raw, "signed_global_ids")
        if not isinstance(value, dict) or value.get("purpose") != purpose:
            raise ValueError("invalid GlobalID purpose")
        if value.get("expires_at"):
            from .models import now

            if now() > _time(value["expires_at"]):
                raise ValueError("expired GlobalID")
        value = value.get("gid")
    if not isinstance(value, str):
        raise ValueError("invalid GlobalID")
    return value


def unverified_user_gid(raw):
    # reference/lib/rails_ext/action_text_attachables.rb explicitly permits User
    # mentions across secret rotation. This exception never authorizes blob access.
    try:
        envelope = json.loads(decode64(raw.rsplit("--", 1)[0]))
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("_rails"), dict
        ):
            return None
        metadata = envelope["_rails"]
        value = metadata.get("data")
        if value is None and "message" in metadata:
            # The Ruby extension extracts this URI without unmarshaling untrusted bytes.
            match = re.search(
                rb"gid://campfire/[^/]+/\d+", decode64(metadata["message"])
            )
            value = match.group().decode() if match else None
        if not isinstance(value, str):
            return None
        if not value.startswith("gid://"):
            value = decode64(value).decode()
        uri = urlsplit(value)
        if uri.scheme == "gid" and uri.netloc and re.fullmatch(r"/User/\d+", uri.path):
            return int(uri.path.rsplit("/", 1)[1])
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        pass
    return None


def mask_csrf(raw):
    if len(raw) != 32:
        raise ValueError("invalid CSRF secret")
    pad = secrets.token_bytes(32)
    return (
        base64.urlsafe_b64encode(pad + bytes(a ^ b for a, b in zip(pad, raw)))
        .decode()
        .rstrip("=")
    )


def valid_csrf(raw, token, path=None, method=None):
    try:
        if len(raw) != 32:
            return False
        value = decode64(token)
        if len(value) == 32:
            return hmac.compare_digest(value, raw)
        if len(value) == 64:
            value = bytes(a ^ b for a, b in zip(value[:32], value[32:]))
        if len(value) != 32:
            return False
        candidates = [raw, hmac.new(raw, b"!real_csrf_token", "sha256").digest()]
        if path is not None and method is not None:
            identifier = path.removesuffix("/") + "#" + method.lower()
            candidates.append(hmac.new(raw, identifier.encode(), "sha256").digest())
        return any(hmac.compare_digest(value, candidate) for candidate in candidates)
    except (ValueError, TypeError, AttributeError):
        return False
