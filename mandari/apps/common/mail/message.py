# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eine Mail als Daten (Issue #528): Betreff, Text, HTML, Empfänger, Antwortadresse, Anhänge.

``Mail`` lässt sich verlustfrei als JSON speichern (``to_bytes``/``from_bytes``) – so liegt sie im
Postausgang, bis der Auftrag sie versendet. Anhänge stehen darin Base64-kodiert; der Postausgang
verschlüsselt das Ganze.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Final

from django.core.mail import EmailMessage, EmailMultiAlternatives

#: Format der gespeicherten Mail; steigt, wenn sich Felder inkompatibel ändern
FORMAT: Final = 1

#: Mailarten sind kurze, punktgetrennte Namen in Kleinbuchstaben (``work.fraktion.einladung``)
_KIND: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}$")


def check_kind(kind: str) -> str:
    """Prüft den Namen einer Mailart (Bezeichner in Metrik und Schalter); liefert ihn zurück."""
    if not _KIND.match(kind or ""):
        raise ValueError("Mailart muss ein punktgetrennter Name in Kleinbuchstaben sein, z. B. work.einladung")
    return kind


@dataclass(frozen=True)
class Attachment:
    """Ein Anhang: Dateiname, Inhalt, MIME-Typ."""

    filename: str
    content: bytes
    mimetype: str

    @classmethod
    def of(cls, value: Any) -> Attachment:
        """Aus ``(name, inhalt, typ)`` wie bei ``EmailMessage.attach``; Text wird UTF-8-kodiert."""
        if isinstance(value, Attachment):
            return value
        filename, content, mimetype = value
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return cls(str(filename), data, str(mimetype or "application/octet-stream"))


@dataclass(frozen=True)
class Mail:
    """Eine Mail, unabhängig vom Versandweg."""

    subject: str
    body: str
    to: tuple[str, ...]
    html_body: str | None = None
    from_email: str | None = None
    reply_to: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = field(default=(), repr=False)

    @property
    def size(self) -> int:
        """Ungefähre Größe in Bytes (Texte und Anhänge)."""
        texte = len(self.body.encode("utf-8")) + len((self.html_body or "").encode("utf-8"))
        return texte + sum(len(a.content) for a in self.attachments)

    def to_email_message(self, from_email: str) -> EmailMessage:
        """Djangos Nachricht für den Versand; ``from_email`` ist der Absender des gewählten Weges."""
        message: EmailMessage
        if self.html_body:
            alternative = EmailMultiAlternatives(
                subject=self.subject,
                body=self.body,
                from_email=from_email,
                to=list(self.to),
                reply_to=list(self.reply_to) or None,
            )
            alternative.attach_alternative(self.html_body, "text/html")
            message = alternative
        else:
            message = EmailMessage(
                subject=self.subject,
                body=self.body,
                from_email=from_email,
                to=list(self.to),
                reply_to=list(self.reply_to) or None,
            )
        for anhang in self.attachments:
            message.attach(anhang.filename, anhang.content, anhang.mimetype)
        return message

    def to_bytes(self) -> bytes:
        daten = {
            "format": FORMAT,
            "subject": self.subject,
            "body": self.body,
            "html_body": self.html_body,
            "from_email": self.from_email,
            "to": list(self.to),
            "reply_to": list(self.reply_to),
            "attachments": [
                {"filename": a.filename, "mimetype": a.mimetype, "content": base64.b64encode(a.content).decode("ascii")}
                for a in self.attachments
            ],
        }
        return json.dumps(daten, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> Mail:
        daten = json.loads(data.decode("utf-8"))
        if daten.get("format") != FORMAT:
            raise ValueError("Unbekanntes Format einer gespeicherten Mail")
        return cls(
            subject=daten["subject"],
            body=daten["body"],
            to=tuple(daten["to"]),
            html_body=daten.get("html_body"),
            from_email=daten.get("from_email"),
            reply_to=tuple(daten.get("reply_to") or ()),
            attachments=tuple(
                Attachment(a["filename"], base64.b64decode(a["content"]), a["mimetype"])
                for a in daten.get("attachments") or ()
            ),
        )
