# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schlüsselwechsel mit Werten jeder Schlüsselart.

Der Bestand enthält je Art mindestens einen Wert: Mandanteninhalte (auch über mehrere
Fremdschlüssel entfernt), Zugangsdaten der Organisation, plattformweite Geheimnisse mit
dem Hauptschlüssel und den zweiten Faktor (verschlüsselt und als Klartext-Altbestand).
Geprüft wird über die Zugriffswege der Anwendung, nicht über den Wechsel selbst.
"""

from __future__ import annotations

import base64
import io
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from unittest import mock

import pytest
from cryptography.exceptions import InvalidTag
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import TwoFactorDevice, User
from apps.accounts.services import TwoFactorService
from apps.common import key_rotation
from apps.common.crypto_registry import ENCRYPTED_FIELDS
from apps.common.encryption import TenantEncryption, aes_open, decode_master_key
from apps.common.key_rotation import FieldReport, KeyRotation, RotationError, ScanResult, Status
from apps.common.models import AISettings
from apps.common.tests.factories import MembershipFactory, OrganizationFactory, UserFactory
from apps.minutes.models import Recording, RecordingSegment, TranscriptSegment
from apps.minutes.models_compute import ComputeSettings
from apps.session.models import SessionMeeting, SessionOrganization, SessionPerson, SessionTenant
from apps.tenants.models import Organization
from apps.work.faction.models import FactionMeeting
from apps.work.support.models import SupportTicket, SupportTicketMessage

pytestmark = pytest.mark.django_db

ALT_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def _neuer_schluessel() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


ALT = _neuer_schluessel()
NEU = _neuer_schluessel()


def _nur(schluessel: str) -> Any:
    return override_settings(ENCRYPTION_MASTER_KEY=schluessel, ENCRYPTION_MASTER_KEY_PREVIOUS="")


def _uebergang() -> Any:
    """Neuer Hauptschlüssel aktiv, alter nur zum Lesen – so läuft die Anwendung während des Wechsels."""
    return override_settings(ENCRYPTION_MASTER_KEY=NEU, ENCRYPTION_MASTER_KEY_PREVIOUS=ALT)


@dataclass
class Bestand:
    org: Organization
    ticket: SupportTicket
    nachricht: SupportTicketMessage
    mandant: SessionTenant
    person: SessionPerson
    sitzung: SessionMeeting
    transkript_session: TranscriptSegment
    transkript_fraktion: TranscriptSegment
    nutzer: User
    altnutzer: User


def _transkript(text: str, **sitzung: Any) -> TranscriptSegment:
    aufnahme = Recording.objects.create(retention_until=timezone.now() + timedelta(days=30), **sitzung)
    abschnitt = RecordingSegment.objects.create(recording=aufnahme, started_at=timezone.now())
    transkript = TranscriptSegment(segment=abschnitt, start_ms=0, end_ms=1000)
    cast(Any, transkript).set_text_encrypted(text)
    transkript.save()
    return transkript


def _anlegen() -> Bestand:
    org = cast(Organization, cast(Any, OrganizationFactory)(name="Fraktion Wechsel", slug="fraktion-wechsel"))
    org.set_smtp_password("smtp-geheim-org")
    org.set_ai_api_key("ki-geheim-org")
    org.save()
    mitglied = cast(Any, MembershipFactory)(organization=org)
    ticket = SupportTicket(organization=org, subject="Frage", created_by=mitglied)
    cast(Any, ticket).set_description_encrypted("ticket-geheim")
    ticket.save()
    nachricht = SupportTicketMessage(ticket=ticket, author_membership=mitglied)
    cast(Any, nachricht).set_content_encrypted("nachricht-geheim")
    nachricht.save()

    mandant = SessionTenant.objects.create(name="Stadt Wechsel", slug="stadt-wechsel")
    person = SessionPerson(tenant=mandant, given_name="P", family_name="Iban")
    cast(Any, person).set_bank_iban_encrypted("iban-geheim")
    person.save()
    gremium = SessionOrganization.objects.create(tenant=mandant, name="Rat")
    sitzung = SessionMeeting(tenant=mandant, name="Rat", organization=gremium, start=timezone.now(), is_public=True)
    cast(Any, sitzung).set_internal_notes_encrypted("notiz-geheim")
    sitzung.save()
    fraktionssitzung = FactionMeeting.objects.create(organization=org, title="Sitzung", start=timezone.now())

    ki = AISettings.objects.get_or_create(pk=1)[0]
    ki.set_api_key("ki-geheim-global")
    cast(Any, ki).save()
    rechenknoten = ComputeSettings.load(use_cache=False)
    rechenknoten.set_client_secret("client-geheim")
    rechenknoten.set_s3_secret_key("s3-geheim")
    rechenknoten.save()

    nutzer = cast(User, cast(Any, UserFactory)(email="zwei-faktor@example.org"))
    TwoFactorService().setup_2fa(nutzer)
    altnutzer = cast(User, cast(Any, UserFactory)(email="altbestand@example.org"))
    TwoFactorDevice.objects.create(user=altnutzer, secret_encrypted=ALT_TOTP.encode(), is_confirmed=True)

    return Bestand(
        org=org,
        ticket=ticket,
        nachricht=nachricht,
        mandant=mandant,
        person=person,
        sitzung=sitzung,
        transkript_session=_transkript("transkript-session-geheim", session_meeting=sitzung),
        transkript_fraktion=_transkript("transkript-fraktion-geheim", faction_meeting=fraktionssitzung),
        nutzer=nutzer,
        altnutzer=altnutzer,
    )


def _lesen(b: Bestand) -> dict[str, str]:
    """Alle Werte über die Zugriffswege der Anwendung, frisch aus der Datenbank."""
    org = Organization.objects.get(pk=b.org.pk)
    rechenknoten = ComputeSettings.load(use_cache=False)
    service = TwoFactorService()
    geraet = TwoFactorDevice.objects.get(user=b.nutzer)

    def entschluesselt(model: Any, pk: Any, getter: str) -> str:
        return str(getattr(model.objects.get(pk=pk), getter)())

    return {
        "smtp": org.get_smtp_password(),
        "ki_org": org.get_ai_api_key(),
        "ticket": entschluesselt(SupportTicket, b.ticket.pk, "get_description_decrypted"),
        "nachricht": entschluesselt(SupportTicketMessage, b.nachricht.pk, "get_content_decrypted"),
        "iban": entschluesselt(SessionPerson, b.person.pk, "get_bank_iban_decrypted"),
        "notiz": entschluesselt(SessionMeeting, b.sitzung.pk, "get_internal_notes_decrypted"),
        "transkript_session": entschluesselt(TranscriptSegment, b.transkript_session.pk, "get_text_decrypted"),
        "transkript_fraktion": entschluesselt(TranscriptSegment, b.transkript_fraktion.pk, "get_text_decrypted"),
        "ki_global": AISettings.objects.get(pk=1).get_api_key(),
        "client": rechenknoten.get_client_secret(),
        "s3": rechenknoten.get_s3_secret_key(),
        "totp": service._decrypt(geraet.secret_encrypted),
        "backup": service._decrypt(geraet.backup_codes_encrypted),
        "totp_alt": service._decrypt(TwoFactorDevice.objects.get(user=b.altnutzer).secret_encrypted),
    }


def _roh() -> dict[str, list[tuple[str, bytes | None]]]:
    """Gespeicherte Bytes aller eingetragenen Felder (zum Vergleich vorher/nachher)."""
    ergebnis = {}
    for entry in ENCRYPTED_FIELDS:
        zeilen = apps.get_model(entry.model)._base_manager.values_list("pk", entry.field)
        ergebnis[entry.label] = sorted((str(pk), bytes(wert) if wert is not None else None) for pk, wert in zeilen)
    return ergebnis


def _mandantenschluessel(model: Any, pk: Any, master: str) -> bytes:
    return aes_open(decode_master_key(master), bytes(model.objects.get(pk=pk).encryption_key))


def _befehl(*args: str) -> str:
    ausgabe = io.StringIO()
    call_command("rotate_encryption", *args, stdout=ausgabe)
    return ausgabe.getvalue()


@pytest.fixture
def bestand() -> Iterator[tuple[Bestand, dict[str, str]]]:
    with _nur(ALT):
        b = _anlegen()
        erwartet = _lesen(b)
    assert all(erwartet.values())
    yield b, erwartet


def test_uebergang_liest_mit_altem_und_neuem_hauptschluessel(bestand: tuple[Bestand, dict[str, str]]) -> None:
    b, erwartet = bestand
    with _uebergang():
        assert _lesen(b) == erwartet


def test_wechsel_erfasst_jede_schluesselart(bestand: tuple[Bestand, dict[str, str]]) -> None:
    b, erwartet = bestand
    with _nur(ALT):
        alt_org = _mandantenschluessel(Organization, b.org.pk, ALT)
        alt_session = _mandantenschluessel(SessionTenant, b.mandant.pk, ALT)

    with _uebergang():
        ausgabe = _befehl()
        assert _lesen(b) == erwartet  # auch vor dem Abschluss vollständig lesbar
        ausgabe += _befehl("--finalize")
    assert "ENCRYPTION_MASTER_KEY_PREVIOUS kann jetzt geleert werden" in ausgabe

    with _nur(NEU):
        assert _lesen(b) == erwartet
        neu_org = _mandantenschluessel(Organization, b.org.pk, NEU)
        neu_session = _mandantenschluessel(SessionTenant, b.mandant.pk, NEU)
        scan = KeyRotation().scan()
    assert neu_org != alt_org and neu_session != alt_session
    assert not Organization.objects.get(pk=b.org.pk).encryption_key_previous
    assert not SessionTenant.objects.get(pk=b.mandant.pk).encryption_key_previous
    assert scan.count(Status.PREVIOUS, Status.PLAINTEXT, Status.UNREADABLE) == 0
    assert scan.count(Status.CURRENT) == len(erwartet) + 2  # dazu die beiden Mandantenschlüssel

    # Weder alter Hauptschlüssel noch alte Mandantenschlüssel öffnen noch irgendetwas
    alt_master = decode_master_key(ALT)
    org = Organization.objects.get(pk=b.org.pk)
    for wert, schluessel in (
        (AISettings.objects.get(pk=1).api_key_encrypted, alt_master),
        (org.encryption_key, alt_master),
        (org.smtp_password_encrypted, alt_org),
        (SupportTicketMessage.objects.get(pk=b.nachricht.pk).content_encrypted, alt_org),
        (TranscriptSegment.objects.get(pk=b.transkript_session.pk).text_encrypted, alt_session),
        (TranscriptSegment.objects.get(pk=b.transkript_fraktion.pk).text_encrypted, alt_org),
    ):
        with pytest.raises(InvalidTag):
            aes_open(schluessel, bytes(cast(Any, wert)))
    # Der Klartext-Altbestand des zweiten Faktors ist jetzt verschlüsselt
    assert bytes(TwoFactorDevice.objects.get(user=b.altnutzer).secret_encrypted) != ALT_TOTP.encode()
    # Keine Werte in der Ausgabe
    assert not any(wert in ausgabe for wert in erwartet.values())


def test_nur_hauptschluessel_behaelt_mandantenschluessel(bestand: tuple[Bestand, dict[str, str]]) -> None:
    b, erwartet = bestand
    with _nur(ALT):
        alt_org = _mandantenschluessel(Organization, b.org.pk, ALT)
    with _uebergang():
        _befehl("--master-only")
    with _nur(NEU):
        assert _lesen(b) == erwartet
        assert _mandantenschluessel(Organization, b.org.pk, NEU) == alt_org
    assert not Organization.objects.get(pk=b.org.pk).encryption_key_previous


def test_dry_run_aendert_nichts_und_zeigt_keine_werte(bestand: tuple[Bestand, dict[str, str]]) -> None:
    b, erwartet = bestand
    vorher = _roh()
    with _uebergang():
        ausgabe = _befehl("--dry-run")
    assert _roh() == vorher
    assert "DRY-RUN" in ausgabe
    assert "tenants.Organization.smtp_password_encrypted" in ausgabe
    assert "accounts.TwoFactorDevice.secret_encrypted" in ausgabe
    assert "Neue Mandantenschlüssel: 2" in ausgabe
    assert not any(wert in ausgabe for wert in erwartet.values())
    assert ALT not in ausgabe and NEU not in ausgabe


def test_fehler_auf_hauptschluessel_ebene_laesst_alten_stand_vollstaendig(
    bestand: tuple[Bestand, dict[str, str]],
) -> None:
    b, erwartet = bestand
    vorher = _roh()
    echt = key_rotation._store
    aufrufe = {"n": 0}

    def abbruch_beim_vierten(*args: Any) -> int:
        aufrufe["n"] += 1
        if aufrufe["n"] == 4:
            raise RuntimeError("Simulierter Abbruch")
        return echt(*args)

    with (
        _uebergang(),
        mock.patch.object(key_rotation, "_store", side_effect=abbruch_beim_vierten),
        pytest.raises(RuntimeError),
    ):
        _befehl()
    assert aufrufe["n"] == 4
    assert _roh() == vorher
    with _nur(ALT):
        assert _lesen(b) == erwartet  # Umgebung zurückdrehen genügt


def test_fehler_bei_mandanteninhalten_bleibt_lesbar_und_wird_fortgesetzt(
    bestand: tuple[Bestand, dict[str, str]],
) -> None:
    b, erwartet = bestand
    with _nur(ALT):
        alt_session = _mandantenschluessel(SessionTenant, b.mandant.pk, ALT)
    echt = key_rotation._store

    def abbruch_mitten_im_session_mandanten(model: Any, *args: Any) -> int:
        if model is SessionMeeting:  # SessionPerson des Mandanten ist zu diesem Zeitpunkt schon umgeschrieben
            raise RuntimeError("Simulierter Abbruch")
        return echt(model, *args)

    with _uebergang():
        with (
            mock.patch.object(key_rotation, "_store", side_effect=abbruch_mitten_im_session_mandanten),
            pytest.raises(RuntimeError),
        ):
            _befehl()
        assert _lesen(b) == erwartet
        # Organisation fertig umgestellt, Session-Mandant vollständig beim alten Schlüssel
        assert Organization.objects.get(pk=b.org.pk).encryption_key_previous
        assert not SessionTenant.objects.get(pk=b.mandant.pk).encryption_key_previous
        assert _mandantenschluessel(SessionTenant, b.mandant.pk, NEU) == alt_session
        org_schluessel = _mandantenschluessel(Organization, b.org.pk, NEU)

        ausgabe = _befehl()  # zweiter Lauf setzt fort
        assert re.search(r"Fortgesetzte Wechsel:\s+1\n", ausgabe)
        assert re.search(r"Neue Mandantenschlüssel:\s+1\n", ausgabe)
        assert _mandantenschluessel(Organization, b.org.pk, NEU) == org_schluessel  # kein weiterer neuer Schlüssel
        assert _lesen(b) == erwartet
        _befehl("--finalize")
    with _nur(NEU):
        assert _lesen(b) == erwartet


def test_unlesbare_werte_brechen_vor_jeder_aenderung_ab(bestand: tuple[Bestand, dict[str, str]]) -> None:
    b, _erwartet = bestand
    SupportTicket.objects.filter(pk=b.ticket.pk).update(description_encrypted=secrets.token_bytes(48))
    vorher = _roh()
    with _uebergang():
        with pytest.raises(CommandError, match="work.SupportTicket.description_encrypted"):
            _befehl()
        assert _roh() == vorher
        _befehl("--ignore-unreadable")
        _befehl("--finalize", "--ignore-unreadable")
    fremd = [wert for pk, wert in _roh()["work.SupportTicket.description_encrypted"] if pk == str(b.ticket.pk)]
    assert fremd == [vorher["work.SupportTicket.description_encrypted"][0][1]]


def test_werte_ohne_schluesselart_brechen_ab() -> None:
    reserviert = next(entry for entry in ENCRYPTED_FIELDS if entry.field == "embedding_encrypted")
    bericht = FieldReport(reserviert)
    bericht.add(Status.UNASSIGNED, "123")
    with _nur(ALT), pytest.raises(RotationError, match="minutes.VoiceProfile.embedding_encrypted"):
        KeyRotation().check_preconditions(ScanResult([bericht], {}, {}))


def test_veraltete_instanz_schreibt_keinen_alten_schluessel_zurueck(
    bestand: tuple[Bestand, dict[str, str]],
) -> None:
    b, erwartet = bestand
    veraltet = Organization.objects.get(pk=b.org.pk)
    with _uebergang():
        _befehl()
        veraltet.name = "Fraktion Wechsel umbenannt"
        veraltet.save()  # z. B. ein vor dem Wechsel geöffnetes Admin-Formular
        _befehl("--finalize")
        # Liest trotz veralteter Schlüsselspalten: die Instanz lädt die Schlüssel nach
        wert = TenantEncryption(veraltet).decrypt(SupportTicket.objects.get(pk=b.ticket.pk).description_encrypted)
    assert wert == erwartet["ticket"]
    assert Organization.objects.get(pk=b.org.pk).name == "Fraktion Wechsel umbenannt"
    with _nur(NEU):
        assert _lesen(b) == erwartet
