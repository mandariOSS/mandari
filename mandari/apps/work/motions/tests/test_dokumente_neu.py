# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumente im neuen Erscheinungsbild (Issue #951, Teilpaket 1).

Mit eingeschaltetem Schalter (Organization.work_new_design) rendern Liste, Neues Dokument, Import, Papierkorb,
Freigaben und Einreichen eigene Vorlagen aus work/motions/neu/; ausgeschaltet bleibt alles wie bisher, und
``?ansicht=bisher`` öffnet die bisherige Vorlage auch bei eingeschaltetem Schalter. Geprüft wird hier:

- Gleichstand der Funktionen: Jedes Formular (Methode, Ziel, Feldnamen), jeder Work-Link (Pfad und Parameter),
  jede Aktion (data-post, data-href) und jeder Aufruf der Alpine-Komponenten der bisherigen Vorlage steht auch in der
  neuen. Ausnahmen stehen begründet in AUSNAHMEN.
- Rechte: dieselben Antwortcodes je Rolle in beiden Vorlagen.
- Bausteine statt Hand-Markup: keine Kartenrahmen, keine eigene Überlagerung, Felder über c-form.

Dieselben fachlichen Prüfungen wie bisher (Sichtbarkeit, Einreichen, Freigaben …) laufen mit eingeschaltetem
Schalter in den Modulen test_schalter_an_*.py (apps/work/tests/schalter_an.py).
"""

from __future__ import annotations

import re
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.tenants.models import AdministrationContact, Topic
from apps.work.motions.models import (
    DocumentFolder,
    FolderGuestShare,
    Motion,
    MotionApproval,
    MotionDocument,
    MotionShare,
)

pytestmark = pytest.mark.django_db

RECHTE = ["dashboard.view", "motions.view", "motions.create", "motions.edit", "motions.delete"]
EINREICHEN = [*RECHTE, "motions.submit_to_ris", "motions.approve"]
NEU = "work/motions/neu/"
VORLAGEN = Path(settings.BASE_DIR) / "templates" / "work" / "motions" / "neu"
INHALT = "<h2>Beschlussvorschlag</h2><p>Die Verwaltung plant einen Radweg.</p><h2>Begründung</h2><p>Sicherheit.</p>"


@pytest.fixture
def org(org: Any) -> Any:
    """Organisation mit eingeschaltetem neuen Erscheinungsbild (wie in Produktion seit 06.10.2026)."""
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture(autouse=True)
def _medien(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, EINREICHEN, email="autorin@example.org")


@pytest.fixture
def leserin(org: Any, make_member: Any) -> Any:
    """Darf Dokumente sehen, aber nicht bearbeiten oder anlegen."""
    return make_member(org, ["dashboard.view", "motions.view"], email="leserin@example.org")


@pytest.fixture
def gast(org: Any) -> Any:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]


@pytest.fixture
def ordner(org: Any, autorin: Any) -> DocumentFolder:
    return DocumentFolder.objects.create(organization=org, name="Haushalt", created_by=autorin, color="teal")


@pytest.fixture
def bestand(org: Any, autorin: Any, leserin: Any, ordner: DocumentFolder) -> dict[str, Any]:
    """Liste mit allem, was eine Zeile zeigen kann: Ordner, Unterordner, Änderungsantrag, Themen, Frist, Freigaben."""
    unterordner = DocumentFolder.objects.create(
        organization=org, name="Anträge 2026", parent=ordner, created_by=autorin
    )
    thema = Topic.objects.create(organization=org, name="Verkehr", color="blue")
    antrag = Motion.objects.create(
        organization=org,
        author=autorin,
        responsible=autorin,
        title="Antrag Spielplatz & Bolzplatz",
        visibility="organization",
        folder=ordner,
        status="internal_review",
        due_date=timezone.localdate() - timedelta(days=2),
    )
    antrag.topics.add(thema)
    MotionApproval.objects.create(motion=antrag, approver=leserin, approval_type="chair")
    aenderung = Motion.objects.create(
        organization=org,
        author=autorin,
        title="Änderung zum Spielplatz",
        visibility="organization",
        parent_motion=antrag,
    )
    entwurf = Motion.objects.create(organization=org, author=autorin, title="Anfrage Radweg", folder=unterordner)
    geloescht = Motion.objects.create(
        organization=org, author=autorin, title="Alter Entwurf", visibility="organization"
    )
    geloescht.status = "deleted"
    geloescht.deleted_at = timezone.now()
    geloescht.save(update_fields=["status", "deleted_at"])
    return {"antrag": antrag, "aenderung": aenderung, "entwurf": entwurf, "geloescht": geloescht, "thema": thema}


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def _vorlagen(antwort: Any) -> set[str]:
    return {t.name for t in antwort.templates if t.name}


# ---------------------------------------------------------------------------
# Gleichstand der Funktionen
# ---------------------------------------------------------------------------

#: Aufrufe in Alpine-Ausdrücken, die keine Funktion der Seite sind
_KEINE_AKTION = {"if", "confirm", "submit", "trim", "includes", "toLowerCase", "toString", "preventDefault"}
_AUFRUF = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")


class _Funktionen(HTMLParser):
    """Sammelt, was eine Seite tun kann: Formulare, Felder, Links, Aktionen und Alpine-Aufrufe."""

    def __init__(self, pfad: str) -> None:
        super().__init__(convert_charrefs=True)
        self.pfad = pfad
        self.formulare: list[tuple[str, str, set[str]]] = []
        self._offen: list[tuple[str, str, set[str]]] = []
        self.links: set[str] = set()
        self.aktionen: set[str] = set()
        self.aufrufe: set[str] = set()
        self.komponenten: set[str] = set()
        self.json_skripte: set[str] = set()

    def _ziel(self, href: str) -> str | None:
        if not href or href.startswith(("#", "javascript:", "mailto:", "http")):
            return None
        teile = urlsplit(href)
        pfad = teile.path or self.pfad
        if not pfad.startswith("/work/"):
            return None
        # ``ansicht`` trägt nur den Rückweg in die bisherige Vorlage weiter (Filter-Links behalten die Anfrage)
        parameter = ",".join(sorted(k for k in parse_qs(teile.query, keep_blank_values=True) if k != "ansicht"))
        return f"{pfad}?{parameter}" if parameter else pfad

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        for name, wert in a.items():
            if name.startswith(("@", "x-on:")) or name in ("x-init",):
                self.aufrufe |= {f for f in _AUFRUF.findall(wert) if f not in _KEINE_AKTION}
        if "x-data" in a and re.fullmatch(r"[A-Za-z_]\w*", a["x-data"].strip()):
            self.komponenten.add(a["x-data"].strip())
        if tag == "script" and a.get("type") == "application/json" and a.get("id"):
            self.json_skripte.add(a["id"])
        for daten in ("data-post", "data-href"):
            if a.get(daten):
                self.aktionen.add(f"{daten}:{self._ziel(a[daten]) or a[daten]}")
        if tag == "form":
            ziel = a.get("action") if "action" in a else self.pfad
            if ":action" in a:
                ziel = f"alpine:{a[':action']}"
            self._offen.append((a.get("method", "get").lower(), self._ziel(ziel or self.pfad) or ziel or "", set()))
        elif tag in ("input", "select", "textarea", "button") and a.get("name") and self._offen:
            name = a["name"]
            if a.get("type") == "hidden" and name in ("channel", "action", "section"):
                name = f"{name}={a.get('value', '')}"
            self._offen[-1][2].add(name)
        elif tag == "a" and a.get("href"):
            ziel = self._ziel(a["href"])
            if ziel:
                self.links.add(ziel)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._offen:
            self.formulare.append(self._offen.pop())


def _funktionen(html: str, pfad: str) -> _Funktionen:
    parser = _Funktionen(pfad)
    parser.feed(html)
    return parser


#: Was die neue Vorlage bewusst nicht übernimmt (Begründung im Kommentar)
AUSNAHMEN: dict[str, set[str]] = {
    # Der Teilen-Dialog der bisherigen Liste hatte keinen Auslöser (openShareModal ruft keine Vorlage auf); geteilt
    # wird im Editor, die Endpunkte bleiben.
    "documents": {"closeShareModal", "form:post:alpine:`/work/fraktion-test/documents/${shareMotionId}/share/update/`"},
}


def _gleichstand(client: Any, url: str, schluessel: str) -> tuple[_Funktionen, _Funktionen]:
    bisher = client.get(url, {"ansicht": "bisher"} if "?" not in url else None)
    if "?" in url:
        bisher = client.get(f"{url}&ansicht=bisher")
    neu = client.get(url)
    assert bisher.status_code == neu.status_code == 200, (url, bisher.status_code, neu.status_code)
    assert not any(n.startswith(NEU) for n in _vorlagen(bisher)), url
    assert any(n.startswith(NEU) for n in _vorlagen(neu)), url
    pfad = urlsplit(url).path
    alt, jetzt = _funktionen(bisher.content.decode(), pfad), _funktionen(neu.content.decode(), pfad)
    ausnahmen = AUSNAHMEN.get(schluessel, set())

    fehlt = []
    jetzt_formulare = [(m, z, f) for m, z, f in jetzt.formulare]
    for methode, ziel, felder in alt.formulare:
        if f"form:{methode}:{ziel}" in ausnahmen:
            continue
        passend = [f for m, z, f in jetzt_formulare if m == methode and z == ziel]
        if not any(felder <= f for f in passend):
            fehlt.append(f"Formular {methode.upper()} {ziel} mit {sorted(felder)}")
    fehlt += [f"Link {link}" for link in sorted(alt.links - jetzt.links) if link not in ausnahmen]
    fehlt += [f"Aktion {aktion}" for aktion in sorted(alt.aktionen - jetzt.aktionen) if aktion not in ausnahmen]
    fehlt += [f"Aufruf {aufruf}()" for aufruf in sorted(alt.aufrufe - jetzt.aufrufe) if aufruf not in ausnahmen]
    fehlt += [f"Komponente {k}" for k in sorted(alt.komponenten - jetzt.komponenten)]
    fehlt += [f"JSON {j}" for j in sorted(alt.json_skripte - jetzt.json_skripte)]
    assert not fehlt, f"{url}: in der neuen Vorlage fehlt\n" + "\n".join(fehlt)
    return alt, jetzt


def test_gleichstand_liste(
    org: Any, autorin: Any, bestand: dict[str, Any], ordner: DocumentFolder, client_for: Any
) -> None:
    client = client_for(autorin.user)
    alt, neu = _gleichstand(client, _url("documents", org), "documents")
    assert {"openFolderCreate", "openFolderEdit", "moveDocuments", "setViewMode", "toggleFolderPanel"} <= neu.aufrufe
    _gleichstand(client, f"{_url('documents', org)}?ordner={ordner.id}&status=internal_review&q=Spiel", "documents")


def test_gleichstand_neues_dokument_auch_als_aenderungsantrag(
    org: Any, autorin: Any, bestand: dict[str, Any], ordner: DocumentFolder, client_for: Any
) -> None:
    client = client_for(autorin.user)
    _gleichstand(client, _url("document_create", org), "document_create")
    _, neu = _gleichstand(
        client, f"{_url('document_create', org)}?ordner={ordner.id}&bezug={bestand['antrag'].id}", "document_create"
    )
    felder = {f for _, _, fs in neu.formulare for f in fs}
    assert {"title", "summary", "template", "folder", "parent_motion"} <= felder


def test_gleichstand_import_papierkorb_und_freigaben(
    org: Any, autorin: Any, gast: Any, bestand: dict[str, Any], ordner: DocumentFolder, client_for: Any
) -> None:
    client = client_for(autorin.user)
    _gleichstand(client, _url("document_import", org), "document_import")
    _, neu = _gleichstand(client, _url("document_trash", org), "document_trash")
    geloescht = bestand["geloescht"].id
    assert f"data-post:{_url('document_restore', org, motion_id=geloescht)}" in neu.aktionen
    assert f"data-post:{_url('document_permanent_delete', org, motion_id=geloescht)}" in neu.aktionen
    assert f"data-post:{_url('document_empty_trash', org)}" in neu.aktionen

    FolderGuestShare.objects.create(folder=ordner, user=gast.user, level="comment", created_by=autorin.user)
    MotionShare.objects.create(
        motion=bestand["aenderung"], scope="user", user=gast.user, level="view", created_by=autorin.user
    )
    gast_client = client_for(gast.user)
    _gleichstand(gast_client, _url("guest_documents", org), "guest_documents")
    _gleichstand(gast_client, f"{_url('guest_documents', org)}?ordner={ordner.id}&sort=name", "guest_documents")


@pytest.fixture
def antrag(org: Any, autorin: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Radweg Hauptstraße", visibility="organization", status="approved"
    )
    motion.set_content_encrypted(INHALT)  # type: ignore[attr-defined]
    motion.save()
    MotionDocument.objects.create(
        motion=motion,
        file=SimpleUploadedFile("lageplan.pdf", b"%PDF-1.4 Lageplan"),
        filename="Lageplan.pdf",
        mime_type="application/pdf",
        file_size=17,
    )
    return motion


def test_gleichstand_einreichen_session_und_email(org: Any, autorin: Any, antrag: Motion, client_for: Any) -> None:
    client = client_for(autorin.user)
    url = _url("document_submit_ris", org, motion_id=antrag.id)
    _, neu = _gleichstand(client, url, "document_submit_ris")  # ohne Verbindung: Formular für mandari Session
    felder = {f for _, _, fs in neu.formulare for f in fs}
    assert {"title", "application_type", "resolution_proposal", "justification", "confirm", "is_urgent"} <= felder

    AdministrationContact.objects.create(organization=org, label="Ratsbüro", email="ratsbuero@stadt.example", order=0)
    _, neu = _gleichstand(client, url, "document_submit_ris")  # mit Kontakten: Einreichung per E-Mail
    assert any("channel=email" in f and {"contacts", "subject", "message", "confirm"} <= f for _, _, f in neu.formulare)

    with mock.patch("apps.work.motions.export_service.motion_export_service.export_to_pdf", return_value=b"%PDF-1.4"):
        antwort = client.post(
            url,
            {
                "channel": "email",
                "contacts": [str(AdministrationContact.objects.get().pk)],
                "subject": "Antrag: Radweg",
                "message": "",
                "confirm": "on",
            },
        )
    assert antwort.status_code == 302
    _gleichstand(client, url, "document_submit_ris")  # Stand der Einreichung per E-Mail


# ---------------------------------------------------------------------------
# Schalter, Rückweg und Rechte
# ---------------------------------------------------------------------------

SEITEN = ("documents", "document_create", "document_import", "document_trash", "guest_documents")


@pytest.mark.parametrize("seite", SEITEN)
def test_schalter_aus_und_rueckweg_zeigen_die_bisherige_vorlage(
    org: Any, autorin: Any, seite: str, client_for: Any
) -> None:
    client = client_for(autorin.user)
    neu = client.get(_url(seite, org))
    assert any(n.startswith(NEU) for n in _vorlagen(neu)) and 'data-gestaltung="neu"' in neu.content.decode()
    assert not any(n.startswith(NEU) for n in _vorlagen(client.get(_url(seite, org), {"ansicht": "bisher"})))

    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    bisher = client.get(_url(seite, org))
    assert not any(n.startswith(NEU) for n in _vorlagen(bisher))
    assert 'data-rahmen="neu"' not in bisher.content.decode()


@pytest.mark.parametrize("seite", [*SEITEN, "document_submit_ris"])
def test_rechte_wie_bisher(
    org: Any, autorin: Any, leserin: Any, gast: Any, antrag: Motion, seite: str, client_for: Any
) -> None:
    kwargs = {"motion_id": antrag.id} if seite == "document_submit_ris" else {}
    url = _url(seite, org, **kwargs)
    for person in (autorin, leserin, gast):
        client = client_for(person.user)
        neu, bisher = client.get(url), client.get(url, {"ansicht": "bisher"})
        assert neu.status_code == bisher.status_code, (seite, person.user.email, neu.status_code, bisher.status_code)


def test_leserin_sieht_keine_anlegen_aktionen(org: Any, leserin: Any, bestand: dict[str, Any], client_for: Any) -> None:
    html = client_for(leserin.user).get(_url("documents", org)).content.decode()
    assert 'openFolderCreate()"' not in html and "openFolderEdit(" not in html
    assert "Antrag Spielplatz &amp; Bolzplatz" in html  # einfach maskiert


# ---------------------------------------------------------------------------
# Inhalt der Liste
# ---------------------------------------------------------------------------


def test_liste_zeigt_stand_ordner_und_zeilenangaben(
    org: Any, autorin: Any, bestand: dict[str, Any], ordner: DocumentFolder, client_for: Any
) -> None:
    antwort = client_for(autorin.user).get(_url("documents", org))
    html = antwort.content.decode()
    assert antwort.status_code == 200
    assert "work/motions/neu/liste.html" in _vorlagen(antwort)
    assert 'data-testid="dokumente-kopf"' in html and "Dokumente:" in html  # Stand als Satz im Kopfband
    assert "Haushalt" in html and "Anträge 2026" in html
    assert "Änderungsantrag zu „Antrag Spielplatz" in html
    assert "Interne Absprache" in html and "überfällig" in html
    assert "Verkehr" in html and "Freigaben 0 von 1" in html
    assert 'x-data="documentManager"' in html and 'id="document-manager-config"' in html
    assert 'data-testid="dokumente-tabelle"' in html and 'data-testid="dokumente-kacheln"' in html


def test_leere_liste_und_filter_ohne_treffer(org: Any, autorin: Any, client_for: Any) -> None:
    client = client_for(autorin.user)
    assert "Noch keine Dokumente." in client.get(_url("documents", org)).content.decode()
    assert "Keine Dokumente für diese Auswahl." in client.get(_url("documents", org), {"q": "nichts"}).content.decode()


# ---------------------------------------------------------------------------
# Bausteine statt Hand-Markup
# ---------------------------------------------------------------------------


def _quelle(name: str) -> str:
    return (VORLAGEN / name).read_text(encoding="utf-8")


def test_neue_vorlagen_ohne_kartenrahmen_ueberlagerung_und_style() -> None:
    for datei in sorted(VORLAGEN.glob("*.html")):
        text = datei.read_text(encoding="utf-8")
        assert "rounded-xl border" not in text and "shadow-sm" not in text, datei.name
        assert "fixed inset-0" not in text, datei.name  # Dialoge über c-ui.alpine-modal
        assert ' style="' not in text, datei.name
        assert "uppercase" not in text, datei.name
        assert len(text.splitlines()) <= 300, datei.name


def test_seiten_nutzen_die_gemeinsamen_bausteine() -> None:
    for seite in ("liste.html", "neu.html", "import.html", "papierkorb.html", "freigaben.html", "einreichen.html"):
        text = _quelle(seite)
        assert "<c-rahmen.kopfband" in text and "dicht" in text, seite
        assert "{% block gestaltung %}neu{% endblock %}" in text, seite
    assert "<c-ui.alpine-modal" in _quelle("_ordner_dialog.html")
    assert "<c-liste.seiten" in _quelle("liste.html") and "<c-liste.seiten" in _quelle("papierkorb.html")
    for formular in ("neu.html", "_einreichen_formular.html", "_einreichen_email.html"):
        text = _quelle(formular)
        assert "<c-form.field" in text and '<input type="text"' not in text, formular
        assert "<textarea" not in text, formular
