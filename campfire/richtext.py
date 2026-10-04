"""Action Text content is sanitized before rendering; mention GIDs stay persisted."""

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import nh3

from . import rails
from .models import Attachment, Blob, User

SOUNDS = json.loads(Path(__file__).with_name("sounds.json").read_text())

TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "caption",
    "code",
    "dd",
    "del",
    "div",
    "dl",
    "dt",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
    "action-text-attachment",
    "actiontext-opengraph-embed",
    "tfoot",
    "time",
}
ATTRS = {
    "*": {"class", "dir", "lang", "data-language"},
    "a": {"href", "title"},
    "img": {"src", "alt", "width", "height"},
    "action-text-attachment": {
        "sgid",
        "content-type",
        "content",
        "url",
        "href",
        "filename",
        "filesize",
        "width",
        "height",
        "presentation",
        "caption",
    },
}


def canonicalize(body):
    def trix_figure(match):
        parser = PlainTree()
        parser.feed(match.group(0))
        node = parser.root.children[0] if parser.root.children else None
        if not node or "data-trix-attachment" not in node.attrs:
            return match.group(0)
        attributes = {}
        for key in ("data-trix-attachment", "data-trix-attributes"):
            try:
                attributes.update(json.loads(node.attrs.get(key, "{}")))
            except (ValueError, TypeError):
                continue
        attributes["content-type"] = attributes.pop("contentType", "")
        serialized = " ".join(
            f'{key}="{html.escape(str(value), quote=True)}"'
            for key, value in attributes.items()
            if value is not None
        )
        return f"<action-text-attachment {serialized}></action-text-attachment>"

    return re.sub(r"<figure\b[^>]*>.*?</figure>", trix_figure, body or "", flags=re.S)


def sanitize(body):
    return nh3.clean(
        canonicalize(body),
        tags=TAGS,
        attributes=ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
        strip_comments=True,
    )


def attachment_attributes(node):
    parser = AttachmentParser()
    parser.feed(node)
    return parser.attributes


class AttachmentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes = {}

    def handle_starttag(self, tag, attrs):
        if tag == "action-text-attachment":
            self.attributes = dict(attrs)


def attached_blob(attributes):
    try:
        from urllib.parse import urlsplit

        gid = rails.verify_sgid(attributes.get("sgid", ""))
        parsed = urlsplit(gid)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme != "gid"
            or parsed.netloc != "campfire"
            or len(parts) != 2
            or parts[0] != "ActiveStorage::Blob"
        ):
            return None
        return Blob.objects.filter(id=int(parts[1])).first()
    except (ValueError, TypeError):
        return None


class PlainNode:
    def __init__(self, tag="", attrs=None, parent=None, text=""):
        self.tag, self.attrs, self.parent, self.text = (
            tag,
            dict(attrs or []),
            parent,
            text,
        )
        self.children = []


class PlainTree(HTMLParser):
    """A bounded fragment tree for Action Text's bottom-up conversion."""

    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__()
        self.root = PlainNode()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 200:
            return
        if self.stack[-1].tag == "p" and tag in {
            "p",
            "div",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "ul",
            "ol",
            "pre",
        }:
            self.stack.pop()
        elif tag == "li" and self.stack[-1].tag == "li":
            self.stack.pop()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.stack[-1].tag in {
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self.stack.pop()
        node = PlainNode(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, text):
        if (
            self.stack[-1].tag in {"pre", "textarea"}
            and not self.stack[-1].children
            and text.startswith("\n")
        ):
            text = text[1:]
        self.stack[-1].children.append(
            PlainNode("#text", parent=self.stack[-1], text=text)
        )


def plain_node(node):
    # Installed actiontext/lib/action_text/plain_text_conversion.rb.
    if node.tag == "#text":
        return node.text.rstrip("\r\n")
    if node.tag in {"script", "style", "unsupported"}:
        return ""
    if node.tag == "action-text-attachment":
        id = rails.unverified_user_gid(node.attrs.get("sgid", ""))
        user = User.objects.filter(id=id).first() if id else None
        if user:
            return "@" + user.name
        if blob := attached_blob(node.attrs):
            return node.attrs.get("caption") or "[" + blob.filename + "]"
        kind = node.attrs.get("content-type", "").split("/")[0].title()
        return "[" + kind + "]" if kind in {"Image", "Video", "Audio"} else ""
    text = "".join(plain_node(child) for child in node.children)
    trimmed = text.rstrip("\r\n")
    ancestors = []
    parent = node.parent
    while parent:
        if parent.tag in {"ul", "ol"}:
            ancestors.append(parent.tag)
        parent = parent.parent
    if node.tag in {"p", "h1"}:
        return trimmed + "\n\n"
    if node.tag in {"ul", "ol"}:
        return ("\n" if ancestors else "") + trimmed + "\n\n"
    if node.tag == "br":
        return "\n"
    if node.tag == "div":
        return trimmed + "\n"
    if node.tag == "figcaption":
        return "[" + trimmed + "]"
    if node.tag == "blockquote":
        text = trimmed + "\n\n"
        content = text.strip()
        if not content:
            return "“”"
        start = text.index(content)
        return text[:start] + "“" + content + "”" + text[start + len(content) :]
    if node.tag == "li":
        bullet = "•"
        if ancestors and ancestors[0] == "ol":
            elements = [child for child in node.parent.children if child.tag != "#text"]
            bullet = str(elements.index(node) + 1) + "."
        return "  " * max(0, len(ancestors) - 1) + bullet + " " + trimmed + "\n"
    return text


def plain_text(body):
    parser = PlainTree()
    parser.feed(
        canonicalize(body)
        .strip(" \t\n\v\f\r\0")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\0", "")
    )
    return plain_node(parser.root).rstrip("\r\n")


def reconcile_embeds(rich):
    blobs = [
        blob
        for node in re.findall(
            r"<action-text-attachment\b[^>]*>.*?</action-text-attachment>",
            rich.body or "",
            flags=re.S,
        )
        if (blob := attached_blob(attachment_attributes(node))) is not None
    ]
    ids = {blob.id for blob in blobs}
    attachments = Attachment.objects.filter(
        record_type="ActionText::RichText", record_id=rich.id, name="embeds"
    )
    obsolete = list(
        attachments.exclude(blob_id__in=ids).values_list("blob_id", flat=True)
    )
    attachments.exclude(blob_id__in=ids).delete()
    for blob in blobs:
        Attachment.objects.get_or_create(
            record_type="ActionText::RichText",
            record_id=rich.id,
            name="embeds",
            blob=blob,
        )
    from django.db import transaction

    from .jobs import enqueue

    for id in obsolete:
        transaction.on_commit(lambda id=id: enqueue("purge", {"blob_id": id}))


def mention_ids(body):
    # reference/lib/rails_ext/action_text_attachables.rb deliberately retains
    # User mentions across secret rotation; unsigned blobs are never accepted.
    return [
        id
        for raw in re.findall(r'sgid="([^\"]+)"', body or "")
        if (id := rails.unverified_user_gid(html.unescape(raw))) is not None
        and User.objects.filter(id=id).exists()
    ]


def safe_preview_url(value, host=""):
    from urllib.parse import urlsplit

    try:
        uri = urlsplit(value)
        name = uri.hostname or ""
        if (
            uri.scheme not in ("http", "https")
            or "%" in name
            or "." not in name
            or not re.search("[a-z]", name.split(".")[-1], re.I)
            or name.split(".")[-1].lower().startswith("0x")
            or name.rstrip(".").lower() == host.rstrip(".").lower()
        ):
            return None
        return value
    except (ValueError, TypeError):
        return None


def opengraph_html(attributes, host=""):
    content = html.unescape(attributes.get("content", ""))
    title = attributes.get("filename", "")
    description = attributes.get("caption", "")
    href = attributes.get("href")
    image = attributes.get("url")
    if not title and content:
        parser = MetadataParser()
        parser.feed(content)
        title = parser.title
        description = parser.description
        href = parser.href
        image = parser.image
    if not title:
        return ""
    href = safe_preview_url(href, host)
    image = safe_preview_url(image, host)
    title = html.escape(title[:279] + "…" if len(title) > 280 else title)
    description = html.escape(
        description[:559] + "…" if len(description) > 560 else description
    )
    if href:
        title = (
            '<a href="'
            + html.escape(href, quote=True)
            + '" rel="noreferrer" target="_blank">'
            + title
            + "</a>"
        )
    return (
        '<figure class="attachment attachment--content attachment--og"><actiontext-opengraph-embed><div class="og-embed gap"><div class="og-embed__content"><div class="og-embed__title">'
        + title
        + '</div><div class="og-embed__description">'
        + description
        + "</div></div>"
        + (
            '<div class="og-embed__image"><img src="'
            + html.escape(image, quote=True)
            + '" class="image center" alt=""></div>'
            if image
            else ""
        )
        + "</div></actiontext-opengraph-embed></figure>"
    )


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self.href = None
        self.image = None
        self.mode = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.stack.append(self.mode)
        if "og-embed__title" in attributes.get("class", ""):
            self.mode = "title"
        if "og-embed__description" in attributes.get("class", ""):
            self.mode = "description"
        if tag == "a" and self.mode == "title":
            self.href = attributes.get("href")
        if tag == "img":
            self.image = attributes.get("src")

    def handle_endtag(self, tag):
        if self.stack:
            self.mode = self.stack.pop()

    def handle_data(self, data):
        if self.mode == "title":
            self.title += data
        if self.mode == "description":
            self.description += data


class AutoLink(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.excluded = 0

    def handle_starttag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())
        if tag in ("a", "code", "pre"):
            self.excluded += 1

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.parts.append("</" + tag + ">")
        if tag in ("a", "code", "pre"):
            self.excluded = max(0, self.excluded - 1)

    def handle_entityref(self, name):
        self.parts.append("&" + name + ";")

    def handle_charref(self, name):
        self.parts.append("&#" + name + ";")

    def handle_data(self, data):
        if self.excluded:
            self.parts.append(data)
            return

        def link(match):
            text = match[0]
            tail = ""
            while text and text[-1] in ".,!?:;)":
                tail = text[-1] + tail
                text = text[:-1]
            href = (
                text if text.startswith(("http://", "https://")) else "http://" + text
            )
            return (
                '<a href="'
                + html.escape(href, quote=True)
                + '" target="_blank">'
                + html.escape(text)
                + "</a>"
                + tail
            )

        self.parts.append(re.sub(r"(?:https?://|www\.)[^\s<>]+", link, data))


def render_body(body, host=""):
    from .middleware import request_host

    host = host or request_host.get()
    body = sanitize(body)

    def attachment(match):
        raw = match[0]
        attributes = attachment_attributes(raw)
        ids = mention_ids(raw)
        if ids:
            user = User.objects.filter(id=ids[0]).first()
            if user:
                from .rendering import avatar

                return (
                    '<span class="mention" sgid="'
                    + html.escape(rails.sgid("User", user.id))
                    + '"><a href="/users/'
                    + str(user.id)
                    + '" class="btn avatar" title="'
                    + html.escape(user.title)
                    + '"><img src="'
                    + avatar(user.id, user.updated_at)
                    + '" width="48" height="48" aria-hidden="true"></a> '
                    + html.escape(user.name)
                    + "</span>"
                )
        if "application/vnd.actiontext.opengraph-embed" in attributes.get(
            "content-type", ""
        ):
            return opengraph_html(attributes, host)
        blob = attached_blob(attributes)
        if blob:
            from .media import representation_url
            from .storage import blob_url

            url = blob_url(blob)
            name = html.escape(blob.filename)
            if (blob.content_type or "").startswith("image/"):
                return (
                    '<figure class="attachment attachment--preview"><a href="'
                    + url
                    + '" data-action="lightbox#open" data-lightbox-url-value="'
                    + url
                    + '"><img src="'
                    + representation_url(blob)
                    + '" alt="'
                    + name
                    + '"></a><figcaption>'
                    + name
                    + "</figcaption></figure>"
                )
            return (
                '<figure class="attachment"><a href="'
                + url
                + '">'
                + name
                + "</a></figure>"
            )
        return ""

    body = re.sub(
        r"<action-text-attachment\b[^>]*>.*?</action-text-attachment>",
        attachment,
        body,
        flags=re.S,
    )
    text = plain_text(body)
    if re.fullmatch(r"/play \w+", text):
        from .rendering import MANIFEST, asset

        name = text.split()[1]
        if name in SOUNDS and name + ".mp3" in MANIFEST:
            sound = SOUNDS[name]
            label = html.escape(sound.get("text", ""))
            if sound.get("image"):
                label = f'<img src="{asset(sound["image"])}" width="{sound["width"]}" height="{sound["height"]}" class="align--middle">'
            return (
                '<div class="sound" data-controller="sound" data-action="messages:play-&gt;sound#play" data-sound-url-value="'
                + asset(name + ".mp3")
                + '"><button class="btn btn--plain" data-action="sound#play">🔊</button>'
                + label
                + "</div>"
            )
    parser = AutoLink()
    parser.feed(body)
    return "".join(parser.parts)
