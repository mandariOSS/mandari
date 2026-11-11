# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Musterstadt-Bestand in Work (``setup_demo_work_bestand``, Issue #99).

Die Musterfraktion soll in der öffentlichen Demo wie eine echte Fraktion nach einem Jahr Arbeit aussehen: Mitglieder
(ohne Anmeldung), Anträge und Anfragen aus dem RIS-Bestand mit passendem Stand, eigene Dokumente, Vorbereitung der
kommenden Sitzungen, Aufgaben und Benachrichtigungen. Ein zweiter Lauf verdoppelt nichts, ``--entfernen`` gibt die
RIS-Objekte des Bestands frei (Positionen und Vorbereitungen schützen sie sonst vor dem Löschen), und die Basisdemo
bleibt unberührt.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.db.models import Model
from django.test import Client
from django.utils import timezone

from apps.common.demo_daten import DEMO_ORG_SLUG, DEMO_USERS, demo_ext, termin_am_sitzungstag
from apps.common.demo_stadt import BESTAND, PERSONEN, Stadtplan, stadtplan, top_kennung
from apps.tenants.models import Membership, Organization
from apps.work.management.commands import setup_demo_work as basis
from apps.work.management.commands import setup_demo_work_bestand as bestand
from apps.work.meetings.models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    MeetingPreparation,
    PaperComment,
)
from apps.work.motions.models import DocumentFolder, Motion, MotionComment, MotionShare, MotionType
from apps.work.notifications.models import Notification, NotificationPreference, NotificationType
from apps.work.organization.models import MemberAbsence
from apps.work.tasks.models import Task, TaskChecklistItem, TaskComment, TaskLabel
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlPaper

pytestmark = pytest.mark.django_db

PASSWORT = "Demo-Pruefung-Passwort-2"
BESTANDSKONTEN = [p for p in PERSONEN if p.fraktion == bestand.FRAKTION and p.key not in bestand.BASIS_KONTEN]
ZAEHLMODELLE: tuple[type[Model], ...] = (
    Membership,
    Motion,
    MotionComment,
    MotionShare,
    MotionType,
    DocumentFolder,
    Task,
    TaskLabel,
    TaskComment,
    TaskChecklistItem,
    MeetingPreparation,
    AgendaItemPosition,
    AgendaItemNote,
    AgendaPrivateNote,
    AgendaSpeechNote,
    PaperComment,
    MemberAbsence,
    Notification,
    NotificationPreference,
)


@pytest.fixture
def demo(settings: Any, tmp_path: Path) -> Organization:
    settings.MEDIA_ROOT = str(tmp_path / "media")
    call_command("setup_demo_environment", stdout=StringIO())
    return Organization.objects.get(slug=DEMO_ORG_SLUG)


def _lauf(*args: str, **kwargs: Any) -> str:
    ausgabe = StringIO()
    call_command("setup_demo_work_bestand", *args, stdout=ausgabe, **kwargs)
    return ausgabe.getvalue()


def _neuaufbau() -> None:
    """Zweiter Lauf der Basisdemo. Den Work-Bestand vorher freigeben, wie ``setup_demo_environment`` es vor dem
    Insight-Bestand tun muss: Positionen, Notizen und Vorbereitungen schützen die RIS-Objekte (``PROTECT``)."""
    _lauf("--entfernen")
    call_command("setup_demo_environment", stdout=StringIO())


def _plan(umfang: str = "klein") -> Stadtplan:
    return stadtplan(timezone.localdate(), umfang)


def _zaehlstand() -> dict[str, int]:
    return {m.__name__: m._default_manager.count() for m in ZAEHLMODELLE}


def _ris_bestand(plan: Stadtplan) -> None:
    """RIS-Objekte des Plans anlegen, soweit der Insight-Bestand sie (noch) nicht angelegt hat."""
    body = OParlBody.objects.get(external_id=demo_ext("body", "musterstadt"))
    heute = timezone.localdate()
    for vorgang in plan.vorgaenge:
        OParlPaper.objects.get_or_create(
            external_id=demo_ext("paper", vorgang.key),
            defaults={
                "body": body,
                "name": vorgang.titel,
                "reference": vorgang.nummer,
                "paper_type": vorgang.oparl_art,
            },
        )
    for sitzung in plan.sitzungen:
        meeting, _ = OParlMeeting.objects.get_or_create(
            external_id=demo_ext("meeting", sitzung.key),
            defaults={"body": body, "name": sitzung.name, "start": termin_am_sitzungstag(heute, sitzung.tag)},
        )
        for top in sitzung.tops:
            OParlAgendaItem.objects.get_or_create(
                external_id=demo_ext("agendaitem", top_kennung(sitzung, top)),
                defaults={
                    "meeting": meeting,
                    "number": top.nummer,
                    "order": top.reihenfolge,
                    "name": top.titel,
                    "public": top.oeffentlich,
                },
            )


def test_ohne_basisdemo_bricht_der_befehl_ab() -> None:
    with pytest.raises(CommandError, match="setup_demo_environment"):
        _lauf()
    _lauf("--entfernen")  # ohne Organisation gibt es nichts zu entfernen


class TestMitglieder:
    def test_konten_ohne_anmeldung_mit_rolle_und_ausschuessen(self, demo: Organization) -> None:
        for person in BESTANDSKONTEN:
            ms = Membership.objects.get(organization=demo, user__email=person.email)
            assert not ms.user.has_usable_password(), person.key
            assert ms.is_active and ms.is_sworn_in and not ms.is_guest
            if person.rat:
                assert ms.oparl_committees.filter(external_id=demo_ext("organization", "rat")).exists() or (
                    not ms.oparl_committees.exists()
                )
        jana = Membership.objects.get(organization=demo, user__email__startswith="jana-jacobs@")
        assert list(jana.roles.values_list("name", flat=True)) == ["Stellv. Vorsitz"]
        tim = Membership.objects.get(organization=demo, user__email__startswith="tim-tessmer@")
        assert list(tim.roles.values_list("name", flat=True)) == ["Sachkundige/r Bürger/in"]

    def test_kein_zweites_konto_fuer_demo_nutzer(self, demo: Organization) -> None:
        """Sofia Sachkundig ist schon Demo-Nutzerin, Anna, Bernd und Dieter auch."""
        for key in bestand.BASIS_KONTEN:
            assert not Membership.objects.filter(organization=demo, user__email__startswith=f"{key}@").exists()

    def test_keine_ankuendigung_und_keine_mails(self, demo: Organization) -> None:
        _neuaufbau()  # zweiter Lauf: Bestandsmitglieder gibt es schon
        emails = [p.email for p in BESTANDSKONTEN]
        assert not Notification.objects.filter(
            notification_type=NotificationType.ANNOUNCEMENT, recipient__user__email__in=emails
        ).exists()
        einstellungen = NotificationPreference.objects.filter(membership__user__email__in=emails)
        assert einstellungen.count() == len(emails)
        assert not any(e.email_enabled for e in einstellungen)


class TestDokumente:
    def test_antraege_und_anfragen_der_fraktion_aus_dem_plan(self, demo: Organization) -> None:
        _ris_bestand(_plan("voll"))
        _lauf(umfang="voll")
        plan = _plan("voll")
        eigene = [v for v in plan.vorgaenge if v.urheber == bestand.FRAKTION]
        assert eigene
        befehl = bestand.Command()
        befehl.aufbautag = timezone.localdate()
        for vorgang in eigene:
            motion = Motion.objects.get(organization=demo, title=bestand._dokumenttitel(vorgang))
            erwartet, _sitzung = befehl._stand(plan, vorgang)
            assert motion.status == erwartet, vorgang.titel
            assert motion.document_type is not None
            assert motion.get_content_decrypted()  # type: ignore[attr-defined]
            if vorgang.tag <= 0:
                assert motion.related_paper is not None
                assert motion.related_paper.external_id == demo_ext("paper", vorgang.key)
                assert motion.submitted_at is not None
        stati = set(Motion.objects.filter(organization=demo).values_list("status", flat=True))
        assert {"adopted", "completed", "draft", "internal_review", "approved"} <= stati

    def test_eigene_dokumente_ordner_und_freigaben(self, demo: Organization) -> None:
        for dok in bestand.EIGENE_DOKUMENTE:
            motion = Motion.objects.get(organization=demo, title=dok.titel)
            assert motion.status == dok.status
            assert motion.folder is not None
        vertraulich = Motion.objects.filter(organization=demo, folder__sworn_in_only=True)
        assert vertraulich.count() == sum(1 for d in bestand.EIGENE_DOKUMENTE if d.ordner == "noe")
        gast = demo.memberships.get(user__email=DEMO_USERS["gast"]["email"])
        geteilt = MotionShare.objects.filter(user=gast.user, motion__organization=demo)
        assert geteilt.count() == sum(1 for d in bestand.EIGENE_DOKUMENTE if d.gast)
        assert MotionType.objects.filter(organization=demo).count() == len(bestand.DOKUMENTTYPEN)
        assert MotionComment.objects.filter(
            motion__organization=demo, motion__title__in=[d.titel for d in bestand.EIGENE_DOKUMENTE]
        ).count() >= sum(len(d.kommentare) for d in bestand.EIGENE_DOKUMENTE)


class TestVorbereitungUndAufgaben:
    def test_positionen_notizen_und_vorbereitungen(self, demo: Organization) -> None:
        plan = _plan()
        _ris_bestand(plan)
        _lauf()
        positionen = AgendaItemPosition.objects.filter(
            organization=demo, agenda_item__external_id__startswith=demo_ext("agendaitem", BESTAND)
        )
        assert positionen.exists()
        assert positionen.exclude(position="open").filter(reasoning_encrypted__isnull=False).exists()
        vergangen = positionen.filter(agenda_item__meeting__start__lt=timezone.now())
        if vergangen.exists():
            assert vergangen.exclude(outcome="").exists()
        assert MeetingPreparation.objects.filter(
            organization=demo, meeting__external_id__startswith=demo_ext("meeting", BESTAND)
        ).exists()

    def test_aufgaben_mit_faelligkeiten_labels_und_checklisten(self, demo: Organization) -> None:
        heute = timezone.localdate()
        titel = [a[0] for a in bestand.AUFGABEN]
        aufgaben = Task.objects.filter(organization=demo, title__in=titel)
        assert aufgaben.count() == len(bestand.AUFGABEN)
        offen = aufgaben.exclude(status="done")
        assert offen.filter(due_date__lt=heute).exists()  # überfällig
        assert offen.filter(due_date=heute).exists()
        assert offen.filter(due_date__gt=heute + timedelta(days=7)).exists()
        assert aufgaben.filter(status="done", is_completed=True).exists()
        assert TaskLabel.objects.filter(organization=demo).count() >= len(bestand.LABELS)
        assert TaskChecklistItem.objects.filter(task__in=aufgaben).exists()
        assert TaskComment.objects.filter(task__in=aufgaben).exists()
        # Für die Demo-Konten gibt es etwas zu tun
        vorsitz = demo.memberships.get(user__email=DEMO_USERS["vorsitz"]["email"])
        assert offen.filter(assigned_to=vorsitz).exists()
        assert Notification.objects.filter(recipient=vorsitz, metadata__demo_bestand=True).exists()
        assert MemberAbsence.objects.filter(organization=demo).count() == len(bestand.ABWESENHEITEN)


class TestIdempotenzUndEntfernen:
    def test_zweiter_lauf_verdoppelt_nichts(self, demo: Organization) -> None:
        _ris_bestand(_plan())
        _lauf()
        vorher = _zaehlstand()
        _lauf()
        assert _zaehlstand() == vorher
        _neuaufbau()
        assert _zaehlstand() == vorher

    def test_entfernen_gibt_die_ris_objekte_frei_und_laesst_die_basis_stehen(self, demo: Organization) -> None:
        _ris_bestand(_plan())
        _lauf()
        basis_positionen = AgendaItemPosition.objects.filter(organization=demo).exclude(
            agenda_item__external_id__startswith=demo_ext("agendaitem", BESTAND)
        )
        anzahl_basis = basis_positionen.count()

        _lauf("--entfernen")

        assert not Motion.objects.filter(organization=demo, title__in=[d.titel for d in bestand.EIGENE_DOKUMENTE])
        assert not Task.objects.filter(organization=demo, title__in=[a[0] for a in bestand.AUFGABEN]).exists()
        assert Motion.objects.filter(organization=demo, title=basis.ANTRAG_ENTWURF).exists()
        assert basis_positionen.count() == anzahl_basis
        assert not Notification.objects.filter(metadata__demo_bestand=True).exists()
        # Insight kann seinen Bestand jetzt löschen (PROTECT hält nichts mehr fest)
        OParlAgendaItem.objects.filter(external_id__startswith=demo_ext("agendaitem", BESTAND)).delete()
        OParlMeeting.objects.filter(external_id__startswith=demo_ext("meeting", BESTAND)).delete()
        OParlPaper.objects.filter(external_id__startswith=demo_ext("paper", BESTAND)).delete()

        # Ohne RIS-Bestand entsteht der Work-Bestand trotzdem, nur ohne Verknüpfung
        _lauf()
        assert Motion.objects.filter(organization=demo, title=bestand.EIGENE_DOKUMENTE[0].titel).exists()


class TestSeiten:
    @pytest.mark.parametrize("rolle", ("vorsitz", "unvereidigt", "gast"))
    def test_kein_serverfehler(self, demo: Organization, rolle: str) -> None:
        _ris_bestand(_plan())
        _lauf()
        nutzer = demo.memberships.get(user__email=DEMO_USERS[rolle]["email"]).user
        nutzer.set_password(PASSWORT)
        nutzer.save(update_fields=["password"])
        browser = Client()
        assert browser.login(email=nutzer.email, password=PASSWORT)
        slug = demo.slug
        dokument = Motion.objects.get(organization=demo, title=bestand.EIGENE_DOKUMENTE[0].titel)
        pfade = [f"/work/{slug}/", f"/work/{slug}/documents/", f"/work/{slug}/documents/{dokument.pk}/"]
        if rolle != "gast":
            sitzung = (
                MeetingPreparation.objects.filter(
                    organization=demo, meeting__external_id__startswith=demo_ext("meeting", BESTAND)
                )
                .order_by("meeting__start")
                .first()
            )
            pfade += [f"/work/{slug}/tasks/", f"/work/{slug}/organization/members/"]
            if sitzung is not None:
                pfade.append(f"/work/{slug}/meetings/{sitzung.meeting_id}/prepare/")
        for pfad in pfade:
            antwort = browser.get(pfad, follow=True)
            assert antwort.status_code < 500, pfad
        if rolle == "unvereidigt":
            vertraulich = Motion.objects.filter(organization=demo, folder__sworn_in_only=True).first()
            assert vertraulich is not None
            assert browser.get(f"/work/{slug}/documents/{vertraulich.pk}/").status_code in (403, 404, 302)
