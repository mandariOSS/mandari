# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Provisioning-API v2 (Issue #1132): Zugang, Anlegen, Wiederholen, Ändern, Übernehmen, Status,
Prüfung der Eingaben, absolute URL und Liste.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from django.core import mail
from django.test import Client

from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.tenants.models import Membership, Organization, PartyGroup, UserInvitation
from insight_core.models import OParlBody, OParlSource

KEY = "test-provisioning-v2-schluessel"
URL = "/api/provisioning/v2/work/"
SITE = "https://work.example.org"


@pytest.fixture
def api(settings: Any) -> Client:
    settings.PROVISIONING_V2_API_KEY = KEY
    settings.PROVISIONING_V2_ALLOWED_NETWORKS = []
    settings.SITE_URL = SITE
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    return Client(HTTP_AUTHORIZATION=f"Bearer {KEY}")


@pytest.fixture
def system_user(db: Any) -> Any:
    return cast(Any, UserFactory)(email="system@example.org", is_superuser=True, is_staff=True)


@pytest.fixture
def source(db: Any) -> OParlSource:
    return OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")


def make_body(source: OParlSource, number: int, ags: str, *, is_listed: bool = True) -> OParlBody:
    return OParlBody.objects.create(
        external_id=f"https://ris.example.org/body/{number}",
        source=source,
        name=f"Stadt {number}",
        ags=ags,
        is_listed=is_listed,
    )


@pytest.fixture
def body(source: OParlSource) -> OParlBody:
    return make_body(source, 1, "01234567")


@pytest.fixture
def party(db: Any) -> PartyGroup:
    return PartyGroup.objects.create(name="Beispielpartei", slug="beispielpartei")


def desired(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Fraktion Beispiel",
        "slug": "fraktion-beispiel",
        "admin_email": "vorsitz@example.org",
        "body_ags": "01234567",
        "party_group": "Beispielpartei",
        "status": "aktiv",
        "member_limit": 10,
        "guest_limit": 25,
        "billing_reference": "V-1",
        "plan": "hosted",
    }
    data.update(overrides)
    return data


def put(client: Client, ref: str, data: Any) -> Any:
    return client.put(f"{URL}{ref}", data=json.dumps(data), content_type="application/json")


# ---------------------------------------------------------------------------
# Zugang
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ohne_schluessel_ist_v2_unsichtbar(settings: Any) -> None:
    settings.PROVISIONING_V2_API_KEY = ""
    client = Client(HTTP_AUTHORIZATION=f"Bearer {KEY}")
    assert client.get(URL).status_code == 404
    assert client.get(f"{URL}kunde-1").status_code == 404
    assert put(client, "kunde-1", desired()).status_code == 404
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_falscher_oder_fehlender_schluessel_gibt_401(api: Client) -> None:
    for headers in ({}, {"Authorization": "Bearer falsch"}, {"Authorization": KEY}):
        response = Client().get(URL, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        assert response["WWW-Authenticate"] == "Bearer"
    # Nicht-ASCII im Header führt nicht zu einem Serverfehler
    assert Client(HTTP_AUTHORIZATION="Bearer schlüssel").get(URL).status_code == 401
    assert put(Client(), "kunde-1", desired()).status_code == 401
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_netzbeschraenkung(api: Client, settings: Any) -> None:
    settings.PROVISIONING_V2_ALLOWED_NETWORKS = ["10.0.0.0/8"]
    assert api.get(URL, REMOTE_ADDR="192.0.2.10").status_code == 404
    assert api.get(URL, REMOTE_ADDR="10.1.2.3").status_code == 200
    # Hinter dem Proxy zählt die von ihm gesetzte Client-Adresse (wie bei ADMIN_ALLOWED_NETWORKS)
    assert api.get(URL, REMOTE_ADDR="172.18.0.2", HTTP_X_FORWARDED_FOR="10.9.9.9").status_code == 200
    assert api.get(URL, REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="192.0.2.10").status_code == 404
    # Aus dem freigegebenen Netz, aber ohne Schlüssel: weiterhin 401
    assert Client().get(URL, REMOTE_ADDR="10.1.2.3").status_code == 401


# ---------------------------------------------------------------------------
# Anlegen, Wiederholen, Ändern
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_anlegen_und_identische_wiederholung(api: Client, system_user: Any, body: OParlBody, party: PartyGroup) -> None:
    response = put(api, "kunde-1", desired())

    assert response.status_code == 201
    data = response.json()
    assert data["ref"] == "kunde-1"
    assert data["slug"] == "fraktion-beispiel"
    assert data["name"] == "Fraktion Beispiel"
    assert data["status"] == "aktiv" and data["is_active"] is True
    assert data["member_limit"] == 10 and data["guest_limit"] == 25
    assert data["member_count"] == 0 and data["guest_count"] == 0
    assert data["work_url"] == f"{SITE}/work/fraktion-beispiel/"
    assert data["billing_reference"] == "V-1" and data["plan"] == "hosted"
    assert data["body"] == {"id": str(body.pk), "ags": "01234567", "name": "Stadt 1"}
    assert data["party_group"] == {"id": str(party.pk), "name": "Beispielpartei"}
    assert data["created_at"] and data["updated_at"] and data["status_changed_at"]
    assert data["invitation_sent"] is True and data["changed"] is True
    assert "/work/invitation/" not in response.content.decode(), "der Einladungslink geht nur per Mail"
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["vorsitz@example.org"]
    org = Organization.objects.get(provisioning_ref="kunde-1")
    invitation = UserInvitation.objects.get(organization=org)
    assert [role.is_admin for role in invitation.roles.all()] == [True]

    repeat = put(api, "kunde-1", desired())

    assert repeat.status_code == 200
    again = repeat.json()
    assert again["changed"] is False
    assert "invitation_sent" not in again
    assert again["updated_at"] == data["updated_at"]
    assert len(mail.outbox) == 1, "keine zweite Einladung"
    assert UserInvitation.objects.filter(organization=org).count() == 1
    assert api.get(f"{URL}kunde-1").json() == {key: value for key, value in again.items() if key not in {"changed"}}


@pytest.mark.django_db
def test_aendern_nur_mitgeschickte_felder(api: Client, system_user: Any, body: OParlBody, party: PartyGroup) -> None:
    put(api, "kunde-1", desired())

    response = put(
        api,
        "kunde-1",
        desired(
            name="Fraktion Umbenannt",
            member_limit=None,
            guest_limit=40,
            billing_reference="V-2",
            plan="hosted-gross",
            admin_email="andere@example.org",
        ),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["changed"] is True
    assert data["name"] == "Fraktion Umbenannt"
    assert data["member_limit"] is None and data["guest_limit"] == 40
    assert data["billing_reference"] == "V-2" and data["plan"] == "hosted-gross"
    assert len(mail.outbox) == 1, "eine geänderte Admin-E-Mail lädt niemanden neu ein"

    # Nicht mitgeschickte Felder bleiben unverändert
    partial = put(api, "kunde-1", {"name": "Fraktion Kurz"})
    assert partial.status_code == 200
    data = partial.json()
    assert data["name"] == "Fraktion Kurz"
    assert data["body"]["ags"] == "01234567" and data["party_group"]["name"] == "Beispielpartei"
    assert data["guest_limit"] == 40 and data["plan"] == "hosted-gross"


@pytest.mark.django_db
def test_slug_belegt_und_unveraenderlich(api: Client, system_user: Any, body: OParlBody, party: PartyGroup) -> None:
    cast(Any, OrganizationFactory)(name="Bestand", slug="fraktion-bestand")
    taken = put(api, "kunde-1", desired(slug="fraktion-bestand"))
    assert taken.status_code == 409 and taken.json() == {"error": "slug_taken"}
    assert not Organization.objects.filter(provisioning_ref="kunde-1").exists()

    assert put(api, "kunde-1", desired()).status_code == 201
    changed = put(api, "kunde-1", desired(slug="fraktion-neu"))
    assert changed.status_code == 409 and changed.json() == {"error": "slug_immutable"}
    assert Organization.objects.get(provisioning_ref="kunde-1").slug == "fraktion-beispiel"


# ---------------------------------------------------------------------------
# Übernahme bestehender Organisationen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_uebernahme_ohne_einladung(api: Client, body: OParlBody, party: PartyGroup) -> None:
    org = cast(Any, OrganizationFactory)(name="Bestand", slug="fraktion-bestand", body=body, party_group=party)
    member = cast(Any, MembershipFactory)(organization=org)
    data = desired(slug="fraktion-bestand", name="Bestand", adopt=True)
    del data["admin_email"]

    response = put(api, "kunde-7", data)

    assert response.status_code == 200
    payload = response.json()
    assert payload["adopted"] is True and payload["changed"] is True
    assert payload["ref"] == "kunde-7" and payload["member_count"] == 1
    assert "invitation_sent" not in payload
    assert not mail.outbox and not UserInvitation.objects.filter(organization=org).exists()
    member.refresh_from_db()
    assert member.is_active is True

    repeat = put(api, "kunde-7", data)
    assert repeat.status_code == 200 and repeat.json()["changed"] is False and "adopted" not in repeat.json()

    other = put(api, "kunde-8", data)
    assert other.status_code == 409 and other.json() == {"error": "ref_conflict"}

    missing = put(api, "kunde-9", desired(slug="gibt-es-nicht", adopt=True))
    assert missing.status_code == 409 and missing.json() == {"error": "adopt_not_found"}
    assert not Organization.objects.filter(slug="gibt-es-nicht").exists()
    assert not mail.outbox


@pytest.mark.django_db
def test_gleichzeitige_uebernahme_derselben_ref(
    api: Client, body: OParlBody, party: PartyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.provisioning.v2 import services as v2_services

    cast(Any, OrganizationFactory)(name="Bestand", slug="fraktion-bestand", body=body, party_group=party)
    data = desired(slug="fraktion-bestand", name="Bestand", adopt=True)
    del data["admin_email"]
    assert put(api, "kunde-7", data).status_code == 200

    # Die zweite Anfrage suchte die Referenz, bevor die erste ihre Übernahme festschrieb
    monkeypatch.setattr(v2_services, "_locked_by_ref", lambda ref: None)
    second = put(api, "kunde-7", data)

    assert second.status_code == 200, second.json()
    assert second.json()["changed"] is False and second.json()["ref"] == "kunde-7"
    assert "adopted" not in second.json()
    # Eine andere Referenz bleibt ein Konflikt
    assert put(api, "kunde-8", data).json() == {"error": "ref_conflict"}


@pytest.mark.django_db
def test_gleichzeitiges_anlegen_derselben_ref(
    api: Client, system_user: Any, body: OParlBody, party: PartyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.provisioning.v2 import services as v2_services

    assert put(api, "kunde-1", desired()).status_code == 201
    monkeypatch.setattr(v2_services, "_locked_by_ref", lambda ref: None)

    second = put(api, "kunde-1", desired())

    assert second.status_code == 200, second.json()
    assert second.json()["changed"] is False and "invitation_sent" not in second.json()
    assert len(mail.outbox) == 1 and UserInvitation.objects.count() == 1


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_status_aktiv_gesperrt_beendet_ohne_datenverlust(
    api: Client, system_user: Any, body: OParlBody, party: PartyGroup
) -> None:
    put(api, "kunde-1", desired())
    org = Organization.objects.get(provisioning_ref="kunde-1")
    member = cast(Any, MembershipFactory)(organization=org)
    guest = cast(Any, MembershipFactory)(organization=org, is_guest=True)

    suspended = put(api, "kunde-1", desired(status="gesperrt")).json()
    assert suspended["status"] == "gesperrt" and suspended["is_active"] is False
    assert suspended["changed"] is True

    ended = put(api, "kunde-1", desired(status="beendet")).json()
    assert ended["status"] == "beendet" and ended["is_active"] is False
    assert ended["status_changed_at"] >= suspended["status_changed_at"]
    org.refresh_from_db()
    assert org.provisioning_status == "beendet" and org.provisioning_status_changed_at is not None
    assert put(api, "kunde-1", desired(status="beendet")).json()["changed"] is False

    # Beenden löscht und ändert keine Nutzerdaten
    assert Membership.objects.filter(organization=org).count() == 2
    member.refresh_from_db()
    guest.refresh_from_db()
    assert member.is_active is True and guest.is_active is True
    assert ended["member_count"] == 1 and ended["guest_count"] == 1

    active = put(api, "kunde-1", desired(status="aktiv")).json()
    assert active["status"] == "aktiv" and active["is_active"] is True


@pytest.mark.django_db
def test_status_zeigt_aenderung_von_hand(api: Client, system_user: Any, body: OParlBody, party: PartyGroup) -> None:
    put(api, "kunde-1", desired())
    put(api, "kunde-1", desired(status="beendet"))
    Organization.objects.filter(provisioning_ref="kunde-1").update(is_active=True)

    assert api.get(f"{URL}kunde-1").json()["status"] == "aktiv"
    corrected = put(api, "kunde-1", desired(status="beendet")).json()
    assert corrected["changed"] is True and corrected["is_active"] is False and corrected["status"] == "beendet"


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["gesperrt", "beendet"])
def test_anlegen_nur_mit_status_aktiv(api: Client, system_user: Any, body: OParlBody, status: str) -> None:
    # Anlegen lädt die Admin-Person ein; gesperrt oder beendet anlegen würde trotzdem einladen
    response = put(api, "kunde-1", desired(status=status, party_group="Ganz Neue Partei"))

    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "create_requires_active"
    assert "aktiv" in data["message"]
    assert not Organization.objects.exists()
    assert not UserInvitation.objects.exists()
    assert not mail.outbox
    assert not PartyGroup.objects.filter(name="Ganz Neue Partei").exists()


# ---------------------------------------------------------------------------
# Prüfung der Eingaben
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_falsche_typen_sind_fehler(api: Client, system_user: Any) -> None:
    response = put(
        api,
        "kunde-1",
        {
            "name": 5,
            "slug": "fraktion-beispiel",
            "admin_email": "keine-adresse",
            "member_limit": "10",
            "guest_limit": True,
            "status": "weg",
            "plan": "Großer Plan",
            "body_ags": 1234567,
            "body_id": "keine-uuid",
            "billing_reference": None,
            "adopt": "ja",
            "unbekannt": 1,
        },
    )

    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "validation"
    assert set(data["fields"]) == {
        "name",
        "admin_email",
        "member_limit",
        "guest_limit",
        "status",
        "plan",
        "body_ags",
        "body_id",
        "billing_reference",
        "adopt",
        "unbekannt",
    }
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_pflichtfelder_und_regeln_aus_clean(api: Client, system_user: Any, body: OParlBody) -> None:
    missing = put(api, "kunde-1", {"slug": "fraktion-beispiel"})
    assert missing.status_code == 400
    assert missing.json()["fields"] == {"name": "erforderlich", "admin_email": "erforderlich"}

    # Aktive Organisationen brauchen Kommune und Partei (Organization.clean)
    data = desired()
    del data["body_ags"], data["party_group"]
    response = put(api, "kunde-1", data)
    assert response.status_code == 400
    assert set(response.json()["fields"]) == {"body_ags", "party_group"}

    bad_slug = put(api, "kunde-1", desired(slug="Fraktion_Gross"))
    assert bad_slug.status_code == 400 and set(bad_slug.json()["fields"]) == {"slug"}

    zero_limit = put(api, "kunde-1", desired(member_limit=0))
    assert zero_limit.status_code == 400 and set(zero_limit.json()["fields"]) == {"member_limit"}
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_unbekannte_kommune_legt_auch_keine_partei_an(api: Client, system_user: Any, body: OParlBody) -> None:
    response = put(api, "kunde-1", desired(body_ags="09999999", party_group="Ganz Neue Partei"))

    assert response.status_code == 400
    assert set(response.json()["fields"]) == {"body_ags"}
    assert not PartyGroup.objects.filter(name="Ganz Neue Partei").exists(), "Transaktion zurückgerollt"
    assert not Organization.objects.exists()


@pytest.mark.django_db
def test_ungueltiges_json_und_ungueltige_ref(api: Client) -> None:
    invalid = api.put(f"{URL}kunde-1", data="{kein json", content_type="application/json")
    assert invalid.status_code == 400 and invalid.json() == {"error": "invalid_json"}

    not_object = put(api, "kunde-1", ["liste"])
    assert not_object.status_code == 400 and set(not_object.json()["fields"]) == {"__all__"}

    for ref in ("a" * 65, "kunde@1", ".."):
        response = put(api, ref, desired())
        assert response.status_code == 400, ref
        assert set(response.json()["fields"]) == {"ref"}
    assert api.get(f"{URL}kunde@1").status_code == 404


# ---------------------------------------------------------------------------
# Kommune und Partei auflösen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_mehrdeutiger_gemeindeschluessel(api: Client, system_user: Any, source: OParlSource, party: PartyGroup) -> None:
    listed = make_body(source, 1, "01234567")
    unlisted = make_body(source, 2, "01234567", is_listed=False)

    # Genau ein gelisteter Eintrag: eindeutig
    assert put(api, "kunde-1", desired()).json()["body"]["id"] == str(listed.pk)

    unlisted.is_listed = True
    unlisted.save(update_fields=["is_listed"])
    ambiguous = put(api, "kunde-2", desired(slug="fraktion-zwei"))
    assert ambiguous.status_code == 400 and set(ambiguous.json()["fields"]) == {"body_ags"}

    chosen = put(api, "kunde-2", desired(slug="fraktion-zwei", body_id=str(unlisted.pk)))
    assert chosen.status_code == 201 and chosen.json()["body"]["id"] == str(unlisted.pk)
    # Die bestehende Zuordnung bleibt bei gleichem Schlüssel unverändert
    assert put(api, "kunde-2", desired(slug="fraktion-zwei")).json()["changed"] is False

    mismatch = put(api, "kunde-2", desired(slug="fraktion-zwei", body_id=str(listed.pk), body_ags="05555555"))
    assert mismatch.status_code == 400 and set(mismatch.json()["fields"]) == {"body_ags"}


@pytest.mark.django_db
def test_kreis_mit_kurzem_schluessel(api: Client, system_user: Any, source: OParlSource, party: PartyGroup) -> None:
    make_body(source, 3, "01234")
    response = put(api, "kunde-1", desired(body_ags="01234"))
    assert response.status_code == 201 and response.json()["body"]["ags"] == "01234"


@pytest.mark.django_db
def test_partei_nach_kennung_name_oder_neu(api: Client, system_user: Any, body: OParlBody, party: PartyGroup) -> None:
    by_id = put(api, "kunde-1", desired(party_group=str(party.pk)))
    assert by_id.status_code == 201 and by_id.json()["party_group"]["name"] == "Beispielpartei"
    # Name statt Kennung für dieselbe Partei: keine Änderung
    assert put(api, "kunde-1", desired(party_group="beispielpartei")).json()["changed"] is False

    created = put(api, "kunde-1", desired(party_group="Neue Liste"))
    assert created.json()["party_group"]["name"] == "Neue Liste"
    assert PartyGroup.objects.filter(name="Neue Liste").count() == 1

    unknown_id = put(api, "kunde-1", desired(party_group="00000000-0000-0000-0000-000000000000"))
    assert unknown_id.status_code == 400 and set(unknown_id.json()["fields"]) == {"party_group"}


# ---------------------------------------------------------------------------
# Lesen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_liste_enthaelt_auch_organisationen_ohne_ref(
    api: Client, system_user: Any, body: OParlBody, party: PartyGroup
) -> None:
    put(api, "kunde-1", desired())
    legacy = cast(Any, OrganizationFactory)(name="Altbestand", slug="altbestand")
    cast(Any, MembershipFactory)(organization=legacy)
    cast(Any, MembershipFactory)(organization=legacy)
    cast(Any, MembershipFactory)(organization=legacy, is_guest=True)
    cast(Any, MembershipFactory)(organization=legacy, is_active=False)

    response = api.get(URL)

    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2
    by_slug = {item["slug"]: item for item in data["organizations"]}
    assert by_slug["fraktion-beispiel"]["ref"] == "kunde-1"
    assert by_slug["altbestand"]["ref"] is None
    assert by_slug["altbestand"]["member_count"] == 2 and by_slug["altbestand"]["guest_count"] == 1
    assert by_slug["altbestand"]["work_url"] == f"{SITE}/work/altbestand/"
    assert by_slug["altbestand"]["status"] == "aktiv"


@pytest.mark.django_db
def test_unbekannte_ref_gibt_404(api: Client) -> None:
    response = api.get(f"{URL}gibt-es-nicht")
    assert response.status_code == 404 and response.json() == {"error": "not_found"}


@pytest.mark.django_db
def test_loeschen_gibt_es_nicht(api: Client, system_user: Any, body: OParlBody, party: PartyGroup) -> None:
    put(api, "kunde-1", desired())
    assert api.delete(f"{URL}kunde-1").status_code == 405
    assert Organization.objects.filter(provisioning_ref="kunde-1").exists()
