# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mail-Dienst (Issue #528): eine Konfigurationsauflösung, Versand als Auftrag, Idempotenz, Metrik.

Ohne Schalter (``MAIL_QUEUE`` leer) geht jede Mail wie bisher im Aufruf raus. Mit Schalter und
``JournalBackend`` liegt sie verschlüsselt im Postausgang, bis der Runner sie versendet.
"""

from __future__ import annotations

import logging
import re
import smtplib
from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.core import mail as django_mail
from django.core.cache import cache
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import call_command
from django.db import transaction
from django.tasks import task_backends
from django.utils import timezone
from prometheus_client import REGISTRY

from apps.common import mail
from apps.common.email_backend import SiteSettingsEmailBackend
from apps.common.mail import config, outbox
from apps.common.mail.message import Attachment, Mail, check_kind
from apps.common.mail_backends import build_backend
from apps.common.models import MailOutbox, SiteSettings
from apps.common.org_email import OrgMailError
from apps.events.models import Task as TaskRow
from apps.events.models import TaskStatus
from apps.events.task_runner import run_pending
from apps.events.tasks_backend import JournalBackend

pytestmark = pytest.mark.django_db

JOURNAL = "apps.events.tasks_backend.JournalBackend"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
MAIL_TASK = "apps.common.mail.outbox.deliver_mail"


@pytest.fixture(autouse=True)
def _systemeinstellungen_frisch() -> Iterator[None]:
    """Die Systemeinstellungen liegen im Cache; ein Test mit eigenem SMTP darf keinen anderen erreichen."""
    cache.delete(SiteSettings.CACHE_KEY)
    yield
    cache.delete(SiteSettings.CACHE_KEY)


@pytest.fixture
def journal(settings: Any) -> JournalBackend:
    """Wie in Produktion mit ``TASKS_BACKEND=journal`` und ``MAIL_QUEUE=*``."""
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": JOURNAL}}
    settings.MAIL_QUEUE = ["*"]
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    return backend


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _eigenes_smtp(org: Any, *, ersatzweg: bool = True) -> None:
    org.mail_sender_mode = "smtp"
    org.smtp_host = "smtp.example.org"
    org.smtp_from_email = "fraktion@example.org"
    org.smtp_from_name = "Fraktion Test"
    org.smtp_fallback_to_mandari = ersatzweg
    org.save()


class Kaputt(BaseEmailBackend):
    """SMTP nicht erreichbar."""

    def send_messages(self, email_messages: Any) -> int:
        raise OSError("SMTP nicht erreichbar")


def _senden(**angaben: Any) -> bool:
    werte: dict[str, Any] = {
        "kind": "konto.passwort",
        "subject": "Betreff GEHEIM",
        "body": "Text mit Link GEHEIM",
        "to": ["empfang@example.org"],
    }
    werte.update(angaben)
    return mail.send(**werte)


# =============================================================================
# Konfigurationsauflösung
# =============================================================================


def test_plattform_aus_systemeinstellungen_sonst_aus_der_umgebung(settings: Any) -> None:
    settings.SMTP_FALLBACK = {**settings.SMTP_FALLBACK, "host": "env.example", "username": "env-user"}
    settings.MAIL_BACKEND = LOCMEM
    umgebung = config.platform_config()
    assert (umgebung.backend, umgebung.host, umgebung.username) == (LOCMEM, "env.example", "env-user")

    site = SiteSettings.get_settings()
    site.email_host = "site.example"
    site.email_port = 2525
    site.default_from_email = "absender@example.org"
    site.default_from_name = "Plattform"
    site.save()
    plattform = config.platform_config()
    assert (plattform.host, plattform.port, plattform.username) == ("site.example", 2525, "env-user")
    assert config.platform_from_email() == "Plattform <absender@example.org>"

    # Systemeinstellungen und Djangos eigenes Backend lesen dieselbe Auflösung
    assert SiteSettings.get_email_config()["EMAIL_HOST"] == "site.example"
    assert SiteSettingsEmailBackend()._get_config()["port"] == 2525  # type: ignore[no-untyped-call]


def test_weg_der_organisation_nur_wenn_gewaehlt_und_erlaubt(org: Any) -> None:
    assert config.resolve(org).name == config.PLATTFORM
    _eigenes_smtp(org)
    weg = config.resolve(org)
    assert weg.name == config.ORGANISATION and weg.fallback is not None
    assert weg.fallback.name == config.ERSATZWEG
    assert config.resolve(org, via_organization=False).name == config.PLATTFORM

    _eigenes_smtp(org, ersatzweg=False)
    assert config.resolve(org).fallback is None


def test_mailart_wird_geprueft() -> None:
    assert check_kind("work.fraktion.einladung") == "work.fraktion.einladung"
    for falsch in ("", "Work.x", "work..x", "work.x-y", "a.b.c.d.e"):
        with pytest.raises(ValueError):
            check_kind(falsch)


def test_mail_ueberlebt_die_speicherung() -> None:
    nachricht = Mail(
        subject="Ä",
        body="Text",
        to=("a@example.org",),
        html_body="<p>HTML</p>",
        reply_to=("r@example.org",),
        attachments=(Attachment.of(("sitzung.ics", "BEGIN:VCALENDAR", "text/calendar")),),
    )
    assert Mail.from_bytes(nachricht.to_bytes()) == nachricht


# =============================================================================
# Sofort (Standard, Rückweg)
# =============================================================================


def test_ohne_schalter_geht_die_mail_sofort_raus() -> None:
    vorher = _sample("mandari_mail_total", kind="konto.passwort", route="plattform", result="sent")

    assert _senden() is True

    assert [m.to for m in django_mail.outbox] == [["empfang@example.org"]]
    assert not MailOutbox.objects.exists() and not TaskRow.objects.exists()
    assert _sample("mandari_mail_total", kind="konto.passwort", route="plattform", result="sent") == vorher + 1


def test_schalter_ohne_journal_bleibt_sofort(settings: Any) -> None:
    settings.MAIL_QUEUE = ["*"]
    assert _senden() is True
    assert len(django_mail.outbox) == 1 and not MailOutbox.objects.exists()


def test_eigenes_smtp_mit_ersatzweg(org: Any) -> None:
    _eigenes_smtp(org)
    with mock.patch("apps.common.mail.config.organization_backend", return_value=Kaputt()):
        assert _senden(kind="work.zugang.invitation", organization=org) is True
    [nachricht] = django_mail.outbox
    assert "fraktion@example.org" not in nachricht.from_email


def test_eigenes_smtp_ohne_ersatzweg_meldet_den_fehler(org: Any) -> None:
    _eigenes_smtp(org, ersatzweg=False)
    with mock.patch("apps.common.mail.config.organization_backend", return_value=Kaputt()):
        with pytest.raises(OrgMailError):
            _senden(kind="work.testmail", organization=org)
        assert _senden(kind="work.testmail", organization=org, fail_silently=True) is False
    assert not django_mail.outbox


# =============================================================================
# Als Auftrag
# =============================================================================


def test_mit_schalter_liegt_die_mail_verschluesselt_im_postausgang(journal: JournalBackend) -> None:
    assert _senden() is True

    assert not django_mail.outbox, "nicht mehr im Aufruf"
    zeile = MailOutbox.objects.get()
    assert zeile.status == MailOutbox.Status.WARTEND and zeile.organization is None
    roh = bytes(zeile.payload_platform_encrypted or b"")
    assert b"GEHEIM" not in roh and b"empfang@example.org" not in roh
    assert zeile.payload_encrypted is None
    auftrag = TaskRow.objects.get()
    assert (auftrag.queue, auftrag.task_path, auftrag.args["args"]) == ("mail", MAIL_TASK, [str(zeile.pk)])

    assert run_pending() == 1
    [nachricht] = django_mail.outbox
    assert nachricht.to == ["empfang@example.org"] and nachricht.subject == "Betreff GEHEIM"
    zeile.refresh_from_db()
    assert zeile.status == MailOutbox.Status.VERSENDET and zeile.route == config.PLATTFORM
    assert zeile.payload_platform_encrypted is None and zeile.finished_at is not None


def test_zurueckgerollt_entsteht_weder_mail_noch_auftrag(journal: JournalBackend) -> None:
    class AbbruchError(Exception):
        pass

    with pytest.raises(AbbruchError), transaction.atomic():
        _senden()
        raise AbbruchError

    assert not MailOutbox.objects.exists() and not TaskRow.objects.exists()
    assert run_pending() == 0 and not django_mail.outbox


def test_gleicher_idempotenzschluessel_ergibt_eine_mail(journal: JournalBackend) -> None:
    for _ in range(3):
        assert _senden(idempotency_key="ereignis-1:empfaenger-1") is True

    assert MailOutbox.objects.count() == 1 and TaskRow.objects.count() == 1
    assert run_pending() == 1
    assert len(django_mail.outbox) == 1
    # Auch nach dem Versand bleibt es bei der einen Mail
    assert _senden(idempotency_key="ereignis-1:empfaenger-1") is True
    assert run_pending() == 0 and len(django_mail.outbox) == 1


def test_wiederholter_auftrag_versendet_nicht_zweimal(journal: JournalBackend) -> None:
    _senden()
    zeile = MailOutbox.objects.get()
    assert run_pending() == 1
    outbox.deliver_mail.enqueue(str(zeile.pk))
    assert run_pending() == 1
    assert len(django_mail.outbox) == 1


def test_smtp_fehler_wird_wiederholt(journal: JournalBackend) -> None:
    _senden()
    with mock.patch("apps.common.mail.delivery.send_with", side_effect=OSError("SMTP nicht erreichbar")):
        assert run_pending() == 1
    auftrag = TaskRow.objects.get()
    assert (auftrag.status, auftrag.attempts) == (TaskStatus.WARTEND, 1)
    zeile = MailOutbox.objects.get()
    assert (zeile.status, zeile.attempts, zeile.error_code) == (MailOutbox.Status.WARTEND, 1, "OSError")

    TaskRow.objects.update(run_after=timezone.now())
    assert run_pending() == 1
    assert len(django_mail.outbox) == 1
    zeile.refresh_from_db()
    assert (zeile.status, zeile.attempts) == (MailOutbox.Status.VERSENDET, 2)


@pytest.mark.parametrize(
    "fehler",
    [
        smtplib.SMTPRecipientsRefused({"empfang@example.org": (451, b"greylisted")}),
        smtplib.SMTPRecipientsRefused({"empfang@example.org": (421, b"zu viele Verbindungen")}),
        smtplib.SMTPRecipientsRefused({"empfang@example.org": (550, b"unbekannt"), "b@example.org": (451, b"spaeter")}),
        smtplib.SMTPSenderRefused(421, b"Dienst nicht verfuegbar", "absender@example.org"),
        smtplib.SMTPDataError(451, b"lokaler Fehler"),
    ],
    ids=["greylisting-451", "ratenbegrenzung-421", "gemischt", "absender-421", "daten-451"],
)
def test_vorlaeufige_ablehnung_wird_wiederholt(journal: JournalBackend, fehler: Exception) -> None:
    """4xx ist vorübergehend: kein Mailverlust, die Mail wartet mit Inhalt auf den nächsten Versuch."""
    _senden()
    with mock.patch("apps.common.mail.delivery.send_with", side_effect=fehler):
        assert run_pending() == 1
    assert TaskRow.objects.get().status == TaskStatus.WARTEND
    zeile = MailOutbox.objects.get()
    assert zeile.status == MailOutbox.Status.WARTEND and zeile.payload_platform_encrypted


def test_protokolle_ohne_empfaengeradressen(journal: JournalBackend, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _senden(to=["geheim@example.org"])
    abgelehnt = smtplib.SMTPRecipientsRefused({"geheim@example.org": (451, b"greylisted geheim@example.org")})
    with mock.patch("apps.common.mail.delivery.send_with", side_effect=abgelehnt):
        run_pending()
        assert _senden(to=["geheim@example.org"], kind="konto.sicherheit", sofort=True, fail_silently=True) is False
    assert "geheim@example.org" not in caplog.text
    assert "SMTPRecipientsRefused 451" in caplog.text


def test_eigenes_smtp_ohne_ersatzweg_bleibt_im_aufruf(journal: JournalBackend, org: Any) -> None:
    """#65: Scheitern ohne Ersatzweg muss der Auslöser sehen – auch mit Schalter kein Postausgang."""
    _eigenes_smtp(org, ersatzweg=False)
    with mock.patch("apps.common.mail.config.organization_backend", return_value=Kaputt()):
        with pytest.raises(OrgMailError):
            _senden(kind="work.zugang.invitation", organization=org)
        assert _senden(kind="work.zugang.invitation", organization=org, fail_silently=True) is False
    assert not MailOutbox.objects.exists() and not TaskRow.objects.exists()

    _eigenes_smtp(org, ersatzweg=True)
    assert _senden(kind="work.zugang.invitation", organization=org) is True
    assert MailOutbox.objects.count() == 1, "mit Ersatzweg wie jede andere Mail als Auftrag"


def test_empfaenger_als_zeichenkette_wird_abgelehnt() -> None:
    # Programmierfehler, kein Versandfehler: auch mit fail_silently eine Ausnahme
    with pytest.raises(TypeError):
        _senden(to="empfang@example.org", fail_silently=True)
    with pytest.raises(TypeError):
        _senden(reply_to="antwort@example.org", fail_silently=True)
    assert not django_mail.outbox


def test_verwaiste_zeilen_neu_einreihen_oder_verwerfen(journal: JournalBackend) -> None:
    _senden(to=["eins@example.org"])
    _senden(to=["zwei@example.org"])
    # Ein älterer Worker kannte den Auftrag nicht und hat ihn aufgegeben
    TaskRow.objects.update(status=TaskStatus.TOT)
    MailOutbox.objects.update(created_at=timezone.now() - timedelta(hours=1))
    ausgabe = StringIO()
    call_command("postausgang", stdout=ausgabe)
    assert "verwaist: 2" in ausgabe.getvalue() and "@" not in ausgabe.getvalue()

    eins = MailOutbox.objects.order_by("created_at").first()
    assert eins is not None
    outbox.neu_einreihen([eins])
    assert run_pending() == 1
    assert [m.to for m in django_mail.outbox] == [["eins@example.org"]]

    call_command("postausgang", "--verwerfen", stdout=StringIO())
    zwei = MailOutbox.objects.exclude(pk=eins.pk).get()
    assert zwei.status == MailOutbox.Status.FEHLGESCHLAGEN and zwei.payload_platform_encrypted is None
    assert outbox.verwaist(timedelta(0)) == []


def test_compose_reicht_die_groessengrenze_durch() -> None:
    compose = (QUELLE.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "MAIL_QUEUE: ${MAIL_QUEUE:-}" in compose
    assert "MAIL_QUEUE_MAX_BYTES: ${MAIL_QUEUE_MAX_BYTES:-" in compose


def test_abgelehnte_empfaenger_sind_endgueltig(journal: JournalBackend) -> None:
    _senden()
    abgelehnt = smtplib.SMTPRecipientsRefused({"empfang@example.org": (550, b"unbekannt")})
    with mock.patch("apps.common.mail.delivery.send_with", side_effect=abgelehnt):
        assert run_pending() == 1
    assert TaskRow.objects.get().status == TaskStatus.FEHLGESCHLAGEN
    zeile = MailOutbox.objects.get()
    assert zeile.status == MailOutbox.Status.FEHLGESCHLAGEN and zeile.payload_platform_encrypted is None


def test_nach_dem_letzten_versuch_fehlgeschlagen(journal: JournalBackend, settings: Any) -> None:
    settings.TASKS = {"default": {**settings.TASKS["default"], "OPTIONS": {"tasks": {MAIL_TASK: {"max_attempts": 1}}}}}
    _senden()
    with mock.patch("apps.common.mail.delivery.send_with", side_effect=OSError("weg")):
        assert run_pending() == 1
    assert TaskRow.objects.get().status == TaskStatus.TOT
    zeile = MailOutbox.objects.get()
    assert zeile.status == MailOutbox.Status.FEHLGESCHLAGEN and zeile.payload_platform_encrypted is None


def test_mail_der_organisation_mit_mandantenschluessel_und_ihrem_weg(journal: JournalBackend, org: Any) -> None:
    _eigenes_smtp(org)
    pdf = b"%PDF-1.7 nicht oeffentlicher Teil"
    assert _senden(
        kind="work.fraktion.einladung", organization=org, attachments=[("tagesordnung.pdf", pdf, "application/pdf")]
    )
    zeile = MailOutbox.objects.get()
    assert zeile.payload_platform_encrypted is None and zeile.payload_encrypted
    assert b"nicht oeffentlicher" not in bytes(zeile.payload_encrypted)

    with mock.patch("apps.common.mail.config.organization_backend", return_value=build_backend(LOCMEM)) as verbindung:
        assert run_pending() == 1
    verbindung.assert_called_once()
    [nachricht] = django_mail.outbox
    assert nachricht.from_email == "Fraktion Test <fraktion@example.org>"
    assert nachricht.attachments[0][1] == pdf
    zeile.refresh_from_db()
    assert zeile.route == config.ORGANISATION


def test_ersatzweg_im_auftrag(journal: JournalBackend, org: Any) -> None:
    _eigenes_smtp(org)
    vorher = _sample("mandari_mail_total", kind="work.zugang.invitation", route="ersatzweg", result="sent")
    _senden(kind="work.zugang.invitation", organization=org)
    with mock.patch("apps.common.mail.config.organization_backend", return_value=Kaputt()):
        assert run_pending() == 1
    assert len(django_mail.outbox) == 1
    assert MailOutbox.objects.get().route == config.ERSATZWEG
    assert _sample("mandari_mail_total", kind="work.zugang.invitation", route="ersatzweg", result="sent") == vorher + 1


def test_passwort_links_nie_ueber_fremdes_smtp(journal: JournalBackend, org: Any) -> None:
    _eigenes_smtp(org)
    _senden(kind="work.zugang.guest_access", organization=org, via_organization=False)
    with mock.patch("apps.common.mail.config.organization_backend") as verbindung:
        assert run_pending() == 1
    verbindung.assert_not_called()
    assert "fraktion@example.org" not in django_mail.outbox[0].from_email


def test_schalter_je_mailart(journal: JournalBackend, settings: Any) -> None:
    settings.MAIL_QUEUE = ["work.*"]
    assert mail.queue_enabled("work.fraktion.einladung") and not mail.queue_enabled("konto.passwort")
    _senden(kind="konto.passwort")
    assert len(django_mail.outbox) == 1 and not MailOutbox.objects.exists()
    _senden(kind="work.benachrichtigung")
    assert len(django_mail.outbox) == 1 and MailOutbox.objects.count() == 1


def test_sofort_und_grosse_mails_umgehen_den_postausgang(journal: JournalBackend, settings: Any) -> None:
    _senden(kind="work.testmail", sofort=True)
    settings.MAIL_QUEUE_MAX_BYTES = 10
    _senden(attachments=[("gross.bin", b"x" * 100, "application/octet-stream")])
    assert len(django_mail.outbox) == 2 and not MailOutbox.objects.exists()


def test_unlesbarer_inhalt_ist_endgueltig(journal: JournalBackend) -> None:
    _senden()
    MailOutbox.objects.update(payload_platform_encrypted=b"x" * 40)
    assert run_pending() == 1
    assert TaskRow.objects.get().status == TaskStatus.FEHLGESCHLAGEN
    assert MailOutbox.objects.get().status == MailOutbox.Status.FEHLGESCHLAGEN
    assert not django_mail.outbox


def test_aufbewahrung(journal: JournalBackend) -> None:
    jetzt = timezone.now()
    for status, alter in (
        (MailOutbox.Status.VERSENDET, 15),
        (MailOutbox.Status.VERSENDET, 13),
        (MailOutbox.Status.FEHLGESCHLAGEN, 91),
        (MailOutbox.Status.FEHLGESCHLAGEN, 89),
    ):
        MailOutbox.objects.create(kind="test", status=status, finished_at=jetzt - timedelta(days=alter))
    alt_wartend = MailOutbox.objects.create(kind="test")
    MailOutbox.objects.filter(pk=alt_wartend.pk).update(created_at=jetzt - timedelta(days=91))

    assert outbox.purge(now=jetzt) == 3
    assert MailOutbox.objects.count() == 2


# =============================================================================
# Fitnessfunktion: ein Einstieg
# =============================================================================

QUELLE = Path(__file__).resolve().parents[3]
#: Direkter Versand an Django vorbei; erlaubt nur im Mail-Dienst selbst
VERBOTEN = (
    re.compile(r"from django\.core\.mail import"),
    re.compile(r"from django\.core import mail\b"),
    re.compile(r"import django\.core\.mail\b"),
    re.compile(r"\bEmailMessage\("),
    re.compile(r"\bEmailMultiAlternatives\("),
    re.compile(r"\.send_messages\("),
    re.compile(r"\bsend_with\("),
)
ERLAUBT = {
    Path("apps/common/mail/message.py"),
    Path("apps/common/mail/delivery.py"),
    Path("apps/common/mail_backends.py"),
}
#: Mails, die bewusst im Aufruf rausgehen, weil die Oberfläche bzw. der Vorgang ihr Ergebnis braucht
SOFORT = {
    Path("apps/common/admin.py"): 1,  # Testmail der Systemeinstellungen
    Path("apps/work/organization/services.py"): 1,  # Testmail der Organisation
    Path("apps/work/motions/email_submission.py"): 1,  # Einreichung: Zustellung je Empfänger vermerkt
    Path("apps/session/services/invitation_response_service.py"): 1,  # Ladung: Zustellung je Empfänger
    Path("apps/work/background_tasks.py"): 1,  # Benachrichtigung: läuft schon als Auftrag
}


def _quellen() -> list[Path]:
    return [
        pfad.relative_to(QUELLE)
        for pfad in QUELLE.rglob("*.py")
        if "tests" not in pfad.parts and "migrations" not in pfad.parts and "tests_e2e" not in pfad.parts
    ]


def test_mails_gehen_nur_ueber_den_mail_dienst() -> None:
    funde = []
    for pfad in _quellen():
        if pfad in ERLAUBT or pfad.parts[:3] == ("apps", "common", "mail"):
            continue
        text = (QUELLE / pfad).read_text(encoding="utf-8")
        funde += [f"{pfad}: {muster.pattern}" for muster in VERBOTEN if muster.search(text)]
    assert funde == [], "Mails nur über apps.common.mail.send versenden"


def test_mails_im_aufruf_bleiben_die_ausnahme() -> None:
    gefunden = {}
    for pfad in _quellen():
        anzahl = (QUELLE / pfad).read_text(encoding="utf-8").count("sofort=True")
        if anzahl and pfad.parts[:3] != ("apps", "common", "mail"):
            gefunden[pfad] = anzahl
    assert gefunden == SOFORT
