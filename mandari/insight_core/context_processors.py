# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Context Processors für Mandari Insight.

Stellt globale Context-Variablen für alle Templates bereit.
"""

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from .models import OParlBody
from .navigation import breadcrumb_area, nav_area
from .publication import PORTAL_NAMESPACE


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    """
    Setzt den Navigationskontext.

    Mandari only serves portal pages now (under /insight/).
    Marketing pages are served by the separate Wagtail site.
    """
    marketing_url = getattr(settings, "MARKETING_URL", "")
    match = getattr(request, "resolver_match", None)
    url_name = match.url_name if match is not None and match.namespace == PORTAL_NAMESPACE else None
    return {
        "is_portal": True,
        "is_marketing": False,
        "nav_context": "portal",
        "has_chat_consent": request.session.get("chat_consent", False),
        "marketing_url": marketing_url,
        # Abos zu Themen und Orten (INSIGHT_SUBSCRIPTIONS_ENABLED): ausgeschaltet keine Links darauf
        "insight_subscriptions_enabled": bool(getattr(settings, "INSIGHT_SUBSCRIPTIONS_ENABLED", False)),
        # Ratsfragen (INSIGHT_QUESTIONS_ENABLED, Issue #734): pausiert lesbar, ohne Stellen und Antwortquoten
        "insight_questions_enabled": bool(getattr(settings, "INSIGHT_QUESTIONS_ENABLED", False)),
        # Bereich der Seite in der Navigation (Hervorhebung und aria-current, Issue #783)
        "insight_area": nav_area(url_name),
        # Bereich als Brotkrume der Kopfzeile (Stufe 2)
        "insight_breadcrumb": breadcrumb_area(url_name),
    }


def active_body(request):
    """
    Stellt die aktive Kommune (Body) im Template-Context bereit.

    Die Kommune wird aus der Session oder URL ermittelt.
    Wenn "all" in der Session steht, wird keine spezifische Kommune ausgewählt.
    """
    from .portal import get_portal

    # Versuche Body aus Session zu laden
    body_id = request.session.get("active_body_id") if hasattr(request, "session") else None
    body = None
    bodies: Any = []
    show_all_bodies = False
    portal = None

    try:
        # Bürgerportal einer Körperschaft (Issue #317): Auswahl auf diese Kommune festgelegt
        portal = get_portal(request)
        if portal is not None:
            body = portal.body
            bodies = [body]
        else:
            # Erst bei Bedarf geladen (alte Auswahl in base.html); der Kommunenwechsel des Bürgerportals fragt das
            # Verzeichnis selbst ab und braucht keine Liste aller Kommunen je Seitenaufruf (Issue #783)
            bodies = SimpleLazyObject(lambda: list(OParlBody.objects.listed().order_by("name")))

            # "all" bedeutet: Alle Kommunen anzeigen (keine spezifische ausgewählt)
            if body_id == "all":
                show_all_bodies = True
                body = None
            elif body_id:
                try:
                    body = OParlBody.objects.get(id=body_id)
                except OParlBody.DoesNotExist:
                    body = None

            # Kein Fallback mehr - wenn keine Kommune ausgewählt, zeigen wir alle
            # Nur bei erster Nutzung (keine Session) setzen wir auf "all"
            if body_id is None and OParlBody.objects.listed().exists():
                show_all_bodies = True
                request.session["active_body_id"] = "all"

    except Exception:
        # Datenbank noch nicht migriert oder andere Fehler
        pass

    # Archiv (Issue #618): Die Kommune veröffentlicht nicht mehr, der Bestand bleibt lesbar. Maßgeblich
    # ist die Kommune der Seite (Middleware), sonst die gewählte. Detailseiten haben eine eigene
    # Kommune: Ohne deren Stand gilt nicht der Stand der gewählten Kommune.
    from .publication import BODY_PAGES, body_state

    publication_state = getattr(request, "insight_publication_state", None)
    match = getattr(request, "resolver_match", None)
    # Nur Seiten des Bürgerportals zeigen den Hinweis; Work und Session fragen den Stand nicht ab
    portal_page = match is not None and (match.namespace or "").startswith("insight_core")
    own_body = hasattr(request, "insight_publication_body")
    if publication_state is None and body is not None and portal_page and not own_body:
        try:
            publication_state = body_state(body.pk)
        except Exception:  # noqa: BLE001 - der Hinweis darf keine Seite brechen (wie oben)
            publication_state = None
    archive = publication_state if publication_state is not None and publication_state.archived else None

    # Beschlüsse stehen nur in der Navigation, wenn die Kommune sie veröffentlicht (Issue #783):
    # Ein Eintrag, der auf „Noch keine Beschlüsse“ führt, hilft niemandem. Nur auf Portalseiten abgefragt.
    decisions_published = False
    if body is not None and portal_page:
        from .services import decision_tracking

        try:
            decisions_published = decision_tracking.publishing_tenants(body).exists()
        except Exception:  # noqa: BLE001 - die Navigation darf keine Seite brechen (wie oben)
            decisions_published = False

    # Hinweis der Kommune (Issue #734, im Admin gepflegt), z. B. „Die Stadt stellt ihre Daten nicht mehr
    # bereit“: auf Einstieg und Listen der gewählten Kommune (BODY_PAGES, dazu der eigene Einstieg
    # /insight/k/<slug>/). Detailseiten haben eine eigene Kommune und zeigen ihn nicht.
    notice = (body.portal_notice or "").strip() if body is not None else ""
    body_page = (
        match is not None
        and match.namespace == PORTAL_NAMESPACE
        and (match.url_name in BODY_PAGES or match.url_name == "portal_entry")
    )

    # Datenstand-Hinweis: Quelle der Kommune seit der kritischen Schwelle nicht synchronisiert. Mit eigenem
    # Hinweis der Kommune entfällt er – der nennt den Grund, „nicht erreichbar“ träfe dann nicht zu.
    stale_days = None
    if body and body.last_sync and archive is None and not notice:
        from datetime import timedelta

        from django.utils import timezone

        critical_days = int(getattr(settings, "INSIGHT_SOURCE_STALE_CRITICAL_DAYS", 7))
        age = timezone.now() - body.last_sync
        if age > timedelta(days=critical_days):
            stale_days = age.days

    from .services.kommunenverzeichnis import ort_der_koerperschaft

    return {
        "active_body": body,
        # Zweite Zeile im Kommunenwechsel und in „Zuletzt besucht“: „Kreisfreie Stadt, Nordrhein-Westfalen“
        "active_body_ort": ort_der_koerperschaft(body) if body is not None else "",
        "available_bodies": bodies,
        "show_all_bodies": show_all_bodies,
        "insight_decisions_published": decisions_published,
        "active_body_stale_days": stale_days,
        "active_body_archive": archive,
        "active_body_notice": notice if body_page else "",
        "insight_portal": portal,
    }
