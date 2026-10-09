# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Message-ID und EHLO mit vollständigem Domainnamen (Issue #957).

Im Container liefert ``socket.getfqdn()`` die Container-ID; Django nähme sie für die Message-ID und als
EHLO-Namen. Die Tests simulieren das und schicken Mails über die üblichen Wege an einen SMTP-Server ohne
Netz (``smtplib`` mit echtem Befehlsablauf), der EHLO und Nachricht aufzeichnet.

Die Tests zu ``DNS_NAME`` brechen, wenn Django das interne Attribut ``_fqdn`` oder die Bildung der
Message-ID ändert – dann ``apps.common.mail_domain.apply`` an die neue Django-Version anpassen.
"""

from __future__ import annotations

import email
import json
import os
import smtplib
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from email.message import Message
from io import StringIO
from typing import Any, ClassVar, cast

import pytest
from django.core import checks
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.core.mail import message as django_message
from django.core.mail import utils as django_mail_utils
from django.core.mail.backends import smtp as django_smtp
from django.core.management import call_command
from django.test import Client

from apps.common import mail, mail_domain
from apps.common.email_backend import SiteSettingsEmailBackend
from apps.common.models import SiteSettings

#: So sieht der Rechnername in einem Container aus
CONTAINER_ID = "a1b2c3d4e5f6"
DOMAIN = "absender.example"


class AufzeichnenderServer(smtplib.SMTP):
    """``smtplib`` ohne Netz: echter Befehlsablauf (EHLO, MAIL, RCPT, DATA), jede Antwort ist ein Erfolg."""

    verbindungen: ClassVar[list[AufzeichnenderServer]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.gesendet: list[bytes] = []
        AufzeichnenderServer.verbindungen.append(self)
        super().__init__(*args, **kwargs)

    def connect(self, host: str = "localhost", port: int = 0, source_address: Any = None) -> tuple[int, bytes]:
        return 220, b"bereit"

    def send(self, s: str | bytes) -> None:  # type: ignore[override]
        self.gesendet.append(s.encode("ascii") if isinstance(s, str) else bytes(s))

    def getreply(self) -> tuple[int, bytes]:
        befehl = self.gesendet[-1].split(b" ", 1)[0].strip().lower() if self.gesendet else b""
        if befehl == b"data":
            return 354, b"weiter"
        if befehl == b"quit":
            return 221, b"ende"
        return 250, b"ok"

    @property
    def ehlo_zeilen(self) -> list[bytes]:
        return [zeile for zeile in self.gesendet if zeile.lower().startswith((b"ehlo ", b"helo "))]

    @property
    def nachrichten(self) -> list[Message]:
        """Die Nachrichten, die nach ``DATA`` übertragen wurden."""
        ergebnis = []
        for vorher, zeile in zip(self.gesendet, self.gesendet[1:], strict=False):
            if vorher.strip().lower() == b"data":
                ergebnis.append(email.message_from_bytes(zeile))
        return ergebnis


@pytest.fixture
def dns_name_sichern() -> Iterator[None]:
    """``DNS_NAME`` gilt für den ganzen Prozess; jeder Test hinterlässt ihn wie vorgefunden."""
    vorher = dict(vars(django_mail_utils.DNS_NAME))
    yield
    vars(django_mail_utils.DNS_NAME).clear()
    vars(django_mail_utils.DNS_NAME).update(vorher)


@pytest.fixture
def im_container(settings: Any, monkeypatch: pytest.MonkeyPatch, dns_name_sichern: None) -> str:
    """Rechnername wie im Container, Absender ``noreply@absender.example``, Domain wie beim Start gesetzt."""
    monkeypatch.setattr(socket, "getfqdn", lambda *args: CONTAINER_ID)
    vars(django_mail_utils.DNS_NAME).pop("_fqdn", None)
    settings.EMAIL_MESSAGE_ID_DOMAIN = ""
    settings.DEFAULT_FROM_EMAIL = f"noreply@{DOMAIN}"
    return mail_domain.apply()


@pytest.fixture
def smtp_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[AufzeichnenderServer]]:
    """Jede SMTP-Verbindung geht an den aufzeichnenden Server; liefert die Verbindungen."""
    AufzeichnenderServer.verbindungen = []
    monkeypatch.setattr(smtplib, "SMTP", AufzeichnenderServer)
    cache.delete(SiteSettings.CACHE_KEY)
    yield AufzeichnenderServer.verbindungen
    cache.delete(SiteSettings.CACHE_KEY)


def _pruefen(verbindungen: list[AufzeichnenderServer], anzahl: int = 1, domain: str = DOMAIN) -> None:
    """``anzahl`` Verbindungen mit je einer Mail: EHLO mit ``domain``, Message-ID endet auf ``@domain>``."""
    assert len(verbindungen) == anzahl
    for verbindung in verbindungen:
        assert verbindung.local_hostname == domain
        assert verbindung.ehlo_zeilen == [f"ehlo {domain}\r\n".encode("ascii")]
        [nachricht] = verbindung.nachrichten
        message_id = str(nachricht["Message-ID"])
        assert message_id.endswith(f"@{domain}>"), message_id
        assert CONTAINER_ID not in message_id


# =============================================================================
# Domain bestimmen
# =============================================================================


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("Mandari.DE.", "mandari.de"),
        ("  mail.example.org ", "mail.example.org"),
        ("bürger.example", "xn--brger-kva.example"),
        ("", ""),
        ("a..b", ""),
    ],
)
def test_normalize(eingabe: str, erwartet: str) -> None:
    assert mail_domain.normalize(eingabe) == erwartet


@pytest.mark.parametrize(
    ("name", "erwartet"),
    [
        ("mandari.de", True),
        ("mail.example.org", True),
        ("xn--brger-kva.example", True),
        (CONTAINER_ID, False),
        ("localhost", False),
        ("10.0.0.1", False),
        ("-mandari.de", False),
        ("mandari_de.example", False),
        ("x" * 64 + ".de", False),
        ("", False),
    ],
)
def test_is_fqdn(name: str, erwartet: bool) -> None:
    assert mail_domain.is_fqdn(name) is erwartet


def test_domain_von_der_absenderadresse() -> None:
    assert mail_domain.domain_of("noreply@Mandari.de") == "mandari.de"
    assert mail_domain.domain_of("mandari <noreply@mandari.de>") == "mandari.de"
    assert mail_domain.domain_of("ohne-at") == ""


def test_einstellung_vor_absender_vor_site_url(settings: Any) -> None:
    settings.EMAIL_MESSAGE_ID_DOMAIN = "Mail.Example.org"
    settings.DEFAULT_FROM_EMAIL = "Plattform <noreply@absender.example>"
    settings.SITE_URL = "https://portal.example"
    assert mail_domain.configured_domain() == "mail.example.org"

    settings.EMAIL_MESSAGE_ID_DOMAIN = ""
    assert mail_domain.configured_domain() == "absender.example"

    settings.DEFAULT_FROM_EMAIL = "noreply@localhost"
    assert mail_domain.configured_domain() == "portal.example"


def test_ohne_vollstaendigen_namen_nie_der_rechnername(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getfqdn", lambda *args: CONTAINER_ID)
    settings.EMAIL_MESSAGE_ID_DOMAIN = ""
    settings.DEFAULT_FROM_EMAIL = "noreply@localhost"
    settings.SITE_URL = "http://localhost:8000"
    assert mail_domain.configured_domain() == "localhost"

    settings.DEFAULT_FROM_EMAIL = ""
    settings.SITE_URL = ""
    assert mail_domain.configured_domain() == mail_domain.NOTNAME


def test_ungueltige_einstellung_faellt_auf_den_absender_zurueck(
    settings: Any, dns_name_sichern: None, caplog: pytest.LogCaptureFixture
) -> None:
    settings.EMAIL_MESSAGE_ID_DOMAIN = "nur-ein-name"
    settings.DEFAULT_FROM_EMAIL = f"noreply@{DOMAIN}"
    assert mail_domain.apply() == DOMAIN
    assert "EMAIL_MESSAGE_ID_DOMAIN" in caplog.text


def test_systempruefung(settings: Any) -> None:
    settings.DEFAULT_FROM_EMAIL = f"noreply@{DOMAIN}"
    settings.EMAIL_MESSAGE_ID_DOMAIN = ""
    assert mail_domain.check_message_id_domain() == []

    settings.EMAIL_MESSAGE_ID_DOMAIN = "mail.example.org"
    assert mail_domain.check_message_id_domain() == []

    # Nur Warnungen: Fehler hielten jeden Verwaltungsbefehl an, auch die Worker
    settings.EMAIL_MESSAGE_ID_DOMAIN = "noreply@mail.example.org"
    meldungen = mail_domain.check_message_id_domain()
    assert [m.id for m in meldungen] == ["common.W001"] and all(m.level == checks.WARNING for m in meldungen)
    assert DOMAIN in meldungen[0].msg

    settings.EMAIL_MESSAGE_ID_DOMAIN = ""
    settings.DEFAULT_FROM_EMAIL = "noreply@localhost"
    settings.SITE_URL = "http://localhost:8000"
    assert [m.id for m in mail_domain.check_message_id_domain()] == ["common.W002"]

    settings.EMAIL_MESSAGE_ID_DOMAIN = "10.0.0.1"
    assert [m.id for m in mail_domain.check_message_id_domain()] == ["common.W001", "common.W002"]


# =============================================================================
# Django-Interna: brechen, wenn Django die Bildung von Message-ID oder EHLO ändert
# =============================================================================


def test_django_bildet_message_id_und_ehlo_aus_dns_name() -> None:
    """``message.py`` und das SMTP-Backend nutzen dasselbe Objekt, das ``apply`` belegt."""
    assert vars(django_message)["DNS_NAME"] is django_mail_utils.DNS_NAME
    assert vars(django_smtp)["DNS_NAME"] is django_mail_utils.DNS_NAME
    assert type(django_mail_utils.DNS_NAME).__name__ == "CachedDnsName"


def test_django_merkt_den_namen_in_fqdn(im_container: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Belegtes ``_fqdn`` ersetzt die Abfrage von ``socket.getfqdn()`` vollständig."""

    def nie_fragen(*args: Any) -> str:
        pytest.fail("socket.getfqdn() darf nach apply() nicht mehr gefragt werden")

    monkeypatch.setattr(socket, "getfqdn", nie_fragen)
    assert im_container == DOMAIN
    assert vars(django_mail_utils.DNS_NAME)["_fqdn"] == DOMAIN
    assert django_mail_utils.DNS_NAME.get_fqdn() == DOMAIN
    assert str(django_mail_utils.DNS_NAME) == DOMAIN
    message_id = str(EmailMessage("Betreff", "Text", "von@example.org", ["an@example.org"]).message()["Message-ID"])
    assert message_id.endswith(f"@{DOMAIN}>")


def test_beim_start_gesetzt() -> None:
    """``CommonConfig.ready`` hat den Namen in diesem Prozess schon gesetzt."""
    assert django_mail_utils.DNS_NAME.get_fqdn() == mail_domain.configured_domain()
    assert mail_domain.is_fqdn(django_mail_utils.DNS_NAME.get_fqdn())


def test_jeder_prozess_setzt_den_namen_beim_start(settings: Any) -> None:
    """Ein frischer Prozess (wie Anwendung, Worker, Verwaltungsbefehl) mit Rechnernamen wie im Container."""
    code = (
        "import socket\n"
        f"socket.getfqdn = lambda *args: {CONTAINER_ID!r}\n"
        "import django\n"
        "django.setup()\n"
        "from django.core.mail import EmailMessage\n"
        "from django.core.mail.utils import DNS_NAME\n"
        "nachricht = EmailMessage('Betreff', 'Text', 'von@example.org', ['an@example.org']).message()\n"
        "print(DNS_NAME.get_fqdn(), nachricht['Message-ID'])\n"
    )
    umgebung = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "mandari.settings_test",
        "EMAIL_MESSAGE_ID_DOMAIN": "mail.example.org",
    }
    ergebnis = subprocess.run(
        [sys.executable, "-c", code],
        cwd=settings.BASE_DIR,
        env=umgebung,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert ergebnis.returncode == 0, ergebnis.stderr[-2000:]
    ehlo, message_id = ergebnis.stdout.strip().splitlines()[-1].split(" ", 1)
    assert ehlo == "mail.example.org"
    assert message_id.endswith("@mail.example.org>") and CONTAINER_ID not in message_id


# =============================================================================
# Versandwege: Message-ID und EHLO bei echter SMTP-Unterhaltung
# =============================================================================


@pytest.mark.django_db
def test_plattform_ueber_systemeinstellungen(im_container: str, smtp_server: list[AufzeichnenderServer]) -> None:
    site = SiteSettings.get_settings()
    site.email_host = "smtp.example.org"
    site.email_port = 587
    site.email_use_tls = False
    site.save()

    assert mail.send(kind="konto.passwort", subject="Betreff", body="Text", to=["an@example.org"]) is True
    _pruefen(smtp_server)


@pytest.fixture
def smtp_aus_der_umgebung(settings: Any) -> None:
    """Plattform ohne Systemeinstellungen: SMTP-Zugang aus ``EMAIL_*`` (``SMTP_FALLBACK``)."""
    settings.MAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.SMTP_FALLBACK = {**settings.SMTP_FALLBACK, "host": "smtp.example.org", "use_tls": False}


@pytest.mark.django_db
def test_kontaktformular(
    im_container: str, smtp_server: list[AufzeichnenderServer], smtp_aus_der_umgebung: None
) -> None:
    """Benachrichtigung ans Team und Bestätigung an die Absenderin (``mail.send_template``)."""
    daten = {"name": "Erika", "email": "erika@example.org", "subject": "demo", "message": "Gibt es eine Demo?"}
    antwort = Client().post("/api/contact/", json.dumps(daten), content_type="application/json")

    assert antwort.status_code == 201
    _pruefen(smtp_server, anzahl=2)
    assert all(v.nachrichten[0].is_multipart() for v in smtp_server), "HTML-Mail mit Textfassung"


@pytest.mark.django_db
def test_insight_digest(
    im_container: str, smtp_server: list[AufzeichnenderServer], smtp_aus_der_umgebung: None, settings: Any
) -> None:
    """Verwaltungsbefehl ``send_digest`` mit eigenem Absender (``INSIGHT_DIGEST_FROM_EMAIL``)."""
    from insight_core.models import InsightSubscriber, OParlBody, OParlSource, SubscriptionAlert

    settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True
    settings.INSIGHT_DIGEST_FROM_EMAIL = "digest@digest.example"
    source = OParlSource.objects.create(name="Beispiel-RIS", url="https://ris.beispiel.example/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.beispiel.example/oparl/body/1", source=source, name="Beispielstadt"
    )
    abonnent = InsightSubscriber.objects.create(
        email="leser@example.org", body=body, keyword="Radweg", keyword_active=True, confirmed=True
    )
    SubscriptionAlert.objects.create(
        subscriber=abonnent,
        alert_type="keyword",
        entity_type="paper",
        entity_id=uuid.uuid4(),
        entity_title="Radweg an der Hauptstraße",
        entity_url="/insight/vorlagen/1/",
    )

    call_command("send_digest", stdout=StringIO())
    _pruefen(smtp_server)
    assert smtp_server[0].nachrichten[0]["From"] == "digest@digest.example"


@pytest.mark.django_db
def test_im_worker_aus_dem_postausgang(
    im_container: str, smtp_server: list[AufzeichnenderServer], smtp_aus_der_umgebung: None, settings: Any
) -> None:
    """Mit ``MAIL_QUEUE`` versendet der Worker die Mail; die Message-ID entsteht erst dort."""
    from django.tasks import task_backends

    from apps.events.task_runner import run_pending
    from apps.events.tasks_backend import JournalBackend

    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.MAIL_QUEUE = ["*"]
    assert isinstance(task_backends["default"], JournalBackend)

    assert mail.send(kind="work.benachrichtigung", subject="Betreff", body="Text", to=["an@example.org"]) is True
    assert smtp_server == [], "liegt im Postausgang"
    assert run_pending() == 1
    _pruefen(smtp_server)


@pytest.mark.django_db
def test_djangos_standardweg_ueber_eigenes_backend(
    im_container: str, smtp_server: list[AufzeichnenderServer], settings: Any
) -> None:
    """``SiteSettingsEmailBackend`` (MAILERS default, etwa für Djangos eigenen Versand) setzt kein eigenes EHLO."""
    settings.SMTP_FALLBACK = {**settings.SMTP_FALLBACK, "host": "smtp.example.org", "use_tls": False}
    backend = cast(Any, SiteSettingsEmailBackend)()
    nachricht = EmailMessage("Betreff", "Text", f"noreply@{DOMAIN}", ["an@example.org"])

    assert backend.send_messages([nachricht]) == 1
    _pruefen(smtp_server)


@pytest.mark.django_db
def test_eigenes_smtp_der_organisation(
    im_container: str, smtp_server: list[AufzeichnenderServer], org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auch über den Server einer Organisation: Message-ID und EHLO mit der Domain der Plattform."""
    monkeypatch.setattr("apps.common.org_email.check_smtp_server", lambda host, port: None)  # ohne DNS-Abfrage
    org.mail_sender_mode = "smtp"
    org.smtp_host = "smtp.fraktion.example"
    org.smtp_use_tls = False
    org.smtp_from_email = "fraktion@fraktion.example"
    org.smtp_fallback_to_mandari = False
    org.save()

    assert mail.send(kind="work.testmail", subject="Test", body="Text", to=["an@example.org"], organization=org)
    _pruefen(smtp_server)
    assert smtp_server[0].nachrichten[0]["From"].endswith("<fraktion@fraktion.example>")
