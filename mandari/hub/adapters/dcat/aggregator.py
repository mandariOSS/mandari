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

**Lizenz:** die Angabe der Kommune (OParl ``Body.license``), übersetzt in die Lizenzliste von DCAT-AP.de
(``vokabular.lizenz``). Die Lizenz der Installation (``OPARL_LICENSE_URL``) gilt nur, wenn die Kommune keine
angibt (OParl 1.1: ``System.license`` gilt für Objekte ohne eigene Angabe) – nie anstelle einer unbekannten oder
einschränkenden Angabe der Kommune. Ohne eine zuordenbare offene Lizenz bietet der Katalog die Kommune nicht an:
Ihr eigener Katalog antwortet mit ``404`` und einem Hinweis, im Gesamtkatalog fehlt sie.

**Fremde Angaben** (``Body.website`` aus dem Ratsinformationssystem, Einstellungen) nimmt der Katalog nur, wenn sie
als IRI taugen (``hub.adapters.dcat.adressen``); eine fehlerhafte Angabe einer Kommune darf den Gesamtkatalog nicht
unlesbar machen.

**Session-Mandanten** dieser Installation geben ihren Katalog selbst heraus (Herausgeber ist die Kommune). Für
eine gelistete Kommune, die einen solchen Mandanten spiegelt, leitet der Katalog des Aggregators dorthin weiter,
und der Gesamtkatalog lässt sie aus – sonst stünden dieselben Daten unter zwei Herausgebern in den Portalen. Die
Weiterleitung ist vorübergehend (``302``, begrenzt zwischenspeicherbar): Die Spiegelung lässt sich ändern.

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
from datetime import datetime

from django.conf import settings
from django.db.models import Max, Min
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse

from hub.adapters.dcat import adressen, http, katalog, vokabular
from hub.adapters.dcat.katalog import Angebot, Dienst, Katalog, Kennzahlen, Kontakt, Stelle
from hub.api import changes
from hub.api.http import endpoint, error_response
from hub.ris.mapping.bestand import BestandUris
from insight_core import publication
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson

#: Wie lange ein Abnehmer die Weiterleitung auf den Katalog eines Session-Mandanten zwischenspeichern darf
WEITERLEITUNG_MAX_AGE = 3600

#: Hinweis, wenn eine Kommune keine zuordenbare offene Lizenz hat
OHNE_LIZENZ = (
    "Für diese Kommune ist keine offene Lizenz aus der Lizenzliste von DCAT-AP.de bekannt. Ohne Lizenz gibt es "
    "keinen Katalog; die Lizenz nennt die Kommune in ihrer OParl-Schnittstelle (Body.license)."
)


# =============================================================================
# Adressen und Stellen
# =============================================================================


def katalog_adresse() -> str:
    """Adresse des Gesamtkatalogs (ohne Endung)."""
    return f"{adressen.site()}{reverse('dcat:catalog')}"


def kommunen_adresse(pk: uuid.UUID | str) -> str:
    """Adresse des Katalogs einer Kommune (ohne Endung); Basis der Kennungen ihrer Datensätze."""
    return f"{adressen.site()}{reverse('dcat:body_catalog', kwargs={'pk': pk})}"


def betreiber() -> Stelle:
    """Der Betreiber der Installation: Herausgeber aller Datensätze des Aggregators."""
    return Stelle(
        uri=f"{katalog_adresse()}#herausgeber",
        name=str(getattr(settings, "DCAT_PUBLISHER_NAME", "") or "mandari"),
        homepage=adressen.webadresse(getattr(settings, "DCAT_PUBLISHER_URL", "")) or adressen.site(),
    )


def kontakt() -> Kontakt:
    """Kontaktstelle des Betreibers: Funktionsadresse (``DCAT_CONTACT_EMAIL``) und Webseite."""
    stelle = betreiber()
    return Kontakt(
        uri=f"{katalog_adresse()}#kontakt",
        name=stelle.name,
        email=adressen.email(str(getattr(settings, "DCAT_CONTACT_EMAIL", "") or "")),
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
    """
    Lizenz der Kommune; die der Installation (``OPARL_LICENSE_URL``) nur, wenn die Kommune keine angibt.

    Eine unbekannte oder einschränkende Angabe der Kommune (etwa CC BY-NC) ergibt ``None``: Der Katalog darf ihre
    Daten nicht unter der offenen Lizenz der Installation anbieten.
    """
    if (body.license or "").strip():
        return vokabular.lizenz(body.license)
    return vokabular.lizenz(getattr(settings, "OPARL_LICENSE_URL", ""))


def _webseite(body: OParlBody) -> str:
    if body.slug:
        return f"{adressen.site()}{reverse('insight_core:insight:portal_entry', kwargs={'slug': body.slug})}"
    return f"{adressen.site()}{reverse('insight_core:insight:set_body', kwargs={'body_id': body.id})}"


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
    kalender = f"{adressen.site()}{reverse('insight_core:insight:calendar_feed')}?kommune={body.id}"
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
        urheber=Stelle(uri=f"{basis}#kommune", name=body.name, homepage=adressen.webadresse(body.website)),
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
        katalog.SITZUNGEN: Kennzahlen(katalog.zeitraum(sitzungen["beginn"], sitzungen["ende"]), sitzungen["geaendert"]),
        katalog.VORLAGEN: Kennzahlen(katalog.zeitraum(vorlagen["beginn"], vorlagen["ende"]), vorlagen["geaendert"]),
        katalog.GREMIEN: Kennzahlen(katalog.zeitraum(gremien["beginn"]), gremien_geaendert),
    }


def session_mandant(body: OParlBody) -> str | None:
    """
    Slug des Session-Mandanten dieser Installation, dessen OParl-Schnittstelle die Kommune spiegelt – sonst
    ``None``. Ein solcher Mandant gibt seinen Katalog selbst heraus (``apps/session/api/dcat.py``); der Aggregator
    verweist darauf, statt dieselben Daten unter einem zweiten Herausgeber anzubieten.
    """
    config = body.source.sync_config if isinstance(body.source.sync_config, dict) else {}
    slug = config.get("session_tenant")
    return slug if isinstance(slug, str) and slug else None


def _weiterleitung(slug: str, endung: str | None) -> HttpResponse:
    """
    Weiterleitung auf den Katalog des Session-Mandanten (in derselben Form).

    Vorübergehend (``302``) und nur begrenzt zwischenspeicherbar: Die Zuordnung hängt an der Spiegel-Konfiguration
    der Quelle und lässt sich ändern. Eine dauerhafte Weiterleitung dürften Abnehmer unbegrenzt behalten.
    """
    if endung is None:
        pfad = reverse("session:dcat_catalog", kwargs={"tenant_slug": slug})
    else:
        pfad = reverse("session:dcat_catalog_format", kwargs={"tenant_slug": slug, "endung": endung})
    response = HttpResponseRedirect(f"{adressen.site()}{pfad}")
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = f"max-age={WEITERLEITUNG_MAX_AGE}"
    return response


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
    for body in OParlBody.objects.listed().select_related("source").order_by("name", "id"):
        state = states.get(str(body.id))
        if (state is not None and state.withdrawn) or session_mandant(body):
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
    body = OParlBody.objects.select_related("source").filter(pk=pk).first()
    # Nur eine gelistete Kommune leitet weiter; eine nicht gelistete oder gelöschte verrät keinen Mandanten
    gelistet = body is not None and not body.deleted and body.is_listed
    slug = session_mandant(body) if body is not None and gelistet else None
    if slug is not None:
        return _weiterleitung(slug, endung)
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
    if body is None or body.deleted or not body.is_listed:
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
