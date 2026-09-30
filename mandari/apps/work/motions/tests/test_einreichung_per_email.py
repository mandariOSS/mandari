# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anträge per E-Mail an die Verwaltung einreichen (Issue #580).

Ohne mandari Session gab es keinen digitalen Einreichungsweg; die gepflegten Verwaltungskontakte
wurden nirgends genutzt. Jetzt: E-Mail je Empfänger mit PDF und Anhängen über den Mailweg der
Organisation, nachvollziehbar im Dokument, Eingangsbestätigung per Link. Die Session-Verbindung
hat Vorrang.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.session.models import SessionAPIToken, SessionTenant
from apps.tenants.models import AdministrationContact
from apps.work.motions import email_submission, ris_submission
from apps.work.motions.models import (
    Motion,
    MotionAdministrationEvent,
    MotionDocument,
    MotionEmailRecipient,
    MotionEmailSubmission,
)
from apps.work.notifications.models import Notification

pytestmark = pytest.mark.django_db

EDITOR = ["dashboard.view", "motions.view", "motions.edit", "motions.submit_to_ris", "motions.approve"]
INHALT = "<h2>Beschlussvorschlag</h2><p>Die Verwaltung plant einen Radweg.</p><h2>Begründung</h2><p>Sicherheit.</p>"
PDF = b"%PDF-1.4 Antragstext"


@pytest.fixture(autouse=True)
def umgebung(tmp_path: Path, settings: Any) -> Iterator[None]:
    settings.MEDIA_ROOT = tmp_path
    settings.SITE_URL = "https://mandari.example.org"
    with mock.patch("apps.work.motions.export_service.motion_export_service.export_to_pdf", return_value=PDF):
        yield


@pytest.fixture
def commit(django_capture_on_commit_callbacks: Any) -> Iterator[Callable[..., Any]]:
    def run() -> Any:
        return django_capture_on_commit_callbacks(execute=True)

    yield run


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    membership = make_member(org, EDITOR, email="autorin@example.org")
    membership.user.first_name, membership.user.last_name = "Anna", "Autorin"
    membership.user.save(update_fields=["first_name", "last_name"])
    return membership


@pytest.fixture
def kontakte(org: Any) -> list[AdministrationContact]:
    return [
        AdministrationContact.objects.create(
            organization=org, label="Ratsbüro", email="ratsbuero@stadt.example", order=0
        ),
        AdministrationContact.objects.create(organization=org, label="OB-Büro", email="ob@stadt.example", order=1),
    ]


@pytest.fixture
def antrag(org: Any, autorin: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Radweg Hauptstraße", visibility="organization", status="approved"
    )
    cast(Any, motion).set_content_encrypted(INHALT)
    motion.save()
    MotionDocument.objects.create(
        motion=motion,
        file=SimpleUploadedFile("lageplan.pdf", b"%PDF-1.4 Lageplan"),
        filename="Lageplan.pdf",
        mime_type="application/pdf",
        file_size=17,
    )
    return motion


def _url(org: Any, motion: Motion) -> str:
    return f"/work/{org.slug}/documents/{motion.id}/submit-ris/"


def _einreichen(client: Client, org: Any, motion: Motion, kontakte: list[Any], **extra: Any) -> Any:
    daten = {
        "channel": "email",
        "contacts": [str(k.pk) for k in kontakte],
        "subject": "Antrag: Radweg Hauptstraße",
        "message": "Wir bitten um Beratung im Bauausschuss.",
        "confirm": "on",
        **extra,
    }
    return client.post(_url(org, motion), daten)


# =============================================================================
# Einreichen
# =============================================================================


def test_seite_bietet_email_weg_ohne_session_verbindung(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    seite = client_for(autorin.user).get(_url(org, antrag)).content.decode()
    assert "Einreichung per E-Mail" in seite and 'name="channel" value="email"' in seite
    assert "ratsbuero@stadt.example" in seite and "ob@stadt.example" in seite
    assert "Lageplan.pdf" in seite and "Radweg Hauptstraße.pdf" in seite


def test_email_je_empfaenger_mit_pdf_und_anhaengen(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any, commit: Any
) -> None:
    with commit():
        antwort = _einreichen(client_for(autorin.user), org, antrag, kontakte)
    assert antwort.status_code == 302

    an_verwaltung = [m for m in mail.outbox if m.to[0].endswith("stadt.example")]
    assert sorted(m.to[0] for m in an_verwaltung) == ["ob@stadt.example", "ratsbuero@stadt.example"]
    for nachricht in an_verwaltung:
        assert len(nachricht.to) == 1  # Empfänger sehen einander nicht
        assert nachricht.subject == "Antrag: Radweg Hauptstraße"
        assert nachricht.reply_to == ["autorin@example.org"]
        namen = [a[0] for a in nachricht.attachments]
        assert namen == ["Radweg Hauptstraße.pdf", "Lageplan.pdf"]
        assert nachricht.attachments[0][1] == PDF
        assert "Wir bitten um Beratung im Bauausschuss." in nachricht.body
        assert "https://mandari.example.org/work/eingang/bestaetigen/" in nachricht.body

    einreichung = MotionEmailSubmission.objects.get(motion=antrag)
    assert einreichung.submitted_by_name == "Anna Autorin" and einreichung.submitted_by_email == "autorin@example.org"
    assert einreichung.attachment_names == ["Radweg Hauptstraße.pdf", "Lageplan.pdf"]
    assert {(r.label, r.delivered) for r in einreichung.recipients.all()} == {("Ratsbüro", True), ("OB-Büro", True)}
    antrag.refresh_from_db()
    assert antrag.status == "submitted" and antrag.submitted_at is not None
    assert MotionAdministrationEvent.objects.filter(motion=antrag, key=f"email:{einreichung.pk}:submitted").exists()
    # Kopie an die einreichende Person
    kopie = [m for m in mail.outbox if m.to == ["autorin@example.org"]]
    assert len(kopie) == 1 and "Ratsbüro" in kopie[0].body

    # Nachvollziehbar im Dokument (Seitenleiste) und auf der Einreichungsseite
    editor = client_for(autorin.user).get(f"/work/{org.slug}/documents/{antrag.id}/").content.decode()
    assert "Per E-Mail eingereicht" in editor and "Ratsbüro: Eingang offen" in editor
    status = client_for(autorin.user).get(_url(org, antrag)).content.decode()
    assert "Eingereicht per E-Mail" in status and "Eingang noch nicht bestätigt" in status


def test_nur_gewaehlte_eigene_kontakte(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any, make_member: Any
) -> None:
    from apps.common.tests.factories import OrganizationFactory

    fremd = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere")
    fremder = AdministrationContact.objects.create(organization=fremd, label="Fremd", email="fremd@example.org")
    antwort = _einreichen(client_for(autorin.user), org, antrag, [fremder])
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("mindestens einen Verwaltungskontakt" in t for t in texte), texte
    assert not mail.outbox and not MotionEmailSubmission.objects.exists()

    _einreichen(client_for(autorin.user), org, antrag, kontakte[:1])
    assert [m.to[0] for m in mail.outbox if m.to[0].endswith("stadt.example")] == ["ratsbuero@stadt.example"]


def test_session_verbindung_hat_vorrang(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    _token, raw = SessionAPIToken.create_token(tenant, "Fraktion Test", can_submit_applications=True)
    ris_submission.connect_with_token(org, raw, autorin)

    seite = client_for(autorin.user).get(_url(org, antrag)).content.decode()
    assert 'name="channel" value="email"' not in seite and "Antrag an Stadt Musterstadt" in seite
    antwort = _einreichen(client_for(autorin.user), org, antrag, kontakte)
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("mit mandari Session verbunden" in t for t in texte), texte
    assert not mail.outbox and not MotionEmailSubmission.objects.exists()


def test_scheitert_der_versand_ueberall_bleibt_nichts_gespeichert(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    with mock.patch("apps.common.org_email.send_org_email", side_effect=RuntimeError("smtp kaputt GEHEIM")):
        antwort = _einreichen(client_for(autorin.user), org, antrag, kontakte)
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("konnte nicht versendet werden" in t for t in texte), texte
    assert not any("GEHEIM" in t for t in texte)
    assert not MotionEmailSubmission.objects.exists()
    antrag.refresh_from_db()
    assert antrag.status == "approved"


def test_teilweiser_versandfehler_wird_vermerkt(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    from apps.common import org_email

    echt = org_email.send_org_email

    def einmal_kaputt(organization: Any, **kwargs: Any) -> bool:
        if kwargs["to"] == ["ob@stadt.example"]:
            raise RuntimeError("abgelehnt")
        return bool(echt(organization, **kwargs))

    with mock.patch("apps.common.org_email.send_org_email", side_effect=einmal_kaputt):
        antwort = _einreichen(client_for(autorin.user), org, antrag, kontakte)
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("Nicht zugestellt an: OB-Büro" in t for t in texte), texte
    einreichung = MotionEmailSubmission.objects.get(motion=antrag)
    assert {(r.label, r.delivered) for r in einreichung.recipients.all()} == {("Ratsbüro", True), ("OB-Büro", False)}


def test_zu_grosse_anhaenge_werden_vor_dem_versand_abgelehnt(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    with mock.patch.object(email_submission, "EMAIL_ATTACHMENTS_MAX_BYTES", 30):
        antwort = _einreichen(client_for(autorin.user), org, antrag, kontakte)
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("zusammen größer als" in t for t in texte), texte
    assert not mail.outbox and not MotionEmailSubmission.objects.exists()


def test_nur_einmal_und_nur_mit_recht(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any, make_member: Any
) -> None:
    ohne_recht = make_member(org, ["dashboard.view", "motions.view", "motions.edit"])
    antwort = _einreichen(client_for(ohne_recht.user), org, antrag, kontakte)
    assert antwort.status_code in (302, 403)
    assert not MotionEmailSubmission.objects.exists()

    _einreichen(client_for(autorin.user), org, antrag, kontakte)
    assert MotionEmailSubmission.objects.count() == 1
    mail.outbox.clear()
    with pytest.raises(ris_submission.SubmissionError, match="bereits per E-Mail"):
        email_submission.submit_by_email(
            antrag, autorin, contact_ids=[str(kontakte[0].pk)], subject="Nochmal", message=""
        )
    assert not mail.outbox


# =============================================================================
# Eingangsbestätigung
# =============================================================================


def _empfaenger(org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any) -> MotionEmailRecipient:
    _einreichen(client_for(autorin.user), org, antrag, kontakte[:1])
    return MotionEmailRecipient.objects.get(submission__motion=antrag)


def test_link_bestaetigt_erst_mit_klick_und_benachrichtigt(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    empfaenger = _empfaenger(org, antrag, autorin, kontakte, client_for)
    url = f"/work/eingang/bestaetigen/{email_submission.make_token(empfaenger)}/"
    verwaltung = Client()

    seite = verwaltung.get(url)
    assert seite.status_code == 200 and seite["Cache-Control"] == "no-store"
    html = seite.content.decode()
    assert "Radweg Hauptstraße" in html and "Eingang bestätigen" in html
    assert "Die Verwaltung plant einen Radweg" not in html  # kein Antragsinhalt ohne Anmeldung
    empfaenger.refresh_from_db()
    assert empfaenger.confirmed_at is None  # GET bestätigt nichts

    bestaetigt = verwaltung.post(url)
    assert "Der Eingang ist bestätigt." in bestaetigt.content.decode()
    empfaenger.refresh_from_db()
    assert empfaenger.confirmed_at is not None
    assert Notification.objects.filter(recipient=autorin, title="Eingang bestätigt").count() == 1

    verwaltung.post(url)  # zweiter Klick: keine zweite Benachrichtigung
    assert Notification.objects.filter(recipient=autorin, title="Eingang bestätigt").count() == 1
    status = client_for(autorin.user).get(_url(org, antrag)).content.decode()
    assert "Eingang bestätigt am" in status


def test_ungueltige_und_ersetzte_links(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    empfaenger = _empfaenger(org, antrag, autorin, kontakte, client_for)
    token = email_submission.make_token(empfaenger)
    verwaltung = Client()

    assert verwaltung.get(f"/work/eingang/bestaetigen/{token}x/").status_code == 404
    empfaenger.confirmation_nonce = "0" * 32  # Link-Schlüssel neu gesetzt: alter Link ungültig
    empfaenger.save(update_fields=["confirmation_nonce"])
    antwort = verwaltung.post(f"/work/eingang/bestaetigen/{token}/")
    assert antwort.status_code == 404 and "Link ungültig" in antwort.content.decode()
    empfaenger.refresh_from_db()
    assert empfaenger.confirmed_at is None


# =============================================================================
# Nebenläufigkeit, Versand ohne offene Transaktion, Randfälle
# =============================================================================


def test_je_dokument_hoechstens_eine_einreichung_in_der_datenbank(antrag: Motion, autorin: Any) -> None:
    from django.db import IntegrityError, transaction

    MotionEmailSubmission.objects.create(motion=antrag, submitted_by=autorin, submitted_by_name="A", subject="Eins")
    with pytest.raises(IntegrityError), transaction.atomic():
        MotionEmailSubmission.objects.create(motion=antrag, submitted_by=autorin, submitted_by_name="A", subject="Zwei")


def test_zweiter_aufruf_waehrend_des_versands_versendet_nichts(
    antrag: Motion, autorin: Any, kontakte: list[Any]
) -> None:
    """Doppelklick oder zwei Personen: Der zweite Aufruf sieht die laufende Einreichung."""
    from apps.common import org_email

    echt = org_email.send_org_email
    zweiter: list[str] = []

    def mit_zweitem_aufruf(organization: Any, **kwargs: Any) -> bool:
        if not zweiter:
            try:
                email_submission.submit_by_email(
                    Motion.objects.get(pk=antrag.pk),
                    autorin,
                    contact_ids=[str(k.pk) for k in kontakte],
                    subject="Doppelt",
                    message="",
                )
                zweiter.append("versendet")
            except ris_submission.SubmissionError as exc:
                zweiter.append(str(exc))
        return bool(echt(organization, **kwargs))

    with mock.patch("apps.common.org_email.send_org_email", side_effect=mit_zweitem_aufruf):
        email_submission.submit_by_email(
            antrag, autorin, contact_ids=[str(k.pk) for k in kontakte], subject="Antrag", message=""
        )
    assert zweiter == [email_submission.SENDING]
    assert len([m for m in mail.outbox if m.to[0].endswith("stadt.example")]) == 2
    assert MotionEmailSubmission.objects.filter(motion=antrag).count() == 1


@pytest.mark.django_db(transaction=True)
def test_versand_laeuft_ohne_offene_transaktion(antrag: Motion, autorin: Any, kontakte: list[Any]) -> None:
    from django.db import connection

    from apps.common import org_email

    echt = org_email.send_org_email
    in_transaktion: list[bool] = []

    def beobachten(organization: Any, **kwargs: Any) -> bool:
        in_transaktion.append(connection.in_atomic_block)
        return bool(echt(organization, **kwargs))

    with mock.patch("apps.common.org_email.send_org_email", side_effect=beobachten):
        email_submission.submit_by_email(
            antrag, autorin, contact_ids=[str(k.pk) for k in kontakte], subject="Antrag", message=""
        )
    assert in_transaktion == [False, False]
    antrag.refresh_from_db()
    assert antrag.status == "submitted"


def test_abgebrochener_versuch_sperrt_nur_voruebergehend(antrag: Motion, autorin: Any, kontakte: list[Any]) -> None:
    from datetime import timedelta

    from django.utils import timezone

    haengend = MotionEmailSubmission.objects.create(
        motion=antrag, submitted_by=autorin, submitted_by_name="A", subject="Abgebrochen"
    )
    MotionEmailRecipient.objects.create(submission=haengend, label="Ratsbüro", email="ratsbuero@stadt.example")
    # Gerade begonnen: gilt als laufend, niemand versendet parallel
    with pytest.raises(ris_submission.SubmissionError, match="gerade versendet"):
        email_submission.submit_by_email(antrag, autorin, contact_ids=[str(kontakte[0].pk)], subject="X", message="")
    assert not mail.outbox

    # Nach Ablauf der Frist ohne zugestellte Mail: verworfen, neue Einreichung möglich
    MotionEmailSubmission.objects.filter(pk=haengend.pk).update(
        sent_at=timezone.now() - email_submission.SENDING_TIMEOUT - timedelta(minutes=1)
    )
    assert email_submission.latest_submission(antrag) is None
    email_submission.submit_by_email(antrag, autorin, contact_ids=[str(kontakte[0].pk)], subject="Neu", message="")
    (einreichung,) = MotionEmailSubmission.objects.filter(motion=antrag)
    assert einreichung.subject == "Neu" and einreichung.pk != haengend.pk


def test_betreff_mit_zeilenumbruch_wird_zu_einer_zeile(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    antwort = _einreichen(
        client_for(autorin.user), org, antrag, kontakte[:1], subject="Antrag Radweg\r\nBcc: fremd@example.org"
    )
    assert antwort.status_code == 302
    (nachricht,) = [m for m in mail.outbox if m.to == ["ratsbuero@stadt.example"]]
    assert nachricht.subject == "Antrag Radweg Bcc: fremd@example.org"
    assert nachricht.bcc == []
    assert MotionEmailSubmission.objects.get(motion=antrag).subject == "Antrag Radweg Bcc: fremd@example.org"


def test_session_weg_nach_einreichung_per_email_gesperrt(
    org: Any, antrag: Motion, autorin: Any, kontakte: list[Any], client_for: Any
) -> None:
    from apps.session.models import SessionApplication

    _einreichen(client_for(autorin.user), org, antrag, kontakte)
    assert MotionEmailSubmission.objects.filter(motion=antrag).exists()

    # Später eingerichtete Session-Verbindung: ein POST ohne channel=email reicht nicht erneut ein
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    _token, raw = SessionAPIToken.create_token(tenant, "Fraktion Test", can_submit_applications=True)
    ris_submission.connect_with_token(org, raw, autorin)
    antwort = client_for(autorin.user).post(
        _url(org, antrag), {**ris_submission.build_prefill(antrag), "application_type": "motion", "confirm": "on"}
    )
    assert antwort.status_code == 200
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("bereits per E-Mail eingereicht" in t for t in texte), texte
    assert not SessionApplication.objects.filter(tenant=tenant).exists()
    antrag.refresh_from_db()
    assert antrag.session_application is None


def test_groessengrenze_beruecksichtigt_base64() -> None:
    """Base64 macht Anhänge etwa 4/3 so groß; die Mail muss unter der verbreiteten 25-MB-Grenze bleiben."""
    from apps.common.uploads import MB

    assert email_submission.EMAIL_ATTACHMENTS_MAX_BYTES * 4 / 3 <= 21 * MB
