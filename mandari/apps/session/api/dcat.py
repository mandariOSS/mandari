# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalog nach DCAT-AP.de je Session-Mandant (Issue #104, ``docs/DCAT_KATALOG.md``).

``/session/<slug>/api/dcat/catalog`` (Inhaltsaushandlung) und ``…/catalog.ttl``, ``.rdf``, ``.jsonld``: dieselben drei
Datensätze wie im Katalog des Aggregators – Sitzungen und Tagesordnungen, Vorlagen und Beschlüsse, Gremien und
Mandate –, hier mit der OParl-Schnittstelle des Mandanten als Zugang. Modell, Serialisierung und HTTP-Hülle liegen
in der Drehscheibe (``hub.adapters.dcat``); dieses Modul legt nur fest, was der Mandant anbietet, denn nur Session
weiß, was eines Mandanten öffentlich ist.

- **Herausgeber und Kontakt** ist die Kommune selbst: Name, Webseite und Kontakt-E-Mail des Mandanten – dieselben
  Angaben, die seine OParl-Schnittstelle am System und am Body nennt. Keine Personen.
- **Lizenz** aus den Einstellungen des Mandanten (Karte „OParl-Schnittstelle“). Ohne Lizenz kein Katalog, sondern
  ``404`` mit Hinweis.
- **Kennung bei GovData** (``dcatde:contributorID``) aus ``SessionTenant.dcat_contributor_id``.
- **Zeitraum und letzte Änderung** nur aus öffentlichen Objekten (``oparl_publication``).
- Abrufbar wie die OParl-Schnittstelle nur nach der Freischaltung (Issue #319); das Ende der Veröffentlichung im
  Bürgerportal berührt den Katalog nicht. Die Webseite im Bürgerportal nennt er nur, solange der Mandant dort
  veröffentlicht.
"""

from __future__ import annotations

from dataclasses import replace

from django.db.models import Max, Min
from django.http import HttpRequest, HttpResponse
from django.urls import reverse

from apps.session import oparl_publication as pub
from apps.session.models import SessionTenant
from apps.session.services.insight_service import oparl_system_url
from hub.adapters.dcat import adressen, http, katalog, vokabular
from hub.adapters.dcat.katalog import Angebot, Dienst, Katalog, Kennzahlen, Kontakt, Stelle
from hub.api import changes
from hub.api.http import endpoint, error_response
from hub.ris.mapping.session import SessionUris

#: Hinweis, wenn der Mandant keine zuordenbare offene Lizenz festgelegt hat
OHNE_LIZENZ = (
    "Für diesen Mandanten ist keine Lizenz der offenen Daten festgelegt. Ohne Lizenz gibt es keinen Katalog; die "
    "Verwaltung wählt sie in den Einstellungen (Karte „OParl-Schnittstelle“)."
)


def _mandant(tenant_slug: str) -> SessionTenant | None:
    """Aktiv und mit freigeschalteter OParl-Schnittstelle – sonst nach außen wie ein unbekannter Mandant."""
    tenant: SessionTenant | None = SessionTenant.objects.filter(
        slug=tenant_slug, is_active=True, oparl_public_since__isnull=False
    ).first()
    return tenant


def katalog_adresse(tenant: SessionTenant) -> str:
    """Adresse des Katalogs (ohne Endung); Basis der Kennungen seiner Datensätze."""
    return f"{adressen.site()}{reverse('session:dcat_catalog', kwargs={'tenant_slug': tenant.slug})}"


def angebot(tenant: SessionTenant) -> Angebot | None:
    """Was der Mandant anbietet – ohne Kennzahlen; ``None`` ohne zuordenbare offene Lizenz."""
    lizenz = vokabular.lizenz(tenant.oparl_license)
    if lizenz is None:
        return None
    basis = katalog_adresse(tenant)
    uris = SessionUris(oparl_system_url(tenant))
    stelle = Stelle(uri=f"{basis}#herausgeber", name=tenant.name, homepage=adressen.webadresse(tenant.website))
    email = adressen.email(tenant.contact_email)
    kontakt = (
        Kontakt(uri=f"{basis}#kontakt", name=tenant.name, email=email, url=stelle.homepage)
        if email or stelle.homepage
        else None
    )
    feed = changes.enabled()
    webseite = (
        f"{adressen.site()}{reverse('insight_core:insight:portal_entry', kwargs={'slug': tenant.slug})}"
        if tenant.insight_publish
        else stelle.homepage
    )
    return Angebot(
        basis=basis,
        kommune=tenant.name,
        herausgeber=stelle,
        lizenz=lizenz,
        listen={
            katalog.LISTE_SITZUNGEN: uris.list("meetings"),
            katalog.LISTE_VORLAGEN: uris.list("papers"),
            katalog.LISTE_GREMIEN: uris.list("organizations"),
            katalog.LISTE_PERSONEN: uris.list("people"),
        },
        dienst=f"{basis}#oparl",
        kontakt=kontakt,
        raum=vokabular.raumbezug(tenant.ags),
        veroeffentlicht=tenant.oparl_public_since,
        webseite=webseite,
        feed=uris.changes() if feed else None,
        snapshot=uris.snapshot() if feed else None,
        bereitsteller=tenant.dcat_contributor_id or None,
    )


def kennzahlen(tenant: SessionTenant) -> dict[str, Kennzahlen]:
    """Zeitraum und letzte Änderung je Datensatz – nur aus dem, was die OParl-Schnittstelle ausliefert."""
    sitzungen = pub.visible_meetings(tenant).aggregate(
        beginn=Min("start"), ende=Max("start"), geaendert=Max("updated_at")
    )
    vorlagen = pub.visible_papers(tenant).aggregate(beginn=Min("date"), ende=Max("date"), geaendert=Max("updated_at"))
    gremien = pub.visible_organizations(tenant).aggregate(beginn=Min("start_date"), geaendert=Max("updated_at"))
    personen = pub.visible_persons(tenant).aggregate(geaendert=Max("updated_at"))
    gremien_geaendert = max((w for w in (gremien["geaendert"], personen["geaendert"]) if w), default=None)
    return {
        katalog.SITZUNGEN: Kennzahlen(katalog.zeitraum(sitzungen["beginn"], sitzungen["ende"]), sitzungen["geaendert"]),
        katalog.VORLAGEN: Kennzahlen(katalog.zeitraum(vorlagen["beginn"], vorlagen["ende"]), vorlagen["geaendert"]),
        katalog.GREMIEN: Kennzahlen(katalog.zeitraum(gremien["beginn"]), gremien_geaendert),
    }


def mandantenkatalog(tenant: SessionTenant, eintrag: Angebot) -> Katalog:
    """Der Katalog des Mandanten mit seinen drei Datensätzen und seiner OParl-Schnittstelle als Dienst."""
    datensaetze = katalog.datensaetze(replace(eintrag, kennzahlen=kennzahlen(tenant)))
    dienst = Dienst(
        uri=eintrag.dienst,
        titel=f"OParl-Schnittstelle – {tenant.name}",
        beschreibung=(
            f"Lesende, anonyme Schnittstelle nach OParl 1.1 über die öffentlichen Ratsinformationen von {tenant.name}."
        ),
        endpunkt=oparl_system_url(tenant),
        herausgeber=eintrag.herausgeber,
        kontakt=eintrag.kontakt,
        datensaetze=tuple(d.uri for d in datensaetze),
    )
    return Katalog(
        uri=eintrag.basis,
        titel=f"Offene Ratsinformationen – {tenant.name}",
        beschreibung=(
            f"Katalog der offenen Ratsinformationen von {tenant.name}: Sitzungen und Tagesordnungen, Vorlagen und "
            "Beschlüsse, Gremien und Mandate, bereitgestellt über die OParl-Schnittstelle des Sitzungsdienstes."
        ),
        herausgeber=eintrag.herausgeber,
        datensaetze=datensaetze,
        dienste=(dienst,),
        homepage=eintrag.webseite,
        lizenz=eintrag.lizenz,
        raum=eintrag.raum,
        veroeffentlicht=eintrag.veroeffentlicht,
    )


@endpoint
def catalog_view(request: HttpRequest, tenant_slug: str, endung: str | None = None) -> HttpResponse:
    """Katalog des Mandanten."""
    if not http.enabled():
        return http.ausgeschaltet()
    tenant = _mandant(tenant_slug)
    if tenant is None:
        return error_response(404, "Mandant nicht gefunden.")
    eintrag = angebot(tenant)
    if eintrag is None:
        return http.problem_antwort(request, 404, "keine-lizenz", OHNE_LIZENZ)
    return http.katalog_antwort(
        request,
        adresse=eintrag.basis,
        endung=endung,
        schluessel=f"session:{tenant.pk}",
        bauen=lambda: mandantenkatalog(tenant, eintrag),
    )
