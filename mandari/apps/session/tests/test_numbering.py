# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nummernkreise (Issue #150): Vorlagen- und Drucksachennummern werden klar, eindeutig und
unveränderlich vergeben – nach Hamburger Bezirks-Praxis (22-0593, 22-0593.1) ebenso wie nach
NRW-Mustern (V/0599/2026, AN/1492/2026).
"""

from __future__ import annotations

import importlib
from datetime import date
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.test import Client
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionApplication,
    SessionLegislativeTerm,
    SessionNumberCounter,
    SessionNumberRange,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import numbering_service
from apps.session.services.numbering_service import NumberingError

pytestmark = pytest.mark.django_db
JAHR = timezone.localdate().year


def _tenant(slug: str = "nord") -> SessionTenant:
    return SessionTenant.objects.create(name=f"Bezirk {slug}", slug=slug)


def _paper(tenant: SessionTenant, **kwargs: Any) -> SessionPaper:
    kwargs.setdefault("name", "Vorlage")
    return SessionPaper.objects.create(tenant=tenant, **kwargs)


def _hamburg(tenant: SessionTenant, wp: int | None = 22) -> None:
    SessionLegislativeTerm.objects.create(tenant=tenant, name="22. Wahlperiode", number=wp, start_date=date(2024, 6, 9))
    numbering_service.apply_preset(tenant, "hamburg_bezirk")


class TestVergabe:
    def test_neuer_mandant_hat_standardkreis_und_zaehlt_fortlaufend(self) -> None:
        tenant = _tenant()
        assert tenant.number_ranges.filter(is_active=True).count() == 1
        eins, zwei = _paper(tenant), _paper(tenant)
        assert (eins.reference, zwei.reference) == (f"V/{JAHR}/0001", f"V/{JAHR}/0002")
        assert eins.reference_assigned_at is not None

    def test_mandanten_zaehlen_unabhaengig(self) -> None:
        a, b = _tenant("altona"), _tenant("eimsbuettel")
        _hamburg(a)
        _hamburg(b)
        assert [_paper(a).reference for _ in range(2)] == ["22-0001", "22-0002"]
        assert _paper(b).reference == "22-0001"

    def test_hamburg_unternummern(self) -> None:
        tenant = _tenant()
        _hamburg(tenant)
        antrag = _paper(tenant, paper_type="motion")
        empfehlung = _paper(tenant, parent_paper=antrag, relation_type="recommendation", paper_type="recommendation")
        antwort = _paper(tenant, parent_paper=antrag, relation_type="answer", paper_type="answer")
        assert (antrag.reference, empfehlung.reference, antwort.reference) == ("22-0001", "22-0001.1", "22-0001.2")
        # Unternummern verbrauchen keine Hauptnummer
        assert _paper(tenant).reference == "22-0002"
        assert tenant.reference_label == "Drucksache"

    def test_ohne_nummer_der_wahlperiode_klarer_fehler(self) -> None:
        tenant = _tenant()
        _hamburg(tenant, wp=None)
        with pytest.raises(NumberingError, match="Wahlperiode"):
            _paper(tenant)
        assert not SessionPaper.objects.filter(tenant=tenant).exists()
        # Kein verbrannter Zähler: Transaktion wurde komplett zurückgerollt
        assert not SessionNumberCounter.objects.filter(number_range__tenant=tenant, value__gt=0).exists()

    def test_vergabe_bei_freigabe(self) -> None:
        tenant = _tenant()
        tenant.number_ranges.update(assign_on="release")
        entwurf = _paper(tenant)
        assert entwurf.reference == "" and entwurf.display_reference == "Nummer folgt"
        _paper(tenant)  # zweiter Entwurf ohne Nummer: kein Konflikt mit der Eindeutigkeit
        entwurf.status = "approved"
        entwurf.save()
        entwurf.refresh_from_db()
        assert entwurf.reference == f"V/{JAHR}/0001"

    def test_vergebene_nummer_bleibt(self) -> None:
        tenant = _tenant()
        paper = _paper(tenant)
        numbering_service.apply_preset(tenant, "gemeinde")
        paper.name = "Geändert"
        paper.save()
        paper.refresh_from_db()
        assert paper.reference == f"V/{JAHR}/0001"

    def test_altbestand_wird_uebersprungen(self) -> None:
        tenant = _tenant()
        _paper(tenant, reference=f"V/{JAHR}/0001")  # von Hand übernommen
        assert _paper(tenant).reference == f"V/{JAHR}/0002"

    def test_verwaltung_und_politik_getrennt(self) -> None:
        tenant = _tenant()
        numbering_service.apply_preset(tenant, "nrw_verwaltung_politik")
        assert _paper(tenant, paper_type="proposal").reference == f"0001/{JAHR}"
        assert _paper(tenant, paper_type="motion").reference == f"AN/0001/{JAHR}"
        assert _paper(tenant, paper_type="inquiry").reference == f"AN/0002/{JAHR}"

    def test_praefix_je_art_mit_fortschreibung(self) -> None:
        tenant = _tenant()
        numbering_service.apply_preset(tenant, "nrw_praefix_je_art")
        vorlage = _paper(tenant, paper_type="proposal")
        erg = _paper(tenant, parent_paper=vorlage, relation_type="supplement")
        assert (vorlage.reference, erg.reference) == (f"V/0001/{JAHR}", f"V/0001/{JAHR}/1")

    def test_preset_erneut_behaelt_zaehler(self) -> None:
        tenant = _tenant()
        _hamburg(tenant)
        _paper(tenant)
        numbering_service.apply_preset(tenant, "standard")
        numbering_service.apply_preset(tenant, "hamburg_bezirk")
        assert _paper(tenant).reference == "22-0002"
        assert SessionNumberRange.objects.filter(tenant=tenant, is_active=True).count() == 1


class TestStartwertUndMuster:
    def test_startwert_nur_aufwaerts(self) -> None:
        tenant = _tenant()
        _hamburg(tenant)
        rng = tenant.number_ranges.get(is_active=True)
        numbering_service.set_next_number(rng, 2615)
        assert numbering_service.preview(rng) == "22-2615"
        assert _paper(tenant).reference == "22-2615"
        with pytest.raises(NumberingError, match="nur höher"):
            numbering_service.set_next_number(rng, 100)

    @pytest.mark.parametrize(
        ("muster", "reset", "ok"),
        [
            ("{wp}-{lfd:4}", "term", True),
            ("V/{lfd:4}/{jahr}", "yearly", True),
            ("{lfd:4}", "yearly", False),  # Jahr fehlt → gleiche Nummern jedes Jahr
            ("{jahr}/{lfd}", "term", False),  # Wahlperiode fehlt
            ("V/{jahr}", "yearly", False),  # keine laufende Nummer
            ("{wp}-{nummer}", "term", False),  # unbekannter Platzhalter
        ],
    )
    def test_muster_pruefung(self, muster: str, reset: str, ok: bool) -> None:
        assert (numbering_service.validate_pattern(muster, reset) == []) is ok

    def test_bestand_aus_migration(self) -> None:
        """Die Migration setzt die Zähler auf die höchste V/<Jahr>/nnnn-Nummer – nahtloses Weiterzählen."""
        tenant = _tenant()
        tenant.number_ranges.all().delete()
        SessionPaper.objects.bulk_create(
            [
                SessionPaper(tenant=tenant, reference=f"V/{JAHR}/0009", name="Neun"),
                SessionPaper(tenant=tenant, reference=f"V/{JAHR - 1}/0042", name="Vorjahr"),
                SessionPaper(tenant=tenant, reference=f"V/{JAHR}/sonder", name="Freitext"),
            ]
        )
        migration = importlib.import_module("apps.session.migrations.0026_nummernkreise")
        migration.bestand_uebernehmen(django_apps, None)
        assert _paper(tenant).reference == f"V/{JAHR}/0010"


def _client(tenant: SessionTenant, *, admin: bool = False, **perms: bool) -> Client:
    role = SessionRole.objects.create(tenant=tenant, name=f"r{SessionRole.objects.count()}", is_admin=admin, **perms)
    user = cast(Any, UserFactory)()
    su = SessionUser.objects.create(user=user, tenant=tenant)
    su.roles.add(role)
    client = Client()
    client.force_login(user)
    return client


class TestOberflaeche:
    def test_anlegen_ohne_nummernfeld_vergibt_automatisch(self) -> None:
        tenant = _tenant()
        _hamburg(tenant)
        client = _client(tenant, can_view_papers=True, can_create_papers=True)
        antwort = client.get(f"/session/{tenant.slug}/papers/create/")
        assert antwort.status_code == 200 and 'name="reference"' not in antwort.content.decode()
        antwort = client.post(
            f"/session/{tenant.slug}/papers/create/", {"name": "Radweg", "paper_type": "motion", "is_public": "on"}
        )
        assert antwort.status_code == 302
        assert SessionPaper.objects.get(tenant=tenant, name="Radweg").reference == "22-0001"

    def test_doppelte_handnummer_ist_formularfehler_statt_500(self) -> None:
        tenant = _tenant()
        _paper(tenant, reference="ALT/1")
        client = _client(tenant, admin=True)
        antwort = client.post(
            f"/session/{tenant.slug}/papers/create/",
            {"reference": "ALT/1", "name": "Doppelt", "paper_type": "proposal", "is_public": "on"},
        )
        assert antwort.status_code == 200
        assert "bereits vergeben" in antwort.content.decode()

    def test_unternummer_ueber_detailseite(self) -> None:
        tenant = _tenant()
        _hamburg(tenant)
        antrag = _paper(tenant, paper_type="motion")
        client = _client(tenant, can_view_papers=True, can_create_papers=True, can_edit_papers=True)
        seite = client.get(f"/session/{tenant.slug}/papers/{antrag.id}/").content.decode()
        assert "Drucksache 22-0001" in seite and "Neue Unternummer" in seite
        antwort = client.post(f"/session/{tenant.slug}/papers/{antrag.id}/unternummer/", {"relation_type": "answer"})
        assert antwort.status_code == 302
        kind = SessionPaper.objects.get(parent_paper=antrag)
        assert (kind.reference, kind.paper_type) == ("22-0001.1", "answer")

    def test_antrag_umwandeln_nutzt_nummernkreis(self) -> None:
        tenant = _tenant()
        numbering_service.apply_preset(tenant, "nrw_verwaltung_politik")
        antrag = SessionApplication.objects.create(
            tenant=tenant, title="Mehr Bänke", application_type="motion", submitter_name="Fraktion"
        )
        client = _client(tenant, can_process_applications=True, can_create_papers=True, can_view_papers=True)
        antwort = client.post(f"/session/{tenant.slug}/applications/{antrag.id}/convert/", {})
        assert antwort.status_code == 302
        assert SessionPaper.objects.get(source_application=antrag).reference == f"AN/0001/{JAHR}"

    def test_einstellungen_preset_und_startwert(self) -> None:
        tenant = _tenant()
        SessionLegislativeTerm.objects.create(tenant=tenant, name="WP", number=22, start_date=date(2024, 6, 9))
        client = _client(tenant, admin=True)
        url = f"/session/{tenant.slug}/settings/numbering/"
        assert client.get(url).status_code == 200
        client.post(url + "save/", {"action": "preset", "preset": "hamburg_bezirk"})
        rng = tenant.number_ranges.get(is_active=True)
        client.post(url + "save/", {"action": "next", "range_id": str(rng.id), "next_number": "2615"})
        seite = client.get(url).content.decode()
        assert "22-2615" in seite
        # Muster eines Kreises mit vergebenen Nummern bleibt
        _paper(tenant)
        client.post(
            url + "save/",
            {"action": "range", "range_id": str(rng.id), "name": "X", "pattern": "{lfd}/{jahr}", "reset": "yearly"},
        )
        rng.refresh_from_db()
        assert rng.pattern == "{wp}-{lfd:4}"
