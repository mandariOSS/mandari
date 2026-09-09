# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Snapshot-Tests aller E-Mail-Templates (Issue #175).

Jede Mail wird mit einem festen Beispielkontext über ``apps.common.email.render_email`` gerendert
(Basis-Layout, Inliner, Text-Alternative) und mit den Snapshots unter ``snapshots/emails/`` verglichen.
Fehlt ein Snapshot oder ist ``UPDATE_SNAPSHOTS=1`` gesetzt, wird er geschrieben; sonst schlägt der Test
bei Abweichung mit einem Diff fehl. Zusätzlich je Mail: kein ``<style>``/``style=`` im Fach-Template,
Inliner hat ``style=``-Attribute erzeugt, Textfassung nicht leer, keine ungerenderten Template-Tags.
"""

from __future__ import annotations

import difflib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.template.loader import get_template

from apps.accounts.forms import PasswordResetForm
from apps.common.email import html_to_text, render_email, send_template_email

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "emails"
BASE_TEMPLATE = "emails/base_email.html"
SITE_URL = "https://mandari.example"
WHEN = datetime(2026, 9, 14, 18, 30, tzinfo=ZoneInfo("Europe/Berlin"))


def _uuid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


# =============================================================================
# Snapshot-Mechanik
# =============================================================================


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def assert_snapshot(name: str, actual: str) -> None:
    """Vergleicht ``actual`` mit ``snapshots/emails/<name>``; schreibt bei UPDATE_SNAPSHOTS=1 oder fehlender Datei."""
    path = SNAPSHOT_DIR / name
    actual = _normalize(actual)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        return
    expected = _normalize(path.read_text(encoding="utf-8"))
    if expected != actual:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name} (Snapshot)",
                tofile=f"{name} (aktuell)",
            )
        )
        pytest.fail(f"Snapshot {name} weicht ab (UPDATE_SNAPSHOTS=1 zum Aktualisieren):\n{diff}")


# =============================================================================
# Beispielkontexte
# =============================================================================


def _contact() -> Any:
    from insight_core.models import ContactRequest

    contact = ContactRequest.objects.create(
        id=_uuid(0x101),
        name="Erika Mustermann",
        email="erika@example.org",
        organization_name="Fraktion Test",
        subject="demo",
        message="Guten Tag,\n\nwir würden mandari gern in unserer Fraktion ausprobieren.\nGibt es eine Demo?",
        ip_address="203.0.113.7",
    )
    ContactRequest.objects.filter(pk=contact.pk).update(created_at=WHEN)
    contact.refresh_from_db()
    return contact


def contact_confirmation(org: Any, make_member: Any) -> dict[str, Any]:
    contact = _contact()
    return {"contact": contact, "admin_url": f"{SITE_URL}/admin/insight_core/contactrequest/{contact.id}/change/"}


def contact_notification(org: Any, make_member: Any) -> dict[str, Any]:
    return contact_confirmation(org, make_member)


def _decision_item() -> Any:
    from apps.session.models import SessionAgendaItem, SessionMeeting, SessionOrganization, SessionTenant

    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
    council = SessionOrganization.objects.create(tenant=tenant, name="Rat der Stadt Musterstadt")
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=council, start=WHEN, is_public=True
    )
    return SessionAgendaItem.objects.create(
        id=_uuid(0x201),
        meeting=meeting,
        number="7",
        name="Neubau der Radwegbrücke über den Mühlbach",
        is_public=True,
        vote_result="approved",
        resolution_number="2026/0815",
        implementation_status="in_progress",
        implementation_public_note="Die Ausführungsplanung läuft.\nBaubeginn ist für Frühjahr 2027 vorgesehen.",
        implementation_deadline=date(2027, 3, 31),
    )


def decisions_confirm(org: Any, make_member: Any) -> dict[str, Any]:
    item = _decision_item()
    return {
        "item": item,
        "confirm_url": f"{SITE_URL}/insight/beschluesse/abo/bestaetigen/{_uuid(0x202)}/",
        "site_url": SITE_URL,
        "decision_url": f"{SITE_URL}/insight/beschluesse/{item.id}/",
    }


def decisions_status(org: Any, make_member: Any) -> dict[str, Any]:
    item = _decision_item()
    return {
        "item": item,
        "status_label": "In Umsetzung",
        "status_changed": True,
        "decision_url": f"{SITE_URL}/insight/beschluesse/{item.id}/",
        "unsubscribe_url": f"{SITE_URL}/insight/beschluesse/abo/abmelden/{_uuid(0x203)}/",
        "site_url": SITE_URL,
    }


def _body() -> Any:
    from insight_core.models import OParlBody, OParlSource

    source = OParlSource.objects.create(
        id=_uuid(0x301),
        name="RIS Musterstadt",
        url="https://ris.musterstadt.example/oparl/v1/system",
        last_sync=WHEN,
        consecutive_failures=5,
    )
    return OParlBody.objects.create(
        id=_uuid(0x302),
        external_id="https://ris.musterstadt.example/oparl/v1/body/1",
        source=source,
        name="Stadt Musterstadt",
        short_name="Musterstadt",
        slug="musterstadt",
    )


def _subscriber() -> Any:
    from insight_core.models import InsightSubscriber

    return InsightSubscriber.objects.create(
        id=_uuid(0x303),
        email="erika@example.org",
        body=_body(),
        token=_uuid(0x304),
        neighborhood_active=True,
        neighborhood_name="Hafenviertel",
        neighborhood_radius=500,
        keyword_active=True,
        keyword="Radweg",
        bookmarks_active=True,
        digest_frequency="weekly",
    )


def insight_confirm(org: Any, make_member: Any) -> dict[str, Any]:
    subscriber = _subscriber()
    return {
        "subscriber": subscriber,
        "confirm_url": f"{SITE_URL}/insight/abo/bestaetigen/{subscriber.token}/",
        "site_url": SITE_URL,
    }


def insight_digest(org: Any, make_member: Any) -> dict[str, Any]:
    from insight_core.models import SubscriptionAlert

    subscriber = _subscriber()

    def alert(n: int, alert_type: str, title: str, entity_type: str, context: dict[str, Any]) -> Any:
        return SubscriptionAlert.objects.create(
            id=_uuid(0x310 + n),
            subscriber=subscriber,
            alert_type=alert_type,
            entity_type=entity_type,
            entity_id=_uuid(0x320 + n),
            entity_title=title,
            entity_url=f"/insight/vorgaenge/{_uuid(0x320 + n)}/",
            context=context,
        )

    neighborhood = [
        alert(1, "neighborhood", "Bebauungsplan Hafenviertel Nord", "Vorlage", {"distance": 120}),
        alert(2, "neighborhood", "Sanierung Kaistraße", "Vorlage", {}),
    ]
    keyword = [alert(3, "keyword", "Radwegkonzept 2030", "Antrag", {})]
    bookmarks = [alert(4, "bookmark", "Haushaltsplan 2027", "Vorlage", {})]
    return {
        "subscriber": subscriber,
        "alert_count": 4,
        "neighborhood_alerts": neighborhood,
        "keyword_alerts": keyword,
        "bookmark_alerts": bookmarks,
        "site_url": SITE_URL,
    }


def monitoring_source_health(org: Any, make_member: Any) -> dict[str, Any]:
    source = _body().source
    return {
        "kind": "alert",
        "source": source,
        "item": {
            "label": "kritisch",
            "bodies": ["Musterstadt", "Nachbarstadt"],
            "consecutive_failures": 5,
            "reasons": ["HTTP 503 seit 3 Tagen", "Zertifikat der Quelle abgelaufen"],
        },
        "site_url": SITE_URL,
        "monitoring_url": f"{SITE_URL}/admin/monitoring/",
        "admin_url": f"{SITE_URL}/admin/insight_core/oparlsource/{source.id}/change/",
    }


def monitoring_source_health_daemon(org: Any, make_member: Any) -> dict[str, Any]:
    return {
        "kind": "daemon",
        "item": {"detail": "Letzter Sync-Lauf vor 26 Stunden"},
        "site_url": SITE_URL,
        "monitoring_url": f"{SITE_URL}/admin/monitoring/",
    }


def _question() -> Any:
    from insight_core.models import OParlPerson, PublicQuestion

    body = _body()
    person = OParlPerson.objects.create(
        id=_uuid(0x401),
        external_id="https://ris.musterstadt.example/oparl/v1/person/1",
        body=body,
        name="Max Beispiel",
        email="max.beispiel@example.org",
    )
    return PublicQuestion.objects.create(
        id=_uuid(0x402),
        body=body,
        recipient=person,
        topic="verkehr",
        questioner_name="Erika Mustermann",
        questioner_email="erika@example.org",
        questioner_city="Musterstadt",
        subject="Radweg an der Hauptstraße",
        question_text="Wann wird der Radweg an der Hauptstraße fertiggestellt?\n\nDie Baustelle steht seit Monaten.",
        answer_text="Vielen Dank für die Frage.\nDie Fertigstellung ist für Oktober geplant.",
        verification_token=_uuid(0x403),
        answer_token=_uuid(0x404),
        status="published",
        answer_status="published",
    )


def _question_context(**extra: Any) -> dict[str, Any]:
    question = _question()
    return {
        "question": question,
        "question_url": f"{SITE_URL}{question.get_absolute_url()}",
        "answer_url": f"{SITE_URL}/insight/fragen/antworten/{question.answer_token}/",
        "verify_url": f"{SITE_URL}/insight/fragen/verifizieren/{question.verification_token}/",
        "admin_url": f"{SITE_URL}/admin/insight_core/publicquestion/{question.id}/change/",
        "site_url": SITE_URL,
        **extra,
    }


def questions_verification(org: Any, make_member: Any) -> dict[str, Any]:
    return _question_context()


def questions_notification(org: Any, make_member: Any) -> dict[str, Any]:
    return _question_context()


def questions_published(org: Any, make_member: Any) -> dict[str, Any]:
    return _question_context()


def questions_answer_notification(org: Any, make_member: Any) -> dict[str, Any]:
    return _question_context()


def questions_answer_reminder(org: Any, make_member: Any) -> dict[str, Any]:
    return _question_context()


def questions_moderation(org: Any, make_member: Any) -> dict[str, Any]:
    return _question_context(kind="question")


def accounts_password_reset(org: Any, make_member: Any) -> dict[str, Any]:
    return {"protocol": "https", "domain": "mandari.example", "uid": "MTIz", "token": "cabc12-0123456789abcdef"}


def _member(org: Any, make_member: Any, email: str, first_name: str, last_name: str) -> Any:
    """Mitglied mit Namen (make_member verwirft user_kwargs, sobald eine E-Mail angegeben ist)."""
    member = make_member(org, email=email)
    member.user.first_name = first_name
    member.user.last_name = last_name
    member.user.save(update_fields=["first_name", "last_name"])
    return member


def _faction_meeting(org: Any, make_member: Any) -> dict[str, Any]:
    from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting

    member = _member(org, make_member, "anna@example.org", "Anna", "Beispiel")
    meeting = FactionMeeting.objects.create(
        id=_uuid(0x501),
        organization=org,
        title="Fraktionssitzung September",
        description="Bitte die Haushaltsunterlagen mitbringen.",
        start=WHEN,
        location="Rathaus, Raum 204",
        is_virtual=True,
        video_link="https://meet.example/fraktion-test",
        status="planned",
    )
    top1 = FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt 2027", visibility="public")
    top1.set_description_encrypted("Erste Lesung des Haushaltsentwurfs mit Schwerpunkt Radverkehr.")  # type: ignore[attr-defined]
    top1.save()
    top2 = FactionAgendaItem.objects.create(
        meeting=meeting, number="2", title="Antrag Radwegkonzept", visibility="public", order=1
    )
    internal = FactionAgendaItem.objects.create(
        meeting=meeting, number="NÖ 1", title="Personalie Geschäftsstelle", visibility="internal", order=2
    )
    attendance = FactionAttendance.objects.create(meeting=meeting, membership=member, status="tentative")
    return {
        "meeting": meeting,
        "user": member.user,
        "organization": org,
        "attendance": attendance,
        "public_agenda_items": [top1, top2],
        "internal_agenda_items": [internal],
        "meeting_url": f"{SITE_URL}/work/{org.slug}/faction/{meeting.id}/",
    }


def work_faction_invitation(org: Any, make_member: Any) -> dict[str, Any]:
    return {
        **_faction_meeting(org, make_member),
        "is_sworn_in": True,
        "is_update": False,
        "invitation_mode": "opt_in",
        "is_opt_out": False,
    }


def work_faction_reminder(org: Any, make_member: Any) -> dict[str, Any]:
    context = _faction_meeting(org, make_member)
    del context["public_agenda_items"], context["internal_agenda_items"]
    return {**context, "hours_before": 24}


def work_notification(org: Any, make_member: Any) -> dict[str, Any]:
    from apps.work.notifications.models import Notification

    recipient = _member(org, make_member, "anna@example.org", "Anna", "Beispiel")
    actor = _member(org, make_member, "bernd@example.org", "Bernd", "Muster")
    notification = Notification.objects.create(
        id=_uuid(0x601),
        recipient=recipient,
        notification_type="task_assigned",
        title="Neue Aufgabe: Haushaltsrede vorbereiten",
        message="Bernd Muster hat dir die Aufgabe „Haushaltsrede vorbereiten“ zugewiesen (fällig am 30.09.2026).",
        link=f"/work/{org.slug}/tasks/{_uuid(0x602)}/",
        actor=actor,
    )
    return {
        "notification": notification,
        "recipient": recipient,
        "actor": actor,
        "site_name": "Mandari Work",
        "base_url": SITE_URL,
    }


@dataclass(frozen=True)
class MailCase:
    name: str
    template: str
    build: Callable[[Any, Any], dict[str, Any]]


CASES = [
    MailCase("contact_confirmation", "emails/contact/confirmation.html", contact_confirmation),
    MailCase("contact_notification", "emails/contact/notification.html", contact_notification),
    MailCase("decisions_confirm", "emails/decisions/confirm.html", decisions_confirm),
    MailCase("decisions_status", "emails/decisions/status.html", decisions_status),
    MailCase("insight_confirm", "emails/insight_confirm.html", insight_confirm),
    MailCase("insight_digest", "emails/insight_digest.html", insight_digest),
    MailCase("monitoring_source_health", "emails/monitoring/source_health.html", monitoring_source_health),
    MailCase(
        "monitoring_source_health_daemon", "emails/monitoring/source_health.html", monitoring_source_health_daemon
    ),
    MailCase("questions_verification", "emails/questions/verification.html", questions_verification),
    MailCase("questions_notification", "emails/questions/notification.html", questions_notification),
    MailCase("questions_published", "emails/questions/published.html", questions_published),
    MailCase(
        "questions_answer_notification", "emails/questions/answer_notification.html", questions_answer_notification
    ),
    MailCase("questions_answer_reminder", "emails/questions/answer_reminder.html", questions_answer_reminder),
    MailCase("questions_moderation", "emails/questions/moderation.html", questions_moderation),
    MailCase("accounts_password_reset", "accounts/emails/password_reset.html", accounts_password_reset),
    MailCase("work_faction_invitation", "work/faction/email/invitation.html", work_faction_invitation),
    MailCase("work_faction_reminder", "work/faction/email/reminder.html", work_faction_reminder),
    MailCase("work_notification", "work/notifications/email/notification.html", work_notification),
]

ALL_MAIL_TEMPLATES = sorted({case.template for case in CASES})


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.django_db
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_email_snapshot(case: MailCase, org: Any, make_member: Any) -> None:
    template: Any = get_template(case.template)  # Backend-Template hat .origin, die Stubs kennen es nicht
    source = Path(template.origin.name).read_text(encoding="utf-8")
    assert f'{{% extends "{BASE_TEMPLATE}" %}}' in source, "Fach-Mail muss das Basis-Layout erweitern"
    assert "<style" not in source, "Fach-Mails haben keinen eigenen <style>-Block"
    assert 'style="' not in source, "Fach-Mails haben keine style=-Attribute (Inliner übernimmt das)"

    html, text = render_email(case.template, case.build(org, make_member))

    assert 'style="' in html, "Inliner hat keine style=-Attribute erzeugt"
    assert text.strip(), "Text-Alternative ist leer"
    for marker in ("{{", "{%"):
        assert marker not in html, f"ungerendertes Template-Tag {marker} im HTML"
        assert marker not in text, f"ungerendertes Template-Tag {marker} im Text"

    assert_snapshot(f"{case.name}.html", html)
    assert_snapshot(f"{case.name}.txt", text)


def test_all_email_templates_are_covered() -> None:
    """Jedes Mail-Template im Baum hat einen Snapshot-Fall."""
    from django.conf import settings

    templates_dir = Path(settings.BASE_DIR) / "templates"
    found = set()
    for pattern in ("emails/**/*.html", "accounts/emails/*.html", "work/**/email/*.html"):
        for path in templates_dir.glob(pattern):
            rel = path.relative_to(templates_dir).as_posix()
            if rel != BASE_TEMPLATE:
                found.add(rel)
    assert found == set(ALL_MAIL_TEMPLATES)


def test_html_to_text_skips_marked_regions_and_keeps_links() -> None:
    html = (
        "<html><body><!-- text:skip --><span>Preheader</span><!-- /text:skip -->"
        '<h1>Titel</h1><p>Bitte <a href="https://mandari.example/x">bestätigen</a>.</p></body></html>'
    )
    text = html_to_text(html)
    assert "Preheader" not in text
    assert "# Titel" in text
    assert "[bestätigen](https://mandari.example/x)" in text
    assert text.endswith("\n") and "\n\n\n" not in text


@pytest.mark.django_db
def test_render_email_prefers_txt_sibling(org: Any, make_member: Any) -> None:
    """Gepflegte .txt-Templates (z. B. Fraktionseinladung) bleiben die Textfassung."""
    _html, text = render_email("work/faction/email/invitation.html", work_faction_invitation(org, make_member))
    assert "TAGESORDNUNG" in text  # stammt aus invitation.txt, nicht aus html2text


@pytest.mark.django_db
def test_send_template_email_sends_multipart(org: Any, make_member: Any) -> None:
    ok = send_template_email(
        subject="Testmail",
        template_name="emails/questions/published",
        context=questions_published(org, make_member),
        to=["erika@example.org"],
    )
    assert ok is True
    assert len(mail.outbox) == 1
    message: Any = mail.outbox[0]
    assert "Ihre Frage ist jetzt öffentlich" in message.body  # aus published.txt
    assert message.alternatives[0][1] == "text/html"
    assert 'style="' in message.alternatives[0][0]


@pytest.mark.django_db
def test_password_reset_mail_has_text_and_inlined_html(org: Any, make_member: Any) -> None:
    member = make_member(org, email="anna@example.org")
    form = PasswordResetForm(data={"email": member.user.email})
    assert form.is_valid()
    form.save(
        domain_override="mandari.example",
        email_template_name="accounts/emails/password_reset.txt",
        html_email_template_name="accounts/emails/password_reset.html",
        subject_template_name="accounts/emails/password_reset_subject.txt",
    )
    assert len(mail.outbox) == 1
    message: Any = mail.outbox[0]
    assert message.subject == "Passwort zurücksetzen - Mandari"
    assert "/accounts/" in message.body and "<" not in message.body
    html = message.alternatives[0][0]
    assert 'class="btn"' in html and 'style="' in html
