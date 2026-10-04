# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Context Processors für Mandari Insight.

Stellt globale Context-Variablen für alle Templates bereit.
"""

from typing import Any

from django.apps import apps
from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

from .models import OParlBody
from .navigation import breadcrumb_area, nav_area
from .publication import DETAIL_PAGES, PORTAL_NAMESPACE, body_id_of


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
        # Namensnennung der Quellen im Kommunenwechsel; erst abgefragt, wenn ein Template sie zeigt
        "kommunenverzeichnis_quellen": SimpleLazyObject(_kommunenverzeichnis_quellen),
    }


def _kommunenverzeichnis_quellen() -> list[dict[str, str]]:
    from .services.kommunenverzeichnis import quellen

    try:
        return quellen()
    except Exception:  # noqa: BLE001 - die Namensnennung darf keine Seite brechen (Datenbank noch nicht migriert)
        return []


def _seiten_kommune(request: HttpRequest, gewaehlt: OParlBody | None, match: Any) -> OParlBody | None:
    """
    Kommune der angezeigten Seite: bei Detailseiten die des Eintrags, sonst die gewählte (Issue #783).

    Wer über eine Suchmaschine direkt auf einen Vorgang kommt oder bei gewählter Kommune A einen Vorgang von B
    öffnet, sieht in Brotkrumen, Fuß und Kopfzeile die Kommune des Vorgangs, nicht die der Sitzung. Die Middleware
    des Veröffentlichungsstands kennt die Kommune des Eintrags schon, wenn eine Quelle einen Stand hat
    (``request.insight_publication_body``); sonst genügt eine Abfrage über den Primärschlüssel.
    """
    if match is None or match.namespace != PORTAL_NAMESPACE:
        return gewaehlt
    detail = DETAIL_PAGES.get(match.url_name or "")
    if detail is None:
        return gewaehlt
    if hasattr(request, "insight_publication_body"):
        body_id = request.insight_publication_body
    else:
        model_name, key = detail
        body_id = body_id_of(apps.get_model("insight_core", model_name), match.kwargs.get(key))
    if body_id is None:
        return None
    if gewaehlt is not None and str(gewaehlt.pk) == str(body_id):
        return gewaehlt
    return OParlBody.objects.filter(pk=body_id).first()


def active_body(request: HttpRequest) -> dict[str, Any]:
    """
    Stellt die aktive Kommune (Body) im Template-Context bereit.

    Die Kommune wird aus der Session oder URL ermittelt.
    Wenn "all" in der Session steht, wird keine spezifische Kommune ausgewählt.

    ``active_body`` ist die gewählte Kommune (Seitenleiste, Navigation, Listen), ``page_body`` die Kommune der
    angezeigten Seite (Brotkrumen, Fuß, Datenstand, „Zuletzt besucht“); beide unterscheiden sich nur auf
    Detailseiten.
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

    # Kommune der Seite: auf Detailseiten die des Eintrags (Herkunft im Fuß, Brotkrumen, Datenstand)
    try:
        page_body = _seiten_kommune(request, body, match)
    except Exception:  # noqa: BLE001 - der Rahmen darf keine Seite brechen (wie oben)
        page_body = body

    # Datenstand-Hinweis: Quelle der Kommune der Seite seit der kritischen Schwelle nicht synchronisiert. Mit
    # eigenem Hinweis der Kommune entfällt er – der nennt den Grund, „nicht erreichbar“ träfe dann nicht zu.
    stale_days = None
    page_notice = (page_body.portal_notice or "").strip() if page_body is not None else ""
    if page_body is not None and page_body.last_sync and archive is None and not page_notice:
        from datetime import timedelta

        from django.utils import timezone

        critical_days = int(getattr(settings, "INSIGHT_SOURCE_STALE_CRITICAL_DAYS", 7))
        age = timezone.now() - page_body.last_sync
        if age > timedelta(days=critical_days):
            stale_days = age.days

    from .services.kommunenverzeichnis import ort_der_koerperschaft

    return {
        "active_body": body,
        "page_body": page_body,
        # Zweite Zeile in „Zuletzt besucht“: „Kreisfreie Stadt, Nordrhein-Westfalen“
        "page_body_ort": ort_der_koerperschaft(page_body) if page_body is not None else "",
        # Seite einer anderen als der gewählten Kommune: Brotkrumen wählen beim Klick erst diese Kommune
        "page_body_foreign": page_body is not None
        and portal is None
        and (body is None or str(page_body.pk) != str(body.pk)),
        "available_bodies": bodies,
        "show_all_bodies": show_all_bodies,
        "insight_decisions_published": decisions_published,
        "active_body_stale_days": stale_days,
        "active_body_archive": archive,
        "active_body_notice": notice if body_page else "",
        "insight_portal": portal,
    }
