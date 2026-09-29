# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben-Labels löschen (Issue #585).

Labels ließen sich anlegen, aber nicht löschen: Die Schnittstelle existierte, die Oberfläche rief sie
nicht auf. Jetzt bietet die Label-Verwaltung im Aufgaben-Panel das Löschen mit Rückfrage an (Recht
``tasks.manage``); Aufgaben verlieren das Label und bleiben erhalten.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import reverse

from apps.common.tests.factories import OrganizationFactory
from apps.work.tasks.models import Task, TaskLabel


@pytest.fixture
def verwaltung(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create", "tasks.manage"], email="verwaltung@example.org")


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create"], email="mitglied@example.org")


@pytest.fixture
def labels(org: Any) -> dict[str, TaskLabel]:
    return {
        "haushalt": TaskLabel.objects.create(organization=org, name="Haushalt", color="green"),
        "presse": TaskLabel.objects.create(organization=org, name="Presse", color="blue"),
    }


@pytest.fixture
def aufgabe(org: Any, mitglied: Any, labels: dict[str, TaskLabel]) -> Task:
    task = Task.objects.create(
        organization=org, title="Haushaltsrede", created_by=mitglied, assigned_to=mitglied, visibility="organization"
    )
    task.labels.add(labels["haushalt"], labels["presse"])
    return task


def _loeschen_url(org: Any, label: TaskLabel) -> str:
    return reverse("work:task_label_delete", kwargs={"org_slug": org.slug, "label_id": label.id})


@pytest.mark.django_db
def test_label_loeschen_behaelt_aufgaben(
    org: Any, verwaltung: Any, aufgabe: Task, labels: dict[str, TaskLabel], client_for: Any
) -> None:
    antwort = client_for(verwaltung.user).delete(_loeschen_url(org, labels["haushalt"]))

    assert antwort.status_code == 200
    assert not TaskLabel.objects.filter(pk=labels["haushalt"].pk).exists()
    aufgabe.refresh_from_db()
    assert list(aufgabe.labels.values_list("name", flat=True)) == ["Presse"]


@pytest.mark.django_db
def test_label_loeschen_nur_mit_label_verwaltung(
    org: Any, mitglied: Any, aufgabe: Task, labels: dict[str, TaskLabel], client_for: Any
) -> None:
    antwort = client_for(mitglied.user).delete(_loeschen_url(org, labels["haushalt"]))

    assert antwort.status_code == 403
    assert TaskLabel.objects.filter(pk=labels["haushalt"].pk).exists()
    assert aufgabe.labels.count() == 2


@pytest.mark.django_db
def test_label_fremder_organisation_bleibt(org: Any, verwaltung: Any, client_for: Any) -> None:
    fremd_org: Any = OrganizationFactory(name="Fraktion Fremd", slug="fraktion-fremd")  # type: ignore[no-untyped-call]
    fremd = TaskLabel.objects.create(organization=fremd_org, name="Fremd", color="red")

    antwort = client_for(verwaltung.user).delete(_loeschen_url(org, fremd))

    assert antwort.status_code == 404
    assert TaskLabel.objects.filter(pk=fremd.pk).exists()


@pytest.mark.django_db
def test_panel_bietet_loeschen_nur_der_label_verwaltung(
    org: Any, verwaltung: Any, mitglied: Any, aufgabe: Task, labels: dict[str, TaskLabel], client_for: Any
) -> None:
    url = reverse("work:task_panel", kwargs={"org_slug": org.slug, "task_id": aufgabe.id})
    loeschen = f'data-label-url="{_loeschen_url(org, labels["haushalt"])}"'

    html = client_for(verwaltung.user).get(url).content.decode()
    assert 'x-data="labelPicker"' in html
    assert loeschen in html
    assert "deleteLabel($el)" in html
    assert "Neues Label" in html

    # Die Erstellerin darf die Aufgabe bearbeiten, verwaltet aber keine Labels
    html = client_for(mitglied.user).get(url).content.decode()
    assert "Verfügbare Labels" in html
    assert loeschen not in html
    assert "Neues Label" not in html


@pytest.mark.django_db
def test_karten_kennzeichnen_ihre_labels(org: Any, mitglied: Any, aufgabe: Task, client_for: Any) -> None:
    """Das Board entfernt die Label-Chips gelöschter Labels ohne Neuladen (``data-card-label``)."""
    html = client_for(mitglied.user).get(reverse("work:tasks", kwargs={"org_slug": org.slug})).content.decode()
    assert f'data-card-label="{aufgabe.labels.get(name="Haushalt").id}"' in html
