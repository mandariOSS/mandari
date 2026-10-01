# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DCAT-AP.de-Katalog des Aggregators: die offenen Ratsinformationen der gelisteten Kommunen.

Adressen (unter ``/data/dcat/``, Formen nach ``hub.adapters.dcat.http``):

- ``catalog`` – alle gelisteten Kommunen mit Lizenz in einem Katalog: ein Einstieg für ein Datenportal
- ``body/<uuid>/catalog`` – der Katalog einer Kommune (Kennung wie in der OParl-Schnittstelle)

**Wer ist wer:** Herausgeber (``dct:publisher``) und Kontakt ist der Betreiber der Installation – er macht
die Daten über seine Schnittstelle zugänglich (``DCAT_PUBLISHER_NAME``, ``DCAT_PUBLISHER_URL``,
``DCAT_CONTACT_EMAIL``). Urheber (``dct:creator``) ist die Kommune, aus deren Ratsinformationssystem die
Daten stammen; ihr Name ist auch der Text der Namensnennung.

**Lizenz:** die Angabe der Kommune (OParl ``Body.license``), ersatzweise die der Installation
(``OPARL_LICENSE_URL``), übersetzt in die Lizenzliste von DCAT-AP.de (``vokabular.lizenz``). Ohne eine
zuordenbare offene Lizenz bietet der Katalog die Kommune nicht an: Ihr eigener Katalog antwortet mit
``404`` und einem Hinweis, im Gesamtkatalog fehlt sie.

**Veröffentlichungsstand** wie bei Feed und Snapshot (``insight_core.publication``): nicht gelistet ``404``,
vorübergehend abgeschaltet ``503`` mit ``Retry-After``, dauerhaft zurückgenommen ``410``. Im Gesamtkatalog
bleibt eine vorübergehend abgeschaltete Kommune stehen – nicht erreichbar ist nicht gelöscht; ein Portal,
das sie dort verlöre, würde ihre Datensätze löschen. Eine als Archiv behaltene Kommune erscheint mit der
Aktualisierungsfrequenz „nie“.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from django.conf import settings
from django.db.models import Max, Min
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils import timezone

from hub.adapters.dcat import http, katalog, vokabular
from hub.adapters.dcat.katalog import Angebot, Dienst, Katalog, Kennzahlen, Kontakt, Stelle, Zeitraum
from hub.api import changes
from hub.api.http import endpoint, error_response
from hub.ris.mapping.bestand import BestandUris
from insight_core import publication
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson

#: Hinweis, wenn eine Kommune keine zuordenbare offene Lizenz hat
OHNE_LIZENZ = (
    "Für diese Kommune ist keine offene Lizenz aus der Lizenzliste von DCAT-AP.de bekannt. Ohne Lizenz gibt es "
    "keinen Katalog; die Lizenz nennt die Kommune in ihrer OParl-Schnittstelle (Body.license)."
)


# =============================================================================
# Adressen und Stellen
# =============================================================================


def _site() -> str:
    return str(settings.SITE_URL).rstrip("/")


def _web(adresse: str | None) -> str | None:
    """Nur vollständige Webadressen (eine Angabe ohne Schema wäre im Katalog keine gültige IRI)."""
    text = (adresse or "").strip()
    return text if text.lower().startswith(("https://", "http://")) else None


def katalog_adresse() -> str:
    """Adresse des Gesamtkatalogs (ohne Endung)."""
    return f"{_site()}{reverse('dcat:catalog')}"


def kommunen_adresse(pk: uuid.UUID | str) -> str:
    """Adresse des Katalogs einer Kommune (ohne Endung); Basis der Kennungen ihrer Datensätze."""
    return f"{_site()}{reverse('dcat:body_catalog', kwargs={'pk': pk})}"


def betreiber() -> Stelle:
    """Der Betreiber der Installation: Herausgeber aller Datensätze des Aggregators."""
    return Stelle(
        uri=f"{katalog_adresse()}#herausgeber",
        name=str(getattr(settings, "DCAT_PUBLISHER_NAME", "") or "mandari"),
        homepage=_web(getattr(settings, "DCAT_PUBLISHER_URL", "")) or _site(),
    )


def kontakt() -> Kontakt:
    """Kontaktstelle des Betreibers: Funktionsadresse (``DCAT_CONTACT_EMAIL``) und Webseite."""
    stelle = betreiber()
    email = str(getattr(settings, "DCAT_CONTACT_EMAIL", "") or "").strip()
    return Kontakt(
        uri=f"{katalog_adresse()}#kontakt",
        name=stelle.name,
        email=email if "@" in email else None,
        url=stelle.homepage,
    )


def _bereitsteller() -> str | None:
    """Kennung des Betreibers bei GovData (``DCAT_CONTRIBUTOR_ID``), wenn vergeben."""
    wert = str(getattr(settings, "DCAT_CONTRIBUTOR_ID", "") or "").strip()
    return wert if wert.startswith("http://dcat-ap.de/def/contributors/") else None


def _dienst(datensaetze: tuple[katalog.Datensatz, ...]) -> Dienst:
    uris = BestandUris(settings.OPARL_BASE_URL, settings.SITE_URL)
    stelle = betreiber()
    return Dienst(
        uri=f"{katalog_adresse()}#oparl",
        titel=f"OParl-Schnittstelle ({stelle.name})",
        beschreibung=(
            "Lesende, anonyme Schnittstelle nach OParl 1.1 über die Ratsinformationen aller angebundenen "
            "Kommunen, mit Änderungsfeed und Snapshot je Kommune, wenn eingeschaltet."
        ),
        endpunkt=uris.system(),
        herausgeber=stelle,
        kontakt=kontakt(),
        datensaetze=tuple(d.uri for d in datensaetze),
    )


# =============================================================================
# Angebot einer Kommune
# =============================================================================


def _lizenz(body: OParlBody) -> vokabular.Lizenz | None:
    return vokabular.lizenz(body.license) or vokabular.lizenz(getattr(settings, "OPARL_LICENSE_URL", ""))


def _webseite(body: OParlBody) -> str:
    if body.slug:
        return f"{_site()}{reverse('insight_core:insight:portal_entry', kwargs={'slug': body.slug})}"
    return f"{_site()}{reverse('insight_core:insight:set_body', kwargs={'body_id': body.id})}"


def angebot(body: OParlBody, state: publication.BodyState | None = None) -> Angebot | None:
    """
    Was der Aggregator von einer Kommune anbietet – ohne Kennzahlen (``kennzahlen``); ``None`` ohne
    zuordenbare offene Lizenz.
    """
    lizenz = _lizenz(body)
    if lizenz is None:
        return None
    uris = BestandUris(settings.OPARL_BASE_URL, settings.SITE_URL)
    basis = kommunen_adresse(body.id)
    feed = changes.enabled() and body.is_listed
    kalender = f"{_site()}{reverse('insight_core:insight:calendar_feed')}?kommune={body.id}"
    return Angebot(
        basis=basis,
        kommune=body.name,
        herausgeber=betreiber(),
        lizenz=lizenz,
        listen={
            katalog.LISTE_SITZUNGEN: uris.list(body.id, "meetings"),
            katalog.LISTE_VORLAGEN: uris.list(body.id, "papers"),
            katalog.LISTE_GREMIEN: uris.list(body.id, "organizations"),
            katalog.LISTE_PERSONEN: uris.list(body.id, "people"),
        },
        dienst=f"{katalog_adresse()}#oparl",
        urheber=Stelle(uri=f"{basis}#kommune", name=body.name, homepage=_web(body.website)),
        kontakt=kontakt(),
        raum=vokabular.raumbezug(body.ags, body.rgs),
        veroeffentlicht=body.created_at,
        webseite=_webseite(body),
        feed=uris.changes(body.id) if feed else None,
        snapshot=uris.snapshot(body.id) if feed else None,
        kalender=kalender,
        frequenz=vokabular.FREQUENZ_KEINE if state is not None and state.archived else vokabular.FREQUENZ_LAUFEND,
        bereitsteller=_bereitsteller(),
    )


def _tag(wert: datetime | date | None) -> date | None:
    if isinstance(wert, datetime):
        return timezone.localtime(wert).date() if timezone.is_aware(wert) else wert.date()
    return wert


def _zeitraum(werte: dict[str, Any]) -> Zeitraum | None:
    beginn, ende = _tag(werte.get("beginn")), _tag(werte.get("ende"))
    return Zeitraum(beginn, ende) if beginn or ende else None


def kennzahlen(body_id: uuid.UUID) -> dict[str, Kennzahlen]:
    """
    Zeitraum und letzte Änderung je Datensatz aus dem Bestand der Kommune (vier Abfragen, nur bei einem
    Fehltreffer im Cache). Letzte Änderung wie ``modified`` der OParl-Ausgabe: der Zeitstempel der Quelle,
    ersatzweise der eigene.
    """
    geaendert = Max(Coalesce("oparl_modified", "updated_at"))
    sitzungen = OParlMeeting.objects.filter(body_id=body_id, deleted=False).aggregate(
        beginn=Min("start"), ende=Max("start"), geaendert=geaendert
    )
    vorlagen = OParlPaper.objects.filter(body_id=body_id, deleted=False).aggregate(
        beginn=Min("date"), ende=Max("date"), geaendert=geaendert
    )
    gremien = OParlOrganization.objects.filter(body_id=body_id, deleted=False).aggregate(
        beginn=Min("start_date"), geaendert=geaendert
    )
    personen = OParlPerson.objects.filter(body_id=body_id, deleted=False).aggregate(geaendert=geaendert)
    gremien_geaendert = max((w for w in (gremien["geaendert"], personen["geaendert"]) if w), default=None)
    return {
        katalog.SITZUNGEN: Kennzahlen(_zeitraum(sitzungen), sitzungen["geaendert"]),
        katalog.VORLAGEN: Kennzahlen(_zeitraum(vorlagen), vorlagen["geaendert"]),
        katalog.GREMIEN: Kennzahlen(_zeitraum(gremien), gremien_geaendert),
    }


# =============================================================================
# Kataloge
# =============================================================================


def kommunenkatalog(body: OParlBody, angebot: Angebot) -> Katalog:
    """Der Katalog einer Kommune mit ihren drei Datensätzen."""
    datensaetze = katalog.datensaetze(replace(angebot, kennzahlen=kennzahlen(body.id)))
    return Katalog(
        uri=angebot.basis,
        titel=f"Offene Ratsinformationen – {angebot.kommune}",
        beschreibung=(
            f"Katalog der offenen Ratsinformationen von {angebot.kommune}: Sitzungen und Tagesordnungen, "
            "Vorlagen und Beschlüsse, Gremien und Mandate. Die Daten stammen aus dem Ratsinformationssystem der "
            f"Kommune; {angebot.herausgeber.name} gleicht sie fortlaufend ab und stellt sie über die "
            "OParl-Schnittstelle bereit."
        ),
        herausgeber=angebot.herausgeber,
        datensaetze=datensaetze,
        dienste=(_dienst(datensaetze),),
        homepage=angebot.webseite,
        lizenz=angebot.lizenz,
        raum=angebot.raum,
        veroeffentlicht=angebot.veroeffentlicht,
    )


def gesamtkatalog() -> Katalog:
    """Alle gelisteten Kommunen mit zuordenbarer Lizenz in einem Katalog (dieselben Datensätze und Kennungen)."""
    states = publication.states()
    datensaetze: list[katalog.Datensatz] = []
    seit: list[datetime] = []
    for body in OParlBody.objects.listed().order_by("name", "id"):
        state = states.get(str(body.id))
        if state is not None and state.withdrawn:
            continue
        eintrag = angebot(body, state)
        if eintrag is None:
            continue
        datensaetze.extend(katalog.datensaetze(replace(eintrag, kennzahlen=kennzahlen(body.id))))
        seit.append(body.created_at)
    stelle = betreiber()
    return Katalog(
        uri=katalog_adresse(),
        titel=f"Offene Ratsinformationen ({stelle.name})",
        beschreibung=(
            f"Katalog der offenen Ratsinformationen aller Kommunen, die {stelle.name} veröffentlicht: je Kommune "
            "Sitzungen und Tagesordnungen, Vorlagen und Beschlüsse, Gremien und Mandate, mit der "
            "OParl-Schnittstelle als Zugang."
        ),
        herausgeber=stelle,
        datensaetze=tuple(datensaetze),
        dienste=(_dienst(tuple(datensaetze)),),
        homepage=stelle.homepage,
        veroeffentlicht=min(seit, default=None),
    )


# =============================================================================
# Endpunkte
# =============================================================================


def _staende_kennung() -> str:
    """Kurzform der Veröffentlichungsstände aller Kommunen: Ein neuer Stand ergibt einen neuen Cache-Schlüssel."""
    staende = sorted((body_id, state.mode) for body_id, state in publication.states().items())
    return hashlib.sha256(repr(staende).encode()).hexdigest()[:16] if staende else "-"


@endpoint
def catalog_view(request: HttpRequest, endung: str | None = None) -> HttpResponse:
    """Gesamtkatalog aller gelisteten Kommunen."""
    if not http.enabled():
        return http.ausgeschaltet()
    return http.katalog_antwort(
        request,
        adresse=katalog_adresse(),
        endung=endung,
        schluessel=f"alle:{_staende_kennung()}",
        bauen=gesamtkatalog,
    )


@endpoint
def body_catalog_view(request: HttpRequest, pk: uuid.UUID, endung: str | None = None) -> HttpResponse:
    """Katalog einer Kommune."""
    if not http.enabled():
        return http.ausgeschaltet()
    state = publication.body_state(pk)
    if state is not None and state.paused:
        response = error_response(503, "Die Kommune hat die Veröffentlichung vorübergehend abgeschaltet.")
        response["Retry-After"] = str(publication.RETRY_AFTER_SECONDS)
        response["Cache-Control"] = "no-store"
        return response
    if state is not None and state.withdrawn:
        return http.problem_antwort(
            request, 410, "kommune-zurueckgenommen", "Die Kommune hat die Veröffentlichung dauerhaft zurückgenommen."
        )
    body = OParlBody.objects.listed().filter(pk=pk).first()
    if body is None:
        return error_response(404, "Kommune (Body) nicht gefunden.")
    eintrag = angebot(body, state)
    if eintrag is None:
        return http.problem_antwort(request, 404, "keine-lizenz", OHNE_LIZENZ)
    return http.katalog_antwort(
        request,
        adresse=eintrag.basis,
        endung=endung,
        schluessel=f"body:{pk}:{state.mode if state else '-'}",
        bauen=lambda: kommunenkatalog(body, eintrag),
    )
