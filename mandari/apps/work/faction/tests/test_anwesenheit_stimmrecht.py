# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwesenheitsliste und Stimmrecht in der laufenden Fraktionssitzung (Issue #1166).

- Beschlussfähigkeit, Gruppe „Stimmberechtigt“ und Niederschrift zählen gleich: Die Administrator-Rolle allein gibt
  kein Stimmrecht (vorher 5 statt 3 Stimmberechtigte, wenn zwei Personen nur Admin bzw. Admin und Geschäftsführung
  waren).
- Liste in Gruppen: Stimmberechtigt, Weitere Mitglieder, Gäste, je mit Zahl.
- Je Person ein Klick: vor Ort, online oder entschuldigt; die schon gewählte Angabe noch einmal gewählt setzt auf
  abwesend. Der Server nimmt Status und Teilnahmeart in einer Anfrage an, Statuswerte unverändert.
- Ohne Recht zur Pflege bleibt die Liste lesbar, ohne Bedienelemente.
- Je Gruppe nach dem angezeigten Namen (Vorname zuerst); aktive Mitglieder ohne Einladung stehen für alle, die die
  Sitzung verwalten, in der Gruppe „Nicht eingeladen“ und lassen sich mit einem Klick eintragen (Issue #1436).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.permissions import DEFAULT_ROLES, STIMMRECHT
from apps.common.tests.factories import OrganizationFactory, RoleFactory
from apps.work.faction import protokoll, sitzung
from apps.work.faction.models import FactionAttendance, FactionMeeting
from apps.work.faction.quorum import faction_quorum_status

pytestmark = pytest.mark.django_db

VORSITZ = ["faction.view_public", "faction.view_non_public", "faction.manage", "faction.start", STIMMRECHT]
MITGLIED = ["faction.view_public", STIMMRECHT]


def schalter_an(org: Any) -> None:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


def person(org: Any, make_member: Any, vorname: str, nachname: str, rechte: list[str], **kwargs: Any) -> Any:
    m = make_member(org, rechte, email=f"{vorname.lower()}@example.org", **kwargs)
    m.user.first_name, m.user.last_name = vorname, nachname
    m.user.save(update_fields=["first_name", "last_name"])
    return m


@pytest.fixture
def fraktion(org: Any, make_member: Any) -> dict[str, Any]:
    """Wie in der gemeldeten Sitzung: drei Stimmberechtigte, eine Person nur Admin, eine Admin und Geschäftsführung."""
    from apps.tenants.models import Role

    gf = DEFAULT_ROLES["managing_director"]
    geschaeftsfuehrung = Role.objects.filter(organization=org, name=gf["name"]).first() or cast(Any, RoleFactory)(
        organization=org, name=gf["name"], permissions=list(cast(list[str], gf["permissions"]))
    )
    admin_gf = person(org, make_member, "Gerd", "Admin", [], is_admin=True)
    admin_gf.roles.add(geschaeftsfuehrung)
    return {
        "vorsitz": person(org, make_member, "Vera", "Zander", VORSITZ),
        "berta": person(org, make_member, "Berta", "Brandt", MITGLIED),
        "carl": person(org, make_member, "Carl", "Clemens", MITGLIED),
        "anna": person(org, make_member, "Anna", "Admin", [], is_admin=True),
        "admin_gf": admin_gf,
    }


@pytest.fixture
def meeting(org: Any, fraktion: dict[str, Any]) -> FactionMeeting:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung Nr. 25",
        start=timezone.now() - timedelta(minutes=10),
        status="ongoing",
        created_by=fraktion["vorsitz"],
    )
    for m in fraktion.values():
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="invited")
    return meeting


def teilnahme(meeting: FactionMeeting, m: Any) -> FactionAttendance:
    return meeting.attendances.get(membership=m)


def setzen(meeting: FactionMeeting, m: Any, status: str) -> None:
    meeting.attendances.filter(membership=m).update(status=status)


def aktion_url(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_session_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


def seite(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


# ---- Stimmrecht: Beschlussfähigkeit und Niederschrift --------------------------------------------------------------


def test_beschlussfaehigkeit_zaehlt_admins_ohne_stimmrecht_nicht(meeting: FactionMeeting, fraktion: Any) -> None:
    for key in ("vorsitz", "anna", "admin_gf"):
        setzen(meeting, fraktion[key], "present")
    quorum = faction_quorum_status(meeting)
    # 3 Stimmberechtigte, davon einer da: nicht beschlussfähig, auch wenn beide Admins anwesend sind
    assert (quorum["voting_total"], quorum["voting_present"], quorum["required"], quorum["met"]) == (3, 1, 2, False)
    setzen(meeting, fraktion["berta"], "present")
    assert faction_quorum_status(meeting)["met"]


def test_admin_mit_einzelrecht_zaehlt_mit(meeting: FactionMeeting, fraktion: Any) -> None:
    from apps.tenants.models import Permission

    fraktion["anna"].individual_permissions.add(Permission.objects.get(codename=STIMMRECHT))
    assert faction_quorum_status(meeting)["voting_total"] == 4


def test_niederschrift_markiert_admins_ohne_stimmrecht(meeting: FactionMeeting, fraktion: Any) -> None:
    for key in ("vorsitz", "anna"):
        setzen(meeting, fraktion[key], "present")
    angaben = protokoll.angaben(meeting)
    assert angaben.ohne_stimme
    assert "Anna Admin*" in angaben.vor_ort
    assert "Vera Zander" in angaben.vor_ort


# ---- Anwesenheitsliste: Gruppen --------------------------------------------------------------------------------------


def test_gruppen_stimmberechtigte_zuerst_dann_weitere_dann_gaeste(
    meeting: FactionMeeting, fraktion: Any, org: Any, make_member: Any
) -> None:
    ehemalig = person(org, make_member, "Erik", "Ehemalig", MITGLIED)
    ehemalig.is_active = False
    ehemalig.save(update_fields=["is_active"])
    FactionAttendance.objects.create(meeting=meeting, membership=ehemalig, status="invited")
    sitzung.gast_hinzufuegen(meeting, "Gina Gast")

    anw = sitzung.anwesenheit(meeting)
    namen = {g.schluessel: [a.get_display_name() for a in g.teilnahmen] for g in anw.gruppen}
    assert [g.schluessel for g in anw.gruppen] == ["stimmberechtigt", "weitere", "gaeste"]
    assert [g.titel for g in anw.gruppen] == ["Stimmberechtigt", "Weitere Mitglieder", "Gäste"]
    # je Gruppe nach dem angezeigten Namen (Issue #1436); nicht mehr aktive Mitglieder zählen wie in der
    # Beschlussfähigkeit nicht als stimmberechtigt
    assert namen["stimmberechtigt"] == ["Berta Brandt", "Carl Clemens", "Vera Zander"]
    assert namen["weitere"] == ["Anna Admin", "Erik Ehemalig", "Gerd Admin"]
    # die Niederschrift nennt die Personen weiter nach Nachnamen
    assert [a.get_display_name() for a in anw.teilnahmen if not a.is_guest][-3:] == [
        "Carl Clemens",
        "Erik Ehemalig",
        "Vera Zander",
    ]
    assert namen["gaeste"] == ["Gina Gast"]
    assert len(namen["stimmberechtigt"]) == faction_quorum_status(meeting)["voting_total"]
    assert (anw.anwesend, anw.gesamt) == (0, 6)


def test_leere_gruppen_fehlen(org: Any, make_member: Any) -> None:
    vorsitz = person(org, make_member, "Vera", "Zander", VORSITZ)
    meeting = FactionMeeting.objects.create(
        organization=org, title="Kurz", start=timezone.now(), status="ongoing", created_by=vorsitz
    )
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="present")
    assert [g.schluessel for g in sitzung.anwesenheit(meeting).gruppen] == ["stimmberechtigt"]


def test_seite_zeigt_gruppen_mit_zahl_in_dieser_reihenfolge(
    org: Any, meeting: FactionMeeting, fraktion: Any, client_for: Any
) -> None:
    schalter_an(org)
    sitzung.gast_hinzufuegen(meeting, "Gina Gast")
    html = client_for(fraktion["vorsitz"].user).get(seite(org, meeting)).content.decode()
    # Ab der ersten Gruppe (davor nennen die Auswahlfelder Sitzungsleitung und Schriftführung dieselben Namen)
    liste = html[html.index("Stimmberechtigt · 3") :]
    weitere = liste.index("Weitere Mitglieder · 2")
    gaeste = liste.index("Gäste · 1")
    assert liste.index("Berta Brandt") < liste.index("Carl Clemens") < liste.index("Vera Zander") < weitere
    assert weitere < liste.index("Anna Admin") < liste.index("Gerd Admin") < gaeste < liste.index("Gina Gast")
    assert ", stimmberechtigt" not in html
    # Ein Klick je Person statt Häkchen und Auswahl abwesend/entschuldigt
    assert 'type="checkbox" data-id' not in html
    assert 'aria-label="Grund für' not in html
    assert html.count('data-wahl="excused"') == 5
    assert 'role="group" aria-label="Anwesenheit von Berta Brandt"' in html


# ---- Anwesenheitsliste: ein Klick je Person --------------------------------------------------------------------------


def _gedrueckt(html: str, name: str) -> list[str]:
    """Welche Angaben der Person gewählt sind (Beschriftung ohne Namen)."""
    return [
        wahl
        for wahl in ("vor Ort", "online", "entschuldigt")
        if f'aria-label="{name} {wahl}" aria-pressed="true"' in html
    ]


def test_ein_klick_setzt_status_und_teilnahmeart(
    org: Any, meeting: FactionMeeting, fraktion: Any, client_for: Any
) -> None:
    schalter_an(org)
    client = client_for(fraktion["vorsitz"].user)
    url = aktion_url(org, meeting)
    berta = teilnahme(meeting, fraktion["berta"])

    def klick(**werte: str) -> str:
        antwort = client.post(url, {"action": "anwesenheit", "attendance_id": berta.pk, **werte})
        assert antwort.status_code == 200
        berta.refresh_from_db()
        return str(antwort.content.decode())

    # „online“: anwesend und online in einer Anfrage
    html = klick(status="present", participation_type="online")
    assert (berta.status, berta.participation_type) == ("present", "online")
    assert berta.checked_in_at is not None
    assert _gedrueckt(html, "Berta Brandt") == ["online"]
    assert "Nicht beschlussfähig." in html and "1 von 3 Stimmberechtigten anwesend" in html

    # „vor Ort“: bleibt anwesend, Eintrittszeit bleibt
    eingetreten = berta.checked_in_at
    html = klick(status="present", participation_type="onsite")
    assert (berta.status, berta.participation_type, berta.checked_in_at) == ("present", "onsite", eingetreten)
    assert _gedrueckt(html, "Berta Brandt") == ["vor Ort"]

    # „entschuldigt“
    html = klick(status="excused")
    assert berta.status == "excused"
    assert _gedrueckt(html, "Berta Brandt") == ["entschuldigt"]

    # Noch einmal „entschuldigt“: abwesend, nichts gewählt
    html = klick(status="absent")
    assert berta.status == "absent"
    assert _gedrueckt(html, "Berta Brandt") == []


def test_absage_gilt_als_entschuldigt(org: Any, meeting: FactionMeeting, fraktion: Any, client_for: Any) -> None:
    """Wie in der Niederschrift: Wer abgesagt hat, steht als entschuldigt."""
    schalter_an(org)
    setzen(meeting, fraktion["carl"], "declined")
    html = client_for(fraktion["vorsitz"].user).get(seite(org, meeting)).content.decode()
    assert _gedrueckt(html, "Carl Clemens") == ["entschuldigt"]
    assert "Carl Clemens" in protokoll.angaben(meeting).entschuldigt


def test_ohne_pflegerecht_lesbar_ohne_bedienelemente(
    org: Any, meeting: FactionMeeting, fraktion: Any, client_for: Any
) -> None:
    schalter_an(org)
    meeting.attendances.filter(membership=fraktion["vorsitz"]).update(status="present", participation_type="online")
    setzen(meeting, fraktion["carl"], "excused")
    antwort = client_for(fraktion["berta"].user).get(seite(org, meeting))
    assert antwort.status_code == 200
    html = antwort.content.decode()
    assert "Stimmberechtigt · 3" in html
    assert "data-wahl" not in html
    assert "Ein Klick setzt die Teilnahme" not in html
    for text in ("nicht anwesend", "entschuldigt", "online"):
        assert f'<span class="fs-meta">{text}</span>' in html
    # Pflegen darf nur, wer die Sitzung verwaltet oder Schriftführung ist
    antwort = client_for(fraktion["berta"].user).post(
        aktion_url(org, meeting),
        {"action": "anwesenheit", "attendance_id": teilnahme(meeting, fraktion["anna"]).pk, "status": "present"},
    )
    assert antwort.status_code == 403


# ---- Reihenfolge und Mitglieder ohne Einladung (Issue #1436) ----------------------------------------------------------


def gastzugang(org: Any, make_member: Any) -> Any:
    m = person(org, make_member, "Gitta", "Gastzugang", [])
    m.is_guest = True
    m.save(update_fields=["is_guest"])
    return m


def test_reihenfolge_nach_angezeigtem_namen_ohne_gross_klein_und_umlaute(org: Any, make_member: Any) -> None:
    vorsitz = person(org, make_member, "Vera", "Zander", VORSITZ)
    meeting = FactionMeeting.objects.create(
        organization=org, title="Reihenfolge", start=timezone.now(), status="ongoing", created_by=vorsitz
    )
    for vorname, nachname in (("Oezlem", "Aksoy"), ("otto", "Zimmer"), ("Anke", "Zobel"), ("Paul", "Albers")):
        m = person(org, make_member, vorname, nachname, MITGLIED)
        FactionAttendance.objects.create(meeting=meeting, membership=m, status="invited")
    # Umlaut erst im Namen, nicht in der E-Mail-Adresse
    User.objects.filter(first_name="Oezlem").update(first_name="Özlem")

    gruppe = sitzung.anwesenheit(meeting).gruppen[0]
    assert [a.get_display_name() for a in gruppe.teilnahmen] == [
        "Anke Zobel",
        "otto Zimmer",
        "Özlem Aksoy",
        "Paul Albers",
    ]


def test_mitglieder_ohne_einladung_stehen_in_eigener_gruppe(
    org: Any, meeting: FactionMeeting, fraktion: Any, make_member: Any, client_for: Any
) -> None:
    schalter_an(org)
    neu = person(org, make_member, "Nora", "Neu", MITGLIED)
    gast = gastzugang(org, make_member)
    inaktiv = person(org, make_member, "Ingo", "Inaktiv", MITGLIED)
    inaktiv.is_active = False
    inaktiv.save(update_fields=["is_active"])

    assert sitzung.anwesenheit(meeting).uebrige == []  # nur auf Anforderung
    assert sitzung.anwesenheit(meeting, mit_uebrigen=True).uebrige == [neu]
    assert gast.pk and inaktiv.pk

    html = client_for(fraktion["vorsitz"].user).get(seite(org, meeting)).content.decode()
    liste = html[html.index("Nicht eingeladen · 1") :]
    assert f'data-mitglied="{neu.pk}" data-wahl="onsite"' in liste
    assert 'aria-label="Nora Neu vor Ort" aria-pressed="false"' in liste
    assert "Gitta Gastzugang" not in html and "Ingo Inaktiv" not in html

    # Ohne das Recht, die Sitzung zu verwalten: keine Gruppe, die Liste bleibt lesbar
    html = client_for(fraktion["berta"].user).get(seite(org, meeting)).content.decode()
    assert "Nicht eingeladen" not in html and "Nora Neu" not in html
    assert "Vera Zander" in html


def test_ein_klick_traegt_ein_mitglied_ohne_einladung_ein(
    org: Any, meeting: FactionMeeting, fraktion: Any, make_member: Any, client_for: Any
) -> None:
    schalter_an(org)
    neu = person(org, make_member, "Nora", "Neu", MITGLIED)
    client = client_for(fraktion["vorsitz"].user)
    url = aktion_url(org, meeting)

    antwort = client.post(
        url, {"action": "anwesenheit", "membership_id": neu.pk, "status": "present", "participation_type": "online"}
    )
    assert antwort.status_code == 200
    eintrag = meeting.attendances.get(membership=neu)
    assert (eintrag.status, eintrag.participation_type, eintrag.is_guest) == ("present", "online", False)
    assert eintrag.checked_in_at is not None
    html = antwort.content.decode()
    assert "Nicht eingeladen" not in html
    assert _gedrueckt(html, "Nora Neu") == ["online"]
    assert "Stimmberechtigt · 4" in html and "1 von 4 Stimmberechtigten anwesend" in html

    # Derselbe Klick noch einmal (doppelt gesendet): keine zweite Teilnahme, die Angabe wird gesetzt
    antwort = client.post(url, {"action": "anwesenheit", "membership_id": neu.pk, "status": "excused"})
    assert antwort.status_code == 200
    assert meeting.attendances.filter(membership=neu).count() == 1
    assert meeting.attendances.get(membership=neu).status == "excused"


def test_nachtragen_nur_mit_recht_und_nur_aktive_mitglieder_der_organisation(
    org: Any, meeting: FactionMeeting, fraktion: Any, make_member: Any, client_for: Any
) -> None:
    schalter_an(org)
    neu = person(org, make_member, "Nora", "Neu", MITGLIED)
    andere = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
    fremd = person(andere, make_member, "Frieda", "Fremd", MITGLIED)
    gast = gastzugang(org, make_member)
    url = aktion_url(org, meeting)
    werte = {"action": "anwesenheit", "status": "present", "participation_type": "onsite"}

    # Schriftführung ohne faction.manage pflegt die Anwesenheit, trägt aber niemanden nach
    meeting.minute_taker = fraktion["berta"]
    meeting.save(update_fields=["minute_taker"])
    schrift = client_for(fraktion["berta"].user)
    assert schrift.post(url, {**werte, "attendance_id": teilnahme(meeting, fraktion["carl"]).pk}).status_code == 200
    assert schrift.post(url, {**werte, "membership_id": neu.pk}).status_code == 403
    assert "Nicht eingeladen" not in schrift.get(seite(org, meeting)).content.decode()

    vorsitz = client_for(fraktion["vorsitz"].user)
    for kennung in (fremd.pk, gast.pk, "kaputt"):
        assert vorsitz.post(url, {**werte, "membership_id": kennung}).status_code == 400
    assert vorsitz.post(url, {"action": "anwesenheit", "membership_id": neu.pk, "status": "invited"}).status_code == 400
    assert not meeting.attendances.filter(membership__in=[neu, fremd, gast]).exists()


def test_lesende_sehen_wer_anwesend_ist(org: Any, meeting: FactionMeeting, fraktion: Any, client_for: Any) -> None:
    schalter_an(org)
    meeting.attendances.filter(membership=fraktion["vorsitz"]).update(status="present", participation_type="onsite")
    meeting.attendances.filter(membership=fraktion["carl"]).update(status="present", participation_type="online")
    meeting.attendances.filter(membership=fraktion["anna"]).update(status="excused")

    html = client_for(fraktion["berta"].user).get(seite(org, meeting)).content.decode()
    liste = str(html[html.index("Stimmberechtigt · 3") :])
    assert "data-wahl=" not in liste

    def stand(name: str) -> str:
        zeile = liste[liste.index(name) :]
        marke = '<span class="fs-meta">'
        return zeile[zeile.index(marke) + len(marke) : zeile.index("</span>", zeile.index(marke))]

    assert stand("Vera Zander") == "vor ort"
    assert stand("Carl Clemens") == "online"
    assert stand("Anna Admin") == "entschuldigt"
    assert stand("Berta Brandt") == "nicht anwesend"
