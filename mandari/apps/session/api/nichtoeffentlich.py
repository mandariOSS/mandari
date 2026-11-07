# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliche Ausgabe je Session-Mandant nach dem Profil Nichtöffentliches von kommParl (Issue #565).

Eine eigene OParl-Ausgabe mit eigenem System-Objekt und eigenen Adressen (``…`` steht für
``/session/<mandant>/api/oparl/nichtoeffentlich``): System ``…/``, Körperschaften ``…/bodies/``, Körperschaft
``…/body/``, externe Listen ``…/<liste>/``, Objekte ``…/<art>/<kennung>/`` und Dateien ``…/file/<kennung>/download/``.
Die Metadaten der geschützten Ressource (RFC 9728) stehen unter
``/.well-known/oauth-protected-resource/session/<mandant>/api/oparl/nichtoeffentlich/``. Die öffentliche Ausgabe
verweist nur mit ``kommparl:nonPublicSystem`` am System hierher; ihre Adressen und Kennungen bleiben unverändert (A7).

**Zugang** (Zugriffsmodell aus #565, abgestimmt am 05.11.2026), immer mit Access-Token des OpenID-Connect-Anbieters
im Header ``Authorization: Bearer``:

- **Person:** Scope ``ratsdaten:nichtoeffentlich`` (nur Code-Ablauf mit PKCE, Anmeldung mit zweitem Faktor, höchstens
  sieben Tage nach der Anmeldung; das setzt ``apps.accounts.oidc`` durch). Die Person braucht ein aktives Konto im
  Mandanten mit dem Recht „API-Zugang“ (``access_api``, wie in der Session-API v1: Der Mandant entscheidet, wer Daten
  über die Schnittstelle abrufen darf) und sieht bei jedem Abruf genau, was ``visibility.sicht_fuer_person`` ergibt:
  dieselbe Regel wie die Oberfläche, mit Rechten mit Geltungsbereich, befristeten Zuweisungen und Mitwirkungsverbot.
  Kein zweiter Rechteweg.
- **Dienst:** Client Credentials mit ``private_key_jwt``, Scope ``sitzung:lesen`` und Bindung an genau diesen
  Mandanten (``session-mandant:<Kennung>``, ``apps.accounts.oidc.bindings``). Er liest alles Nichtöffentliche des
  Mandanten außer Arbeitsständen, nie einen anderen Mandanten.

**Antworten** tragen ``Cache-Control: private, no-store`` und ``Vary: Authorization``, kein CORS und keinen ``ETag``.
Fehler kommen nach RFC 9457, bei 401 und 403 mit ``WWW-Authenticate`` nach RFC 6750 und RFC 9728. Objekte, die der
Aufrufer nicht sehen darf, fehlen in Listen und Einbettungen und ergeben am Objekt 404.

**Prüfprotokoll:** Jeder Abruf, dessen Antwort Nichtöffentliches enthält (``kommparl:nonPublic``,
``kommparl:nonPublicResolutionText``, Verweise öffentlicher Objekte auf nichtöffentliche:
``SessionMapping.nonpublic_refs``), steht im Protokoll des Mandanten (``audit.log_nonpublic_read``): Person oder
Dienst, Anwendung, Kennungen der Objekte. Kann der Eintrag nicht geschrieben werden, gibt es keine Inhalte (503).

Änderungsfeed und Snapshot bleiben öffentlich; diese Ausgabe bietet keine an. Standardmäßig aus: Nur mit
``OIDC_PROVIDER_ENABLED`` und ``OPARL_NONPUBLIC_ENABLED`` gibt es diese Adressen, sonst 404.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import wraps
from typing import Any, Final
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Prefetch, QuerySet
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from oauth2_provider.settings import oauth2_settings

from apps.accounts.oidc import access as oidc_access
from apps.accounts.oidc import bindings, catalog, provider_enabled
from apps.common.api.problems import PROBLEM_CONTENT_TYPE, Problem
from apps.session import audit
from apps.session import oparl_publication as pub
from apps.session.models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionFile,
    SessionMeeting,
    SessionPaper,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_service
from apps.session.services.insight_service import oparl_system_url
from apps.session.visibility import Sicht, sicht_fuer_dienst, sicht_fuer_person
from hub.api import kommparl
from hub.api.http import BadRequestError, is_rate_limited
from hub.api.serialization import TimeFilters, list_response, single_page
from hub.ris.canonical import Objekt, with_limited_text
from hub.ris.mapping.session import SessionMapping, SessionSicht, SessionUris

logger = logging.getLogger(__name__)

#: Scope für Anwendungen im Namen einer Person
SCOPE_PERSON: Final = catalog.RATSDATEN_NICHTOEFFENTLICH
#: Scope für Dienste, die an genau einen Mandanten gebunden sind
SCOPE_SERVICE: Final = catalog.SITZUNG_LESEN
#: Recht „API-Zugang“ im Mandanten (wie ``apps.session.api.v1.auth.API_ACCESS``): ohne es keine Schnittstelle
API_ACCESS: Final = "access_api"

#: Kopfzeilen jeder Antwort dieser Ausgabe (kommParl, Profil Nichtöffentliches, „Zwischenspeicher“)
CACHE_CONTROL: Final = "private, no-store"
_OHNE: Final = ("Access-Control-Allow-Origin", "Access-Control-Expose-Headers", "Access-Control-Allow-Methods", "ETag")

#: Zeitfilter auf den Session-Zeitstempeln (wie die öffentliche Ausgabe)
FILTER_LOOKUPS: Final = {
    "created_since": "created_at__gte",
    "created_until": "created_at__lte",
    "modified_since": "updated_at__gte",
    "modified_until": "updated_at__lte",
}


def enabled() -> bool:
    """Bietet die Installation die nichtöffentliche Ausgabe an (Anbieter und ``OPARL_NONPUBLIC_ENABLED`` an)?"""
    return provider_enabled() and bool(getattr(settings, "OPARL_NONPUBLIC_ENABLED", False))


def system_url(tenant: Any) -> str:
    """Adresse des System-Objekts der nichtöffentlichen Ausgabe; zugleich Kennung der geschützten Ressource."""
    base = str(getattr(settings, "SITE_URL", "http://localhost:8000")).rstrip("/")
    return f"{base}{reverse('session:oparl_noe_system', kwargs={'tenant_slug': tenant.slug})}"


def metadata_url(tenant: Any) -> str:
    """Adresse der Metadaten der geschützten Ressource, gebildet nach RFC 9728, Abschnitt 3.1."""
    parts = urlsplit(system_url(tenant))
    return f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{parts.path}"


# =============================================================================
# Bindung eines Dienstes an einen Mandanten (fachfreie Kennung der Plattform)
# =============================================================================


def _bindung_aufloesen(slug: str) -> str | None:
    tenant = SessionTenant.objects.filter(slug=slug, is_active=True).only("pk").first()
    return str(tenant.pk) if tenant is not None else None


def _bindung_beschreiben(key: str) -> str | None:
    try:
        pk = uuid.UUID(key)
    except ValueError:
        return None
    name = SessionTenant.objects.filter(pk=pk).values_list("name", flat=True).first()
    return str(name) if name else None


BINDUNG: Final = bindings.Kind(
    name=bindings.TENANT, label="Session-Mandant", resolve=_bindung_aufloesen, describe=_bindung_beschreiben
)


def register() -> None:
    """Art der Bindung „Session-Mandant“ bei der Plattform anmelden (aus ``SessionConfig.ready``)."""
    bindings.register(BINDUNG)


# =============================================================================
# Antworten
# =============================================================================


def _privat(response: HttpResponseBase) -> HttpResponseBase:
    """Nie zwischenspeichern, nie für andere Herkünfte freigeben, keinen ``ETag``."""
    for header in _OHNE:
        if header in response:
            del response[header]
    response["Cache-Control"] = CACHE_CONTROL
    response["Pragma"] = "no-cache"
    patch_vary_headers(response, ("Authorization",))
    return response


def _json(data: Any) -> HttpResponseBase:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return _privat(HttpResponse(payload, content_type="application/json; charset=utf-8"))


def problem(
    request: HttpRequest, status: int, detail: str, *, kind: str | None = None, headers: dict[str, str] | None = None
) -> HttpResponseBase:
    """Fehler nach RFC 9457, mit den Kopfzeilen dieser Ausgabe."""
    body = Problem(status=status, kind=kind, detail=detail).to_dict(instance=request.path)
    # Typ gleich beim Anlegen setzen: Die Antwort ist nie HTML, auch nicht kurzzeitig mit dem Standardtyp
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    response = HttpResponse(payload, status=status, content_type=PROBLEM_CONTENT_TYPE)
    for key, value in (headers or {}).items():
        response[key] = value
    return _privat(response)


class Abgelehnt(Exception):  # noqa: N818 – deutscher Begriff wie im Rest des Moduls
    """Die Anfrage bekommt keine Inhalte; ``response`` ist die Antwort dafür."""

    def __init__(self, response: HttpResponseBase) -> None:
        super().__init__()
        self.response = response


def _bearer(request: HttpRequest, tenant: Any, *, error: str | None = None, scope: str | None = None) -> str:
    """Wert von ``WWW-Authenticate`` (RFC 6750, Abschnitt 3, mit ``resource_metadata`` nach RFC 9728, 5.1)."""
    teile = [f'resource_metadata="{metadata_url(tenant)}"']
    if error:
        teile.insert(0, f'error="{error}"')
    if scope:
        teile.insert(1, f'scope="{scope}"')
    return "Bearer " + ", ".join(teile)


# =============================================================================
# Zugang
# =============================================================================


@dataclass(frozen=True)
class Zugang:
    """Ein angemeldeter Aufrufer der nichtöffentlichen Ausgabe eines Mandanten und was er sieht."""

    tenant: Any
    access: oidc_access.OAuthAccess
    #: Konto der Person im Mandanten; ``None`` bei einem Dienst
    session_user: Any
    sicht: Sicht

    @property
    def dienst(self) -> bool:
        return not self.access.acts_for_person


def zugang(request: HttpRequest, tenant: Any) -> Zugang:
    """
    Aufrufer der Anfrage prüfen und seine Sicht berechnen (bei jedem Abruf).

    Raises: :class:`Abgelehnt` mit 401 oder 403
    """
    header = str(request.META.get("HTTP_AUTHORIZATION", ""))
    if not header:
        raise Abgelehnt(
            problem(
                request,
                401,
                "Für diese Ausgabe ist ein gültiges Zugriffstoken nötig.",
                kind="nicht-authentifiziert",
                headers={"WWW-Authenticate": _bearer(request, tenant)},
            )
        )
    scheme, _, raw = header.partition(" ")
    try:
        if scheme.lower() != "bearer":
            raise oidc_access.InvalidAccessToken
        access = oidc_access.authenticate(raw.strip(), resource_uri=system_url(tenant))
    except oidc_access.InvalidAccessToken:
        raise Abgelehnt(
            problem(
                request,
                401,
                "Das Zugriffstoken ist ungültig, abgelaufen oder widerrufen.",
                kind="nicht-authentifiziert",
                headers={"WWW-Authenticate": _bearer(request, tenant, error="invalid_token")},
            )
        ) from None

    def fehlender_scope(scope: str) -> Abgelehnt:
        return Abgelehnt(
            problem(
                request,
                403,
                f"Dem Zugriffstoken fehlt der Scope „{scope}“.",
                kind="keine-berechtigung",
                headers={"WWW-Authenticate": _bearer(request, tenant, error="insufficient_scope", scope=scope)},
            )
        )

    if access.acts_for_person:
        if not access.has_scope(SCOPE_PERSON):
            raise fehlender_scope(SCOPE_PERSON)
        session_user = (
            SessionUser.objects.filter(user=access.user, tenant=tenant, is_active=True)
            .select_related("tenant", "user")
            .prefetch_related("roles")
            .first()
        )
        if session_user is None:
            raise Abgelehnt(problem(request, 403, "Kein Zugang zu diesem Mandanten.", kind="keine-berechtigung"))
        sicht = sicht_fuer_person(session_user)
        if API_ACCESS not in sicht.permissions:
            # Wie in der Session-API v1: ohne „API-Zugang“ keine Daten über die Schnittstelle, auch mit freigegebener
            # Anwendung; so behält der Mandant den Hebel gegen den Abfluss über Anwendungen
            raise Abgelehnt(
                problem(request, 403, "Das Konto hat in diesem Mandanten keinen API-Zugang.", kind="keine-berechtigung")
            )
        return Zugang(tenant=tenant, access=access, session_user=session_user, sicht=sicht)
    if not access.has_scope(SCOPE_SERVICE):
        raise fehlender_scope(SCOPE_SERVICE)
    if bindings.key_of(access.binding, bindings.TENANT) != str(tenant.pk):
        raise Abgelehnt(
            problem(request, 403, "Der Dienst ist an einen anderen Mandanten gebunden.", kind="keine-berechtigung")
        )
    return Zugang(tenant=tenant, access=access, session_user=None, sicht=sicht_fuer_dienst())


View = Callable[..., HttpResponseBase]


def endpoint(view: View) -> View:
    """
    Dekorator jedes Endpunkts: eingeschaltet, nur GET und HEAD, Ratenbegrenzung, Mandant, Zugang, Kopfzeilen.

    Der Endpunkt bekommt statt des Kurznamens den :class:`Zugang`.
    """

    @wraps(view)
    def wrapper(request: HttpRequest, tenant_slug: str, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not enabled():
            return problem(request, 404, "Diese Adresse gibt es nicht.", kind="nicht-gefunden")
        if request.method not in ("GET", "HEAD"):
            return problem(request, 405, "Die Ausgabe ist rein lesend.", headers={"Allow": "GET, HEAD"})
        if is_rate_limited(request):
            return problem(request, 429, "Zu viele Anfragen. Bitte später erneut versuchen.", kind="ratenlimit")
        tenant = SessionTenant.objects.filter(slug=tenant_slug, is_active=True).first()
        if tenant is None:
            return problem(request, 404, "Mandant nicht gefunden.", kind="nicht-gefunden")
        try:
            return view(request, zugang(request, tenant), *args, **kwargs)
        except Abgelehnt as abgelehnt:
            return abgelehnt.response
        except BadRequestError as exc:
            return problem(request, 400, exc.message, kind="ungueltige-anfrage")

    return wrapper


# =============================================================================
# Abbildung und Querysets
# =============================================================================


def _nichtoeffentlicher_beschlusstext(item: Any) -> str:
    return str(item.get_resolution_text_decrypted() or "")


def mapping(zugang: Zugang) -> SessionMapping:
    """Abbildung der nichtöffentlichen Ausgabe: eigene Adressen, Sicht des Aufrufers, Verweise auf die öffentliche."""
    from apps.session.api.oparl import SOURCE

    tenant = zugang.tenant
    sicht = zugang.sicht
    public = SessionUris(oparl_system_url(tenant)) if tenant.oparl_public_since else None
    return SessionMapping(
        tenant,
        system_url(tenant),
        SOURCE,
        conforms_to=kommparl.conforms_to(feed_offered=False, nonpublic=True),
        sicht=SessionSicht(
            meeting=sicht.sitzung_noe,
            agenda_item=sicht.top_noe,
            paper=sicht.vorlage_noe,
            file=sicht.anlage_noe,
            nonpublic_resolution_text=_nichtoeffentlicher_beschlusstext,
            public=public,
        ),
    )


def _meetings(zugang: Zugang) -> QuerySet[Any]:
    return SessionMeeting.objects.filter(tenant=zugang.tenant).filter(zugang.sicht.meeting_q())


def _agenda_items(zugang: Zugang) -> QuerySet[Any]:
    return SessionAgendaItem.objects.filter(meeting__tenant=zugang.tenant).filter(zugang.sicht.agenda_item_q())


def _papers(zugang: Zugang) -> QuerySet[Any]:
    return SessionPaper.objects.filter(tenant=zugang.tenant).filter(zugang.sicht.paper_q())


def _consultations(zugang: Zugang) -> QuerySet[Any]:
    return SessionConsultation.objects.filter(paper__tenant=zugang.tenant).filter(zugang.sicht.paper_q("paper__"))


def _files(zugang: Zugang) -> QuerySet[Any]:
    return SessionFile.objects.filter(tenant=zugang.tenant).filter(zugang.sicht.file_q())


def _embedded_files(zugang: Zugang) -> QuerySet[Any]:
    # Eingebettete Dateien ohne Text: Er steht nur am Objekt-Endpunkt
    return _files(zugang).select_related("paper", "meeting", "agenda_item__meeting").defer("text_content")


def _prepare_meetings(qs: QuerySet[Any], zugang: Zugang) -> QuerySet[Any]:
    return (
        qs.select_related("protocol__public_file__meeting", "protocol__approval_meeting", "tenant__state_profile")
        .defer("protocol__public_file__text_content")
        .prefetch_related(
            "joint_organizations",
            Prefetch(
                "agenda_items",
                queryset=_agenda_items(zugang).select_related("consultation__paper").order_by("order", "number"),
            ),
            Prefetch("agenda_items__files", queryset=_embedded_files(zugang)),
            "agenda_items__votes__person",
            Prefetch("files", queryset=_embedded_files(zugang)),
        )
    )


def _prepare_papers(qs: QuerySet[Any], zugang: Zugang) -> QuerySet[Any]:
    return qs.prefetch_related(
        Prefetch("files", queryset=_embedded_files(zugang)),
        Prefetch(
            "consultations",
            queryset=SessionConsultation.objects.select_related("meeting", "agenda_item__meeting"),
        ),
    )


def _prepare_agenda_items(qs: QuerySet[Any], zugang: Zugang) -> QuerySet[Any]:
    return qs.select_related("meeting__tenant", "consultation__paper").prefetch_related(
        "meeting__joint_organizations",
        Prefetch("files", queryset=_embedded_files(zugang)),
        "votes__person",
    )


def _prepare_related(qs: QuerySet[Any], zugang: Zugang) -> QuerySet[Any]:
    return qs.select_related("paper", "meeting", "agenda_item__meeting")


def _prepare_file_list(qs: QuerySet[Any], zugang: Zugang) -> QuerySet[Any]:
    return _prepare_related(qs, zugang).defer("text_content")


def _prepare_file_object(qs: QuerySet[Any], zugang: Zugang) -> QuerySet[Any]:
    return with_limited_text(_prepare_related(qs, zugang))


def _prepare_memberships(qs: QuerySet[Any], zugang: Zugang) -> QuerySet[Any]:
    return qs.prefetch_related("memberships")


#: Liste -> (Queryset, Vorbereitung, Abbildung, Objekttyp)
LIST_SPECS: Final[dict[str, tuple[Callable[[Zugang], QuerySet[Any]], Any, Any, str]]] = {
    "organizations": (
        lambda z: pub.visible_organizations(z.tenant),
        _prepare_memberships,
        "organization",
        "organization",
    ),
    "people": (lambda z: pub.visible_persons(z.tenant), _prepare_memberships, "person", "person"),
    "memberships": (lambda z: pub.visible_memberships(z.tenant), None, "membership", "membership"),
    "meetings": (_meetings, _prepare_meetings, "meeting", "meeting"),
    "agendaitems": (_agenda_items, _prepare_agenda_items, "agenda_item", "agendaitem"),
    "papers": (_papers, _prepare_papers, "paper", "paper"),
    "consultations": (_consultations, _prepare_related, "consultation", "consultation"),
    "files": (_files, _prepare_file_list, "file", "file"),
    "legislativeterms": (
        lambda z: pub.visible_legislative_terms(z.tenant),
        None,
        "legislative_term",
        "legislativeterm",
    ),
}

#: Objekttyp -> (Queryset, Vorbereitung, Abbildung)
OBJECT_SPECS: Final[dict[str, tuple[Callable[[Zugang], QuerySet[Any]], Any, str]]] = {
    "organization": (lambda z: pub.visible_organizations(z.tenant), _prepare_memberships, "organization"),
    "person": (lambda z: pub.visible_persons(z.tenant), _prepare_memberships, "person"),
    "membership": (lambda z: pub.visible_memberships(z.tenant), None, "membership"),
    "meeting": (_meetings, _prepare_meetings, "meeting"),
    "agendaitem": (_agenda_items, _prepare_agenda_items, "agenda_item"),
    "paper": (_papers, _prepare_papers, "paper"),
    "consultation": (_consultations, _prepare_related, "consultation"),
    # Der erkannte Text nur am Objekt-Endpunkt, nicht in Listen und Einbettungen
    "file": (_files, _prepare_file_object, "file_with_text"),
    "legislativeterm": (lambda z: pub.visible_legislative_terms(z.tenant), None, "legislative_term"),
}


def _queryset(spec_query: Callable[[Zugang], QuerySet[Any]], prepare: Any, zugang: Zugang) -> QuerySet[Any]:
    queryset = spec_query(zugang)
    return prepare(queryset, zugang) if prepare is not None else queryset


# =============================================================================
# Prüfprotokoll
# =============================================================================


def nichtoeffentliche_kennungen(objekte: Iterable[Any]) -> list[str]:
    """Kennungen (``id``) aller nichtöffentlichen Objekte und Objekte mit nichtöffentlichen Eigenschaften, auch eingebettet."""
    gefunden: list[str] = []

    def besuchen(knoten: Any) -> None:
        if isinstance(knoten, list):
            for wert in knoten:
                besuchen(wert)
        elif isinstance(knoten, dict):
            if knoten.get("kommparl:nonPublic") is True or "kommparl:nonPublicResolutionText" in knoten:
                gefunden.append(str(knoten.get("id", "")))
            for wert in knoten.values():
                besuchen(wert)

    besuchen(list(objekte))
    return [kennung for kennung in dict.fromkeys(gefunden) if kennung]


def _protokollieren(
    request: HttpRequest, zugang: Zugang, abruf: str, kennungen: list[str], *, action: str = "view"
) -> HttpResponseBase | None:
    """Abruf mit Nichtöffentlichem protokollieren; ``None`` bei Erfolg oder ohne Nichtöffentliches, sonst 503."""
    if not kennungen:
        return None
    try:
        audit.log_nonpublic_read(
            request,
            tenant=zugang.tenant,
            user=zugang.session_user,
            anwendung=zugang.access.client_id,
            anwendung_name=zugang.access.application_name,
            dienst=zugang.dienst,
            abruf=abruf,
            objekte=kennungen,
            action=action,
        )
    except Exception:
        logger.exception("Abruf der nichtöffentlichen Ausgabe konnte nicht protokolliert werden")
        return problem(request, 503, "Der Abruf konnte nicht protokolliert werden. Bitte später erneut versuchen.")
    return None


def _kennungen(objekte: Iterable[Any], abbildung: SessionMapping) -> list[str]:
    """Kennungen fürs Prüfprotokoll: gekennzeichnete Objekte und Verweise öffentlicher Objekte auf nichtöffentliche."""
    return list(dict.fromkeys([*nichtoeffentliche_kennungen(objekte), *abbildung.nonpublic_refs]))


def _ausliefern(
    request: HttpRequest, zugang: Zugang, abbildung: SessionMapping, data: Any, abruf: str
) -> HttpResponseBase:
    """Antwort mit Eintrag im Prüfprotokoll, wenn sie Nichtöffentliches enthält."""
    abgelehnt = _protokollieren(request, zugang, abruf, _kennungen([data], abbildung))
    return abgelehnt if abgelehnt is not None else _json(data)


# =============================================================================
# Endpunkte
# =============================================================================


@endpoint
def system_view(request: HttpRequest, zugang: Zugang) -> HttpResponseBase:
    abbildung = mapping(zugang)
    return _ausliefern(request, zugang, abbildung, abbildung.system(), "System")


@endpoint
def bodies_view(request: HttpRequest, zugang: Zugang) -> HttpResponseBase:
    abbildung = mapping(zugang)
    seite = single_page(abbildung.uris.bodies(), [abbildung.body()])
    return _ausliefern(request, zugang, abbildung, seite, "Körperschaften")


@endpoint
def body_view(request: HttpRequest, zugang: Zugang) -> HttpResponseBase:
    abbildung = mapping(zugang)
    return _ausliefern(request, zugang, abbildung, abbildung.body(), "Körperschaft")


@endpoint
def list_view(request: HttpRequest, zugang: Zugang, segment: str) -> HttpResponseBase:
    spec = LIST_SPECS.get(segment.lower())
    if spec is None:
        return problem(request, 404, f"Unbekannte Liste '{segment}'.", kind="nicht-gefunden")
    query, prepare, method, _kind = spec
    abbildung = mapping(zugang)
    serializer = getattr(abbildung, method)
    filters = TimeFilters.from_request(request)
    entries = filters.apply(_queryset(query, prepare, zugang), FILTER_LOOKUPS).order_by("updated_at", "id")
    gerendert: list[Objekt] = []

    def render(page: list[Any]) -> list[Objekt]:
        objekte = [serializer(obj) for obj in page]
        gerendert.extend(objekte)
        return objekte

    response = list_response(request, abbildung.uris.list(segment.lower()), entries, filters, render)
    if response.status_code != 200:
        meldung = json.loads(response.content).get("error", "Nicht gefunden.")
        return problem(request, response.status_code, str(meldung), kind="nicht-gefunden")
    abgelehnt = _protokollieren(request, zugang, f"Liste {segment.lower()}", _kennungen(gerendert, abbildung))
    return abgelehnt if abgelehnt is not None else _privat(response)


@endpoint
def object_view(request: HttpRequest, zugang: Zugang, kind: str, pk: Any) -> HttpResponseBase:
    kind = kind.lower()
    abbildung = mapping(zugang)
    if kind == "location":
        meeting = _meetings(zugang).filter(pk=pk).first()
        data = None
        if meeting is not None and (meeting.is_public or zugang.sicht.sitzung_noe(meeting)):
            data = abbildung.location(meeting)
        if data is None:
            return problem(request, 404, "Nicht gefunden.", kind="nicht-gefunden")
        return _ausliefern(request, zugang, abbildung, data, "Ort")
    spec = OBJECT_SPECS.get(kind)
    if spec is None:
        return problem(request, 404, f"Unbekannter Objekttyp '{kind}'.", kind="nicht-gefunden")
    query, prepare, method = spec
    obj = _queryset(query, prepare, zugang).filter(pk=pk).first()
    if obj is None:
        # Gibt es nicht oder darf der Aufrufer nicht sehen; die Antwort verrät nicht, was davon
        return problem(request, 404, "Nicht gefunden.", kind="nicht-gefunden")
    return _ausliefern(request, zugang, abbildung, getattr(abbildung, method)(obj), kind)


@endpoint
def file_download_view(request: HttpRequest, zugang: Zugang, pk: Any) -> HttpResponseBase:
    file_obj = _prepare_related(_files(zugang), zugang).filter(pk=pk).first()
    if file_obj is None or not file_obj.file:
        return problem(request, 404, "Datei nicht gefunden.", kind="nicht-gefunden")
    if not pub._is_published(file_obj):
        kennung = mapping(zugang).uris.obj("file", file_obj.pk)
        abgelehnt = _protokollieren(request, zugang, "Datei", [kennung], action="download")
        if abgelehnt is not None:
            return abgelehnt
    # Im Browser nur PDF und Rasterbilder; HTML, SVG und alles andere als Download (file_service)
    response = file_service.file_response(
        file_obj.file.open("rb"), file_service.download_name(file_obj), inline="download" not in request.GET
    )
    return _privat(response)


def metadata_view(request: HttpRequest, tenant_slug: str) -> HttpResponseBase:
    """
    Metadaten der geschützten Ressource nach RFC 9728, ohne Anmeldung: Kennung, Aussteller, Scopes, Verfahren.

    Öffentlich und für jede Herkunft lesbar; sie nennen nichts, was nicht ohnehin öffentlich ist.
    """
    if not enabled():
        return problem(request, 404, "Diese Adresse gibt es nicht.", kind="nicht-gefunden")
    if request.method not in ("GET", "HEAD"):
        return problem(request, 405, "Nur GET.", headers={"Allow": "GET, HEAD"})
    tenant = SessionTenant.objects.filter(slug=tenant_slug, is_active=True).first()
    if tenant is None:
        return problem(request, 404, "Mandant nicht gefunden.", kind="nicht-gefunden")
    site = str(getattr(settings, "SITE_URL", "")).rstrip("/")
    data = {
        "resource": system_url(tenant),
        "authorization_servers": [str(oauth2_settings.OIDC_ISS_ENDPOINT).rstrip("/")],
        "scopes_supported": [SCOPE_PERSON, SCOPE_SERVICE],
        "bearer_methods_supported": ["header"],
        "resource_name": f"{tenant.name}, nichtöffentliche Ausgabe",
        "resource_documentation": f"{site}/api/docs/",
    }
    response = HttpResponse(json.dumps(data, ensure_ascii=False).encode("utf-8"), content_type="application/json")
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "no-cache"
    return response
