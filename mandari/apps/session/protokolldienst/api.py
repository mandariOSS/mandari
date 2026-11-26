# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bereich ``protokolldienst`` der Schnittstelle unter ``/api/mandari/v1/protokolldienst/{tenant_slug}/``.

Ein angebundener Protokolldienst übergibt hier die freigegebenen Entwürfe der Niederschrift als Vorschlag
(Befehl ``session.minutes_draft.propose``). Anmeldung mit dem Rückgabeschlüssel des Mandanten
(``Authorization: Bearer <schlüssel>``), den die Verwaltung in den Einstellungen erzeugt; er gilt nur für die
Sitzungen dieses Mandanten und nur, solange die Anbindung eingeschaltet ist.

- ``GET  /{tenant_slug}/``: Prüfung der Verbindung (Mandant, Adresse der Vorschläge).
- ``POST /{tenant_slug}/niederschrift/vorschlaege/``: Vorschläge je TOP. Sitzung und TOPs werden über ihre
  OParl-Kennung genannt (``…/meeting/<uuid>/``, ``…/agendaitem/<uuid>/``), unter der der Dienst sie übernommen hat;
  TOPs ohne Kennung über die TOP-Nummer. Eine genannte Kennung gilt allein: Passt sie zu keinem TOP, ordnet die
  Nummer nicht ersatzweise zu (der TOP kann entfernt oder umnummeriert sein). Nicht zuzuordnende TOPs nennt die
  Antwort (``unassigned``), die übrigen gelten. ``Idempotency-Key`` ist empfohlen; ohne ihn gilt
  ``protokolldienst.<Dokument>.<Fassung>``, eine Fassung lässt sich also nur einmal übergeben.

**Anmeldung zuerst:** Der Rückgabeschlüssel wird vor dem Inhalt der Anfrage geprüft; ohne gültigen Schlüssel kommt
401, nie 422.

**Wiederholung:** Die Quittung gilt für die Übergabe, wie der Dienst sie geschickt hat. Eine Wiederholung mit
demselben Schlüssel und Inhalt erhält sie, bevor die TOPs erneut zugeordnet werden; ein inzwischen umnummerierter TOP
ändert daran nichts. Anderer Inhalt unter demselben Schlüssel: 422.

Fehler nach RFC 9457 wie in allen Bereichen (``apps.common.api``).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Final, Literal, cast
from uuid import UUID

from django.core.cache import cache
from django.http import HttpRequest
from django.urls import reverse
from ninja import Field, Header, NinjaAPI, Router, Schema
from ninja.security.base import AuthBase
from pydantic import model_validator

from apps.common.api.idempotency import IDEMPOTENCY_HEADER, parse_idempotency_key
from apps.common.api.ninja import install_problem_handlers
from apps.common.api.problems import ProblemError
from apps.events.idempotency import IdempotencyConflictError, run_once
from apps.events.models import IdempotencyKey
from apps.session.models import SessionMeeting, SessionProtokolldienst, SessionTenant
from hub.commands import Command, CommandError, content_hash, dispatch
from hub.commands.dispatcher import KEY_REUSED

from . import einstellungen
from .vorschlaege import ACTOR, PARTIAL_COUNTS, PROPOSE

REALM: Final = "mandari Protokolldienst"
UNAUTHENTICATED: Final = "Rückgabeschlüssel des Mandanten erforderlich (Authorization: Bearer <schlüssel>)."
#: Anfragen je Mandant und Minute
RATENLIMIT: Final = 60

DESCRIPTION = """
Rückweg eines angebundenen Protokolldienstes: freigegebene Entwürfe der Niederschrift je TOP als Vorschlag, den die
Protokollführung in Session übernimmt oder verwirft. Anmeldung mit dem Rückgabeschlüssel des Mandanten
(Einstellungen › Datenschutz und Zugänge › Protokolldienst), nur für dessen Sitzungen.
"""

Ergebnis = Literal["adopted", "adopted_amended", "rejected", "noted", "postponed", "referred", "withdrawn"]


class Schluessel(AuthBase):
    """
    Bearer-Schema mit dem Rückgabeschlüssel des Mandanten aus dem Pfad (``_anbindung``). Läuft vor der Prüfung des
    Inhalts: Ohne gültigen Schlüssel kommt 401, nie 422. Ergebnis (``request.auth``): Mandant und Anbindung.
    """

    openapi_type = "http"
    openapi_scheme = "bearer"

    def __call__(self, request: HttpRequest) -> Any:
        treffer = getattr(request, "resolver_match", None)
        tenant_slug = str(treffer.kwargs.get("tenant_slug", "")) if treffer is not None else ""
        return _anbindung(request, tenant_slug)


class AbstimmungIn(Schema):
    yes: int | None = Field(None, ge=0, le=9999, description="Ja-Stimmen")
    no: int | None = Field(None, ge=0, le=9999, description="Nein-Stimmen")
    abstain: int | None = Field(None, ge=0, le=9999, description="Enthaltungen")
    unanimous: bool = Field(False, description="Einstimmig")
    outcome: Ergebnis | None = Field(None, description="Ergebnis")

    @model_validator(mode="after")
    def _summen_vollstaendig(self) -> AbstimmungIn:
        # Übernommen werden nur vollständige Summen; eine Teilsumme sähe in der Anzeige wie „Nein 0“ aus
        if len({wert is None for wert in (self.yes, self.no, self.abstain)}) > 1:
            raise ValueError(PARTIAL_COUNTS)
        return self


class TopIn(Schema):
    agenda_item: str | None = Field(
        None, max_length=500, description="OParl-Kennung des TOP (…/agendaitem/<uuid>/) oder seine Kennung in Session"
    )
    number: str | None = Field(None, max_length=30, description="TOP-Nummer, wenn der TOP keine Kennung hat")
    draft_text: str = Field("", max_length=100000, description="Entwurf des Protokolltexts")
    resolution_text: str = Field("", max_length=20000, description="Entwurf des Beschlusstexts")
    vote: AbstimmungIn | None = Field(None, description="Abstimmungsergebnis, nur Summen und Ergebnis")


class VorschlaegeIn(Schema):
    meeting: str = Field(..., max_length=500, description="OParl-Kennung der Sitzung (…/meeting/<uuid>/)")
    source_document: UUID = Field(..., description="Kennung des Dokuments beim Protokolldienst")
    revision: int = Field(..., ge=1, le=100000, description="Fassung des Dokuments")
    released_at: datetime | None = Field(None, description="Freigabe beim Protokolldienst")
    items: list[TopIn] = Field(..., min_length=1, max_length=300, description="Vorschläge je TOP")


class QuittungOut(Schema):
    meeting: str = Field(description="OParl-Kennung der Sitzung")
    reference: str = Field(description="Eingangsnummer der Übergabe")
    received_at: datetime
    content_hash: str = Field(description="SHA-256 über den Befehlsinhalt (RFC 8785)")
    accepted: int = Field(description="Angenommene Vorschläge")
    unassigned: list[int] = Field(description="Indizes der TOPs, die keinem TOP der Sitzung zuzuordnen waren")


class EinstiegOut(Schema):
    tenant: str
    name: str
    vorschlaege: str = Field(description="Adresse für die Vorschläge")


api = NinjaAPI(
    title="mandari Protokolldienst-Schnittstelle",
    version="1",
    description=DESCRIPTION.strip(),
    urls_namespace="protokolldienst_api",
)
install_problem_handlers(api, realm=REALM, unauthenticated=UNAUTHENTICATED)
router = Router(auth=Schluessel(), tags=["Protokolldienst"])


def _bearer(request: HttpRequest) -> str:
    kopf = str(request.META.get("HTTP_AUTHORIZATION", ""))
    return kopf[7:].strip() if kopf.startswith("Bearer ") else ""


def _anbindung(request: HttpRequest, tenant_slug: str) -> tuple[SessionTenant, SessionProtokolldienst]:
    """Mandant und Anbindung zum Schlüssel; unbekannt, ausgeschaltet oder falscher Schlüssel: 401, nie mehr."""
    tenant = SessionTenant.objects.filter(slug=tenant_slug, is_active=True).first() if tenant_slug else None
    konfiguration = einstellungen.aktiv(tenant) if tenant is not None else None
    if (
        tenant is None
        or konfiguration is None
        or not einstellungen.rueckgabe_schluessel_passt(konfiguration, _bearer(request))
    ):
        raise ProblemError(
            401,
            UNAUTHENTICATED,
            kind="nicht-authentifiziert",
            headers={"WWW-Authenticate": f'Bearer realm="{REALM}"'},
        )
    schluessel = f"protokolldienst:rate:{tenant.pk}"
    if not cache.add(schluessel, 1, timeout=60):
        try:
            anzahl = int(cache.incr(schluessel))
        except ValueError:
            anzahl = 1
        if anzahl > RATENLIMIT:
            raise ProblemError(
                429,
                "Zu viele Anfragen; bitte in einer Minute erneut.",
                kind="ratenlimit",
                headers={"Retry-After": "60"},
            )
    return tenant, konfiguration


def _angemeldet(request: HttpRequest) -> tuple[SessionTenant, SessionProtokolldienst]:
    """Mandant und Anbindung, die ``Schluessel`` vor dem Endpunkt geprüft hat."""
    return cast(tuple[SessionTenant, SessionProtokolldienst], cast(Any, request).auth)


@router.get("/{tenant_slug}/", response=EinstiegOut, url_name="einstieg", summary="Verbindung prüfen")
def einstieg(request: HttpRequest, tenant_slug: str) -> dict[str, Any]:
    tenant, _ = _angemeldet(request)
    adresse = reverse("protokolldienst_api:vorschlaege", kwargs={"tenant_slug": tenant.slug})
    return {"tenant": tenant.slug, "name": tenant.name, "vorschlaege": request.build_absolute_uri(adresse)}


def _sitzung(tenant: SessionTenant, kennung: str) -> SessionMeeting:
    pk = einstellungen.kennung_aufloesen(kennung, "meeting")
    meeting = SessionMeeting.objects.filter(pk=pk, tenant=tenant).first() if pk else None
    if meeting is None:
        raise ProblemError(404, "Die Sitzung ist nicht bekannt.", kind="nicht-gefunden")
    return meeting


def _zuordnen(meeting: SessionMeeting, tops: list[TopIn]) -> tuple[list[tuple[UUID, TopIn]], list[int]]:
    """
    TOPs über die Kennung, ohne Kennung über die Nummer; liefert (Kennung in Session, Eintrag) und nicht zugeordnete
    Indizes. Eine genannte Kennung, die zu keinem TOP passt, bleibt offen: Die Nummer könnte inzwischen einem anderen
    TOP gehören.
    """
    vorhanden = list(meeting.agenda_items.values_list("pk", "number"))
    nach_pk = {pk: pk for pk, _ in vorhanden}
    nach_nummer: dict[str, UUID] = {}
    for pk, nummer in vorhanden:
        nach_nummer.setdefault(str(nummer).strip(), pk)
    zugeordnet, offen = [], []
    for index, top in enumerate(tops):
        ziel: UUID | None = None
        if (top.agenda_item or "").strip():
            kennung = einstellungen.kennung_aufloesen(top.agenda_item or "", "agendaitem")
            ziel = nach_pk.get(kennung) if kennung else None
        elif (top.number or "").strip():
            ziel = nach_nummer.get((top.number or "").strip())
        if ziel is None:
            offen.append(index)
        else:
            zugeordnet.append((ziel, top))
    return zugeordnet, offen


_ZEIGER_TOP: Final = re.compile(r"^/items/(\d+)(?=/|$)")


def _command_problem(error: CommandError, indizes: list[int]) -> ProblemError:
    """Problem des Befehls; Stellen in ``items`` zeigen auf die Übergabe (``indizes``: Index im Befehl → Übergabe)."""
    problem = error.problem
    extensions: dict[str, Any] = {}
    if problem.errors:
        extensions["errors"] = [_zeiger_der_uebergabe(dict(eintrag), indizes) for eintrag in problem.errors]
    return ProblemError(problem.status, problem.detail, kind=problem.kind, **extensions)


def _zeiger_der_uebergabe(eintrag: dict[str, Any], indizes: list[int]) -> dict[str, Any]:
    treffer = _ZEIGER_TOP.match(str(eintrag.get("pointer", "")))
    if treffer is not None and int(treffer.group(1)) < len(indizes):
        eintrag["pointer"] = f"/items/{indizes[int(treffer.group(1))]}{eintrag['pointer'][treffer.end() :]}"
    return eintrag


def _eingangsbereich(tenant: SessionTenant) -> str:
    """Bereich der Quittungen je Übergabe im Idempotenzspeicher, getrennt von dem des Befehls (``hub.commands``)."""
    return f"session:{tenant.pk} {ACTOR} eingang"


def _gespeicherte_quittung(bereich: str, schluessel: str, eingang: str) -> dict[str, Any] | None:
    """Quittung einer früheren Übergabe unter demselben Schlüssel; anderer Inhalt: 422."""
    vorher = IdempotencyKey.objects.filter(scope=bereich, key=schluessel).only("request_hash", "response").first()
    if vorher is None:
        return None
    if vorher.request_hash != eingang:
        raise ProblemError(422, KEY_REUSED, kind="idempotenzschluessel-wiederverwendet")
    return dict(vorher.response)


@router.post(
    "/{tenant_slug}/niederschrift/vorschlaege/",
    response={201: QuittungOut},
    url_name="vorschlaege",
    summary="Entwürfe der Niederschrift vorschlagen",
)
def vorschlaege(
    request: HttpRequest,
    tenant_slug: str,
    payload: VorschlaegeIn,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias=IDEMPOTENCY_HEADER,
            description=(
                "Empfohlen: Wiederholungen mit demselben Schlüssel und Inhalt liefern dieselbe Quittung. Ohne Angabe "
                "gilt protokolldienst.<Dokument>.<Fassung>."
            ),
        ),
    ] = None,
) -> tuple[int, dict[str, Any]]:
    """
    Vorschläge je TOP für die Niederschrift einer Sitzung des Mandanten. Die Niederschrift ändert sich erst, wenn die
    Protokollführung einen Vorschlag übernimmt; eine neuere Fassung ersetzt offene Vorschläge desselben TOP, eine
    ältere ersetzt nichts und zählt nicht als angenommen.
    """
    tenant, _ = _angemeldet(request)
    schluessel = (
        parse_idempotency_key(idempotency_key) or f"protokolldienst.{payload.source_document}.{payload.revision}"
    )
    # Wiederholung vor der Zuordnung erkennen: Die TOPs der Sitzung können sich seitdem geändert haben
    bereich, eingang = _eingangsbereich(tenant), content_hash(payload.model_dump(mode="json"))
    gespeichert = _gespeicherte_quittung(bereich, schluessel, eingang)
    if gespeichert is not None:
        return 201, gespeichert
    meeting = _sitzung(tenant, payload.meeting)
    zugeordnet, offen = _zuordnen(meeting, payload.items)
    if not zugeordnet:
        raise ProblemError(422, "Keiner der TOPs gehört zu dieser Sitzung.", kind="validierung", unassigned=offen)
    eintraege = []
    for pk, top in zugeordnet:
        eintrag: dict[str, Any] = {"agenda_item": str(pk)}
        if top.draft_text.strip():
            eintrag["draft_text"] = top.draft_text
        if top.resolution_text.strip():
            eintrag["resolution_text"] = top.resolution_text
        if top.vote is not None:
            eintrag["vote"] = top.vote.model_dump(exclude_none=True)
        eintraege.append(eintrag)
    body: dict[str, Any] = {
        "meeting": str(meeting.pk),
        "source_document": str(payload.source_document),
        "revision": payload.revision,
        "items": eintraege,
    }
    if payload.released_at is not None:
        body["released_at"] = payload.released_at.isoformat()
    try:
        quittung = dispatch(
            Command(
                name=PROPOSE,
                body=body,
                idempotency_key=schluessel,
                tenant_ref=f"session:{tenant.pk}",
                actor_ref=ACTOR,
            )
        )
    except CommandError as exc:
        nicht_zugeordnet = set(offen)
        indizes = [index for index in range(len(payload.items)) if index not in nicht_zugeordnet]
        raise _command_problem(exc, indizes) from None
    antwort: dict[str, Any] = {
        "meeting": einstellungen.sitzung_kennung(meeting),
        "reference": quittung.reference,
        "received_at": quittung.received_at.isoformat(),
        "content_hash": quittung.content_hash,
        "accepted": int(quittung.data.get("vorschlaege", len(eintraege))),
        "unassigned": offen,
    }
    # Die Quittung gilt für diese Übergabe: Eine Wiederholung erhält sie, ohne erneut zuzuordnen
    try:
        gespeichert, _ = run_once(bereich, schluessel, eingang, lambda: antwort, label=PROPOSE)
    except IdempotencyConflictError:
        raise ProblemError(422, KEY_REUSED, kind="idempotenzschluessel-wiederverwendet") from None
    return 201, gespeichert


api.add_router("", router)
