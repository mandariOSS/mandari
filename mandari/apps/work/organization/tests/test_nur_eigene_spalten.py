# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dienste der Organisation schreiben nur ihre eigenen Spalten (Teil von #1172).

Konto, Mitgliedschaft und Organisation stammen in einer Anfrage vom Stand bei ihrem Beginn. Ändert bis zum Schreiben
ein anderer Weg dieselbe Zeile (Passwortwechsel, Sperre, Deaktivierung, Entzug der Vereidigung, Vertragsstatus,
Limit, Zwei-Faktor-Pflicht), bleibt diese Änderung stehen. SQLite kennt keine Zeilensperre; die Tests belegen
deshalb das frische Lesen und die gezielt geschriebenen Spalten: Sie laden das Objekt, ändern die Zeile an ihm
vorbei in der Datenbank und rufen danach den Dienst mit dem alten Objekt auf.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.models.query import QuerySet

from apps.accounts.models import User
from apps.common.tests.factories import DEFAULT_PASSWORD, MembershipFactory, UserFactory
from apps.tenants.models import Membership, Organization, UserInvitation
from apps.work.organization import services
from apps.work.organization.services import ServiceError
from insight_core.models import OParlBody, OParlPerson, OParlSource

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org/oparl"


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(
        org, ["members.invite", "members.edit", "guests.invite"], email="admin@example.org", is_admin=True
    )


def _konto(pk: Any) -> User:
    return cast(User, User.objects.get(pk=pk))


# ---------------------------------------------------------------------------
# Profil
# ---------------------------------------------------------------------------


def test_profil_speichern_laesst_passwort_sperre_und_plattformrechte_stehen(org: Any, make_member: Any) -> None:
    mitglied = make_member(org, [], email="profil@example.org")
    User.objects.filter(pk=mitglied.user.pk).update(is_staff=True)
    konto = _konto(mitglied.user.pk)
    neues_passwort = make_password("ganz-neues-passwort-5678!")
    # Zwischen Lesen und Speichern: Passwort gewechselt, Konto gesperrt, Plattformrecht entzogen
    User.objects.filter(pk=konto.pk).update(password=neues_passwort, is_active=False, is_staff=False)

    services.update_profile(konto, first_name="Erika", last_name="Beispiel", phone="0251 123", avatar=None)

    aktuell = _konto(konto.pk)
    assert (aktuell.first_name, aktuell.last_name, aktuell.phone) == ("Erika", "Beispiel", "0251 123")
    assert aktuell.password == neues_passwort and not aktuell.check_password(DEFAULT_PASSWORD)
    assert aktuell.is_active is False and aktuell.is_staff is False


def test_profilbild_entfernen_laesst_passwort_und_sperre_stehen(org: Any, make_member: Any) -> None:
    mitglied = make_member(org, [], email="bild@example.org")
    User.objects.filter(pk=mitglied.user.pk).update(avatar="avatars/bild.png")
    konto = _konto(mitglied.user.pk)
    neues_passwort = make_password("ganz-neues-passwort-5678!")
    User.objects.filter(pk=konto.pk).update(password=neues_passwort, is_active=False)

    assert services.remove_avatar(konto) is True

    aktuell = _konto(konto.pk)
    assert not aktuell.avatar
    assert aktuell.password == neues_passwort and aktuell.is_active is False


# ---------------------------------------------------------------------------
# Mitgliedschaft
# ---------------------------------------------------------------------------


def _alter_stand(org: Any, **werte: Any) -> Membership:
    """Mitgliedschaft laden und danach an ihr vorbei deaktivieren und die Vereidigung entziehen."""
    mitglied = cast(Membership, cast(Any, MembershipFactory)(organization=org, is_sworn_in=True, **werte))
    geladen = Membership.objects.get(pk=mitglied.pk)
    Membership.objects.filter(pk=mitglied.pk).update(is_active=False, is_sworn_in=False)
    return geladen


def _zugang(mitglied: Membership) -> tuple[bool, bool]:
    aktuell = Membership.objects.get(pk=mitglied.pk)
    return aktuell.is_active, aktuell.is_sworn_in


def test_ris_person_verknuepfen_und_loesen_schreibt_nur_die_verknuepfung(org: Any) -> None:
    quelle = OParlSource.objects.create(name="RIS", url=f"{RIS}/system")
    kommune = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Stadt Beispiel")
    org.body = kommune
    org.save(update_fields=["body"])
    person = OParlPerson.objects.create(external_id=f"{RIS}/person/1", body=kommune, name="Erika Beispiel")
    mitglied = _alter_stand(org)

    services.link_oparl_person(org, mitglied, str(person.pk))

    assert Membership.objects.get(pk=mitglied.pk).oparl_person_id == person.pk
    assert _zugang(mitglied) == (False, False)

    # Dasselbe beim Lösen, wieder mit einem Objekt, das noch den Stand vor der Deaktivierung trägt
    veraltet = Membership.objects.get(pk=mitglied.pk)
    veraltet.is_active = True
    veraltet.is_sworn_in = True
    services.unlink_oparl_person(veraltet)

    assert Membership.objects.get(pk=mitglied.pk).oparl_person_id is None
    assert _zugang(mitglied) == (False, False)


def test_vereidigung_setzen_hebt_keine_deaktivierung_auf(org: Any, admin: Any) -> None:
    mitglied = _alter_stand(org)

    services.update_sworn_in(mitglied, admin, True)

    assert _zugang(mitglied) == (False, True)


def test_vereidigung_historie_nennt_den_wirklichen_uebergang(
    org: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Das geladene Objekt sagt noch „vereidigt“; in der Datenbank ist die Vereidigung schon entzogen."""
    from apps.work.organization import audit

    mitglied = _alter_stand(org)
    eintraege: list[tuple[bool, bool]] = []
    original = audit.log_sworn_in

    def merken(member: Any, old: bool, new: bool, **kwargs: Any) -> Any:
        eintraege.append((old, new))
        return original(member, old, new, **kwargs)

    monkeypatch.setattr(audit, "log_sworn_in", merken)
    services.update_sworn_in(mitglied, admin, True)

    assert eintraege == [(False, True)]


def test_selbst_austreten_laesst_den_entzug_der_vereidigung_stehen(org: Any) -> None:
    mitglied = _alter_stand(org)
    Membership.objects.filter(pk=mitglied.pk).update(is_active=True)

    services.request_account_deletion(org, mitglied, DEFAULT_PASSWORD)

    assert _zugang(mitglied) == (False, False)


def test_selbst_austreten_meldet_eine_inzwischen_entfernte_mitgliedschaft(org: Any) -> None:
    mitglied = _alter_stand(org)
    Membership.objects.filter(pk=mitglied.pk).delete()

    with pytest.raises(ServiceError, match="gibt es nicht mehr"):
        services.request_account_deletion(org, mitglied, DEFAULT_PASSWORD)

    assert not Membership.objects.filter(pk=mitglied.pk).exists()


# ---------------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------------


def _vertrag_geaendert(org: Any) -> Organization:
    """Organisation laden; danach ändern Betreiber und Verwaltung die Zeile an diesem Objekt vorbei."""
    geladen = Organization.objects.get(pk=org.pk)
    Organization.objects.filter(pk=org.pk).update(
        is_active=False, member_limit=1, require_2fa=True, publish_protocols=True
    )
    return geladen


def _vertrag(org: Any) -> tuple[bool, int | None, bool, bool]:
    aktuell = Organization.objects.get(pk=org.pk)
    return aktuell.is_active, aktuell.member_limit, aktuell.require_2fa, aktuell.publish_protocols


GEAENDERT = (False, 1, True, True)


def test_allgemeine_und_kontakt_einstellungen_schreiben_nur_ihre_spalten(org: Any) -> None:
    geladen = _vertrag_geaendert(org)

    services.update_general_settings(
        geladen, name="Fraktion Neu", description="Text", primary_color="#112233", logo=None, remove_logo=False
    )
    services.update_contact_settings(
        geladen, contact_email="info@example.org", contact_phone="0251 1", website="", address="Markt 1"
    )

    aktuell = Organization.objects.get(pk=org.pk)
    assert (aktuell.name, aktuell.primary_color, aktuell.contact_email) == (
        "Fraktion Neu",
        "#112233",
        "info@example.org",
    )
    assert _vertrag(org) == GEAENDERT


def test_mail_einstellungen_schreiben_nur_ihre_spalten(org: Any) -> None:
    geladen = _vertrag_geaendert(org)

    services.save_email_settings(
        geladen,
        services.EmailSettingsInput(
            mail_sender_mode="smtp",
            smtp_fallback_to_mandari=False,
            smtp_host="",
            smtp_port_raw="2525",
            smtp_username="fraktion",
            smtp_use_tls=False,
            smtp_from_email="info@example.org",
            smtp_from_name="Fraktion",
            smtp_password="geheim",
            smtp_password_clear=False,
        ),
    )

    aktuell = Organization.objects.get(pk=org.pk)
    assert (aktuell.mail_sender_mode, aktuell.smtp_port, aktuell.smtp_username) == ("smtp", 2525, "fraktion")
    assert cast(Any, aktuell).get_smtp_password() == "geheim"
    assert _vertrag(org) == GEAENDERT


def test_fraktionsformular_als_ganzes_schreibt_nur_die_sitzungseinstellungen(org: Any, make_member: Any) -> None:
    """Auch ohne Recht, Protokolle zu veröffentlichen: Der inzwischen gesetzte Schalter bleibt, wie er ist."""
    leitung = make_member(org, ["faction.manage"], email="leitung@example.org")
    geladen = _vertrag_geaendert(org)
    # Eine zweite Person hat inzwischen eine andere Einstellung der Organisation gespeichert
    einstellungen = dict(Organization.objects.get(pk=org.pk).settings or {})
    einstellungen["anderes"] = {"wert": 1}
    Organization.objects.filter(pk=org.pk).update(settings=einstellungen)

    services.save_faction_settings(geladen, leitung, {"quorum_rule": "simple_majority"})

    aktuell = Organization.objects.get(pk=org.pk)
    assert "faction" in aktuell.settings and aktuell.settings["anderes"] == {"wert": 1}
    assert _vertrag(org) == GEAENDERT
    # Das Objekt des Aufrufers trägt danach die gespeicherten Einstellungen
    assert geladen.settings == aktuell.settings and geladen.publish_protocols is True


def test_eigentuemerschaft_uebertragen_schreibt_nur_die_eigentuemerspalte(org: Any, admin: Any) -> None:
    nachfolge = cast(Any, MembershipFactory)(organization=org)
    Organization.objects.filter(pk=org.pk).update(owner=admin.user)
    geladen = _vertrag_geaendert(org)

    services.transfer_ownership(geladen, nachfolge, admin.user)

    assert Organization.objects.get(pk=org.pk).owner_id == nachfolge.user_id
    assert _vertrag(org) == GEAENDERT


def test_einladung_annehmen_setzt_nur_einen_leeren_eigentuemer(org: Any, admin: Any) -> None:
    Organization.objects.filter(pk=org.pk).update(owner=None)
    erste = UserInvitation.create_for_organization(organization=org, email="erste@example.org", invited_by=admin.user)
    zweite = UserInvitation.create_for_organization(organization=org, email="zweite@example.org", invited_by=admin.user)
    # Beide Einladungen tragen die Organisation noch ohne Eigentümer; dazwischen ändert sich der Vertragsstand
    erste = UserInvitation.objects.select_related("organization").get(pk=erste.pk)
    zweite = UserInvitation.objects.select_related("organization").get(pk=zweite.pk)
    Organization.objects.filter(pk=org.pk).update(is_active=False, require_2fa=True, publish_protocols=True)

    erstes_konto = cast(Any, UserFactory)(email="erste@example.org")
    services.accept_invitation(erste, erstes_konto)
    services.accept_invitation(zweite, cast(Any, UserFactory)(email="zweite@example.org"))

    aktuell = Organization.objects.get(pk=org.pk)
    assert aktuell.owner_id == erstes_konto.pk
    assert (aktuell.is_active, aktuell.require_2fa, aktuell.publish_protocols) == (False, True, True)


# ---------------------------------------------------------------------------
# Gast-Limit
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_gast_limit_wird_unter_sperre_der_organisation_gezaehlt(admin: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Einladung und Reaktivierung eines Gastes sperren die Organisationszeile, bevor sie die Gäste zählen.

    SQLite kennt kein SELECT … FOR UPDATE; der Test zeichnet deshalb die Sperraufrufe auf und prüft, dass sie in
    einer Transaktion und vor dem Zählen laufen.
    """
    org = admin.organization
    ablauf: list[str] = []
    sperre = QuerySet.select_for_update
    zaehlen = Organization.has_free_guest_slot

    def sperren(self: Any, *args: Any, **kwargs: Any) -> Any:
        if self.model is Organization and kwargs.get("no_key") and connection.in_atomic_block:
            ablauf.append("sperre")
        return sperre(self, *args, **kwargs)

    def gezaehlt(self: Any) -> bool:
        ablauf.append("zaehlen")
        return zaehlen(self)

    monkeypatch.setattr(QuerySet, "select_for_update", sperren)
    monkeypatch.setattr(Organization, "has_free_guest_slot", gezaehlt)

    services.invite_guest(
        org, admin, email="gast@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
    )
    assert ablauf == ["sperre", "zaehlen"]

    gast = Membership.objects.get(organization=org, is_guest=True)
    Membership.objects.filter(pk=gast.pk).update(is_active=False)
    ablauf.clear()
    services.reactivate_member(org, Membership.objects.get(pk=gast.pk), admin)

    assert ablauf == ["sperre", "zaehlen"]
    assert Membership.objects.get(pk=gast.pk).is_active is True


def test_gast_limit_gilt_bei_einladung_und_reaktivierung(org: Any, admin: Any) -> None:
    Organization.objects.filter(pk=org.pk).update(guest_limit=1)
    org.refresh_from_db()
    services.invite_guest(
        org, admin, email="gast@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
    )
    ruhend = cast(Any, MembershipFactory)(organization=org, is_guest=True, is_active=False)

    with pytest.raises(ServiceError, match="Gast-Limit"):
        services.invite_guest(
            org, admin, email="zweiter@example.org", note="", share_level="view", document_ids=[], folder_ids=[]
        )
    with pytest.raises(ServiceError, match="Gast-Limit"):
        services.reactivate_member(org, ruhend, admin)

    assert Membership.objects.filter(organization=org, is_guest=True, is_active=True).count() == 1
