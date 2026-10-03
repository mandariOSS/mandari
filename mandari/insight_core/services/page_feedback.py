# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anonyme Rückmeldung am Seitenende des Bürgerportals: „War diese Seite hilfreich? Ja / Nein“.

Statt A/B-Tests und Messung im Browser fragen wir auf jeder Seite schlicht nach. Gespeichert werden
Antwort, optional ein Satz (höchstens 500 Zeichen), Seitentyp, Pfad, Kommune und der Tag. Nicht
gespeichert werden IP-Adresse, Uhrzeit, Browserdaten oder eine Kennung; die Rückmeldung setzt kein
Cookie. Gegen Massenabgaben zählt ``throttle`` grob je Adresse im Cache (gehasht, eine Stunde).
Spamschutz: verstecktes Feld (Honeypot) wie bei den Bürgerfragen.

Der optionale Satz wird über ein signiertes, eine Stunde gültiges Zeichen der Antwort zugeordnet, das
nur in der Antwortseite steht – ohne Sitzung und ohne Cookie. Nach zwölf Monaten löscht der tägliche
Auftrag ``rueckmeldungen_aufraeumen`` (``insight_core/schedules.py``) die Einträge.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from django.conf import settings
from django.core import signing
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from .. import throttle

if TYPE_CHECKING:
    from ..models import OParlBody, PageFeedback

#: Seiten des Bürgerportals mit Rückmeldung: URL-Name → Bezeichnung in der Auswertung
PAGE_TYPES: Final[dict[str, str]] = {
    "portal_home": "Übersicht",
    "portal_entry": "Übersicht",
    "meeting_list": "Sitzungen",
    "meeting_calendar": "Sitzungskalender",
    "meeting_year_plan": "Jahresplan",
    "meeting_detail": "Sitzung",
    "paper_list": "Vorgänge",
    "paper_detail": "Vorgang",
    "organization_list": "Gremien",
    "organization_detail": "Gremium",
    "person_list": "Personen",
    "person_detail": "Person",
    "decision_list": "Beschlüsse",
    "decision_detail": "Beschluss",
    "question_portal": "Ratsfragen",
    "question_detail": "Ratsfrage",
    "file_list": "Dokumente",
    "search": "Suche",
    "map": "Karte",
    "neighborhood": "Nachbarschaft",
    "chat": "KI-Assistent",
    "saved": "Gespeichert",
    "notifications": "Benachrichtigungen",
}

#: Aufbewahrung in Tagen (zwölf Monate), überschreibbar mit ``INSIGHT_FEEDBACK_RETENTION_DAYS``
RETENTION_DAYS: Final = 365
#: Gültigkeit des Zeichens, mit dem der optionale Satz der Antwort zugeordnet wird
TOKEN_MAX_AGE: Final = 60 * 60
_SALT: Final = "insight.page_feedback"
_PATH_RE: Final = re.compile(r"^/[A-Za-z0-9/_\-.~%]{0,254}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class PageContext:
    """Was das Formular am Seitenende über die Seite mitschickt."""

    page_type: str
    path: str
    body_id: str


def page_context(request: HttpRequest, *, obj: Any = None, active_body: Any = None) -> PageContext | None:
    """Kontext für das Formular, ``None`` auf Seiten ohne Rückmeldung.

    Die Kommune kommt bei Detailseiten vom angezeigten Objekt (Besuch über eine Suchmaschine), sonst
    aus der Auswahl.
    """
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", None)
    if url_name not in PAGE_TYPES:
        return None
    body_id = getattr(obj, "body_id", None) or getattr(active_body, "pk", None)
    return PageContext(page_type=str(url_name), path=clean_path(request.path), body_id=str(body_id or ""))


def clean_path(path: str | None) -> str:
    """Nur der Pfad (ohne Abfrage), nur unbedenkliche Zeichen, sonst leer."""
    value = str(path or "").split("?", 1)[0].split("#", 1)[0]
    return value if _PATH_RE.match(value) and not value.startswith("//") else ""


def clean_comment(text: str | None) -> str:
    """Steuerzeichen entfernen, Leerraum glätten (Zeilenumbrüche bleiben)."""
    value = _CONTROL_RE.sub("", str(text or ""))
    return "\n".join(" ".join(line.split()) for line in value.strip().splitlines()).strip()


def rate_limited(request: HttpRequest) -> bool:
    """Zu viele Rückmeldungen von dieser Adresse in der letzten Stunde? (zählt mit)"""
    return throttle.hit(
        "feedback-ip",
        throttle.client_ip(request),
        limit=throttle.setting("INSIGHT_FEEDBACK_PER_IP_HOUR"),
        window=throttle.HOUR,
    )


def _body(body_id: Any) -> OParlBody | None:
    from ..models import OParlBody

    try:
        pk = uuid.UUID(str(body_id))
    except (TypeError, ValueError):
        return None
    return OParlBody.objects.filter(pk=pk).first()


def record(*, page_type: str, path: str, body_id: Any, helpful: bool) -> PageFeedback:
    """Antwort speichern (Seitentyp muss bekannt sein)."""
    from ..models import PageFeedback

    if page_type not in PAGE_TYPES:
        raise ValueError("Unbekannter Seitentyp")
    return PageFeedback.objects.create(page_type=page_type, path=clean_path(path), body=_body(body_id), helpful=helpful)


def token_for(feedback: PageFeedback) -> str:
    return signing.dumps(feedback.pk, salt=_SALT)


def add_comment(token: str, comment: str) -> bool:
    """Satz an die Antwort hängen; einmalig, nur mit gültigem Zeichen. ``True``, wenn gespeichert."""
    from ..models import PageFeedback

    try:
        pk = signing.loads(token, salt=_SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return False
    text = clean_comment(comment)[: PageFeedback.COMMENT_MAX_LENGTH]
    if not text or not isinstance(pk, int):
        return False
    return PageFeedback.objects.filter(pk=pk, comment="").update(comment=text) == 1


def retention_days() -> int:
    return int(getattr(settings, "INSIGHT_FEEDBACK_RETENTION_DAYS", RETENTION_DAYS))


def purge_expired() -> int:
    """Rückmeldungen nach Ablauf der Aufbewahrung löschen; liefert ihre Anzahl."""
    from ..models import PageFeedback

    cutoff = timezone.localdate() - timedelta(days=retention_days())
    deleted, _ = PageFeedback.objects.filter(created_on__lt=cutoff).delete()
    return deleted


def summary(queryset: QuerySet[PageFeedback], *, by: str = "page_type", limit: int = 30) -> list[dict[str, Any]]:
    """Zählung je Seitentyp bzw. Seite für die Auswertung im Admin (gefiltertes Queryset)."""
    rows = (
        queryset.order_by()
        .values(by)
        .annotate(
            total=Count("id"),
            yes=Count("id", filter=Q(helpful=True)),
            no=Count("id", filter=Q(helpful=False)),
            comments=Count("id", filter=~Q(comment="")),
        )
        .order_by("-total", by)[:limit]
    )
    result = []
    for row in rows:
        key = row[by]
        result.append(
            {
                **row,
                "label": PAGE_TYPES.get(key, key) if by == "page_type" else (key or "–"),
                "share": round(100 * row["yes"] / row["total"]) if row["total"] else 0,
            }
        )
    return result
