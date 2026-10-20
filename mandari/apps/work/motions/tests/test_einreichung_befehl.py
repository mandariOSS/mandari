# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einreichung aus Work über den Befehl ``submission.submit`` (Issue #540).

Work reicht über den Client ein (``ris_submission._client``) und hält seinen Stand getrennt fest: Schlüssel aus
dem Dokument ableiten, Befehl senden, Quittung speichern. Geprüft werden der gesendete Befehl, die Wiederholung
nach einem Fehler beim Speichern in Work, die Prüfung im Session-Handler (auch an Work vorbei) und die Meldungen
für Nutzer:innen. Anhänge, Antragsfassung und Rückmeldung prüfen ``test_dokument_anhaenge.py`` und
``test_verwaltung_rueckmeldung.py`` weiter über denselben Weg.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from apps.events.idempotency import NestedTransactionError
from apps.session.models import SessionAPIToken, SessionApplication, SessionFile, SessionTenant
from apps.work.motions import ris_submission
from apps.work.motions.models import Motion, MotionDocument
from hub.commands import Command, InProcessClient, Receipt

pytestmark = pytest.mark.django_db

EDITOR = ["dashboard.view", "motions.view", "motions.edit", "motions.submit_to_ris", "motions.approve"]
INHALT = "<h2>Beschlussvorschlag</h2><p>Die Verwaltung plant einen Radweg.</p><h2>Begründung</h2><p>Sicherheit.</p>"


@pytest.fixture(autouse=True)
def ablage(tmp_path: Path, settings: Any) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, EDITOR, email="autorin@example.org")


@pytest.fixture
def verbunden(org: Any, autorin: Any, tenant: SessionTenant) -> Any:
    _token, raw = SessionAPIToken.create_token(tenant, "Fraktion Test", can_submit_applications=True)
    return ris_submission.connect_with_token(org, raw, autorin)


@pytest.fixture
def antrag(org: Any, autorin: Any, verbunden: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Radweg Hauptstraße", visibility="organization", status="approved"
    )
    cast(Any, motion).set_content_encrypted(INHALT)
    motion.save()
    return motion


class Mitschnitt(InProcessClient):
    """Client der Installation, der jeden gesendeten Befehl festhält."""

    def __init__(self) -> None:
        super().__init__()
        self.befehle: list[Command] = []

    def send(self, command: Command) -> Receipt:
        self.befehle.append(command)
        return super().send(command)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mitschnitt:
    mitschnitt = Mitschnitt()
    monkeypatch.setattr(ris_submission, "_client", lambda: mitschnitt)
    return mitschnitt


def test_work_sendet_den_befehl_ueber_den_client(
    antrag: Motion, autorin: Any, tenant: SessionTenant, client: Mitschnitt
) -> None:
    MotionDocument.objects.create(
        motion=antrag, file=SimpleUploadedFile("Plan.pdf", b"%PDF-1.4 Plan"), filename="Plan.pdf", file_size=13
    )
    daten = {**ris_submission.build_prefill(antrag), "co_signers": "Erika Muster\n\nMax Muster", "is_urgent": False}
    application = ris_submission.submit_motion(antrag, autorin, daten)

    (befehl,) = client.befehle
    assert befehl.name == "submission.submit"
    assert (befehl.tenant_ref, befehl.actor_ref) == (f"session:{tenant.pk}", f"user:{autorin.user_id}")
    assert befehl.idempotency_key.startswith(f"work.motion.{antrag.pk}.")
    assert befehl.body["submitting_organization"] == str(antrag.organization_id)
    # Mitunterzeichnende wie bisher als Text, unverändert gespeichert
    assert befehl.body["co_signers_text"] == "Erika Muster\n\nMax Muster" and befehl.body["application_pdf"] is True
    (anlage,) = befehl.body["attachments"]
    assert (anlage["name"], anlage["size"]) == ("Plan.pdf", 13) and set(befehl.attachments) == {anlage["sha256"]}

    antrag.refresh_from_db()
    assert antrag.session_application == application and antrag.status == "submitted"
    assert application.submitting_organization_id == antrag.organization_id
    assert SessionFile.objects.filter(application=application, is_application_pdf=False).count() == 1


def test_scheitert_das_speichern_in_work_liefert_die_wiederholung_dieselbe_quittung(
    antrag: Motion, autorin: Any, client: Mitschnitt, monkeypatch: pytest.MonkeyPatch
) -> None:
    echt = ris_submission._record_receipt

    def stromausfall(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Work-Datenbank weg")

    monkeypatch.setattr(ris_submission, "_record_receipt", stromausfall)
    with pytest.raises(RuntimeError):
        ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))
    antrag.refresh_from_db()
    assert antrag.session_application is None and SessionApplication.objects.count() == 1

    monkeypatch.setattr(ris_submission, "_record_receipt", echt)
    application = ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))
    assert SessionApplication.objects.count() == 1 and application == SessionApplication.objects.get()
    assert client.befehle[0].idempotency_key == client.befehle[1].idempotency_key
    antrag.refresh_from_db()
    assert antrag.session_application == application


def test_nach_einer_festgehaltenen_einreichung_gilt_ein_neuer_schluessel(
    antrag: Motion, autorin: Any, client: Mitschnitt
) -> None:
    erste = ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))
    # Die Verwaltung löscht den Eingang (wie das Zurücksetzen einer Probe); die Verknüpfung fällt weg
    erste.delete()
    antrag.refresh_from_db()
    assert antrag.session_application is None

    zweite = ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))
    assert zweite.pk != erste.pk and SessionApplication.objects.filter(pk=zweite.pk).exists()
    assert client.befehle[0].idempotency_key != client.befehle[1].idempotency_key


def test_zweiter_klick_nach_der_ersten_einreichung_sendet_nicht_noch_einmal(
    antrag: Motion, autorin: Any, client: Mitschnitt
) -> None:
    """Beide Anfragen haben das Dokument vor der Einreichung geladen; die zweite liest noch Anlagen."""
    zweiter_klick = Motion.objects.get(pk=antrag.pk)
    ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))

    with pytest.raises(ris_submission.SubmissionError, match="bereits eingereicht"):
        ris_submission.submit_motion(zweiter_klick, autorin, ris_submission.build_prefill(zweiter_klick))
    assert SessionApplication.objects.count() == 1 and len(client.befehle) == 1


def test_schluessel_gehoert_zum_geladenen_stand(antrag: Motion) -> None:
    """Ein zweiter Klick mit demselben geladenen Stand sendet denselben Schlüssel (dieselbe Quittung)."""
    geladen = Motion.objects.get(pk=antrag.pk)
    antrag.save()  # Speicherung nach dem Laden, etwa durch die erste Einreichung
    stempel = int(geladen.updated_at.timestamp() * 1_000_000)
    assert ris_submission.idempotency_key(geladen) == f"work.motion.{antrag.pk}.{stempel}"
    assert ris_submission.idempotency_key(geladen) != ris_submission.idempotency_key(antrag)


def test_einreichen_nie_aus_einer_offenen_transaktion(antrag: Motion, autorin: Any) -> None:
    with pytest.raises(NestedTransactionError), transaction.atomic():
        ris_submission.submit_motion(antrag, autorin, ris_submission.build_prefill(antrag))
    assert not SessionApplication.objects.exists()


def test_handler_prueft_das_recht_auch_an_work_vorbei(
    org: Any, antrag: Motion, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ohne „Anträge ans RIS übermitteln“ lehnt Session ab, selbst wenn Work die eigene Prüfung ausließe."""
    ohne_recht = make_member(org, ["motions.view", "motions.edit", "motions.approve"])
    monkeypatch.setattr(ris_submission, "can_submit", lambda *_args: (True, ""))
    with pytest.raises(ris_submission.SubmissionError, match="keine Anträge einreichen"):
        ris_submission.submit_motion(antrag, ohne_recht, ris_submission.build_prefill(antrag))
    assert not SessionApplication.objects.exists()


def test_pruefung_der_verwaltung_erscheint_mit_feldnamen(antrag: Motion, autorin: Any) -> None:
    daten = {**ris_submission.build_prefill(antrag), "title": "  ", "target_organization_id": str(antrag.pk)}
    with pytest.raises(ris_submission.SubmissionError) as fehler:
        ris_submission.submit_motion(antrag, autorin, daten)
    assert str(fehler.value) == "Bitte prüfen: Titel; Zielgremium."
    antrag.refresh_from_db()
    assert antrag.session_application is None and antrag.status == "approved"


def test_formular_zeigt_die_pruefung_der_verwaltung(org: Any, antrag: Motion, autorin: Any, client_for: Any) -> None:
    antwort = client_for(autorin.user).post(
        f"/work/{org.slug}/documents/{antrag.id}/submit-ris/",
        {**ris_submission.build_prefill(antrag), "title": "x" * 501, "application_type": "motion", "confirm": "on"},
    )
    assert antwort.status_code == 200
    assert "Bitte prüfen: Titel." in antwort.content.decode()
    assert not SessionApplication.objects.exists()
