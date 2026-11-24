# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Organisationen nach der Frist löschen (Issue #1575).

Eine gesperrte Demo löscht der Zeitplan ``demo_ablauf`` ``DEMO_LOESCHFRIST_TAGE`` nach ihrem Ende endgültig: Organisation,
Inhalte, Dateien, Datenexporte und Konten ohne weitere Bindung. Verlängerte (wieder aktive), gebuchte (Bereitstellung)
und gewöhnliche Organisationen bleiben. Der Hinweis „Demozeit abgelaufen“ nennt Löschdatum und Verlängerung.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests import factories
from apps.tenants import demo
from apps.tenants.models import Membership, Organization
from apps.work.faction.models import FactionMeeting
from apps.work.tasks.models import Task, TaskAttachment

pytestmark = pytest.mark.django_db

#: Fabriken sind untypisiert (factory_boy)
fabrik: Any = factories

TAG = timedelta(days=1)
FRIST = timedelta(days=30)


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _medien(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    settings.DEMO_LOESCHFRIST_TAGE = 30
    return tmp_path


def _demo(slug: str, *, vor: timedelta, aktiv: bool = False) -> Organization:
    """Demo, deren Ende ``vor`` zurückliegt (gesperrt, solange nicht ``aktiv``)."""
    org: Organization = fabrik.OrganizationFactory(name=f"Demo {slug}", slug=slug)
    org.demo_laufzeit_tage = 14
    org.demo_endet_am = timezone.now() - vor
    org.is_active = aktiv
    org.save(update_fields=["demo_laufzeit_tage", "demo_endet_am", "is_active"])
    return org


def _lauf(*args: str) -> str:
    ausgabe = StringIO()
    call_command("demo_ablauf", *args, stdout=ausgabe)
    return ausgabe.getvalue()


# ---------------------------------------------------------------------------
# Fällige Demo wird gelöscht
# ---------------------------------------------------------------------------


def test_faellige_demo_wird_mit_inhalten_dateien_und_konten_geloescht(
    make_member: Any, _medien: Path, django_capture_on_commit_callbacks: Any
) -> None:
    org = _demo("demo-alt", vor=FRIST + TAG)
    admin = make_member(org, ["tasks.view"], email="admin@demo.example", is_admin=True)
    mitglied = make_member(org, ["tasks.view"], email="nur-demo@demo.example")
    # Zweite Mitgliedschaft in einer anderen Organisation hält das Konto
    anderswo = make_member(org, ["tasks.view"], email="anderswo@demo.example")
    fabrik.MembershipFactory(user=anderswo.user, organization=fabrik.OrganizationFactory())
    mitarbeiter = make_member(org, ["tasks.view"], email="team@demo.example")
    User.objects.filter(pk=mitarbeiter.user_id).update(is_staff=True)

    org.logo = SimpleUploadedFile("logo.png", b"\x89PNG")
    org.save(update_fields=["logo"])
    aufgabe = Task.objects.create(organization=org, title="Aufgabe", created_by=admin)
    anhang = TaskAttachment.objects.create(
        task=aufgabe,
        file=SimpleUploadedFile("plan.txt", b"Plan"),
        filename="plan.txt",
        mime_type="text/plain",
        uploaded_by=admin,
    )
    # Fraktionssitzung: Ihre Sitzungsart hängt per RESTRICT an der Organisation und muss mit gelöscht werden
    FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now() - 40 * TAG, status="planned", created_by=admin
    )
    export = _medien / "exports" / str(org.pk) / str(mitglied.pk)
    export.mkdir(parents=True)
    (export / "export.json").write_text("{}")
    dateien = [_medien / str(anhang.file.name), _medien / str(org.logo.name)]
    assert all(datei.exists() for datei in dateien)

    with django_capture_on_commit_callbacks(execute=True):
        ausgabe = _lauf()

    assert "gelöscht: demo-alt" in ausgabe
    assert "1 Demo(s) gelöscht" in ausgabe
    assert not Organization.objects.filter(pk=org.pk).exists()
    assert not Membership.objects.filter(organization_id=org.pk).exists()
    assert not Task.objects.filter(pk=aufgabe.pk).exists()
    assert not FactionMeeting.objects.filter(organization_id=org.pk).exists()
    assert not any(datei.exists() for datei in dateien)
    assert not (_medien / "exports" / str(org.pk)).exists()
    # Konten: nur noch in der Demo gebunden → weg; weitere Mitgliedschaft oder Mitarbeiterkonto → bleibt
    assert not User.objects.filter(email__in=["admin@demo.example", "nur-demo@demo.example"]).exists()
    assert User.objects.filter(email="anderswo@demo.example").exists()
    assert User.objects.filter(email="team@demo.example").exists()
    connection.check_constraints()


def test_ergebnis_nennt_umfang_ohne_personendaten(make_member: Any) -> None:
    org = _demo("demo-umfang", vor=FRIST + TAG)
    make_member(org, ["tasks.view"], email="a@demo.example")

    lauf = demo.faellige_loeschen()

    assert [g.slug for g in lauf.geloescht] == ["demo-umfang"]
    ergebnis = lauf.geloescht[0]
    assert ergebnis.eintraege > 0
    assert ergebnis.konten_geloescht == 1
    assert ergebnis.konten_behalten == 0
    assert lauf.fehlgeschlagen == []


# ---------------------------------------------------------------------------
# Was bleibt
# ---------------------------------------------------------------------------


def test_vor_ablauf_der_frist_bleibt_die_demo() -> None:
    org = _demo("demo-frist", vor=FRIST - TAG)

    _lauf()

    assert Organization.objects.filter(pk=org.pk).exists()


def test_verlaengerte_aktive_demo_bleibt() -> None:
    """Im Admin verlängert: Datum verschoben und wieder aktiv. Auch ein vergessenes Datum löscht nicht, solange aktiv."""
    org = _demo("demo-aktiv", vor=FRIST + TAG, aktiv=True)

    assert demo.faellige_loeschen().geloescht == []
    assert Organization.objects.filter(pk=org.pk).exists()


def test_gebuchte_demo_der_bereitstellung_bleibt() -> None:
    org = _demo("demo-gebucht", vor=FRIST + TAG)
    org.provisioning_ref = "BS-00042"
    org.save(update_fields=["provisioning_ref"])

    _lauf()

    assert Organization.objects.filter(pk=org.pk).exists()


def test_organisation_ohne_demo_laufzeit_bleibt() -> None:
    """Laufzeit im Admin geleert (keine Demo mehr), auch wenn noch ein altes Ende gespeichert ist."""
    org = _demo("keine-demo", vor=FRIST + 10 * TAG)
    org.demo_laufzeit_tage = None
    org.save(update_fields=["demo_laufzeit_tage"])
    gesperrt: Organization = fabrik.OrganizationFactory(slug="gesperrt-ohne-demo", is_active=False)

    _lauf()

    assert Organization.objects.filter(pk__in=[org.pk, gesperrt.pk]).count() == 2


def test_verlaengerung_zwischen_auswahl_und_loeschung_gewinnt() -> None:
    """Die Löschung prüft unter Zeilensperre erneut: Wer gerade verlängert hat, wird nicht gelöscht."""
    org = _demo("demo-knapp", vor=FRIST + TAG)
    Organization.objects.filter(pk=org.pk).update(is_active=True)

    assert demo.demo_loeschen(org.pk) is None
    assert Organization.objects.filter(pk=org.pk).exists()


# ---------------------------------------------------------------------------
# Lauf: Probelauf, Grenze je Lauf, Fehler
# ---------------------------------------------------------------------------


def test_probelauf_nennt_faellige_loeschungen_und_aendert_nichts() -> None:
    org = _demo("demo-probe", vor=FRIST + TAG)

    ausgabe = _lauf("--dry-run")

    assert "würde löschen: demo-probe" in ausgabe
    assert "1 Demo(s) zum Löschen fällig (Probelauf)" in ausgabe
    assert Organization.objects.filter(pk=org.pk).exists()


def test_hoechstens_je_lauf_aelteste_zuerst() -> None:
    alt = _demo("demo-a", vor=FRIST + 5 * TAG)
    mittel = _demo("demo-b", vor=FRIST + 3 * TAG)
    neu = _demo("demo-c", vor=FRIST + TAG)

    lauf = demo.faellige_loeschen(hoechstens=2)

    assert [g.slug for g in lauf.geloescht] == ["demo-a", "demo-b"]
    assert not Organization.objects.filter(pk__in=[alt.pk, mittel.pk]).exists()
    assert Organization.objects.filter(pk=neu.pk).exists()


def test_gescheiterte_loeschung_haelt_die_uebrigen_nicht_auf(monkeypatch: pytest.MonkeyPatch) -> None:
    kaputt = _demo("demo-kaputt", vor=FRIST + 2 * TAG)
    heil = _demo("demo-heil", vor=FRIST + TAG)
    original = demo.demo_loeschen

    def loeschen(organization_id: Any, jetzt: Any = None) -> Any:
        if organization_id == kaputt.pk:
            raise RuntimeError("Speicher nicht erreichbar")
        return original(organization_id, jetzt)

    monkeypatch.setattr(demo, "demo_loeschen", loeschen)

    with pytest.raises(CommandError, match="demo-kaputt"):
        _lauf()

    assert Organization.objects.filter(pk=kaputt.pk).exists()
    assert not Organization.objects.filter(pk=heil.pk).exists()


def test_sperren_und_loeschen_im_selben_lauf(make_member: Any) -> None:
    """Eine gerade abgelaufene Demo wird gesperrt, eine alte gelöscht."""
    frisch = _demo("demo-frisch", vor=TAG, aktiv=True)
    alt = _demo("demo-uralt", vor=FRIST + TAG)

    ausgabe = _lauf()

    frisch.refresh_from_db()
    assert frisch.is_active is False
    assert "gesperrt: demo-frisch" in ausgabe
    assert not Organization.objects.filter(pk=alt.pk).exists()


# ---------------------------------------------------------------------------
# Hinweis „Demozeit abgelaufen“
# ---------------------------------------------------------------------------


def test_hinweis_nennt_loeschdatum_und_kostenpflichtige_verlaengerung(settings: Any) -> None:
    settings.DEMO_KONTAKT_EMAIL = "hallo@example.org"
    settings.DEMO_VERLAENGERN_URL = ""
    org = _demo("demo-hinweis", vor=TAG)
    assert org.demo_endet_am is not None
    loeschung = timezone.localtime(org.demo_endet_am + FRIST).strftime("%d.%m.%Y")

    text = demo.hinweis(org)

    assert f"bis zum {loeschung} erhalten" in text
    assert "danach löschen wir die Organisation mit allen Daten" in text
    assert "kostenpflichtige Verlängerung an hallo@example.org" in text


def test_hinweis_mit_buchungslink(settings: Any) -> None:
    settings.DEMO_KONTAKT_EMAIL = "hallo@example.org"
    settings.DEMO_VERLAENGERN_URL = "https://kunden.example.org/buchen/"
    org = _demo("demo-link", vor=TAG)

    text = demo.hinweis(org)

    assert "unter https://kunden.example.org/buchen/ oder schreibt uns an hallo@example.org" in text


def test_ohne_eigene_adresse_bleibt_der_absender_der_plattform(settings: Any) -> None:
    settings.DEMO_KONTAKT_EMAIL = ""
    settings.DEMO_VERLAENGERN_URL = ""
    settings.DEFAULT_FROM_EMAIL = "plattform@example.org"
    org = _demo("demo-absender", vor=TAG)

    assert demo.kontakt() != ""
    assert demo.kontakt() in demo.hinweis(org)


def test_bereitstellung_hat_kein_loeschdatum() -> None:
    org = _demo("demo-bs", vor=TAG)
    org.provisioning_ref = "BS-1"

    assert demo.loeschung_am(org) is None
    assert "Eure Daten bleiben erhalten." in demo.hinweis(org)


def test_hinweisseite_zeigt_knopf_zur_buchung(settings: Any, make_member: Any) -> None:
    settings.DEMO_VERLAENGERN_URL = "https://kunden.example.org/buchen/"
    org = _demo("demo-seite", vor=TAG)
    mitglied = make_member(org, ["dashboard.view"], email="seite@demo.example")
    client = Client()
    client.force_login(mitglied.user)

    antwort = client.get(reverse("accounts:demo_abgelaufen"))

    inhalt = antwort.content.decode()
    assert antwort.status_code == 200
    assert 'href="https://kunden.example.org/buchen/"' in inhalt
    assert "Verlängerung buchen" in inhalt


# ---------------------------------------------------------------------------
# Ohne Löschfrist, Probelauf, nicht entfernte Dateien (Prüfung von PR #1576)
# ---------------------------------------------------------------------------


def test_ohne_loeschfrist_bleibt_alles_wie_bisher(settings: Any) -> None:
    """Ohne DEMO_LOESCHFRIST_TAGE löscht der Lauf nichts, und der Hinweis sagt wie bisher, dass die Daten bleiben."""
    settings.DEMO_LOESCHFRIST_TAGE = None
    org = _demo("demo-ohne-frist", vor=FRIST + 100 * TAG)

    ausgabe = _lauf()

    assert "0 Demo(s) gelöscht" in ausgabe
    assert Organization.objects.filter(pk=org.pk).exists()
    assert demo.loeschung_am(org) is None
    assert "Eure Daten bleiben erhalten." in demo.hinweis(org)


@pytest.mark.parametrize("wert", [0, -30, "abc"])
def test_ungueltige_loeschfrist_loescht_nichts(settings: Any, wert: Any) -> None:
    settings.DEMO_LOESCHFRIST_TAGE = wert
    org = _demo("demo-ungueltig", vor=TAG)

    _lauf()

    assert demo.loeschfrist() is None
    assert Organization.objects.filter(pk=org.pk).exists()


def test_probelauf_nennt_auch_demos_die_erst_gesperrt_und_dann_geloescht_wuerden() -> None:
    org = _demo("demo-noch-aktiv", vor=FRIST + TAG, aktiv=True)

    ausgabe = _lauf("--dry-run")

    assert "würde sperren: demo-noch-aktiv" in ausgabe
    assert "würde löschen: demo-noch-aktiv" in ausgabe
    org.refresh_from_db()
    assert org.is_active is True


def test_nicht_entfernte_dateien_werden_gemeldet(django_capture_on_commit_callbacks: Any) -> None:
    class Speicher:
        def exists(self, name: str) -> bool:
            return True

        def delete(self, name: str) -> None:
            raise PermissionError(name)

    nicht_entfernt: list[str] = []
    with django_capture_on_commit_callbacks(execute=True):
        demo._nach_commit_entfernen([(Speicher(), "tasks/attachments/2026/11/a.txt")], nicht_entfernt)

    assert nicht_entfernt == ["tasks/attachments/2026/11/a.txt"]


def test_nicht_entfernte_dateien_lassen_den_lauf_scheitern(monkeypatch: pytest.MonkeyPatch) -> None:
    _demo("demo-reste", vor=FRIST + TAG)

    def loeschen(organization_id: Any, jetzt: Any = None) -> Any:
        return demo.GeloeschteDemo(
            slug="demo-reste",
            demo_endet_am=None,
            eintraege=3,
            dateien=2,
            konten_geloescht=0,
            konten_behalten=0,
            dateien_nicht_entfernt=1,
        )

    monkeypatch.setattr(demo, "demo_loeschen", loeschen)

    with pytest.raises(CommandError, match="demo-reste .1 Dateien nicht entfernt."):
        _lauf()
