# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben mit mehreren Zuständigen (Issue #1462).

Eine Aufgabe hat eine zugewiesene Person und beliebig viele weitere Zuständige. Alle Zuständigen sind gleichberechtigt:
Sie sehen die Aufgabe (auch eine private), finden sie unter „Meine Aufgaben“ und auf der Startseite, bearbeiten sie und
haken sie ab; die Aufgabe hat einen gemeinsamen Stand. Wer neu zuständig wird, erfährt es; Kommentare, Erledigung und
die Erinnerung an die Fälligkeit gehen an alle. Für andere ändert sich nichts.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.tenants.models import Membership
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.notifications.models import Notification, NotificationType
from apps.work.organization.export_service import DsgvoExportService
from apps.work.tasks import export_service, selectors, services
from apps.work.tasks.forms import WEITERE_GESENDET, TaskPanelForm
from apps.work.tasks.models import Task, TaskActivity

pytestmark = pytest.mark.django_db

RECHTE = ["dashboard.view", "tasks.view", "tasks.create"]


def _mitglied(org: Any, make_member: Any, name: str, *, rechte: list[str] | None = None) -> Any:
    member = make_member(org, RECHTE if rechte is None else rechte, email=f"{name}@example.org")
    member.user.first_name = name.capitalize()
    member.user.last_name = "Beispiel"
    member.user.save(update_fields=["first_name", "last_name"])
    return member


@pytest.fixture
def anna(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "anna")


@pytest.fixture
def ben(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "ben")


@pytest.fixture
def cem(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "cem")


@pytest.fixture
def dora(org: Any, make_member: Any) -> Any:
    """Mitglied ohne Bezug zur Aufgabe."""
    return _mitglied(org, make_member, "dora")


def _aufgabe(org: Any, erstellt: Any, zugewiesen: Any, *weitere: Any, **felder: Any) -> Task:
    werte = {"title": "Haushaltsrede vorbereiten", "visibility": "private", "status": "todo", **felder}
    aufgabe = Task.objects.create(organization=org, created_by=erstellt, assigned_to=zugewiesen, **werte)
    aufgabe.weitere_zustaendige.set(weitere)
    return Task.objects.get(pk=aufgabe.pk)


def _zugewiesen_an(aufgabe: Task) -> set[str]:
    """E-Mail-Adressen aller, die „Aufgabe zugewiesen“ zu dieser Aufgabe bekommen haben."""
    return set(
        Notification.objects.filter(
            notification_type=NotificationType.TASK_ASSIGNED, metadata__task_id=str(aufgabe.pk)
        ).values_list("recipient__user__email", flat=True)
    )


def _empfaenger(aufgabe: Task, art: Any) -> set[str]:
    return set(
        Notification.objects.filter(notification_type=art, metadata__task_id=str(aufgabe.pk)).values_list(
            "recipient__user__email", flat=True
        )
    )


# ---------------------------------------------------------------------------
# Modell, Sichtbarkeit und Rechte
# ---------------------------------------------------------------------------


def test_zustaendige_zugewiesene_person_zuerst_dann_nach_namen(org: Any, anna: Any, ben: Any, cem: Any) -> None:
    aufgabe = _aufgabe(org, anna, cem, ben, anna)

    assert [m.pk for m in aufgabe.zustaendige()] == [cem.pk, anna.pk, ben.pk]
    assert aufgabe.zustaendige_anzeige == {
        "erste": "Cem Beispiel",
        "weitere": 2,
        "alle": "Cem Beispiel, Anna Beispiel, Ben Beispiel",
    }
    assert aufgabe.weitere_namen() == ["Anna Beispiel", "Ben Beispiel"]


def test_ohne_zugewiesene_person_zaehlen_die_weiteren(org: Any, anna: Any, ben: Any) -> None:
    aufgabe = _aufgabe(org, anna, None, ben)

    assert [m.pk for m in aufgabe.zustaendige()] == [ben.pk]
    assert aufgabe.ist_zustaendig(ben) and not aufgabe.ist_zustaendig(anna)
    assert Task(organization=org, title="neu").zustaendige() == []


def test_weitere_zustaendige_sehen_und_bearbeiten_eine_private_aufgabe(
    org: Any, anna: Any, ben: Any, cem: Any, dora: Any
) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem)

    for person in (anna, ben, cem):
        assert aufgabe.can_access(person), person.user.email
        assert services.can_edit_task(aufgabe, person), person.user.email
    # Ohne Bezug bleibt die private Aufgabe verschlossen, wie bisher
    assert not aufgabe.can_access(dora)
    assert not services.can_edit_task(aufgabe, dora)


def test_listen_nennen_die_aufgabe_fuer_weitere_zustaendige_genau_einmal(
    org: Any, anna: Any, ben: Any, cem: Any, dora: Any
) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem, dora)
    basis = Task.objects.filter(organization=org)

    for person in (anna, ben, cem, dora):
        assert list(selectors.visible_tasks(org, person)) == [aufgabe], person.user.email
        assert list(selectors.own_tasks(person, base=basis)) == [aufgabe], person.user.email
    # Der Filter „Zuständig“ des Boards findet auch weitere Zuständige
    for person in (ben, cem, dora):
        assert list(selectors.apply_board_filters(basis, assignee_id=str(person.pk))) == [aufgabe]
    assert list(selectors.apply_board_filters(basis, assignee_id=str(anna.pk))) == []
    # Zählungen stimmen ohne Dopplung, auch mit mehreren weiteren Zuständigen
    assert basis.filter(selectors.zustaendig_q(cem)).count() == 1


def test_start_und_uebersicht_zaehlen_weitere_zustaendige_mit(org: Any, anna: Any, ben: Any, cem: Any) -> None:
    from apps.work.organization.selectors import activity_statistics

    aufgabe = _aufgabe(org, anna, ben, cem)
    client_seite = reverse("work:dashboard", kwargs={"org_slug": org.slug})
    from django.test import Client

    client = Client()
    client.force_login(cem.user)

    assert aufgabe.title in client.get(client_seite).content.decode()
    assert activity_statistics(org, cem)["tasks_total"] == 1


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------


def test_neue_aufgabe_mit_weiteren_zustaendigen(org: Any, anna: Any, ben: Any, cem: Any, client_for: Any) -> None:
    client = client_for(anna.user)
    url = reverse("work:task_create", kwargs={"org_slug": org.slug})

    seite = client.get(url).content.decode()
    assert f'name="{WEITERE_GESENDET}"' in seite and 'name="weitere_zustaendige"' in seite

    antwort = client.post(
        url,
        {
            "title": "Haushaltsrede vorbereiten",
            "priority": "medium",
            "status": "todo",
            "visibility": "private",
            "assigned_to": str(ben.pk),
            # Die zugewiesene Person steht nicht noch einmal unter den weiteren
            "weitere_zustaendige": [str(cem.pk), str(ben.pk), str(anna.pk)],
            WEITERE_GESENDET: "1",
        },
    )

    assert antwort.status_code == 302
    aufgabe = Task.objects.get(title="Haushaltsrede vorbereiten")
    assert aufgabe.assigned_to == ben
    assert {m.pk for m in aufgabe.weitere_zustaendige.all()} == {cem.pk, anna.pk}
    # Benachrichtigt werden alle neuen Zuständigen außer der anlegenden Person
    assert _zugewiesen_an(aufgabe) == {ben.user.email, cem.user.email}


def test_gastzugang_und_fremde_organisation_werden_nicht_zustaendig(
    org: Any, anna: Any, ben: Any, make_member: Any, client_for: Any
) -> None:
    from apps.tenants.models import Organization

    gast = _mitglied(org, make_member, "gast")
    Membership.objects.filter(pk=gast.pk).update(is_guest=True)
    andere = Organization.objects.create(name="Andere Fraktion", slug="andere-fraktion")
    fremd = _mitglied(andere, make_member, "fremd")
    url = reverse("work:task_create", kwargs={"org_slug": org.slug})

    for person in (gast, fremd):
        antwort = client_for(anna.user).post(
            url,
            {
                "title": f"Aufgabe für {person.user.email}",
                "priority": "medium",
                "status": "todo",
                "visibility": "private",
                "weitere_zustaendige": [str(person.pk)],
                WEITERE_GESENDET: "1",
            },
        )
        # Ungültige Auswahl: Das Formular kommt zurück, angelegt wird nichts
        assert antwort.status_code == 200, person.user.email
        assert not Task.objects.filter(title=f"Aufgabe für {person.user.email}").exists()

    # Auch am Formular vorbei speichert der Dienst sie nicht
    aufgabe = services.create_task(
        Task(title="Direkt", visibility="private"), org, anna, weitere_zustaendige=[_frisch(gast), fremd, ben]
    )
    assert {m.pk for m in aufgabe.weitere_zustaendige.all()} == {ben.pk}


def _frisch(member: Any) -> Any:
    return Membership.objects.get(pk=member.pk)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def _panel(client: Any, org: Any, aufgabe: Task, **felder: Any) -> Any:
    daten = {
        "action": "update",
        "title": aufgabe.title,
        "description": aufgabe.description,
        "status": aufgabe.status,
        "priority": aufgabe.priority,
        "visibility": aufgabe.visibility,
        "assigned_to": str(aufgabe.assigned_to_id or ""),
        **felder,
    }
    url = reverse("work:task_panel_action", kwargs={"org_slug": org.slug, "task_id": aufgabe.pk})
    return client.post(url, daten, HTTP_HX_REQUEST="true")


def test_panel_zeigt_auswahl_und_speichert_sie(
    org: Any, anna: Any, ben: Any, cem: Any, dora: Any, client_for: Any
) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem)
    client = client_for(anna.user)
    url = reverse("work:task_panel", kwargs={"org_slug": org.slug, "task_id": aufgabe.pk})

    panel = client.get(url, HTTP_HX_REQUEST="true").content.decode()
    assert f'name="{WEITERE_GESENDET}"' in panel
    assert f'value="{cem.pk}" class' in panel.replace("  ", " ") or f'value="{cem.pk}"' in panel
    # Das Formular ist noch untypisiert (forms.py); der Test liest es als Any
    formular = cast(Any, TaskPanelForm)(instance=aufgabe, organization=org)
    assert formular.initial["weitere_zustaendige"] == [cem.pk]

    # Dora kommt dazu, Cem entfällt
    antwort = _panel(client, org, aufgabe, **{"weitere_zustaendige": [str(dora.pk)], WEITERE_GESENDET: "1"})

    assert antwort.status_code == 200
    aufgabe.refresh_from_db()
    assert {m.pk for m in aufgabe.weitere_zustaendige.all()} == {dora.pk}
    # Nur wer neu dazukommt, wird benachrichtigt
    assert _zugewiesen_an(aufgabe) == {dora.user.email}
    eintrag = TaskActivity.objects.filter(task=aufgabe, activity_type="assigned").latest("created_at")
    assert eintrag.details == {
        "field": "weitere_zustaendige",
        "old": "Weitere Zuständige: Cem Beispiel",
        "new": "Weitere Zuständige: Dora Beispiel",
    }


def test_panel_ohne_marke_laesst_die_weiteren_zustaendigen_stehen(
    org: Any, anna: Any, ben: Any, cem: Any, client_for: Any
) -> None:
    """Ein Panel von vor der Funktion sendet das Feld nicht mit; die Auswahl darf dabei nicht verloren gehen."""
    aufgabe = _aufgabe(org, anna, ben, cem)

    antwort = _panel(client_for(anna.user), org, aufgabe, priority="high")

    assert antwort.status_code == 200
    aufgabe.refresh_from_db()
    assert aufgabe.priority == "high"
    assert {m.pk for m in aufgabe.weitere_zustaendige.all()} == {cem.pk}
    assert not TaskActivity.objects.filter(task=aufgabe, details__field="weitere_zustaendige").exists()


def test_panel_leere_auswahl_mit_marke_entfernt_alle(org: Any, anna: Any, ben: Any, cem: Any, client_for: Any) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem)

    antwort = _panel(client_for(anna.user), org, aufgabe, **{WEITERE_GESENDET: "1"})

    assert antwort.status_code == 200
    assert not Task.objects.get(pk=aufgabe.pk).weitere_zustaendige.exists()


def test_weitere_zustaendige_person_wird_zugewiesen_und_steht_nicht_doppelt(
    org: Any, anna: Any, ben: Any, cem: Any, client_for: Any
) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem)

    # Mit Auswahl: die neue zugewiesene Person fällt aus den weiteren heraus
    _panel(
        client_for(anna.user),
        org,
        aufgabe,
        **{"assigned_to": str(cem.pk), "weitere_zustaendige": [str(cem.pk), str(ben.pk)], WEITERE_GESENDET: "1"},
    )
    aufgabe = Task.objects.get(pk=aufgabe.pk)
    assert aufgabe.assigned_to == cem
    assert {m.pk for m in aufgabe.weitere_zustaendige.all()} == {ben.pk}

    # Ohne Auswahl (altes Panel) ebenso
    _panel(client_for(anna.user), org, aufgabe, assigned_to=str(ben.pk))
    aufgabe = Task.objects.get(pk=aufgabe.pk)
    assert aufgabe.assigned_to == ben
    assert not aufgabe.weitere_zustaendige.exists()


def test_weitere_zustaendige_person_bearbeitet_und_hakt_ab(
    org: Any, anna: Any, ben: Any, cem: Any, client_for: Any
) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem)
    client = client_for(cem.user)

    assert (
        _panel(
            client, org, aufgabe, priority="urgent", **{"weitere_zustaendige": [str(cem.pk)], WEITERE_GESENDET: "1"}
        ).status_code
        == 200
    )
    url = reverse("work:task_panel_action", kwargs={"org_slug": org.slug, "task_id": aufgabe.pk})
    assert client.post(url, {"action": "toggle_complete"}, HTTP_HX_REQUEST="true").status_code == 200

    aufgabe.refresh_from_db()
    assert aufgabe.priority == "urgent" and aufgabe.is_completed
    # Ein gemeinsamer Stand: Erstellende und übrige Zuständige erfahren es, wer abgehakt hat, nicht
    assert _empfaenger(aufgabe, NotificationType.TASK_COMPLETED) == {anna.user.email, ben.user.email}


def test_readonly_panel_und_karte_nennen_alle_zustaendigen(
    org: Any, anna: Any, ben: Any, cem: Any, make_member: Any, client_for: Any
) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem, visibility="organization")
    lesend = _mitglied(org, make_member, "lesend", rechte=["dashboard.view", "tasks.view"])
    client = client_for(lesend.user)

    panel = client.get(
        reverse("work:task_panel", kwargs={"org_slug": org.slug, "task_id": aufgabe.pk}), HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "Weitere Zuständige" in panel and "Cem Beispiel" in panel
    assert f'name="{WEITERE_GESENDET}"' not in panel

    board = client.get(reverse("work:tasks", kwargs={"org_slug": org.slug}) + "?view=all").content.decode()
    assert "Ben Beispiel" in board
    assert "und 1 weitere" in board


# ---------------------------------------------------------------------------
# Kommentare, Erinnerung, Entfernen eines Mitglieds, Export
# ---------------------------------------------------------------------------


def test_kommentar_geht_an_alle_zustaendigen(org: Any, anna: Any, ben: Any, cem: Any) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem)

    services.add_comment(aufgabe, ben, "Entwurf liegt im Ordner.")

    assert _empfaenger(aufgabe, NotificationType.TASK_COMMENT) == {anna.user.email, cem.user.email}


def test_erinnerung_an_die_faelligkeit_geht_an_alle_zustaendigen(org: Any, anna: Any, ben: Any, cem: Any) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem, due_date=timezone.localdate() + timedelta(days=1))
    # Deaktivierte Zuständige werden übersprungen, die übrigen erinnert
    Membership.objects.filter(pk=ben.pk).update(is_active=False)

    call_command("send_task_due_reminders")
    call_command("send_task_due_reminders")

    assert _empfaenger(aufgabe, NotificationType.TASK_DUE_SOON) == {cem.user.email}
    assert Notification.objects.filter(notification_type=NotificationType.TASK_DUE_SOON).count() == 1


def test_aufgabe_mit_weiteren_zustaendigen_ist_nicht_persoenlich(org: Any, anna: Any, ben: Any) -> None:
    """Persönliche Aufgaben entfallen mit der Mitgliedschaft; eine Aufgabe, für die noch jemand zuständig ist, bleibt."""
    allein = _aufgabe(org, anna, anna, title="Nur meine")
    geteilt = _aufgabe(org, anna, anna, ben, title="Mit Ben")

    assert allein.is_personal and not geteilt.is_personal
    assert list(selectors.personal_tasks(anna)) == [allein]
    assert services.delete_personal_tasks(anna) == 1
    assert Task.objects.filter(pk=geteilt.pk).exists()


def test_entferntes_mitglied_faellt_aus_den_weiteren_zustaendigen(org: Any, anna: Any, ben: Any, cem: Any) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem)

    cem.delete()

    assert [m.pk for m in Task.objects.get(pk=aufgabe.pk).zustaendige()] == [ben.pk]


def test_export_nennt_die_weiteren_zustaendigen(org: Any, anna: Any, ben: Any, cem: Any) -> None:
    aufgabe = _aufgabe(org, anna, ben, cem, anna)
    aufgaben = list(selectors.export_queryset(org, anna))

    assert export_service.serialize_task(aufgaben[0])["weitere_zustaendige"] == [anna.user.email, cem.user.email]
    csv_text, _typ = export_service.render_csv(aufgaben)
    kopf, zeile = csv_text.lstrip("﻿").strip().split("\r\n")
    assert kopf.split(";")[-1] == "Weitere Zuständige"
    assert zeile.split(";")[-1] == f"{anna.user.email}, {cem.user.email}"
    assert json.loads(export_service.render_json(aufgaben, org)[0])["tasks"][0]["id"] == str(aufgabe.pk)


def test_persoenlicher_datenexport_nennt_aufgaben_der_weiteren_zustaendigen(
    org: Any, anna: Any, ben: Any, make_member: Any
) -> None:
    """Wofür die Person zuständig ist, gehört zu ihrer Auskunft, auch ohne das Recht, Aufgaben anderer zu sehen."""
    emil = _mitglied(org, make_member, "emil", rechte=["dashboard.view"])
    aufgabe = _aufgabe(org, anna, ben, emil, title="Rede gegenlesen")
    TaskActivity.objects.create(task=aufgabe, actor=emil, activity_type="status_changed")
    _aufgabe(org, anna, ben, title="Ohne Emil")
    dienst = DsgvoExportService()

    eintraege = dienst._collect_tasks(emil, org)
    vermerke = [v for v in dienst._collect_vermerke(emil, org) if v["area"] == "Aufgaben"]

    assert [(e["title"], e["is_assignee"], e["is_creator"]) for e in eintraege] == [("Rede gegenlesen", True, False)]
    assert len(vermerke) == 1 and vermerke[0]["object"].startswith("Rede gegenlesen")


def test_persoenlicher_datenexport_fragt_nicht_je_aufgabe_nach_den_zustaendigen(
    org: Any, anna: Any, ben: Any, cem: Any
) -> None:
    def abfragen() -> int:
        member = Membership.objects.get(pk=cem.pk)
        member.has_permission("tasks.view")  # Rechte sind in einer Anfrage schon geladen
        with CaptureQueriesContext(connection) as erfasst:
            eintraege = DsgvoExportService()._collect_tasks(member, org)
        assert eintraege and all(e["is_assignee"] for e in eintraege)
        return len(erfasst)

    _aufgabe(org, anna, ben, cem)
    wenige = abfragen()
    for nummer in range(5):
        _aufgabe(org, anna, ben, cem, title=f"Weitere {nummer}")

    assert abfragen() == wenige


# ---------------------------------------------------------------------------
# Aufgaben aus nichtöffentlichen TOPs: weitere Zuständige nur Vereidigte
# ---------------------------------------------------------------------------


def test_nichtoeffentlicher_top_bietet_nur_vereidigte_als_weitere_zustaendige(
    org: Any, cem: Any, make_member: Any, client_for: Any
) -> None:
    # Vereidigt ist, wer das Häkchen und das Recht auf den nichtöffentlichen Teil hat (faction.visibility)
    vereidigt = [*RECHTE, "faction.view_public", "faction.view_non_public"]
    vera = _mitglied(org, make_member, "vera", rechte=vereidigt)
    viktor = _mitglied(org, make_member, "viktor", rechte=vereidigt)
    Membership.objects.filter(pk__in=[vera.pk, viktor.pk]).update(is_sworn_in=True)
    vera, viktor = _frisch(vera), _frisch(viktor)
    sitzung = FactionMeeting.objects.create(organization=org, title="Fraktionssitzung", start=timezone.now())
    top = FactionAgendaItem.objects.create(meeting=sitzung, title="Grundstück", number="1", visibility="internal")
    aufgabe = _aufgabe(org, vera, vera, title="Angebot prüfen", related_faction_agenda_item=top)

    # Das Formular ist noch untypisiert (forms.py); der Test liest es als Any
    formular = cast(Any, TaskPanelForm)(instance=aufgabe, organization=org)
    angeboten = {m.pk for m in formular.fields["weitere_zustaendige"].queryset}
    assert viktor.pk in angeboten and cem.pk not in angeboten

    # Eine vereidigte Person lässt sich eintragen
    antwort = _panel(
        client_for(vera.user), org, aufgabe, **{"weitere_zustaendige": [str(viktor.pk)], WEITERE_GESENDET: "1"}
    )
    assert antwort.status_code == 200
    assert {m.pk for m in Task.objects.get(pk=aufgabe.pk).weitere_zustaendige.all()} == {viktor.pk}

    # Eine nicht vereidigte nicht: abgelehnt, nichts geändert, und Cem erfährt den Titel nicht
    antwort = _panel(
        client_for(vera.user),
        org,
        aufgabe,
        **{"weitere_zustaendige": [str(viktor.pk), str(cem.pk)], WEITERE_GESENDET: "1"},
    )
    assert antwort.status_code == 422
    assert {m.pk for m in Task.objects.get(pk=aufgabe.pk).weitere_zustaendige.all()} == {viktor.pk}
    assert not Notification.objects.filter(recipient=cem).exists()
