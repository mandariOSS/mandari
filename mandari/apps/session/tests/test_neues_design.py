# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes je Mandant (Issue #944).

Geprüft: Der Schalter ist für neue und bestehende Mandanten aus und ändert beim Umschalten nur sich selbst; ohne
Schalter bleibt jede Seite im bisherigen Rahmen. Mit Schalter nutzen alle Seiten den neuen Rahmen, Start, Sitzungen,
Sitzung, Vorlagen und Vorlage ihre neuen Vorlagen – mit allen Aktionen der bisherigen Seiten, aber nur mit den Rechten
wie bisher. Die Navigation zeigt jeden Bereich nur mit dem Recht seiner Seite; die Mandantentrennung bleibt. Unterseiten
führen in den Brotkrumen zurück zu ihrer Elternseite.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from html.parser import HTMLParser
from io import StringIO
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session import rahmen
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionCircularResolution,
    SessionCosignature,
    SessionFile,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPaperVersion,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
)
from apps.session.services import body_service, file_version_service, paper_version_service

pytestmark = pytest.mark.django_db

KERNSEITEN = {
    "start": "session/neu/start.html",
    "sitzungen": "session/neu/sitzungen.html",
    "sitzung": "session/neu/sitzung.html",
    "vorlagen": "session/neu/vorlagen.html",
    "vorlage": "session/neu/vorlage.html",
}


def _mandant(slug: str, *, neu: bool) -> SessionTenant:
    tenant = SessionTenant.objects.create(name=f"Stadt {slug.title()}", slug=slug, session_new_design=neu)
    SessionRole.create_default_roles(tenant)
    return tenant


def _client(
    tenant: SessionTenant,
    rolle: str | None,
    *,
    auch: tuple[SessionTenant, ...] = (),
    leitstelle: SessionTenantGroup | None = None,
) -> Client:
    """Angemeldetes Konto mit ``rolle`` im Mandanten; ``auch`` und ``leitstelle`` für Mandantenwechsel und Leitstelle."""
    user = cast(Any, UserFactory)()
    for raum in (tenant, *auch):
        session_user = SessionUser.objects.create(user=user, tenant=raum)
        if rolle:
            session_user.roles.add(SessionRole.objects.get(tenant=raum, name=rolle))
    if leitstelle is not None:
        SessionTenantGroupMembership.objects.create(group=leitstelle, user=user)
    client = Client()
    client.force_login(user)
    return client


def _welt(tenant: SessionTenant) -> dict[str, Any]:
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        organization=gremium,
        name="Ratssitzung",
        start=timezone.now() + timedelta(days=10),
        meeting_state="scheduled",
    )
    vorlage = SessionPaper.objects.create(
        tenant=tenant,
        name="Neue Brücke",
        reference="V/2026/001",
        paper_type="proposal",
        status="draft",
        main_organization=gremium,
        main_text="Sachverhalt der Brücke",
        resolution_text="Der Rat beschließt die Brücke.",
    )
    SessionAgendaItem.objects.create(meeting=sitzung, number="1", order=1, name="Eröffnung")
    SessionAgendaItem.objects.create(meeting=sitzung, number="2", order=2, name="Neue Brücke", paper=vorlage)
    slug = tenant.slug
    return {
        "start": reverse("session:dashboard", kwargs={"tenant_slug": slug}),
        "sitzungen": reverse("session:meetings", kwargs={"tenant_slug": slug}),
        "sitzung": reverse("session:meeting_detail", kwargs={"tenant_slug": slug, "meeting_id": sitzung.pk}),
        "vorlagen": reverse("session:papers", kwargs={"tenant_slug": slug}),
        "vorlage": reverse("session:paper_detail", kwargs={"tenant_slug": slug, "paper_id": vorlage.pk}),
        "sitzung_obj": sitzung,
        "vorlage_obj": vorlage,
    }


def _vorlagen(antwort: Any) -> set[str]:
    return {t.name for t in antwort.templates if t.name}


# =============================================================================
# Schalter
# =============================================================================


def test_schalter_ist_standardmaessig_aus() -> None:
    tenant = SessionTenant.objects.create(name="Stadt", slug="stadt")
    assert tenant.session_new_design is False
    assert rahmen.neues_design(tenant) is False
    assert rahmen.neues_design(None) is False


def test_setzen_aendert_nur_den_schalter() -> None:
    tenant = _mandant("nord", neu=False)
    vorher: dict[str, Any] = dict(SessionTenant.objects.filter(pk=tenant.pk).values().get())
    assert rahmen.setzen(tenant, True) is True
    assert rahmen.setzen(tenant, True) is False
    nachher: dict[str, Any] = dict(SessionTenant.objects.filter(pk=tenant.pk).values().get())
    assert nachher.pop(rahmen.FELD) is True and vorher.pop(rahmen.FELD) is False
    assert nachher == vorher, "Umschalten darf nur den Schalter ändern (auch nicht updated_at)"


def test_verwaltungsbefehl() -> None:
    _mandant("nord", neu=False)
    _mandant("sued", neu=True)
    ausgabe = StringIO()
    call_command("session_neues_design", "status", stdout=ausgabe)
    assert "nord: neues Design aus" in ausgabe.getvalue() and "sued: neues Design an" in ausgabe.getvalue()

    call_command("session_neues_design", "an", "--mandant", "nord", stdout=StringIO())
    assert SessionTenant.objects.get(slug="nord").session_new_design is True
    call_command("session_neues_design", "aus", "--alle", stdout=StringIO())
    assert not SessionTenant.objects.filter(session_new_design=True).exists()

    with pytest.raises(CommandError):
        call_command("session_neues_design", "an", "--alle", stdout=StringIO())
    with pytest.raises(CommandError):
        call_command("session_neues_design", "an", "--mandant", "gibt-es-nicht", stdout=StringIO())


def test_verwaltungsbefehl_probelauf_rueckweg_und_fehler() -> None:
    _mandant("nord", neu=False)
    _mandant("sued", neu=True)
    _mandant("west", neu=False)

    # Probelauf zeigt nur, was sich ändern würde
    ausgabe = StringIO()
    call_command("session_neues_design", "an", "--mandant", "nord", "--mandant", "sued", "--probelauf", stdout=ausgabe)
    text = ausgabe.getvalue()
    assert "nord: würde auf an schalten" in text and "sued: bereits an – nichts zu tun" in text
    assert "1 von 2 Mandant(en) würden umgeschaltet." in text
    assert not SessionTenant.objects.get(slug="nord").session_new_design

    # Mehrere Mandanten auf einmal, doppelte Angabe zählt einmal; die übrigen bleiben unberührt
    ausgabe = StringIO()
    call_command("session_neues_design", "an", "--mandant", "nord", "--mandant", "nord", stdout=ausgabe)
    assert "1 von 1 Mandant(en) umgeschaltet." in ausgabe.getvalue()
    stand = dict(SessionTenant.objects.values_list("slug", "session_new_design"))
    assert stand == {"nord": True, "sued": True, "west": False}

    # Rückweg für einen Mandanten
    call_command("session_neues_design", "aus", "--mandant", "sued", stdout=StringIO())
    stand = dict(SessionTenant.objects.values_list("slug", "session_new_design"))
    assert stand == {"nord": True, "sued": False, "west": False}
    ausgabe = StringIO()
    call_command("session_neues_design", "status", stdout=ausgabe)
    assert "Neues Design an: 1 von 3" in ausgabe.getvalue()

    # Ohne Mandant, mit beidem oder einem unbekannten Mandanten ändert der Befehl nichts
    for argumente in (
        ("an",),
        ("aus",),
        ("aus", "--alle", "--mandant", "nord"),
        ("aus", "--mandant", "nord", "--mandant", "gibt-es-nicht"),
    ):
        with pytest.raises(CommandError):
            call_command("session_neues_design", *argumente, stdout=StringIO())
    assert SessionTenant.objects.get(slug="nord").session_new_design is True


# =============================================================================
# Rahmen und Seiten
# =============================================================================


def test_ohne_schalter_bleibt_der_bisherige_rahmen() -> None:
    tenant = _mandant("alt", neu=False)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, vorlage in KERNSEITEN.items():
        antwort = client.get(welt[name])
        assert antwort.status_code == 200, name
        html = antwort.content.decode()
        assert "session/base_session_alt.html" in _vorlagen(antwort), name
        assert vorlage not in _vorlagen(antwort), name
        assert 'data-rahmen="neu"' not in html and "session-sidebar" in html, name


def test_mit_schalter_neuer_rahmen_und_neue_kernseiten() -> None:
    tenant = _mandant("neu", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Administrator")
    for name, vorlage in KERNSEITEN.items():
        antwort = client.get(welt[name])
        assert antwort.status_code == 200, name
        html = antwort.content.decode()
        assert {"session/base_session_neu.html", vorlage} <= _vorlagen(antwort), name
        assert 'data-rahmen="neu"' in html and 'data-gestaltung="neu"' in html, name
        assert "session-sidebar" not in html, name
    # Seiten ohne neue Gestaltung stehen im neuen Rahmen, ihr Inhalt wie bisher
    einstellungen = client.get(reverse("session:settings", kwargs={"tenant_slug": tenant.slug}))
    assert einstellungen.status_code == 200
    assert 'data-gestaltung="bisher"' in einstellungen.content.decode()


def test_alle_aktionen_der_sitzung_bleiben_erreichbar() -> None:
    tenant = _mandant("aktionen", neu=True)
    welt = _welt(tenant)
    sitzung = welt["sitzung_obj"]
    html = _client(tenant, "Administrator").get(welt["sitzung"]).content.decode()
    kw = {"tenant_slug": tenant.slug, "meeting_id": sitzung.pk}
    for adresse in (
        reverse("session:meeting_invitation", kwargs=kw),
        reverse("session:meeting_edit", kwargs=kw),
        reverse("session:meeting_cockpit", kwargs=kw),
        reverse("session:meeting_agenda_pdf", kwargs=kw),
        reverse("session:meeting_ics", kwargs=kw),
        reverse("session:resolutions_meeting_pdf", kwargs=kw),
        reverse("session:resolutions_generate", kwargs=kw),
        reverse("session:meeting_protocol", kwargs=kw),
        reverse("session:agenda_item_create", kwargs=kw),
        reverse("session:agenda_reorder", kwargs=kw),
        reverse("session:attendance_generate", kwargs=kw),
    ):
        assert adresse in html, adresse
    top = SessionAgendaItem.objects.get(meeting=sitzung, number="1")
    for name in ("agenda_item_move", "agenda_item_edit", "agenda_item_withdraw", "agenda_item_delete"):
        assert reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug, "item_id": top.pk}) in html, name
    assert 'data-tagesordnung-reihenfolge="' in html and html.count('data-testid="top"') == 2


#: Eine Aktion: Methode (GET, POST, … oder SKRIPT), Adresse und Felder
Aktion = tuple[str, str, tuple[str, ...]]
_HX = ("get", "post", "put", "patch", "delete")
#: Felder mit festem Wert: der Wert gehört zur Aktion (``restore=1`` hebt eine Absetzung auf, ``reason`` setzt ab)
_FESTE_WERTE = {"hidden", "submit", "button"}
_SKRIPT_ADRESSE = re.compile(r"""["'](/session/[^"'\s]+)["']""")


def _intern(adresse: str) -> bool:
    """Adresse dieser Anwendung (auch nur Anfrageparameter wie ``?page=2``), keine Dateien aus static/media."""
    return adresse.startswith("?") or (
        adresse.startswith("/") and not adresse.startswith(("//", "/static/", "/media/"))
    )


def _hx_werte(roh: str) -> tuple[str, ...]:
    try:
        werte = json.loads(roh) if roh else {}
    except ValueError:
        return (roh,)
    return tuple(sorted(f"{k}={v}" for k, v in werte.items())) if isinstance(werte, dict) else (roh,)


class _Aktionen(HTMLParser):
    """
    Was eine Seite anbietet, als Menge von Aktionen: Links (GET), Formulare mit Methode, Ziel und Feldern (feste Werte
    mit Wert, Eingaben nur mit Namen), htmx-Aufrufe mit Methode und ``hx-vals`` sowie Adressen für Skripte (``data-*``,
    Adressen in Skripten). So unterscheiden sich etwa „Absetzen“ und „Absetzung aufheben“ (dieselbe Adresse).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.aktionen: set[Aktion] = set()
        self._formular: tuple[str, str] | None = None
        self._felder: set[str] = set()
        self._im_skript = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {name: wert or "" for name, wert in attrs}
        hx = next(((verb.upper(), a[f"hx-{verb}"]) for verb in _HX if a.get(f"hx-{verb}")), None)
        if tag == "a" and _intern(a.get("href", "")):
            self.aktionen.add(("GET", a["href"], ()))
        if hx and tag != "form" and _intern(hx[1]):
            self.aktionen.add((hx[0], hx[1], _hx_werte(a.get("hx-vals", ""))))
        for name, wert in a.items():
            if name.startswith("data-") and wert.startswith("/session/"):
                self.aktionen.add(("SKRIPT", wert, ()))
        if tag == "form":
            self._formular = hx or (a.get("method", "get").upper(), a.get("action", ""))
            self._felder = set()
        elif self._formular and tag in ("input", "select", "textarea", "button") and a.get("name"):
            name = a["name"]
            typ = a.get("type", "submit" if tag == "button" else "text").lower()
            if name != "csrfmiddlewaretoken":
                fest = tag in ("input", "button") and typ in _FESTE_WERTE
                self._felder.add(f"{name}={a.get('value', '')}" if fest else name)
        self._im_skript = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._formular:
            self.aktionen.add((*self._formular, tuple(sorted(self._felder))))
            self._formular = None
        if tag == "script":
            self._im_skript = False

    def handle_data(self, data: str) -> None:
        if self._im_skript:
            self.aktionen.update(("SKRIPT", adresse, ()) for adresse in _SKRIPT_ADRESSE.findall(data))


def _aktionen(antwort: Any) -> set[Aktion]:
    leser = _Aktionen()
    leser.feed(antwort.content.decode())
    leser.close()
    return leser.aktionen


def _volle_welt(tenant: SessionTenant) -> dict[str, Any]:
    """
    Wie ``_welt``, dazu alles, was eigene Aktionen zeigt: Unter-TOPs, ein abgesetzter und nichtöffentliche TOPs,
    Anlagen an Sitzung, TOP und Vorlage (auch nichtöffentlich), ein Mitzeichnungslauf (eine Station mitgezeichnet,
    die nächste dran, eine wartet), ein offener Antrag und eine vergangene Sitzung mit genehmigter Niederschrift
    (dann nur noch Rücknahme auf nichtöffentlich). ``erwartet``: Aktionen, die nur dieser Bestand zeigt – der
    Vergleich prüft, dass die bisherigen Seiten sie wirklich anbieten.
    """
    welt = _welt(tenant)
    slug = tenant.slug
    sitzung, vorlage = welt["sitzung_obj"], welt["vorlage_obj"]
    top = SessionAgendaItem.objects.get(meeting=sitzung, number="2")
    SessionAgendaItem.objects.create(meeting=sitzung, number="2.1", order=3, name="Planung", parent=top)
    abgesetzt = SessionAgendaItem.objects.create(
        meeting=sitzung, number="3", order=4, name="Alter Antrag", is_withdrawn=True, withdrawn_reason="vertagt"
    )
    noe = SessionAgendaItem.objects.create(meeting=sitzung, number="4", order=5, name="Grundstück", is_public=False)
    noe_unter = SessionAgendaItem.objects.create(
        meeting=sitzung, number="4.1", order=6, name="Kaufpreis", is_public=False, parent=noe
    )
    anlagen: dict[str, SessionFile] = {}
    for name, ziel, oeffentlich in (
        ("einladung.txt", {"meeting": sitzung}, True),
        ("plan.txt", {"agenda_item": top}, True),
        ("bericht.txt", {"paper": vorlage}, True),
        ("vermerk.txt", {"paper": vorlage}, False),
    ):
        datei = SessionFile(tenant=tenant, name=name, mime_type="text/plain", is_public=oeffentlich, **ziel)
        upload = SimpleUploadedFile(name, b"Inhalt", content_type="text/plain")
        file_version_service.attach_upload(datei, upload, user=None)
        anlagen[name] = datei

    stationen = []
    for nummer, (amt, status) in enumerate((("Kämmerei", "signed"), ("Bauamt", "pending"), ("Rechtsamt", "pending"))):
        abteilung = SessionOrganization.objects.create(tenant=tenant, name=amt, organization_type="department")
        stationen.append(
            SessionCosignature.objects.create(paper=vorlage, department=abteilung, order=nummer + 1, status=status)
        )
    antrag = SessionApplication.objects.create(
        tenant=tenant,
        title="Mehr Bänke",
        submitter_name="Fraktion",
        submitter_email="f@example.org",
        status="submitted",
    )

    vergangen = SessionMeeting.objects.create(
        tenant=tenant,
        organization=sitzung.organization,
        name="Vergangene Ratssitzung",
        start=timezone.now() - timedelta(days=20),
        meeting_state="completed",
    )
    beschlossen = SessionAgendaItem.objects.create(
        meeting=vergangen, number="1", order=1, name="Brücke", paper=vorlage, vote_result="approved"
    )
    SessionAgendaItem.objects.create(meeting=vergangen, number="1.1", order=2, name="Geländer", parent=beschlossen)
    SessionAgendaItem.objects.create(meeting=vergangen, number="2", order=3, name="Personal", is_public=False)
    SessionProtocol.objects.create(meeting=vergangen, status="approved", approved_at=timezone.now())
    welt["genehmigt"] = reverse("session:meeting_detail", kwargs={"tenant_slug": slug, "meeting_id": vergangen.pk})
    welt.update(vergangen_obj=vergangen, anlagen=anlagen, antrag_obj=antrag)

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    welt["erwartet"] = {
        ("POST", adresse("agenda_item_withdraw", item_id=abgesetzt.pk), ("restore=1",)),
        ("POST", adresse("agenda_item_withdraw", item_id=noe_unter.pk), ("reason=",)),
        ("GET", adresse("agenda_item_edit", item_id=beschlossen.pk), ()),
        ("GET", adresse("file_download", file_id=anlagen["vermerk.txt"].pk), ()),
        ("POST", adresse("cosignature_action", cosign_id=stationen[1].pk, action="sign"), (f"next={welt['vorlage']}",)),
        ("POST", adresse("paper_workflow", paper_id=vorlage.pk, action="submit"), ()),
        ("POST", adresse("paper_workflow", paper_id=vorlage.pk, action="approve"), ()),
        ("GET", adresse("application_detail", application_id=antrag.pk), ()),
    }
    return welt


def test_keine_funktion_der_bisherigen_seiten_entfaellt() -> None:
    """
    Jede Aktion der bisherigen Kernseiten (Links, Formulare mit Methode und Feldern, htmx, Adressen für Skripte) steht
    auch auf der neuen Seite – für jede Standardrolle, mit Mandantenwechsel und Leitstelle im Rahmen, mit abgesetzten,
    nichtöffentlichen und Unter-TOPs, Anlagen, Mitzeichnungslauf, offenem Antrag und genehmigter Niederschrift, die
    Vorlage in jedem Schritt des Freigabelaufs. Einzige Ausnahme: die vier Seiten, die die bisherige Seitenleiste
    direkt anbot und die jetzt als Reiter über ihrer Liste stehen (Kalender, Zu prüfen, Meine Mitzeichnungen,
    Umlaufbeschlüsse) – erreichbar mit einem Schritt mehr.
    """
    tenant = _mandant("vergleich", neu=False)
    nachbar = _mandant("nachbar", neu=False)
    leitstelle = SessionTenantGroup.objects.create(name="Bezirke", slug="bezirke")
    SessionTenantGroupTenant.objects.create(group=leitstelle, tenant=tenant)
    welt = _volle_welt(tenant)
    reiter: set[Aktion] = {
        ("GET", reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug}), ())
        for name in ("meeting_calendar", "papers_review", "my_cosignatures", "circulars")
    }
    fehlt: list[str] = []
    gesehen: set[Aktion] = set()
    for rolle in [definition["name"] for definition in SessionRole.default_role_definitions().values()]:
        client = _client(tenant, rolle, auch=(nachbar,), leitstelle=leitstelle)
        for status in ("draft", "review", "approved"):
            SessionPaper.objects.filter(pk=welt["vorlage_obj"].pk).update(status=status)
            # Der Stand der Vorlage zeigt sich auf Start, Vorlagen und Vorlage; die Sitzungen nur einmal
            for name in (*KERNSEITEN, "genehmigt") if status == "draft" else ("start", "vorlagen", "vorlage"):
                SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
                alt = client.get(welt[name])
                SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
                neu = client.get(welt[name])
                assert alt.status_code == neu.status_code, f"{rolle}/{status}/{name}"
                if alt.status_code != 200:
                    continue
                bisher = _aktionen(alt)
                gesehen |= bisher
                fehlt.extend(f"{rolle}/{status}/{name}: {a}" for a in sorted(bisher - _aktionen(neu) - reiter))
    assert not fehlt, "\n".join(fehlt)
    # Der Bestand zeigt wirklich, was er prüfen soll (sonst wäre der Vergleich leer)
    assert welt["erwartet"] <= gesehen, sorted(welt["erwartet"] - gesehen)


def test_aktionen_unterscheiden_feste_werte() -> None:
    """Der Vergleich erkennt Formulare an Methode, Ziel und Feldern, nicht nur an der Adresse."""
    leser = _Aktionen()
    leser.feed(
        '<form method="post" action="/session/x/agenda/1/withdraw/"><input type="hidden" name="restore" value="1">'
        '<input type="hidden" name="csrfmiddlewaretoken" value="t"></form>'
        '<form method="post" action="/session/x/agenda/1/withdraw/"><input type="hidden" name="reason" value=""></form>'
        '<a href="/static/x.css"></a><a href="?page=2"></a><button hx-post="/session/x/y/" hx-vals=\'{"a": 1}\'></button>'
        "<script>fetch('/session/x/reorder/')</script>"
    )
    assert leser.aktionen == {
        ("POST", "/session/x/agenda/1/withdraw/", ("restore=1",)),
        ("POST", "/session/x/agenda/1/withdraw/", ("reason=",)),
        ("GET", "?page=2", ()),
        ("POST", "/session/x/y/", ("a=1",)),
        ("SKRIPT", "/session/x/reorder/", ()),
    }


def _brotkrumen(html: str) -> str:
    """Die Brotkrumen in der Kopfzeile des neuen Rahmens."""
    anfang = html.index('<nav aria-label="Brotkrumen"')
    return html[anfang : html.index("</nav>", anfang)]


def test_unterseiten_fuehren_in_den_brotkrumen_zur_elternseite() -> None:
    """
    Seiten mit einer Elternseite zwischen Bereich und Seite (Ladung, Rückmeldungen, Niederschrift, Abstimmung,
    Cockpit, TOP, Verlauf einer Anlage, Fassungen, Antrag, Einstellungen, Umlaufbeschluss) zeigen im neuen Rahmen den
    Weg zurück: Ihre Brotkrumen verlinken die Elternseite. Den bisherigen Block ``breadcrumb`` zeigt dort nur der
    bisherige Rahmen.
    """
    tenant = _mandant("eltern", neu=True)
    welt = _volle_welt(tenant)
    slug = tenant.slug
    sitzung, vorlage, vergangen = welt["sitzung_obj"], welt["vorlage_obj"], welt["vergangen_obj"]
    top = SessionAgendaItem.objects.get(meeting=sitzung, number="1")
    SessionProtocol.objects.create(meeting=sitzung, status="draft")
    paper_version_service.snapshot(vorlage, trigger=SessionPaperVersion.TRIGGER_MANUAL)
    paper_version_service.snapshot(vorlage, trigger=SessionPaperVersion.TRIGGER_MANUAL)
    umlauf = SessionCircularResolution.objects.create(
        tenant=tenant,
        organization=sitzung.organization,
        title="Zuschuss Sportverein",
        resolution_text="Der Zuschuss wird gewährt.",
        deadline=timezone.localdate() + timedelta(days=7),
    )

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    s, v, a = {"meeting_id": sitzung.pk}, {"paper_id": vorlage.pk}, {"application_id": welt["antrag_obj"].pk}
    sitzung_url = adresse("meeting_detail", **s)
    vorlage_url = adresse("paper_detail", **v)
    fassungen_url = adresse("paper_versions", **v)
    anlagen = welt["anlagen"]
    faelle: list[tuple[str, str]] = [
        (adresse("meeting_invitation", **s), sitzung_url),
        (adresse("meeting_invitation_status", **s), sitzung_url),
        (adresse("meeting_invitation_status", **s), adresse("meeting_invitation", **s)),
        (adresse("meeting_protocol", **s), sitzung_url),
        (adresse("meeting_protocol_edit", **s), adresse("meeting_protocol", **s)),
        (adresse("meeting_protocol_correction", meeting_id=vergangen.pk) + "?teil=allgemein",
         adresse("meeting_protocol", meeting_id=vergangen.pk)),
        (adresse("meeting_cockpit", **s), sitzung_url),
        (adresse("voting_capture", item_id=top.pk), sitzung_url),
        (adresse("agenda_item_edit", item_id=top.pk), sitzung_url),
        (adresse("file_versions", file_id=anlagen["bericht.txt"].pk), vorlage_url),
        (adresse("file_versions", file_id=anlagen["plan.txt"].pk), sitzung_url),
        (adresse("file_versions", file_id=anlagen["einladung.txt"].pk), sitzung_url),
        (adresse("paper_versions", **v), vorlage_url),
        (adresse("paper_version_detail", number=1, **v), fassungen_url),
        (adresse("paper_version_compare", **v) + "?a=1&b=2", fassungen_url),
        (adresse("application_process", **a), adresse("application_detail", **a)),
        (adresse("application_convert", **a), adresse("application_detail", **a)),
        (adresse("circular_detail", circular_id=umlauf.pk), adresse("circulars")),
        (adresse("settings_body_create"), adresse("settings_bodies")),
        (adresse("settings_local_rules", body_id=body_service.default_body(tenant).pk), adresse("settings_meeting_formats")),
        (adresse("settings_delegations"), adresse("users")),
        (adresse("user_invite"), adresse("users")),
    ]  # fmt: skip
    client = _client(tenant, "Administrator")
    for seite, eltern in faelle:
        antwort = client.get(seite)
        assert antwort.status_code == 200, seite
        html = antwort.content.decode()
        assert "session/base_session_neu.html" in _vorlagen(antwort), seite
        assert f'href="{eltern}"' in _brotkrumen(html), f"{seite} → {eltern}"


def test_lesezugriff_sieht_keine_bearbeitung() -> None:
    tenant = _mandant("lesen", neu=True)
    welt = _welt(tenant)
    client = _client(tenant, "Lesezugriff")
    sitzung = client.get(welt["sitzung"]).content.decode()
    kw = {"tenant_slug": tenant.slug, "meeting_id": welt["sitzung_obj"].pk}
    assert reverse("session:meeting_edit", kwargs=kw) not in sitzung
    assert reverse("session:agenda_item_create", kwargs=kw) not in sitzung
    assert "data-tagesordnung-reihenfolge" not in sitzung
    assert "Live-Ansicht" in sitzung
    vorlage = client.get(welt["vorlage"]).content.decode()
    assert "Zur Freigabe vorlegen" not in vorlage and "Bearbeiten" not in vorlage
    assert "Neue Sitzung" not in client.get(welt["sitzungen"]).content.decode()


def test_freigabelauf_der_vorlage() -> None:
    tenant = _mandant("freigabe", neu=True)
    welt = _welt(tenant)
    vorlage = welt["vorlage_obj"]
    client = _client(tenant, "Administrator")
    kw = {"tenant_slug": tenant.slug, "paper_id": vorlage.pk}
    html = client.get(welt["vorlage"]).content.decode()
    assert reverse("session:paper_workflow", kwargs={**kw, "action": "submit"}) in html
    assert reverse("session:paper_edit", kwargs=kw) in html
    assert "Sachverhalt der Brücke" in html and "Der Rat beschließt die Brücke." in html
    SessionPaper.objects.filter(pk=vorlage.pk).update(status="review")
    html = client.get(welt["vorlage"]).content.decode()
    assert reverse("session:paper_workflow", kwargs={**kw, "action": "reject"}) in html


def test_mandantentrennung_bleibt() -> None:
    eigen = _mandant("eigen", neu=True)
    fremd = _mandant("fremd", neu=True)
    welt = _welt(fremd)
    client = _client(eigen, "Administrator")
    for name in KERNSEITEN:
        assert client.get(welt[name]).status_code in (403, 404), name


def test_navigation_nach_rechten() -> None:
    tenant = _mandant("nav", neu=True)
    voll = rahmen.navigation(tenant, {"view_meetings", "view_papers", "view_applications"}, "meetings")
    assert [b["key"] for b in voll["bereiche"]] == [
        "start", "sitzungen", "vorlagen", "antraege", "beschluesse", "gremien", "personen",
    ]  # fmt: skip
    assert [b["key"] for b in voll["leiste_unten"]] == ["start", "sitzungen", "vorlagen", "beschluesse"]
    assert [r["key"] for r in voll["reiter"]] == ["liste", "kalender", "jahresplanung", "archiv"]
    assert [k["label"] for k in voll["brotkrumen"]] == ["Stadt Nav", "Sitzungen"]
    assert voll["brotkrumen"][-1]["aktuell"] is True

    # Kontrollrolle: nur Start und das Audit-Log unter „Verwaltung“
    kontrolle = rahmen.navigation(tenant, {"view_dashboard", "view_audit_log"}, "audit_log")
    assert [b["key"] for b in kontrolle["bereiche"]] == ["start"]
    verwaltung = next(e for e in kontrolle["unten"] if e["key"] == "verwaltung")
    assert [u["key"] for u in verwaltung["unterpunkte"]] == ["audit"] and verwaltung["offen"] is True
    assert [k["label"] for k in kontrolle["brotkrumen"]] == ["Stadt Nav", "Verwaltung", "Audit-Log"]

    # Nur das Prüfrecht: „Vorlagen“ führt auf „Zu prüfen“, mit der Zahl der offenen Arbeit
    pruefen = rahmen.navigation(tenant, {"approve_papers"}, "dashboard", zaehler={"papers_review_count": 3})
    vorlagen = next(b for b in pruefen["bereiche"] if b["key"] == "vorlagen")
    assert vorlagen["url"].endswith("/papers/review/") and vorlagen["zahl"] == 3
    assert not any(e["key"] == "einstellungen" for e in pruefen["unten"])

    # Ein Umlaufbeschluss steht unter seinem Reiter (Weg zurück zur Liste der Umlaufbeschlüsse)
    umlauf = rahmen.navigation(tenant, {"view_meetings"}, "circular_detail")
    assert [k["label"] for k in umlauf["brotkrumen"]] == ["Stadt Nav", "Beschlüsse", "Umlaufbeschlüsse"]
    assert umlauf["reiter"] == [] and umlauf["brotkrumen"][-1]["url"].endswith("/circulars/")


def test_zahl_offener_antraege_an_antraege() -> None:
    """„Anträge“ zeigt im neuen Rahmen die offenen Anträge (gezählt wie auf dem Start), nur mit dem Recht dazu."""
    tenant = _mandant("antraege", neu=True)
    for status in ("submitted", "in_review", "accepted"):
        SessionApplication.objects.create(tenant=tenant, title=f"Antrag {status}", submitter_name="F", status=status)
    kw = {"tenant_slug": tenant.slug}
    html = _client(tenant, "Administrator").get(reverse("session:dashboard", kwargs=kw)).content.decode()
    leiste = html[html.index('id="session-navigation"') : html.index("</aside>")]
    eintrag = leiste[leiste.index(f'href="{reverse("session:applications", kwargs=kw)}"') :]
    assert '2<span class="sr-only"> offen</span>' in eintrag[: eintrag.index("</a>")]
    # Ohne das Recht kein Eintrag, also auch keine Zahl; im bisherigen Rahmen bleibt die Seitenleiste wie sie war
    nav = rahmen.navigation(tenant, {"view_dashboard"}, "dashboard", zaehler={"pending_applications_count": 2})
    assert not any(b["key"] == "antraege" for b in nav["bereiche"])


def test_detailseiten_ohne_reiter_mit_seitentitel_als_krume() -> None:
    tenant = _mandant("krumen", neu=True)
    nav = rahmen.navigation(tenant, {"view_meetings"}, "meeting_detail")
    assert nav["reiter"] == []
    assert [k["label"] for k in nav["brotkrumen"]] == ["Stadt Krumen", "Sitzungen"]
    assert nav["brotkrumen"][-1]["aktuell"] is False
    welt = _welt(tenant)
    html = _client(tenant, "Administrator").get(welt["sitzung"]).content.decode()
    assert 'aria-current="page"' in html and "Ratssitzung, " in html


def test_seitenleiste_nur_mit_erreichbaren_links() -> None:
    tenant = _mandant("links", neu=True)
    _welt(tenant)
    for rolle in ("Administrator", "Sachbearbeiter", "Lesezugriff", "Revision", None):
        client = _client(tenant, rolle)
        start = client.get(reverse("session:dashboard", kwargs={"tenant_slug": tenant.slug}))
        if start.status_code != 200:
            continue
        html = start.content.decode()
        leiste = html[html.index('id="session-navigation"') : html.index("</aside>")]
        basis = f"/session/{tenant.slug}/"
        links = {teil.split('"', 1)[0] for teil in leiste.split('href="')[1:]}
        tot = [h for h in sorted(links) if h.startswith(basis) and client.get(h).status_code != 200]
        assert not tot, f"{rolle}: {tot}"


def test_eintrag_der_seitenleiste_mit_zahl() -> None:
    """c-rahmen.nav-link: ``zahl`` zeigt offene Arbeit; ohne Angabe bleibt das Markup wie in Work und Insight."""
    from django.template import engines
    from django_cotton.compiler_regex import CottonCompiler

    def render(source: str) -> str:
        return engines["django"].from_string(CottonCompiler().process(source)).render({})

    mit = render('<c-rahmen.nav-link href="/v/" icon="file-text" zahl="3" dicht>Vorlagen</c-rahmen.nav-link>')
    assert '3<span class="sr-only"> offen</span>' in mit and "rahmen-label shrink-0" in mit
    ohne = render('<c-rahmen.nav-link href="/v/" icon="file-text" dicht>Vorlagen</c-rahmen.nav-link>')
    assert " offen</span>" not in ohne and "tabular-nums" not in ohne
