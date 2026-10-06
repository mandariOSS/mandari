# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work-Erscheinungsbild Stufe 0 (Issue #851).

Geprüft wird, was die Stufe zusagt, ohne dass eine Funktion wegfällt:

- Inter kommt aus dem eigenen Ursprung, Texte stehen in Sie-Form (Ausnahme: Mails, die die Organisation
  an ihre Mitglieder schickt, ``work/faction/email/``), kein Platzhalter nennt eine bestimmte Partei.
- Keine Einblend-Animation, keine Versalien-Überschriften und kein Lila/Violett/Cyan/Rosé als Schmuckfarbe
  in den Work-Templates (Positions- und Abschnittsfarben der Sitzungsvorbereitung bis zu deren Neuentwurf ausgenommen).
- Dokumente, Fraktionssitzungen und RIS-Übersicht nennen ihre Zahlen im Satz statt in Zählerkacheln; der
  Teilnahmenachweis steht unter „Weiteres“ nach der Liste.
- Nebenbefunde: „Einladen“ und „Vorbereiten“ je einmal, keine Sitzungsnummer „#—“, Gast in der Teamliste
  gekennzeichnet.
- Die alte Navigation kennzeichnet die aktive Seite, benennt die Knöpfe per ``aria-label`` und ist am Handy
  ein Dialog.
- Die umgebauten Seiten antworten im alten und im neuen Rahmen (#852) für Vorsitz, Mitglied, Sachkundige,
  nicht Vereidigte und Gäste ohne Serverfehler.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse
from django.utils import timezone

from apps.common.formatting import count_label, format_count, join_parts
from apps.common.tests import factories
from apps.tenants.models import Permission, Role
from apps.work.faction.models import FactionMeeting
from apps.work.motions.models import Motion
from apps.work.stats_text import documents_sentence, faction_meetings_sentence, ris_overview_sentence

BASIS = Path(__file__).resolve().parents[3]
WORK = BASIS / "templates" / "work"
CLASS_RE = re.compile(r'class="([^"]*)"')
#: Anrede in Du-Form (Wortgrenzen, auch mit HTML-Entitäten davor)
DU_RE = re.compile(
    r"(?<![\wäöüß&])(du|dein|deine|deinen|deinem|deiner|deines|dich|dir|bist|hast|kannst|musst|willst|wirst"
    r"|möchtest|m&ouml;chtest)(?![\wäöüß;])",
    re.IGNORECASE,
)


def _work_vorlagen(*, ohne: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    vorlagen = []
    for pfad in sorted(WORK.rglob("*")):
        if pfad.suffix not in (".html", ".txt"):
            continue
        rel = pfad.relative_to(WORK).as_posix()
        if any(rel.startswith(a) or a in rel for a in ohne):
            continue
        vorlagen.append((rel, pfad.read_text(encoding="utf-8")))
    return vorlagen


def _klassen(text: str) -> list[str]:
    return [token for wert in CLASS_RE.findall(text) for token in wert.split()]


def _alter_rahmen() -> dict[str, str]:
    """Der bisherige Rahmen (Schalter aus, #852): ``work/base_work_alt.html`` samt ausgelagerter Teile.

    ``work/base_work.html`` ist seit #852 nur noch der Verteiler auf den alten bzw. neuen Rahmen; Änderungen am
    bisherigen Rahmen stehen in ``base_work_alt*.html``.
    """
    dateien = {p.name: p.read_text(encoding="utf-8") for p in sorted(WORK.glob("base_work_alt*.html"))}
    assert "base_work_alt.html" in dateien and "base_work_alt_stil.html" in dateien, sorted(dateien)
    return dateien


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, is_admin=True, email="vorsitz@example.org")


# ---------------------------------------------------------------------------
# Schrift, Sprache, Farben (statisch über alle Work-Templates)
# ---------------------------------------------------------------------------


class TestSchriftUndSprache:
    def test_inter_kommt_aus_dem_eigenen_ursprung(self) -> None:
        basis = _alter_rahmen()["base_work_alt.html"]
        assert "{% static 'css/schrift-inter.css' %}" in basis
        assert "{% static 'vendor/inter/inter-latin.woff2' %}" in basis
        # Beide Rahmen laden keine Schrift von Dritten (der neue bringt Inter selbst mit, #852)
        for text in (*_alter_rahmen().values(), (WORK / "base_work_neu.html").read_text(encoding="utf-8")):
            assert "fonts.googleapis" not in text and "fonts.gstatic" not in text

        css_pfad = finders.find("css/schrift-inter.css")
        assert css_pfad, "css/schrift-inter.css fehlt"
        css = Path(str(css_pfad)).read_text(encoding="utf-8")
        assert css.count("@font-face") == 2 and "font-family: 'Inter'" in css
        for ziel in re.findall(r'url\("([^"]+)"\)', css):
            assert not ziel.startswith(("http:", "https:", "//")), ziel
            assert finders.find(f"vendor/inter/{Path(ziel).name}"), ziel

    def test_keine_du_form_in_der_work_oberflaeche(self) -> None:
        funde = [
            f"{rel}: {m.group(0)}"
            for rel, text in _work_vorlagen(ohne=("faction/email/",))
            for m in DU_RE.finditer(re.sub(r"\{#.*?#\}|\{% comment %\}.*?\{% endcomment %\}", "", text, flags=re.S))
        ]
        assert not funde, funde

    def test_gemeinsamer_bestaetigungsdialog_in_sie_form(self) -> None:
        quelle = (BASIS / "frontend" / "js" / "htmx-setup.ts").read_text(encoding="utf-8")
        assert "Sind Sie sicher?" in quelle
        assert "Bist du sicher" not in quelle

    def test_kein_platzhalter_nennt_eine_partei(self) -> None:
        for rel, text in _work_vorlagen():
            for platzhalter in re.findall(r'placeholder="([^"]*)"', text):
                assert not re.search(r"\b(volt|spd|cdu|csu|fdp|grüne|linke|afd)\b", platzhalter, re.I), (
                    rel,
                    platzhalter,
                )
        einstellungen = (WORK / "organization" / "settings.html").read_text(encoding="utf-8")
        assert 'placeholder="Name der Partei"' in einstellungen


class TestRuhigesErscheinungsbild:
    def test_keine_einblend_animation(self) -> None:
        funde = [rel for rel, text in _work_vorlagen() if {"fade-up", "d1", "d2", "d3", "d4"} & set(_klassen(text))]
        assert not funde, funde
        # Auch die Animation selbst (@keyframes, .fade-up, .d1–.d4) ist aus dem Stil des alten Rahmens entfernt
        for name, text in _alter_rahmen().items():
            assert "fade-up" not in text and "animation-delay" not in text, name

    def test_keine_versalien_ueberschriften(self) -> None:
        funde = [rel for rel, text in _work_vorlagen(ohne=("/pdf/", "/email/")) if "uppercase" in _klassen(text)]
        assert not funde, funde
        # Gruppenüberschriften der Leiste im alten Rahmen: normale Schreibung (CSS in base_work_alt_stil.html)
        for name, text in _alter_rahmen().items():
            assert "text-transform: uppercase" not in text, name

    def test_kein_lila_cyan_oder_rose_als_schmuckfarbe(self) -> None:
        # Sitzungsvorbereitung: Positions- und Abschnittsfarben sind dort Kategorien mit Legende (Vertagen und
        # Redebeitrag = Lila); sie bleiben bis zum Neuentwurf der Vorbereitung (#856) unverändert
        vorbereitung = (
            "meetings/_summary.html",
            "meetings/partials/_prepare_main.html",
            "meetings/partials/_prepare_modals.html",
        )
        muster = re.compile(r"(?<![\w-])(?:[a-z-]+:)*[a-z-]+-(?:purple|violet|cyan|rose|pink|fuchsia)-\d")
        funde = [
            f"{rel}: {zeile.strip()[:80]}"
            for rel, text in _work_vorlagen(ohne=("/pdf/", "/email/", *vorbereitung))
            for zeile in text.splitlines()
            if muster.search(zeile)
        ]
        assert not funde, funde


# ---------------------------------------------------------------------------
# Zahlen im Satz
# ---------------------------------------------------------------------------


class TestZahlenImSatz:
    def test_zahl_und_aufzaehlung(self) -> None:
        assert format_count(1234) == "1.234"
        assert count_label(1, "Entwurf", "Entwürfe") == "1 Entwurf"
        assert count_label(3, "Entwurf", "Entwürfe") == "3 Entwürfe"
        assert count_label(2, "eingereicht") == "2 eingereicht"
        assert join_parts([]) == ""
        assert join_parts(["a", "", "b"]) == "a und b"
        assert join_parts(["a", "b", "c"]) == "a, b und c"

    def test_dokumente(self) -> None:
        satz = documents_sentence(
            {"total": 6, "draft": 3, "submitted": 2, "in_consultation": 0, "completed": 1, "overdue": 1}
        )
        assert satz == "6 Dokumente: 3 Entwürfe, 2 eingereicht und 1 erledigt. Überfällig: 1."
        assert documents_sentence({"total": 0}) == "0 Dokumente."

    def test_fraktionssitzungen(self) -> None:
        assert faction_meetings_sentence({"total": 1, "upcoming": 1, "pending_protocol": 0}) == (
            "1 Sitzung, davon 1 anstehend."
        )
        assert faction_meetings_sentence({"total": 5, "upcoming": 0, "pending_protocol": 2}) == (
            "5 Sitzungen. Offene Protokolle: 2."
        )

    def test_ratsinformation(self) -> None:
        satz = ris_overview_sentence(
            {
                "papers_total": 1234,
                "papers_this_year": 56,
                "meetings_total": 345,
                "meetings_upcoming": 12,
                "organizations_total": 40,
                "organizations_active": 23,
                "persons_total": 1,
            }
        )
        assert satz == (
            "1.234 Vorgänge (56 in diesem Jahr), 345 Sitzungen (12 kommend), 40 Gremien (23 aktiv) und 1 Person."
        )


# ---------------------------------------------------------------------------
# Seiten
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSeiten:
    def _html(self, client_for: Any, membership: Any, name: str, **kwargs: Any) -> str:
        url = reverse(f"work:{name}", kwargs={"org_slug": membership.organization.slug, **kwargs})
        response = client_for(membership.user).get(url)
        assert response.status_code == 200
        return str(response.content.decode())

    def test_dokumente_ohne_zaehlerkacheln(self, org: Any, vorsitz: Any, client_for: Any) -> None:
        Motion.objects.create(organization=org, author=vorsitz, title="Entwurf eins", status="draft")
        Motion.objects.create(organization=org, author=vorsitz, title="Eingereicht", status="submitted")

        html = self._html(client_for, vorsitz, "documents")

        assert "2 Dokumente: 1 Entwurf und 1 eingereicht." in html
        # Kennzahl-Kacheln (c-ui.kpi-tile) mit ihren Beschriftungen sind weg
        for kachel in ("Gesamt", "Entwürfe", "Eingereicht", "In Beratung", "Überfällig", "Erledigt"):
            assert f'<p class="text-xs text-gray-500 dark:text-gray-400">{kachel}</p>' not in html

    def test_fraktionssitzungen_satz_und_teilnahmenachweis_unter_weiteres(
        self, org: Any, vorsitz: Any, client_for: Any
    ) -> None:
        FactionMeeting.objects.create(
            organization=org, title="Wochensitzung", start=timezone.now() + timedelta(days=3), status="planned"
        )

        html = self._html(client_for, vorsitz, "faction")

        assert "1 Sitzung, davon 1 anstehend." in html
        assert ">Offene Protokolle<" not in html and ">Gesamt<" not in html
        # Formulare bleiben vollständig, stehen aber nach der Liste unter „Weiteres“
        liste = html.index("Wochensitzung")
        weiteres = html.index('data-testid="fraktion-weiteres"')
        assert liste < weiteres
        assert reverse("work:faction_certificate", kwargs={"org_slug": org.slug}) in html[weiteres:]
        assert reverse("work:faction_attendance_export", kwargs={"org_slug": org.slug}) in html[weiteres:]
        for feld in ("nachweis-von", "nachweis-bis", "sammel-von", "sammel-bis", "sammel-format"):
            assert f'for="{feld}"' in html and f'id="{feld}"' in html

    def test_fraktionssitzung_einmal_einladen_und_ohne_nummernstrich(
        self, org: Any, vorsitz: Any, client_for: Any
    ) -> None:
        sitzung = FactionMeeting.objects.create(
            organization=org, title="Wochensitzung", start=timezone.now() + timedelta(days=3), status="draft"
        )

        html = self._html(client_for, vorsitz, "faction_detail", meeting_id=sitzung.id)

        assert html.count("&quot;action&quot;: &quot;invite&quot;") + html.count('"action": "invite"') == 1
        assert "#—" not in html and "#&mdash;" not in html

        sitzung.meeting_number = 7
        sitzung.save(update_fields=["meeting_number"])
        assert "Nr. 7" in self._html(client_for, vorsitz, "faction_detail", meeting_id=sitzung.id)

    def test_ratsinformation_satz_statt_kacheln(self, org: Any, vorsitz: Any, client_for: Any) -> None:
        from insight_core.models import OParlBody, OParlSource

        quelle = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
        org.body = OParlBody.objects.create(
            external_id="https://ris.example.org/body/1", source=quelle, name="Musterstadt"
        )
        org.save(update_fields=["body"])

        html = self._html(client_for, vorsitz, "ris_overview")

        assert "0 Vorgänge, 0 Sitzungen, 0 Gremien und 0 Personen." in html
        assert "Vorgänge gesamt" not in html and "Aktive Gremien" not in html
        for ziel in ("ris_papers", "ris_meetings", "ris_organizations", "ris_persons", "ris_map", "ris_search"):
            assert reverse(f"work:{ziel}", kwargs={"org_slug": org.slug}) in html

    def test_start_ohne_farbchips(self, org: Any, vorsitz: Any, client_for: Any) -> None:
        html = self._html(client_for, vorsitz, "dashboard")

        start = html[html.index('class="work-page-content"') :]
        assert 'style="background-color' not in start
        assert ">FK<" not in start
        assert reverse("work:document_create", kwargs={"org_slug": org.slug}) in start

    def test_gast_in_der_teamliste_gekennzeichnet(
        self, org: Any, vorsitz: Any, make_member: Any, client_for: Any
    ) -> None:
        gast = make_member(org, email="gast@example.org")
        gast.roles.clear()
        gast.is_guest = True
        gast.save(update_fields=["is_guest"])
        gast.user.first_name = "Greta"
        gast.user.save(update_fields=["first_name"])

        html = self._html(client_for, vorsitz, "team")
        zeile = html[html.index("Greta") :]
        zeile = zeile[: zeile.index("</tr>")]

        assert ">Gast</span>" in zeile


@pytest.mark.django_db
class TestNavigation:
    def test_aktive_seite_und_knopfnamen(self, org: Any, vorsitz: Any, client_for: Any) -> None:
        html = client_for(vorsitz.user).get(reverse("work:documents", kwargs={"org_slug": org.slug})).content.decode()

        aktiv = re.findall(r'<a href="[^"]*"\s+class="work-nav-item active"\s+aria-current="page"', html)
        assert len(aktiv) == 1
        assert html.count('aria-current="page"') >= 1

        kopf = html[html.index('class="work-topbar') : html.index("<!-- Page Header -->")]
        assert 'title="' not in re.sub(r"<a [^>]*>", "", kopf.split("<!-- Notifications -->")[0])
        for name in (
            'aria-label="Menü"',
            'aria-label="Dunkelmodus"',
            'aria-label="Benachrichtigungen"',
            'aria-label="Benutzermenü"',
        ):
            assert name in kopf, name
        menue = re.search(r"<button[^>]*x-ref=\"menuButton\"[^>]*>", kopf, re.S)
        assert menue and 'aria-controls="work-navigation"' in menue.group(0)

    def test_menue_am_handy_ist_ein_dialog(self, org: Any, vorsitz: Any, client_for: Any) -> None:
        html = client_for(vorsitz.user).get(reverse("work:dashboard", kwargs={"org_slug": org.slug})).content.decode()

        leiste = re.search(r'<aside id="work-navigation"[^>]*>', html, re.S)
        assert leiste
        for attribut in (
            ":role=\"sidebarOpen ? 'dialog' : null\"",
            'x-trap.noscroll="sidebarOpen"',
            "@keydown.escape.window",
        ):
            assert attribut in leiste.group(0), attribut
        assert 'aria-label="Menü schließen"' in html


# ---------------------------------------------------------------------------
# Beide Rahmen, alle Rollen (der Schalter wird nach dem Deploy für alle Organisationen umgelegt)
# ---------------------------------------------------------------------------

#: Seiten, die Stufe 0 umbaut, samt Start, Aufgaben und Profil
STUFE0_SEITEN = (
    "dashboard",
    "documents",
    "faction",
    "faction_detail",
    "ris_overview",
    "ris_search",
    "team",
    "meetings",
    "tasks",
    "support",
    "notifications",
    "profile",
)
ROLLEN = {
    "vorsitz": ("Fraktionsvorsitz", True),
    "mitglied": ("Fraktionsmitglied", True),
    "sachkundig": ("Sachkundige/r Bürger/in", True),
    "nicht_vereidigt": ("Fraktionsmitglied", False),
}


@pytest.mark.django_db
@pytest.mark.parametrize("neues_design", [False, True], ids=["alter_rahmen", "neuer_rahmen"])
@pytest.mark.parametrize("rolle", [*ROLLEN, "gast"])
def test_seiten_fuer_jede_rolle_in_beiden_rahmen(org: Any, client_for: Any, neues_design: bool, rolle: str) -> None:
    Permission.sync_permissions()
    Role.create_default_roles(org)
    org.work_new_design = neues_design
    org.save(update_fields=["work_new_design"])
    sitzung = FactionMeeting.objects.create(
        organization=org, title="Wochensitzung", start=timezone.now() + timedelta(days=3), status="planned"
    )
    if rolle == "gast":
        mitglied = cast(Any, factories.MembershipFactory)(organization=org, is_guest=True, roles=[])
    else:
        name, vereidigt = ROLLEN[rolle]
        mitglied = cast(Any, factories.MembershipFactory)(
            organization=org, is_sworn_in=vereidigt, roles=[Role.objects.get(organization=org, name=name)]
        )
    client = client_for(mitglied.user)
    erreicht = []
    for seite in STUFE0_SEITEN:
        kwargs: dict[str, Any] = {"meeting_id": sitzung.id} if seite == "faction_detail" else {}
        antwort = client.get(reverse(f"work:{seite}", kwargs={"org_slug": org.slug, **kwargs}), follow=True)
        # Fehlende Rechte enden in 403 oder einer Weiterleitung, nie in einem Serverfehler
        assert antwort.status_code in (200, 403), (seite, antwort.status_code)
        if antwort.status_code == 200:
            html = antwort.content.decode()
            assert ('data-rahmen="neu"' in html) is neues_design, seite
            erreicht.append(seite)
    if rolle == "gast":
        assert "dashboard" in erreicht  # Weiterleitung auf die freigegebenen Dokumente
    else:
        # Mitglieder erreichen Start, Fraktionssitzungen und Ratsinformation in beiden Rahmen
        assert {"dashboard", "faction", "faction_detail", "ris_overview", "profile"} <= set(erreicht), erreicht
    if rolle == "vorsitz":
        assert set(erreicht) == set(STUFE0_SEITEN)
