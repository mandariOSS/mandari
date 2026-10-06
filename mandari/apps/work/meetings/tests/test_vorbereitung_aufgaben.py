# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben aus einem TOP der neuen Sitzungsvorbereitung (#856).

Lesen nach den Sichtbarkeitsregeln des Aufgaben-Moduls, Anlegen nur mit „Aufgaben erstellen“, Zuständige nur aus der
eigenen Organisation (keine Gastzugänge), TOP und Sitzung verknüpft, Org-Grenze für fremde Sitzungen, Abhaken nur, wer
die Aufgabe bearbeiten darf (über den Endpunkt des Aufgabenboards). Der Reiter erscheint nur in der neuen
Vorbereitung; vorhandene Aufgaben bleiben unverändert.
"""

import json
import re
from typing import Any, cast

import pytest
from django.urls import reverse

from apps.common.tests.factories import OrganizationFactory
from apps.work.tasks.models import Task
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org"
RECHTE = ["meetings.prepare", "tasks.view", "tasks.create"]


@pytest.fixture
def sitzung(org: Any) -> OParlMeeting:
    source = OParlSource.objects.create(name="Test-RIS", url=f"{RIS}/system")
    org.body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Stadt Test")
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=org.body, name="Rat")
    for nr in (1, 2):
        OParlAgendaItem.objects.create(
            external_id=f"{RIS}/agenda/{nr}", meeting=meeting, number=str(nr), name=f"TOP {nr}", order=nr
        )
    return meeting


def _url(org: Any, meeting: OParlMeeting, item: OParlAgendaItem) -> str:
    return f"/work/{org.slug}/meetings/{meeting.id}/tasks/{item.id}/"


def _post(client: Any, url: str, **daten: Any) -> Any:
    return client.post(url, data=json.dumps(daten), content_type="application/json")


def test_liste_zeigt_nur_sichtbare_aufgaben_des_tops(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    ich = make_member(org, RECHTE, email="ich@example.org")
    andere = make_member(org, RECHTE, email="andere@example.org")
    top1, top2 = sitzung.agenda_items.order_by("order")
    Task.objects.create(organization=org, title="Für alle", visibility="organization", related_agenda_item=top1)
    Task.objects.create(
        organization=org, title="Privat von anderen", visibility="private", created_by=andere, related_agenda_item=top1
    )
    Task.objects.create(organization=org, title="Anderer TOP", visibility="organization", related_agenda_item=top2)

    response = client_for(ich.user).get(_url(org, sitzung, top1))

    assert response.status_code == 200
    assert [t["title"] for t in response.json()["tasks"]] == ["Für alle"]


def test_anlegen_verknuepft_top_und_sitzung(org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any) -> None:
    ich = make_member(org, RECHTE, email="ich@example.org")
    zustaendig = make_member(org, ["tasks.view"], email="zustaendig@example.org")
    top = sitzung.agenda_items.get(number="2")

    response = _post(
        client_for(ich.user),
        _url(org, sitzung, top),
        title="Rückfrage an die Verwaltung",
        assigned_to=str(zustaendig.id),
        due_date="2026-10-19",
    )

    assert response.status_code == 200, response.content
    task = Task.objects.get(title="Rückfrage an die Verwaltung")
    assert task.organization == org
    assert task.related_agenda_item == top
    assert task.related_meeting == sitzung
    assert task.assigned_to == zustaendig
    assert task.created_by == ich
    assert task.visibility == "organization"
    assert str(task.due_date) == "2026-10-19"
    assert response.json()["task"]["due"] == "19.10.2026"


def test_anlegen_ohne_recht_oder_mit_fremder_person_scheitert(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    nur_lesen = make_member(org, ["meetings.prepare", "tasks.view"], email="lesen@example.org")
    ich = make_member(org, RECHTE, email="ich@example.org")
    fremde_org: Any = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
    fremd = make_member(fremde_org, RECHTE, email="fremd@example.org")
    top = sitzung.agenda_items.get(number="1")
    url = _url(org, sitzung, top)

    assert _post(client_for(nur_lesen.user), url, title="Darf nicht").status_code == 403
    assert _post(client_for(ich.user), url, title="", assigned_to="").status_code == 400
    antwort = _post(client_for(ich.user), url, title="Fremd zuweisen", assigned_to=str(fremd.id))
    assert antwort.status_code == 400
    assert "Organisation" in antwort.json()["error"]
    assert not Task.objects.exists()


def test_fremde_sitzung_bleibt_unerreichbar(org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any) -> None:
    fremde_org: Any = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
    quelle = OParlSource.objects.create(name="Anderes RIS", url="https://anderes.example.org/system")
    fremde_org.body = OParlBody.objects.create(
        external_id="https://anderes.example.org/body/1", source=quelle, name="Andere Stadt"
    )
    fremde_org.save(update_fields=["body"])
    fremd = make_member(fremde_org, RECHTE, email="fremd@example.org")
    top = sitzung.agenda_items.get(number="1")
    url = f"/work/{fremde_org.slug}/meetings/{sitzung.id}/tasks/{top.id}/"

    assert client_for(fremd.user).get(url).status_code == 404
    assert _post(client_for(fremd.user), url, title="Leck").status_code == 404
    assert not Task.objects.exists()


def test_seite_liefert_aufgaben_konfiguration(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    ich = make_member(org, RECHTE, email="ich@example.org")
    gast = make_member(org, ["meetings.prepare"], email="gast@example.org")
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])
    make_member(org, ["tasks.view"], first_name="Zora", last_name="Zett")
    make_member(org, ["tasks.view"], first_name="Anton", last_name="Abel")
    top = sitzung.agenda_items.get(number="2")
    # Zwei sichtbare Aufgaben am selben TOP zählen zweimal (die Sichtbarkeitsabfrage ist DISTINCT)
    Task.objects.create(organization=org, title="Für alle", visibility="organization", related_agenda_item=top)
    Task.objects.create(organization=org, title="Auch für alle", visibility="organization", related_agenda_item=top)

    html = client_for(ich.user).get(f"/work/{org.slug}/meetings/{sitzung.id}/prepare/").content.decode()
    match = re.search(r'<script[^>]*id="vorbereitung-aufgaben"[^>]*>(.*?)</script>', html, re.S)

    assert match
    config = json.loads(match.group(1))
    assert config["darfSehen"] is True
    assert config["darfAnlegen"] is True
    assert config["zahlen"] == {str(top.id): 2}
    assert config["ich"] == str(ich.id)
    assert config["abhaken"] == reverse("work:tasks_api", kwargs={"org_slug": org.slug})
    zustaendige = {m["id"] for m in config["mitglieder"]}
    assert str(ich.id) in zustaendige
    assert str(gast.id) not in zustaendige
    # Auswahl alphabetisch nach Anzeigenamen
    namen = [m["name"] for m in config["mitglieder"]]
    assert namen == sorted(namen, key=str.casefold)
    assert namen.index("Anton Abel") < namen.index("Zora Zett")


def test_bisherige_ansicht_ohne_aufgaben_reiter(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    ich = make_member(org, RECHTE, email="ich@example.org")
    html = client_for(ich.user).get(f"/work/{org.slug}/meetings/{sitzung.id}/prepare/").content.decode()

    assert 'id="vorbereitung-aufgaben"' not in html


def test_gast_und_ungueltige_angaben_werden_abgelehnt(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    ich = make_member(org, RECHTE, email="ich@example.org")
    gast = make_member(org, RECHTE, email="gast@example.org")
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])
    top = sitzung.agenda_items.get(number="1")
    url = _url(org, sitzung, top)

    # Gastzugänge leitet Work vorher um; die Schnittstelle selbst lehnt sie zusätzlich ab
    assert _post(client_for(gast.user), url, title="Gast").status_code in (302, 403)
    assert client_for(gast.user).get(url).status_code in (302, 403)
    zu_gast = _post(client_for(ich.user), url, title="An Gast", assigned_to=str(gast.id))
    assert zu_gast.status_code == 400
    datum = _post(client_for(ich.user), url, title="Datum", due_date="31.12.2026")
    assert datum.status_code == 400
    assert "Datum" in datum.json()["error"]
    lang = _post(client_for(ich.user), url, title="x" * 201)
    assert lang.status_code == 400
    assert not Task.objects.exists()


def test_ohne_zustaendige_person_bin_ich_zustaendig_und_bestand_bleibt(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    ich = make_member(org, RECHTE, email="ich@example.org")
    top = sitzung.agenda_items.get(number="1")
    vorher = Task.objects.create(organization=org, title="Schon da", visibility="private", created_by=ich)
    stand_vorher = Task.objects.filter(pk=vorher.pk).values().get()

    antwort = _post(client_for(ich.user), _url(org, sitzung, top), title="  Selbst erledigen  ")

    assert antwort.status_code == 200
    neu = Task.objects.get(title="Selbst erledigen")
    assert neu.assigned_to == ich
    assert neu.due_date is None
    assert Task.objects.filter(pk=vorher.pk).values().get() == stand_vorher


def test_abhaken_nur_wer_die_aufgabe_bearbeiten_darf(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    ich = make_member(org, RECHTE, email="ich@example.org")
    andere = make_member(org, RECHTE, email="andere@example.org")
    top = sitzung.agenda_items.get(number="1")
    eigene = Task.objects.create(
        organization=org, title="Eigene", visibility="organization", created_by=ich, related_agenda_item=top
    )
    fremde = Task.objects.create(
        organization=org, title="Fremde", visibility="organization", created_by=andere, related_agenda_item=top
    )
    client = client_for(ich.user)

    liste = {t["title"]: t for t in client.get(_url(org, sitzung, top)).json()["tasks"]}
    assert liste["Eigene"]["canToggle"] is True
    assert liste["Fremde"]["canToggle"] is False
    neu = _post(client, _url(org, sitzung, top), title="Gerade angelegt").json()["task"]
    assert neu["canToggle"] is True

    # Abhaken über den Endpunkt des Aufgabenboards, den die Seite in ``aufgaben_config["abhaken"]`` nennt
    board = reverse("work:tasks_api", kwargs={"org_slug": org.slug})
    antwort = client.post(board, {"action": "toggle_complete", "task_id": str(eigene.id)})
    assert antwort.status_code == 200
    assert antwort.json()["is_completed"] is True
    assert client.post(board, {"action": "toggle_complete", "task_id": str(fremde.id)}).status_code == 403
    eigene.refresh_from_db()
    fremde.refresh_from_db()
    assert eigene.is_completed is True
    assert fremde.is_completed is False
    assert client.get(_url(org, sitzung, top)).json()["tasks"][-1]["done"] is True
