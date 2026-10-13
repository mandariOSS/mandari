# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Endgeräte (Issue #944).

Geprüft: Die Seite „Verwaltung › Endgeräte“ hat mit eingeschaltetem Schalter eine eigene neue Vorlage, ohne Schalter
bleibt die bisherige. Keine Funktion entfällt und keine kommt hinzu: Jede Aktion der bisherigen Seite (Gerät aufnehmen,
Ausgeben mit Personenauswahl, Übergabeprotokoll, Zurücknehmen, Defekt, Wieder einsatzbereit, Ausmustern mit Rückfrage,
Zuschuss erfassen, genehmigen, auszahlen, stornieren mit Rückfrage, CSV) steht mit denselben Feldern, Optionen und
Rückfragen auf der neuen und umgekehrt – für alle sechs Standardrollen (nur mit ``manage_devices`` erreichbar), mit
Geräten in jedem Stand, Zuschüssen in jedem Stand und leerem Bestand. Der Bestand steht als Zahl im Satz statt in
Zählerkacheln, der Stand als Punkt mit Text.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.session.models import (
    SessionDevice,
    SessionDeviceGrant,
    SessionDeviceLog,
    SessionPerson,
    SessionTenant,
)
from apps.session.tests.gleichstand import gezeigt, reiter_ausnahmen, rollen, vergleichen
from apps.session.tests.test_neues_design import _client, _mandant, _vorlagen

pytestmark = pytest.mark.django_db

BISHER = "session/devices/index.html"
NEU = "session/neu/endgeraete.html"
DATEIEN = ("endgeraete.html", "_endgeraet.html", "_endgeraet_zuschuss.html")


def _adresse(tenant: SessionTenant, name: str, **kw: Any) -> str:
    return reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, **kw})


def _welt_endgeraete(tenant: SessionTenant) -> dict[str, Any]:
    """
    Vier Geräte, je eines im Bestand, ausgegeben (mit Übergabe), defekt und ausgemustert, mit Verlauf; drei aktive
    Personen und eine inaktive (nicht auswählbar); vier Zuschüsse, je einer ausstehend, genehmigt, ausgezahlt und
    storniert. ``erwartet``: die Aktionen, die nur dieser Bestand zeigt.
    """
    personen = [
        SessionPerson.objects.create(tenant=tenant, given_name=v, family_name=n, start_date=date(2025, 1, 1))
        for v, n in (("Anna", "Amberg"), ("Bernd", "Birk"), ("Clara", "Conrad"))
    ]
    SessionPerson.objects.create(tenant=tenant, given_name="Ina", family_name="Inaktiv", is_active=False)
    geraete = {
        status: SessionDevice.objects.create(
            tenant=tenant,
            label=f"iPad {nummer}",
            serial_number=f"SN-{nummer}" if nummer % 2 else "",
            inventory_number=f"INV-{nummer}",
            accessories="Hülle, Stift" if nummer == 1 else "",
            status=status,
            issued_to=personen[0] if status == "issued" else None,
            issued_at=timezone.now() if status == "issued" else None,
        )
        for nummer, status in enumerate(("in_stock", "issued", "defect", "retired"), start=1)
    }
    for geraet in geraete.values():
        SessionDeviceLog.objects.create(device=geraet, action="created")
    SessionDeviceLog.objects.create(device=geraete["issued"], action="issued", person=personen[0], note="mit Hülle")
    for person, status in zip(personen + personen[:1], ("pending", "approved", "paid", "cancelled"), strict=True):
        SessionDeviceGrant.objects.create(
            tenant=tenant, person=person, amount=Decimal("400.00"), status=status, note=f"Ratsbeschluss {status}"
        )
    zuschuesse = {g.status: g for g in SessionDeviceGrant.objects.filter(tenant=tenant)}

    def aktion(geraet: str, name: str) -> tuple[str, str]:
        return "POST", _adresse(tenant, "device_action", device_id=geraete[geraet].pk, action=name)

    def zuschuss(status: str, name: str) -> tuple[str, str]:
        return "POST", _adresse(tenant, "device_grant_action", grant_id=zuschuesse[status].pk, action=name)

    return {
        "seite": _adresse(tenant, "devices"),
        "erwartet": {
            ("POST", _adresse(tenant, "device_save")),
            aktion("in_stock", "issue"),
            aktion("in_stock", "defect"),
            aktion("in_stock", "retire"),
            aktion("issued", "return"),
            aktion("issued", "defect"),
            aktion("defect", "repair"),
            aktion("defect", "retire"),
            ("GET", _adresse(tenant, "device_handover_pdf", device_id=geraete["issued"].pk)),
            ("POST", _adresse(tenant, "device_grant_save")),
            ("POST", _adresse(tenant, "device_grants_csv")),
            zuschuss("pending", "approve"),
            zuschuss("pending", "cancel"),
            zuschuss("approved", "pay"),
            zuschuss("approved", "cancel"),
        },
        "geraete": geraete,
    }


def test_seite_waehlt_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("geraete2b", neu=False)
    welt = _welt_endgeraete(tenant)
    client = _client(tenant, "Administrator")
    alt = client.get(welt["seite"])
    assert alt.status_code == 200
    assert BISHER in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt) and NEU not in _vorlagen(alt)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    neu = client.get(welt["seite"])
    assert neu.status_code == 200
    assert {NEU, "session/base_session_neu.html"} <= _vorlagen(neu) and BISHER not in _vorlagen(neu)
    assert 'data-gestaltung="neu"' in neu.content.decode()


def test_gleichstand_fuer_alle_rollen() -> None:
    """
    Alle Aktionen mit Feldern, Optionen (Personen, auch der leere Eintrag) und Rückfragen in beide Richtungen; ohne
    ``manage_devices`` antworten beide Seiten gleich (403). Ausnahme nur die Reiter des Rahmens.
    """
    tenant = _mandant("geraetegleich2b", neu=False)
    welt = _welt_endgeraete(tenant)
    fehlt, gesehen = vergleichen(tenant, {"endgeraete": welt["seite"]}, ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    assert welt["erwartet"] <= gezeigt(gesehen), sorted(welt["erwartet"] - gezeigt(gesehen))
    # Rückfragen wörtlich wie bisher
    merkmale = {merkmal for werte in gesehen.values() for merkmal in werte}
    assert {"data-confirm=Gerät wirklich ausmustern?", "data-confirm=Zuschuss wirklich stornieren?"} <= merkmale
    # Nur wer Endgeräte verwalten darf, erreicht die Seite
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    for rolle in rollen():
        status = _client(tenant, rolle).get(welt["seite"]).status_code
        assert status == (200 if rolle == "Administrator" else 403), rolle


def test_gleichstand_bei_leerem_bestand() -> None:
    tenant = _mandant("geraeteleer2b", neu=False)
    seite = _adresse(tenant, "devices")
    fehlt, _ = vergleichen(
        tenant, {"endgeraete": seite}, rollen_liste=["Administrator"], ausnahmen=reiter_ausnahmen(tenant)
    )
    assert not fehlt, "\n".join(fehlt)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    html = _client(tenant, "Administrator").get(seite).content.decode()
    assert "Noch keine Geräte erfasst." in html and "Noch keine Zuschüsse." in html


def test_bestand_als_satz_und_stand_als_punkt() -> None:
    tenant = _mandant("geraetesatz2b", neu=True)
    welt = _welt_endgeraete(tenant)
    html = _client(tenant, "Administrator").get(welt["seite"]).content.decode()
    # Eine Zählung: alle erfassten Geräte im Satz und an der Liste, „im Bestand“ nur als Teilmenge wie der Stand je Gerät
    satz = "4 Geräte erfasst, davon 1 im Bestand und 1 ausgegeben; Zuschüsse gesamt 1200.00 €."
    assert satz in html.replace("1200,00", "1200.00")
    assert "Gerätebestand" not in html
    assert "kpi" not in html and "text-[10px]" not in html
    inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
    for text in (
        "Im Bestand",
        "Ausgegeben",
        "Defekt",
        "Ausgemustert",
        "Ausstehend",
        "Genehmigt",
        "Ausgezahlt",
        "Storniert",
    ):
        assert f'aria-hidden="true"></span>{text}</span>' in inhalt, text
    # Inaktive Personen stehen nicht zur Auswahl
    assert "Inaktiv" not in inhalt
    # Ausgemustert: nur der Verlauf, keine Aktion
    ausgemustert = welt["geraete"]["retired"]
    for name in ("issue", "return", "defect", "repair", "retire"):
        assert _adresse(tenant, "device_action", device_id=ausgemustert.pk, action=name) not in inhalt, name


def test_keine_gefuellten_knoepfe_und_nicht_in_12_px() -> None:
    tenant = _mandant("geraeteknoepfe2b", neu=True)
    welt = _welt_endgeraete(tenant)
    html = _client(tenant, "Administrator").get(welt["seite"]).content.decode()
    inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
    assert "bg-primary-600 hover:bg-primary-700" not in inhalt
    for farbe in ("green", "blue", "indigo", "amber", "teal", "red", "purple", "emerald"):
        # Keine Füllknöpfe und keine Farbchips (der Stand ist ein Punkt mit Text)
        assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in inhalt, farbe
        assert f"bg-{farbe}-100 text-{farbe}-700" not in inhalt, farbe
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for verboten in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]", "c-ui.card", "c-ui.badge",
                         "kpi-tile", "rounded-xl", "onclick="):  # fmt: skip
            assert verboten not in text, f"{datei}: {verboten}"
