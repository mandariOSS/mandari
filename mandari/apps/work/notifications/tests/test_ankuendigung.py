# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ankündigungen in Work: Befehl, Hinweisband auf Start, Glocke, gelesen je Person, Zurückziehen (Issue #857)."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, RoleFactory, UserFactory
from apps.work.notifications import ankuendigung
from apps.work.notifications.models import Notification, NotificationPreference, NotificationType
from apps.work.notifications.services import NotificationHub

TITEL = "Großes Update"
TEXT = "Neues Design und bessere Recherche."
LINK = "https://docs.mandari.de/work/was-ist-neu/"


def ankuendigen(*argumente: str, schluessel: str = "work-update-test") -> str:
    ausgabe = StringIO()
    call_command(
        "work_ankuendigung",
        "--schluessel",
        schluessel,
        "--titel",
        TITEL,
        "--text",
        TEXT,
        "--link",
        LINK,
        "--linktext",
        "Was ist neu",
        *argumente,
        stdout=ausgabe,
    )
    return ausgabe.getvalue()


def ankuendigungen(**filter: Any) -> Any:
    return Notification.objects.filter(notification_type=NotificationType.ANNOUNCEMENT, **filter)


def start(client: Any, org: Any) -> Any:
    return client.get(reverse("work:dashboard", kwargs={"org_slug": org.slug}))


@pytest.fixture
def zwei_orgs(org: Any, make_member: Any) -> dict[str, Any]:
    """Zwei Organisationen; eine Person in beiden, dazu je ein weiteres Mitglied, ein Gast und Inaktive."""
    andere = OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")
    beide = make_member(org, ["dashboard.view"], email="beide@example.org")
    zweite = MembershipFactory(
        user=beide.user, organization=andere, roles=[RoleFactory(organization=andere, permissions=["dashboard.view"])]
    )
    nur_b = make_member(andere, ["dashboard.view"], email="b@example.org")
    nur_a = make_member(org, ["dashboard.view"], email="a@example.org")
    gast = MembershipFactory(user=UserFactory(email="gast@example.org"), organization=org, is_guest=True)
    inaktiv = make_member(org, ["dashboard.view"], email="inaktiv@example.org")
    inaktiv.is_active = False
    inaktiv.save(update_fields=["is_active"])
    konto_aus = make_member(org, ["dashboard.view"], email="konto-aus@example.org")
    konto_aus.user.is_active = False
    konto_aus.user.save(update_fields=["is_active"])
    return {
        "a": org,
        "b": andere,
        "beide_a": beide,
        "beide_b": zweite,
        "nur_a": nur_a,
        "nur_b": nur_b,
        "gast": gast,
        "inaktiv": inaktiv,
        "konto_aus": konto_aus,
    }


@pytest.mark.django_db
class TestBefehl:
    def test_alle_aktiven_mitglieder_ohne_gaeste(self, zwei_orgs: dict[str, Any]) -> None:
        ausgabe = ankuendigen()

        empfaenger = set(ankuendigungen().values_list("recipient_id", flat=True))
        assert empfaenger == {
            zwei_orgs["beide_a"].id,
            zwei_orgs["beide_b"].id,
            zwei_orgs["nur_a"].id,
            zwei_orgs["nur_b"].id,
        }
        assert zwei_orgs["gast"].id not in empfaenger
        assert zwei_orgs["inaktiv"].id not in empfaenger
        assert zwei_orgs["konto_aus"].id not in empfaenger
        assert "Angelegt: 4 neu, 0 schon vorhanden" in ausgabe

        benachrichtigung = ankuendigungen(recipient=zwei_orgs["nur_a"]).get()
        assert (benachrichtigung.title, benachrichtigung.message, benachrichtigung.link) == (TITEL, TEXT, LINK)
        assert benachrichtigung.metadata == {"ankuendigung": "work-update-test", "linktext": "Was ist neu"}
        assert benachrichtigung.is_read is False

    def test_zweiter_aufruf_legt_nichts_doppelt_an(self, zwei_orgs: dict[str, Any]) -> None:
        ankuendigen()
        anzahl = ankuendigungen().count()

        ausgabe = ankuendigen()

        assert ankuendigungen().count() == anzahl
        assert "Angelegt: 0 neu, 4 schon vorhanden" in ausgabe

    def test_neues_mitglied_bekommt_sie_beim_naechsten_aufruf(
        self, zwei_orgs: dict[str, Any], make_member: Any
    ) -> None:
        ankuendigen()
        neu = make_member(zwei_orgs["a"], ["dashboard.view"], email="neu@example.org")

        ankuendigen()

        assert ankuendigungen(recipient=neu).count() == 1
        assert ankuendigungen().count() == 5

    def test_nur_eine_organisation(self, zwei_orgs: dict[str, Any]) -> None:
        ausgabe = ankuendigen("--organisation", zwei_orgs["b"].slug)

        assert set(ankuendigungen().values_list("recipient__organization_id", flat=True)) == {zwei_orgs["b"].id}
        assert f"{zwei_orgs['b'].slug}: 2 neu" in ausgabe
        assert zwei_orgs["a"].slug not in ausgabe

    def test_gaeste_nur_auf_wunsch(self, zwei_orgs: dict[str, Any]) -> None:
        ankuendigen("--mit-gaesten")

        assert ankuendigungen(recipient=zwei_orgs["gast"]).count() == 1

    def test_probelauf_aendert_nichts(self, zwei_orgs: dict[str, Any]) -> None:
        ausgabe = ankuendigen("--probelauf")

        assert not ankuendigungen().exists()
        assert "Würde anlegen: 4 neu" in ausgabe

    def test_unbekannte_organisation_bricht_ab(self, zwei_orgs: dict[str, Any]) -> None:
        with pytest.raises(CommandError, match="Unbekannte Organisation: gibt-es-nicht"):
            ankuendigen("--organisation", zwei_orgs["a"].slug, "--organisation", "gibt-es-nicht")

        assert not ankuendigungen().exists()

    @pytest.mark.parametrize(
        "link",
        ["javascript:alert(1)", "http://docs.mandari.de/", "//fremd.example/", "https://", "/pfad mit leerzeichen"],
    )
    def test_unzulaessiger_link(self, zwei_orgs: dict[str, Any], link: str) -> None:
        with pytest.raises(CommandError, match="Link"):
            call_command(
                "work_ankuendigung", "--schluessel", "x-1", "--titel", TITEL, "--text", TEXT, "--link", link
            )

        assert not ankuendigungen().exists()

    @pytest.mark.parametrize("schluessel", ["Gross", "mit leer", "ende-", "a" * 61, "a:b"])
    def test_unzulaessiger_schluessel(self, zwei_orgs: dict[str, Any], schluessel: str) -> None:
        with pytest.raises(CommandError, match="Schlüssel"):
            ankuendigen(schluessel=schluessel)

    def test_titel_und_text_sind_pflicht(self, zwei_orgs: dict[str, Any]) -> None:
        with pytest.raises(CommandError, match="Titel"):
            call_command("work_ankuendigung", "--schluessel", "x-1", "--text", TEXT)
        with pytest.raises(CommandError, match="Text"):
            call_command("work_ankuendigung", "--schluessel", "x-1", "--titel", TITEL)

    def test_abgeschaltete_ankuendigungen_werden_beachtet(self, zwei_orgs: dict[str, Any]) -> None:
        NotificationPreference.objects.create(
            membership=zwei_orgs["nur_a"], type_settings={NotificationType.ANNOUNCEMENT.value: {"in_app": False}}
        )

        ausgabe = ankuendigen()

        assert not ankuendigungen(recipient=zwei_orgs["nur_a"]).exists()
        assert "3 neu, 0 schon vorhanden, 1 abgeschaltet" in ausgabe

    def test_keine_mail_und_keine_weiterleitung(
        self, zwei_orgs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aufgerufen: list[Any] = []
        monkeypatch.setattr(NotificationHub, "_queue_email", classmethod(lambda cls, n: aufgerufen.append(n)))
        monkeypatch.setattr(NotificationHub, "_forward_to_deputy", classmethod(lambda cls, **k: aufgerufen.append(k)))

        ankuendigen()

        assert aufgerufen == []
        assert mail.outbox == []
        assert not ankuendigungen(email_sent=True).exists()
        assert not Notification.objects.filter(title__startswith="[Vertretung]").exists()


@pytest.mark.django_db
class TestHinweisband:
    def test_start_zeigt_die_ungelesene_ankuendigung(self, zwei_orgs: dict[str, Any], client_for: Any) -> None:
        ankuendigen("--rueckmeldung")

        response = start(client_for(zwei_orgs["nur_a"].user), zwei_orgs["a"])

        assert response.status_code == 200
        inhalt = response.content.decode()
        assert 'data-testid="hinweisband"' in inhalt
        assert TITEL in inhalt and TEXT in inhalt
        assert f'href="{LINK}"' in inhalt and "Was ist neu" in inhalt
        assert "Rückmeldung geben" in inhalt
        assert reverse("work:support", kwargs={"org_slug": zwei_orgs["a"].slug}) in inhalt

    def test_ohne_ankuendigung_kein_band(self, zwei_orgs: dict[str, Any], client_for: Any) -> None:
        response = start(client_for(zwei_orgs["nur_a"].user), zwei_orgs["a"])

        assert 'data-testid="hinweisband"' not in response.content.decode()

    def test_wegklicken_gilt_dauerhaft_und_in_allen_organisationen(
        self, zwei_orgs: dict[str, Any], client_for: Any
    ) -> None:
        ankuendigen()
        client = client_for(zwei_orgs["beide_a"].user)
        band_a = ankuendigungen(recipient=zwei_orgs["beide_a"]).get()

        antwort = client.post(
            reverse(
                "work:notification_mark_read",
                kwargs={"org_slug": zwei_orgs["a"].slug, "notification_id": band_a.id},
            )
        )

        assert antwort.status_code == 200
        assert ankuendigungen(recipient__user=zwei_orgs["beide_a"].user, is_read=False).count() == 0
        for organisation in (zwei_orgs["a"], zwei_orgs["b"]):
            assert 'data-testid="hinweisband"' not in start(client, organisation).content.decode()
        # Andere Personen sehen sie weiter
        anderer = start(client_for(zwei_orgs["nur_a"].user), zwei_orgs["a"]).content.decode()
        assert 'data-testid="hinweisband"' in anderer

    def test_erscheint_bei_zwei_organisationen_nur_einmal_im_band(
        self, zwei_orgs: dict[str, Any], client_for: Any
    ) -> None:
        ankuendigen()

        inhalt = start(client_for(zwei_orgs["beide_a"].user), zwei_orgs["a"]).content.decode()

        assert inhalt.count('data-testid="hinweisband"') == 1
        assert inhalt.count(TITEL) == 1

    def test_glocke_alle_gelesen_gilt_auch_in_der_anderen_organisation(
        self, zwei_orgs: dict[str, Any], client_for: Any
    ) -> None:
        ankuendigen()
        client = client_for(zwei_orgs["beide_a"].user)

        client.post(reverse("work:notifications_mark_all_read", kwargs={"org_slug": zwei_orgs["b"].slug}))

        assert not ankuendigungen(recipient=zwei_orgs["beide_a"], is_read=False).exists()
        assert 'data-testid="hinweisband"' not in start(client, zwei_orgs["a"]).content.decode()

    def test_spaeter_angelegt_nach_lesen_in_anderer_organisation_bleibt_gelesen(
        self, zwei_orgs: dict[str, Any], client_for: Any
    ) -> None:
        ankuendigen("--organisation", zwei_orgs["a"].slug)
        ankuendigung.gelesen(ankuendigungen(recipient=zwei_orgs["beide_a"]).get())

        ankuendigen("--organisation", zwei_orgs["b"].slug)

        assert ankuendigungen(recipient=zwei_orgs["beide_b"]).get().is_read is True
        assert ankuendigungen(recipient=zwei_orgs["nur_b"]).get().is_read is False
        inhalt = start(client_for(zwei_orgs["beide_a"].user), zwei_orgs["b"]).content.decode()
        assert 'data-testid="hinweisband"' not in inhalt

    def test_nur_die_neueste_ankuendigung(self, zwei_orgs: dict[str, Any], client_for: Any) -> None:
        ankuendigen(schluessel="erste")
        call_command(
            "work_ankuendigung",
            "--schluessel",
            "zweite",
            "--titel",
            "Zweite Ankündigung",
            "--text",
            "Noch etwas Neues.",
            stdout=StringIO(),
        )
        mitglied = zwei_orgs["nur_a"]
        client = client_for(mitglied.user)

        inhalt = start(client, zwei_orgs["a"]).content.decode()
        assert "Zweite Ankündigung" in inhalt and TITEL not in inhalt

        ankuendigung.gelesen(ankuendigungen(recipient=mitglied, metadata__ankuendigung="zweite").get())
        # Die ältere, noch ungelesene erscheint danach nicht mehr im Band, bleibt aber in der Glocke
        assert 'data-testid="hinweisband"' not in start(client, zwei_orgs["a"]).content.decode()
        assert ankuendigungen(recipient=mitglied, metadata__ankuendigung="erste", is_read=False).exists()

    def test_fremde_benachrichtigung_laesst_sich_nicht_lesen(
        self, zwei_orgs: dict[str, Any], client_for: Any
    ) -> None:
        ankuendigen()
        fremd = ankuendigungen(recipient=zwei_orgs["beide_a"]).get()

        antwort = client_for(zwei_orgs["nur_a"].user).post(
            reverse("work:notification_mark_read", kwargs={"org_slug": zwei_orgs["a"].slug, "notification_id": fremd.id})
        )

        assert antwort.status_code == 404
        assert not ankuendigungen(recipient__user=zwei_orgs["beide_a"].user, is_read=True).exists()

    def test_gast_bekommt_keinen_link_zur_rueckmeldung(self, zwei_orgs: dict[str, Any]) -> None:
        from apps.work.dashboard.hinweise import hinweis_fuer_start

        ankuendigen("--mit-gaesten", "--rueckmeldung")

        assert hinweis_fuer_start(zwei_orgs["gast"]).rueckmeldung_url == ""  # type: ignore[union-attr]
        assert hinweis_fuer_start(zwei_orgs["nur_a"]).rueckmeldung_url  # type: ignore[union-attr]


@pytest.mark.django_db
class TestGlocke:
    def test_glocke_fuehrt_die_ankuendigung(self, zwei_orgs: dict[str, Any], client_for: Any) -> None:
        ankuendigen()
        client = client_for(zwei_orgs["nur_a"].user)

        liste = client.get(reverse("work:notifications_partial", kwargs={"org_slug": zwei_orgs["a"].slug})).json()

        assert TITEL in liste["html"]
        assert liste["unread_count"] == 1


@pytest.mark.django_db
class TestZurueckziehen:
    def test_zurueckziehen_blendet_aus_und_behaelt_die_daten(
        self, zwei_orgs: dict[str, Any], client_for: Any
    ) -> None:
        ankuendigen()
        vorher = {n.id: (n.title, n.message, n.is_read, n.read_at) for n in ankuendigungen()}
        mitglied = zwei_orgs["nur_a"]
        client = client_for(mitglied.user)
        assert NotificationHub.get_unread_count(mitglied) == 1

        ausgabe = StringIO()
        call_command("work_ankuendigung", "--schluessel", "work-update-test", "--zurueckziehen", stdout=ausgabe)

        assert "Zurückgezogen: 4 Benachrichtigungen" in ausgabe.getvalue()
        # Bestand vorher = nachher: nichts gelöscht, Inhalt und Lesestand unverändert
        assert {n.id: (n.title, n.message, n.is_read, n.read_at) for n in ankuendigungen()} == vorher
        assert all("zurueckgezogen_am" in n.metadata for n in ankuendigungen())
        assert NotificationHub.get_unread_count(mitglied) == 0
        assert 'data-testid="hinweisband"' not in start(client, zwei_orgs["a"]).content.decode()
        liste = client.get(reverse("work:notifications_partial", kwargs={"org_slug": zwei_orgs["a"].slug})).json()
        assert TITEL not in liste["html"]

    def test_zurueckziehen_im_probelauf_aendert_nichts(self, zwei_orgs: dict[str, Any]) -> None:
        ankuendigen()
        ausgabe = StringIO()

        call_command(
            "work_ankuendigung", "--schluessel", "work-update-test", "--zurueckziehen", "--probelauf", stdout=ausgabe
        )

        assert "würde 4 Benachrichtigungen zurückziehen" in ausgabe.getvalue()
        assert not ankuendigungen(metadata__has_key="zurueckgezogen_am").exists()

    def test_andere_benachrichtigungen_bleiben_sichtbar(self, zwei_orgs: dict[str, Any]) -> None:
        mitglied = zwei_orgs["nur_a"]
        Notification.objects.create(
            recipient=mitglied, notification_type=NotificationType.SYSTEM_MESSAGE, title="Andere", message="x"
        )
        ankuendigen()

        ankuendigung.zurueckziehen("work-update-test")

        assert list(NotificationHub.for_recipient(mitglied).values_list("title", flat=True)) == ["Andere"]
