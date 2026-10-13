# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2b: Fassungen einer Vorlage und Verlauf einer Anlage (Issue #944).

Geprüft: Fassungen, Fassung, Vergleich und der Verlauf einer Anlage haben mit eingeschaltetem Schalter je eine eigene
neue Vorlage, ohne Schalter bleiben die bisherigen. Keine Funktion entfällt und keine kommt hinzu: Jede Aktion der
bisherigen Seiten steht mit denselben Merkmalen (Rückfrage bei Wiederherstellen und bei der Datenschutz-Löschung) auf
der neuen und umgekehrt – für jede Standardrolle, mit von Hand gesicherter, automatischer, beschlossener,
nichtöffentlicher, wiederhergestellter und unveränderter Fassung, mit gelöschtem, löschbarem und unlesbarem Inhalt,
Anlagen an Vorlage, Sitzung, TOP und Antrag (auch nichtöffentlich), die Vorlage im Entwurf (Wiederherstellen möglich)
und in der Prüfung (gesperrt). Der Vergleich zeigt Streichungen und Einfügungen ohne Kästen.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionFile,
    SessionFileBlob,
    SessionPaper,
    SessionPaperVersion,
    SessionPaperVersionFile,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_version_service, paper_version_service
from apps.session.tests.gleichstand import gezeigt, reiter_ausnahmen, vergleichen
from apps.session.tests.test_neues_design import _client, _mandant, _volle_welt, _vorlagen

pytestmark = pytest.mark.django_db

V = SessionPaperVersion

#: Seiten dieses Teils: Name → (bisherige Vorlage, neue Vorlage)
VORLAGEN = {
    "fassungen": ("session/papers/versions/list.html", "session/neu/fassungen.html"),
    "fassung": ("session/papers/versions/detail.html", "session/neu/fassung.html"),
    "vergleich": ("session/papers/versions/compare.html", "session/neu/fassungen_vergleich.html"),
    "verlauf": ("session/files/versions.html", "session/neu/dateiversionen.html"),
}

#: Neue Vorlagen dieses Teils (für die Prüfung der Schriftgrößen)
DATEIEN = (
    "fassungen.html",
    "fassung.html",
    "fassungen_vergleich.html",
    "dateiversionen.html",
    "_fassung_inhalt_loeschen.html",
    "_fassung_vergleich_kopf.html",
)


def _ersetzen(datei: SessionFile, inhalt: bytes) -> None:
    upload = SimpleUploadedFile(datei.name, inhalt, content_type="text/plain")
    file_version_service.replace_content(datei, upload, user=None, mime_type="text/plain", text_content="")


def _blob(tenant: SessionTenant, inhalt: bytes) -> SessionFileBlob:
    import hashlib

    return SessionFileBlob.objects.get(tenant=tenant, sha256=hashlib.sha256(inhalt).hexdigest())


def _welt_fassungen(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand auf der Grundlage von ``_volle_welt``: sechs Fassungen der Vorlage – 1 von Hand mit Bemerkung und einer
    Anlage, deren Inhalt beim Sichern nicht lesbar war, 2 automatisch mit geändertem Sachverhalt, neuer Frist,
    ersetztem Bericht und Vermerk und einer hinzugefügten Anlage, 3 beschlossen in der Beratung (TOP), 4
    nichtöffentlich, 5 wiederhergestellt aus Fassung 1, 6 unverändert gegenüber 5. Der Bericht ist dreimal ersetzt (ein
    früherer Inhalt ist löschbar, einer gehört zur beschlossenen Fassung), der frühere Inhalt des nichtöffentlichen
    Vermerks ist aus Datenschutzgründen gelöscht. Dazu ein nichtöffentlicher Anhang eines Antrags.
    """
    welt = _volle_welt(tenant)
    slug = tenant.slug
    vorlage: SessionPaper = welt["vorlage_obj"]
    sitzung = welt["sitzung_obj"]
    anlagen: dict[str, SessionFile] = welt["anlagen"]
    bericht, vermerk = anlagen["bericht.txt"], anlagen["vermerk.txt"]
    top = SessionAgendaItem.objects.get(meeting=sitzung, number="2")
    bearbeitung = SessionUser.objects.create(user=cast(Any, UserFactory)(email="sb@example.org"), tenant=tenant)

    f1 = paper_version_service.snapshot(vorlage, trigger=V.TRIGGER_MANUAL, user=bearbeitung, note="Erster Stand")
    SessionPaperVersionFile.objects.create(
        version=f1,
        attachment_id=uuid.uuid4(),
        file_version_number=1,
        blob=None,
        name="alt.pdf",
        is_public=True,
        position=9,
    )

    vorlage.main_text = "Sachverhalt der Brücke mit Beleuchtung"
    vorlage.deadline = date(2026, 12, 1)
    vorlage.save()
    _ersetzen(bericht, b"Bericht B")
    _ersetzen(vermerk, b"Vermerk B")
    anhang = SessionFile(tenant=tenant, name="anhang.txt", mime_type="text/plain", is_public=True, paper=vorlage)
    file_version_service.attach_upload(anhang, SimpleUploadedFile("anhang.txt", b"Anhang", "text/plain"), user=None)
    f2 = paper_version_service.snapshot(vorlage, trigger=V.TRIGGER_TRANSITION, previous_status="draft")

    _ersetzen(bericht, b"Bericht C")
    _ersetzen(vermerk, b"Vermerk C")
    paper_version_service.snapshot(vorlage, trigger=V.TRIGGER_CONSULTATION, agenda_item=top, is_resolved=True)

    SessionPaper.objects.filter(pk=vorlage.pk).update(is_public=False)
    vorlage.refresh_from_db()
    paper_version_service.snapshot(vorlage, trigger=V.TRIGGER_MANUAL, note="Vertraulicher Zwischenstand")
    SessionPaper.objects.filter(pk=vorlage.pk).update(is_public=True)
    vorlage.refresh_from_db()

    _ersetzen(bericht, b"Bericht D")
    paper_version_service.snapshot(vorlage, trigger=V.TRIGGER_RESTORE, restored_from=f1)
    paper_version_service.snapshot(vorlage, trigger=V.TRIGGER_MANUAL)

    file_version_service.purge_content(_blob(tenant, b"Vermerk B"), user=None, reason="Personenbezug")
    antrag_anhang = SessionFile(
        tenant=tenant, name="antrag.txt", mime_type="text/plain", is_public=False, application=welt["antrag_obj"]
    )
    file_version_service.attach_upload(
        antrag_anhang, SimpleUploadedFile("antrag.txt", b"Antrag", "text/plain"), user=None
    )
    _ersetzen(antrag_anhang, b"Antrag 2")

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    v = {"paper_id": vorlage.pk}
    vergleich = adresse("paper_version_compare", **v)
    welt["seiten"] = {
        "fassungen": adresse("paper_versions", **v),
        **{f"fassung-{n}": adresse("paper_version_detail", number=n, **v) for n in range(1, 7)},
        **{f"vergleich-{a}-{b}": f"{vergleich}?a={a}&b={b}" for a, b in ((1, 2), (2, 3), (3, 4), (5, 6), (6, 1))},
        **{
            f"verlauf-{datei.name}": adresse("file_versions", file_id=datei.pk)
            for datei in (bericht, vermerk, anlagen["plan.txt"], anlagen["einladung.txt"], antrag_anhang)
        },
    }
    eintrag_b = SessionPaperVersionFile.objects.get(version=f2, name="bericht.txt")
    welt["erwartet"] = {
        ("POST", adresse("paper_version_create", **v)),
        ("GET", vergleich),
        ("GET", f"{vergleich}?a=1&b=2"),
        ("GET", adresse("paper_version_detail", number=1, **v)),
        ("POST", adresse("paper_version_restore", number=1, **v)),
        ("GET", adresse("paper_version_file", number=2, entry_id=eintrag_b.pk, **v)),
        ("POST", adresse("file_content_purge", blob_id=_blob(tenant, b"Bericht B").pk)),
        ("GET", adresse("file_version_download", file_id=bericht.pk, number=2)),
        ("GET", adresse("file_download", file_id=bericht.pk)),
        ("GET", adresse("file_download", file_id=antrag_anhang.pk)),
        ("GET", adresse("meeting_detail", meeting_id=sitzung.pk)),
    }
    welt.update(bericht=bericht, vermerk=vermerk, antrag_anhang=antrag_anhang)
    return welt


def _art(name: str) -> str:
    return name.split("-", 1)[0]


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("fassungen2b", neu=False)
    welt = _welt_fassungen(tenant)
    client = _client(tenant, "Administrator")
    for name, adresse in welt["seiten"].items():
        bisher, neu = VORLAGEN[_art(name)]
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(adresse)
        assert alt.status_code == 200, name
        assert bisher in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt), name
        assert neu not in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        antwort = client.get(adresse)
        assert antwort.status_code == 200, name
        assert {neu, "session/base_session_neu.html"} <= _vorlagen(antwort), name
        assert bisher not in _vorlagen(antwort), name
        assert 'data-gestaltung="neu"' in antwort.content.decode(), name


# =============================================================================
# Keine Funktion entfällt, keine kommt hinzu
# =============================================================================


def test_gleichstand_der_fassungen_und_des_verlaufs_fuer_alle_rollen() -> None:
    """
    Jede Aktion der bisherigen Seiten (Links, Formulare mit Feldern und Optionen, Rückfragen) steht mit denselben
    Merkmalen auf den neuen und umgekehrt – für alle sechs Standardrollen, die Vorlage im Entwurf. Einzige Ausnahme:
    die vier Seiten der bisherigen Seitenleiste, die im neuen Rahmen Reiter über ihrer Liste sind (Rahmen, nicht Seite).
    """
    tenant = _mandant("gleich2b", neu=False)
    welt = _welt_fassungen(tenant)
    fehlt, gesehen = vergleichen(tenant, welt["seiten"], ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    # Der Bestand zeigt wirklich, was er prüfen soll (sonst wäre der Vergleich leer)
    assert welt["erwartet"] <= gezeigt(gesehen), sorted(welt["erwartet"] - gezeigt(gesehen))


def test_gleichstand_der_fassung_in_der_pruefung() -> None:
    """In der Prüfung lässt sich keine Fassung wiederherstellen; beide Seiten nennen den Grund statt des Knopfs."""
    tenant = _mandant("pruefung2b", neu=False)
    welt = _welt_fassungen(tenant)
    SessionPaper.objects.filter(pk=welt["vorlage_obj"].pk).update(status="review")
    seiten = {name: adresse for name, adresse in welt["seiten"].items() if _art(name) in ("fassungen", "fassung")}
    fehlt, gesehen = vergleichen(tenant, seiten, ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    assert not any(adresse.endswith("/restore/") for _, adresse, _ in gesehen)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    html = _client(tenant, "Sachbearbeiter").get(welt["seiten"]["fassung-1"]).content.decode()
    assert 'data-testid="wiederherstellen-gesperrt"' in html and "nur im Entwurf" in html


def test_gleichstand_ohne_fassungen_und_ohne_fruehere_inhalte() -> None:
    """Leerzustände: Vorlage ohne Fassung (nur „Fassung sichern“) und Anlage ohne frühere Fassung."""
    tenant = _mandant("leer2b", neu=False)
    welt = _volle_welt(tenant)
    seiten = {
        "fassungen": reverse(
            "session:paper_versions", kwargs={"tenant_slug": tenant.slug, "paper_id": welt["vorlage_obj"].pk}
        ),
        "verlauf": reverse(
            "session:file_versions", kwargs={"tenant_slug": tenant.slug, "file_id": welt["anlagen"]["bericht.txt"].pk}
        ),
    }
    fehlt, _ = vergleichen(tenant, seiten, ausnahmen=reiter_ausnahmen(tenant))
    assert not fehlt, "\n".join(fehlt)
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
    client = _client(tenant, "Administrator")
    assert "Noch keine Fassung gesichert" in client.get(seiten["fassungen"]).content.decode()
    assert "Keine früheren Fassungen gespeichert" in client.get(seiten["verlauf"]).content.decode()


# =============================================================================
# Rechte und Darstellung
# =============================================================================


def test_nichtoeffentliches_bleibt_verborgen() -> None:
    """Ohne NÖ-Recht fehlen die nichtöffentliche Fassung, ihre Seite und der nichtöffentliche Vermerk auch im neuen Bild."""
    tenant = _mandant("noe2b", neu=True)
    welt = _welt_fassungen(tenant)
    lesen = _client(tenant, "Lesezugriff")
    liste = lesen.get(welt["seiten"]["fassungen"]).content.decode()
    assert 'data-fassung="4"' not in liste and 'data-fassung="3"' in liste
    assert lesen.get(welt["seiten"]["fassung-4"]).status_code == 404
    assert "vermerk.txt" not in lesen.get(welt["seiten"]["fassung-2"]).content.decode()
    assert lesen.get(welt["seiten"]["verlauf-vermerk.txt"]).status_code in (403, 404)
    verwaltung = _client(tenant, "Sachbearbeiter")
    assert 'data-fassung="4"' in verwaltung.get(welt["seiten"]["fassungen"]).content.decode()
    # Löschen bietet die Seite nur mit dem Einstellungsrecht an
    loeschen = reverse(
        "session:file_content_purge",
        kwargs={"tenant_slug": tenant.slug, "blob_id": _blob(tenant, b"Bericht B").pk},
    )
    assert loeschen not in verwaltung.get(welt["seiten"]["fassung-2"]).content.decode()
    assert loeschen in _client(tenant, "Administrator").get(welt["seiten"]["fassung-2"]).content.decode()


def test_vergleich_zeigt_streichungen_und_einfuegungen_ohne_kaesten() -> None:
    tenant = _mandant("diff2b", neu=True)
    welt = _welt_fassungen(tenant)
    client = _client(tenant, "Administrator")
    html = client.get(welt["seiten"]["vergleich-1-2"]).content.decode()
    inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
    assert "mit Beleuchtung</ins>" in inhalt
    assert "[&_ins]:bg-green-100" in inhalt and "[&_del]:bg-red-100" in inhalt
    # Anlagen: hinzugefügt, geändert und (für NÖ-Berechtigte) entfernt; unverändert im nächsten Vergleich
    assert 'data-art="added"' in inhalt and 'data-art="changed"' in inhalt and 'data-art="removed"' in inhalt
    assert 'data-art="unchanged"' in client.get(welt["seiten"]["vergleich-2-3"]).content.decode()
    assert "rounded-xl" not in inhalt and "<table" not in inhalt
    gleich = client.get(welt["seiten"]["vergleich-5-6"]).content.decode()
    assert 'data-testid="keine-unterschiede"' in gleich


def test_zustaende_der_inhalte_in_fassung_und_verlauf() -> None:
    """Gelöschter, löschbarer und unlesbarer Inhalt stehen als Satz bzw. aufklappbares Formular in der Zeile."""
    tenant = _mandant("inhalt2b", neu=True)
    welt = _welt_fassungen(tenant)
    client = _client(tenant, "Administrator")
    fassung1 = client.get(welt["seiten"]["fassung-1"]).content.decode()
    assert "Inhalt war beim Sichern nicht lesbar." in fassung1
    fassung2 = client.get(welt["seiten"]["fassung-2"]).content.decode()
    assert "aus Datenschutzgründen gelöscht" in fassung2 and "Inhalt endgültig löschen (Datenschutz)" in fassung2
    verlauf = client.get(welt["seiten"]["verlauf-vermerk.txt"]).content.decode()
    assert "aus Datenschutzgründen gelöscht" in verlauf
    assert 'data-fassung="2"' in verlauf and 'data-fassung="1"' in verlauf
    fassung3 = client.get(welt["seiten"]["fassung-3"]).content.decode()
    assert "Beschlossene Fassung" in fassung3 and "Diese Fassung ist unveränderlich." in fassung3
    liste = client.get(welt["seiten"]["fassungen"]).content.decode()
    assert "Inhalt wie Fassung 5" in liste


def test_keine_gefuellten_knoepfe_ausserhalb_des_kopfbands() -> None:
    """Höchstens die eine Hauptaktion im Kopfband trägt die Füllfarbe, nie ein Abschnitt darunter."""
    tenant = _mandant("knoepfe2b", neu=True)
    welt = _welt_fassungen(tenant)
    client = _client(tenant, "Administrator")
    for name, adresse in welt["seiten"].items():
        html = client.get(adresse).content.decode()
        inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
        kopf = html[html.index('data-gestaltung="neu"') : html.index('class="work-page-content"')]
        assert "bg-primary-600 hover:bg-primary-700" not in inhalt, f"{name}: gefüllter Knopf im Inhalt"
        assert kopf.count("bg-primary-600 hover:bg-primary-700") <= 1, f"{name}: mehr als eine Hauptaktion"
        for farbe in ("green", "blue", "indigo", "amber", "teal", "red", "purple"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in inhalt, f"{name}: Knopf in {farbe}"


def test_neue_vorlagen_nicht_in_12_px() -> None:
    """Die neuen Vorlagen setzen Text in 13 bis 15 px, nie in text-xs (12 px) oder kleiner."""
    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    for datei in DATEIEN:
        text = (neu / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"
        for verboten in ("c-ui.card", "c-ui.badge", "c-ui.kpi-tile", "rounded-xl", "{# \n"):
            assert verboten not in text, f"{datei}: {verboten}"
