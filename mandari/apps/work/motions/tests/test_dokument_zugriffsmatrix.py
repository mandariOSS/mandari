# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugriffsmatrix für Dokumente: jede Person × jedes Dokument × jeder Weg.

Die Regel steht hier unabhängig vom Code (``soll_stufe``); jeder Weg muss sie einhalten:

- **Liste** (``Motion.visible_to`` – Dokumentliste, Papierkorb, Dashboard, Auswahlfelder) und **Öffnen**
  stimmen überein: Niemand öffnet über einen Weg etwas, das die Liste ihm nicht zeigt.
- Mitglieder brauchen ``motions.view``, für Entwürfe anderer zusätzlich ``motions.view_drafts``. Zugang haben
  Autor:in, Federführung, Mitarbeit, alle bei organisationsweiten Dokumenten und persönlich Freigegebene bei
  geteilten Dokumenten.
- Stufe: Autor:in verwaltet (mit ``motions.edit``), ``motions.edit_all`` bearbeitet; Federführung,
  Mitarbeit und eine persönliche Freigabe „Bearbeiten“ bearbeiten mit ``motions.edit``; wer nur über eine Freigabe Zugang hat, bleibt
  bei deren Stufe; sonst Kommentieren (``motions.comment``) oder Lesen.
- Gäste: ausschließlich persönliche oder Ordner-Freigaben, nie Verwaltung, nie Papierkorb.
- Status-Sperre (eingereicht, gelöscht …): Inhalt nur mit ``motions.edit_all``; Gäste kommentieren.
- Status, Metadaten, Checkliste, Anhänge, Wiederherstellen aus dem Papierkorb folgen der Stufe ohne Sperre;
  Teilen und Versionen wiederherstellen verlangen Autor:in oder ``motions.edit_all``.
- Löschen (in den Papierkorb und endgültig): eigene Dokumente als Autor:in, Dokumente anderer nur mit
  ``motions.delete`` und Bearbeiten-Stufe.
- Dokumente entfernter Mitglieder (Autor:in geleert): mit ``motions.view_former_members`` mindestens lesbar,
  auch Entwürfe und private Dokumente; ohne das Recht wie Dokumente anderer (Issue #590).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import MembershipFactory, OrganizationFactory, RoleFactory, UserFactory
from apps.tenants.models import Membership, Organization
from apps.work.motions import export_service
from apps.work.motions.models import (
    DocumentFolder,
    FolderGuestShare,
    Motion,
    MotionDocument,
    MotionRevision,
    MotionShare,
)

pytestmark = pytest.mark.django_db

PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
RANG = {"none": 0, "view": 1, "comment": 2, "edit": 3, "admin": 4}
BEARBEITBAR = ("draft", "review", "internal_review", "external_review")

MITGLIED = (
    "motions.view",
    "motions.view_drafts",
    "motions.create",
    "motions.edit",
    "motions.comment",
    "motions.share",
)
#: Mitglieder und ihre Rechte
RECHTE: dict[str, tuple[str, ...]] = {
    "autor": MITGLIED,
    "mitglied": MITGLIED,
    "leser": ("motions.view", "motions.view_drafts"),
    # wie die Standardrolle Parteimitglied: ohne Entwürfe anderer
    "ohne_entwuerfe": ("motions.view", "motions.comment"),
    "ohne_view": ("dashboard.view",),
    "vorsitz": (*MITGLIED, "motions.edit_all"),
    "loeschberechtigt": (*MITGLIED, "motions.edit_all", "motions.delete"),
    "federfuehrung": MITGLIED,
    "mitarbeit": MITGLIED,
    # Zugewiesen, aber ohne motions.edit (und ohne Entwürfe anderer): Zugang, jedoch kein Bearbeiten
    "mitarbeit_kommentar": ("motions.view", "motions.comment"),
    "mitarbeit_lesen": ("motions.view",),
    "freigabe_lesen": MITGLIED,
    "freigabe_bearbeiten": MITGLIED,
    # Dokumente ehemaliger Mitglieder (Issue #590): nur lesend, ohne Entwürfe anderer …
    "ehemalige_einsicht": ("motions.view", "motions.comment", "motions.view_former_members"),
    # … und wie die Standardrollen Administrator/Fraktionsvorsitz
    "verwaltung": (*MITGLIED, "motions.edit_all", "motions.delete", "motions.view_former_members"),
}
#: Gäste (keine Rechte)
GAESTE = ("gast_lesen", "gast_bearbeiten", "gast_ordner", "gast_ordner_fremd", "gast_ohne")
PERSONEN = (*RECHTE, *GAESTE)
#: Persönliche Freigaben (MotionShare, scope=user) an allen Dokumenten
FREIGABEN = {"freigabe_lesen": "view", "freigabe_bearbeiten": "edit", "gast_lesen": "view", "gast_bearbeiten": "edit"}

#: Alle Dokumente stammen von „autor“ (``ehemalig_*``: von einem entfernten Mitglied, Autor:in geleert),
#: liegen im Ordner und haben Federführung, Mitarbeit und Freigaben
DOKUMENTE: dict[str, tuple[str, str]] = {
    "privat_entwurf": ("private", "draft"),
    "geteilt_entwurf": ("shared", "draft"),
    "org_entwurf": ("organization", "draft"),
    "privat_eingereicht": ("private", "submitted"),
    "geteilt_eingereicht": ("shared", "submitted"),
    "org_eingereicht": ("organization", "submitted"),
    "privat_geloescht": ("private", "deleted"),
    "org_geloescht": ("organization", "deleted"),
    "ehemalig_privat_entwurf": ("private", "draft"),
    "ehemalig_geteilt_eingereicht": ("shared", "submitted"),
    "ehemalig_org_entwurf": ("organization", "draft"),
    "ehemalig_privat_geloescht": ("private", "deleted"),
}
NAECHSTER_STATUS = {"draft": "internal_review", "submitted": "at_admin"}


# =============================================================================
# Die Regel (Soll)
# =============================================================================


def _ehemalig(dok: str) -> bool:
    return dok.startswith("ehemalig_")


def soll_stufe(person: str, dok: str, *, sperre: bool = True) -> str:
    sichtbarkeit, status = DOKUMENTE[dok]
    gesperrt = status not in BEARBEITBAR
    if person in GAESTE:
        if status == "deleted":
            return "none"
        stufen = []
        if person in FREIGABEN:
            stufen.append(FREIGABEN[person])
        # Ordner-Freigabe der Autorin: organisationsweite und ihre eigenen
        if person == "gast_ordner" and (sichtbarkeit == "organization" or not _ehemalig(dok)):
            stufen.append("view")
        if person == "gast_ordner_fremd" and sichtbarkeit == "organization":  # von jemand anderem
            stufen.append("view")
        if not stufen:
            return "none"
        stufe = max(stufen, key=RANG.__getitem__)
        if sperre and gesperrt and stufe == "edit":
            stufe = "comment"
        return stufe

    rechte = set(RECHTE[person])
    if "motions.view" not in rechte:
        return "none"
    autor = person == "autor" and not _ehemalig(dok)
    ehemalige = _ehemalig(dok) and "motions.view_former_members" in rechte
    if status == "draft" and not autor and not ehemalige and "motions.view_drafts" not in rechte:
        return "none"
    zugeteilt = person in ("federfuehrung", "mitarbeit", "mitarbeit_kommentar", "mitarbeit_lesen")
    freigabe = FREIGABEN.get(person) if sichtbarkeit != "private" else None
    if not (autor or zugeteilt or sichtbarkeit == "organization" or (sichtbarkeit == "shared" and freigabe)):
        return "view" if ehemalige else "none"
    kommentar = "comment" if "motions.comment" in rechte else "view"
    if autor:
        stufe = "admin" if "motions.edit" in rechte else kommentar
    elif "motions.edit_all" in rechte or ("motions.edit" in rechte and (freigabe == "edit" or zugeteilt)):
        stufe = "edit"
    elif sichtbarkeit == "shared" and freigabe:  # Zugang nur über die Freigabe
        stufe = min(freigabe, kommentar, key=RANG.__getitem__)
    else:
        stufe = kommentar
    if sperre and gesperrt and stufe in ("edit", "admin"):
        stufe = "edit" if "motions.edit_all" in rechte else kommentar
    return stufe


def _mitglied_mit(person: str, recht: str) -> bool:
    return person not in GAESTE and recht in RECHTE[person]


def soll_verwalten(person: str, dok: str) -> bool:
    """Autor:in (verwaltend) oder ``motions.edit_all`` mit Zugang."""
    stufe = soll_stufe(person, dok, sperre=False)
    return person not in GAESTE and (
        stufe == "admin" or (stufe != "none" and _mitglied_mit(person, "motions.edit_all"))
    )


def soll_loeschen(person: str, dok: str) -> bool:
    """Eigene Dokumente (Autor:in verwaltet) oder ``motions.delete`` mit Bearbeiten-Stufe."""
    stufe = soll_stufe(person, dok, sperre=False)
    return person not in GAESTE and (stufe == "admin" or (stufe == "edit" and _mitglied_mit(person, "motions.delete")))


def _bearbeiten_ohne_sperre(person: str, dok: str) -> bool:
    return _mitglied_mit(person, "motions.edit") and soll_stufe(person, dok, sperre=False) in ("edit", "admin")


# =============================================================================
# Testdaten
# =============================================================================


@dataclass
class Welt:
    organization: Organization
    mitglieder: dict[str, Membership]
    clients: dict[str, Client]
    dokumente: dict[str, Motion]
    anhaenge: dict[str, MotionDocument] = field(default_factory=dict)
    versionen: dict[str, MotionRevision] = field(default_factory=dict)
    freigaben: dict[str, MotionShare] = field(default_factory=dict)


@pytest.fixture
def welt(tmp_path: Any) -> Any:
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        org = cast(Organization, cast(Any, OrganizationFactory)(name="Matrix Dokumente", slug="matrix-dokumente"))
        mitglieder: dict[str, Membership] = {}
        for person in PERSONEN:
            user = cast(User, cast(Any, UserFactory)(email=f"{person}@matrix.example.org"))
            if person in GAESTE:
                mitglieder[person] = Membership.objects.create(user=user, organization=org, is_guest=True)
            else:
                rolle = cast(Any, RoleFactory)(
                    organization=org, name=f"Rolle {person}", permissions=list(RECHTE[person])
                )
                mitglieder[person] = cast(
                    Membership, cast(Any, MembershipFactory)(user=user, organization=org, roles=[rolle])
                )
        ordner = DocumentFolder.objects.create(organization=org, name="Ausschuss", created_by=mitglieder["autor"])
        FolderGuestShare.objects.create(
            folder=ordner, user=mitglieder["gast_ordner"].user, level="view", created_by=mitglieder["autor"].user
        )
        FolderGuestShare.objects.create(
            folder=ordner,
            user=mitglieder["gast_ordner_fremd"].user,
            level="view",
            created_by=mitglieder["vorsitz"].user,
        )

        welt = Welt(org, mitglieder, {}, {})
        for dok, (sichtbarkeit, status) in DOKUMENTE.items():
            motion = Motion.objects.create(
                organization=org,
                author=None if _ehemalig(dok) else mitglieder["autor"],
                title=f"Dokument {dok}",
                visibility=sichtbarkeit,
                status=status,
                folder=ordner,
                responsible=mitglieder["federfuehrung"],
                deleted_at=timezone.now() if status == "deleted" else None,
            )
            motion.set_content_encrypted("<p>Inhalt</p>")  # type: ignore[attr-defined]
            motion.save()
            motion.contributors.add(
                mitglieder["mitarbeit"], mitglieder["mitarbeit_kommentar"], mitglieder["mitarbeit_lesen"]
            )
            for person, stufe in FREIGABEN.items():
                share = MotionShare.objects.create(
                    motion=motion,
                    scope="user",
                    user=mitglieder[person].user,
                    level=stufe,
                    created_by=mitglieder["autor"].user,
                )
                if person == "freigabe_lesen":
                    welt.freigaben[dok] = share
            revision = MotionRevision(motion=motion, version=1, changed_by=mitglieder["autor"])
            revision.set_content_encrypted("<p>Alt</p>")  # type: ignore[attr-defined]
            revision.save()
            welt.versionen[dok] = revision
            welt.anhaenge[dok] = MotionDocument.objects.create(
                motion=motion,
                file=SimpleUploadedFile("anlage.pdf", PDF_BYTES, content_type="application/pdf"),
                filename="anlage.pdf",
                mime_type="application/pdf",
                file_size=len(PDF_BYTES),
                uploaded_by=mitglieder["autor"],
            )
            welt.dokumente[dok] = motion
        for person, membership in mitglieder.items():
            client = Client(raise_request_exception=False)
            client.force_login(membership.user)
            welt.clients[person] = client
        yield welt


# =============================================================================
# Wege
# =============================================================================


@dataclass(frozen=True)
class Weg:
    name: str
    method: str  # get | post | model
    url: Callable[[Welt, str], str] | None
    soll: Callable[[str, str], bool]
    daten: Callable[[Welt, str], dict[str, Any]] = lambda welt, dok: {}
    nur: Callable[[str], bool] = lambda dok: True
    datei: bool = False


def _k(welt: Welt, dok: str, **extra: Any) -> dict[str, Any]:
    return {"org_slug": welt.organization.slug, "motion_id": welt.dokumente[dok].id, **extra}


def _zugang(person: str, dok: str) -> bool:
    return soll_stufe(person, dok, sperre=False) != "none"


def _nicht_geloescht(dok: str) -> bool:
    return DOKUMENTE[dok][1] != "deleted"


def _geloescht(dok: str) -> bool:
    return DOKUMENTE[dok][1] == "deleted"


WEGE: tuple[Weg, ...] = (
    Weg("oeffnen", "get", lambda w, d: reverse("work:document_editor", kwargs=_k(w, d)), _zugang),
    Weg("export", "get", lambda w, d: reverse("work:document_export", kwargs=_k(w, d)) + "?format=pdf", _zugang),
    Weg(
        "anhang_laden",
        "get",
        lambda w, d: reverse("work:document_file_download", kwargs=_k(w, d, document_id=w.anhaenge[d].id)),
        _zugang,
    ),
    Weg("versionen", "get", lambda w, d: reverse("work:document_revisions", kwargs=_k(w, d)), _zugang),
    Weg(
        "version_ansehen",
        "get",
        lambda w, d: reverse("work:document_revision_detail", kwargs=_k(w, d, revision_id=w.versionen[d].id)),
        # Versionen entstehen hier nach den Freigaben; Gäste sehen sie also (Beginn der Freigabe davor)
        _zugang,
    ),
    Weg(
        "kommentieren",
        "post",
        lambda w, d: reverse("work:document_comment", kwargs=_k(w, d)),
        lambda p, d: soll_stufe(p, d) in ("comment", "edit", "admin")
        and (p in GAESTE or _mitglied_mit(p, "motions.comment")),
        lambda w, d: {"content": "Anmerkung"},
    ),
    Weg(
        "inhalt_speichern",
        "post",
        lambda w, d: reverse("work:document_editor", kwargs=_k(w, d)),
        lambda p, d: soll_stufe(p, d) in ("edit", "admin"),
        lambda w, d: {"action": "save", "title": w.dokumente[d].title, "content": "<p>Neu</p>"},
    ),
    Weg(
        "in_papierkorb",
        "post",
        lambda w, d: reverse("work:document_editor", kwargs=_k(w, d)),
        lambda p, d: soll_stufe(p, d) in ("edit", "admin") and soll_loeschen(p, d),
        lambda w, d: {"action": "delete"},
        nur=_nicht_geloescht,
    ),
    Weg(
        "status",
        "post",
        lambda w, d: reverse("work:document_status", kwargs=_k(w, d)),
        _bearbeiten_ohne_sperre,
        lambda w, d: {"status": NAECHSTER_STATUS[DOKUMENTE[d][1]]},
        nur=_nicht_geloescht,
    ),
    Weg(
        "metadaten",
        "post",
        lambda w, d: reverse("work:document_meta", kwargs=_k(w, d)),
        _bearbeiten_ohne_sperre,
        lambda w, d: {"action": "set_due_date", "due_date": "2026-12-31"},
    ),
    Weg(
        "checkliste",
        "post",
        lambda w, d: reverse("work:document_checklist", kwargs=_k(w, d)),
        _bearbeiten_ohne_sperre,
        lambda w, d: {"action": "add", "title": "Punkt"},
    ),
    Weg(
        "anhang_hochladen",
        "post",
        lambda w, d: reverse("work:document_upload", kwargs=_k(w, d)),
        _bearbeiten_ohne_sperre,
        datei=True,
    ),
    Weg(
        "anhang_umbenennen",
        "post",
        lambda w, d: reverse("work:document_file_rename", kwargs=_k(w, d, document_id=w.anhaenge[d].id)),
        _bearbeiten_ohne_sperre,
        lambda w, d: {"filename": "Lageplan"},
    ),
    Weg(
        "anhang_entfernen",
        "post",
        lambda w, d: reverse("work:document_file_delete", kwargs=_k(w, d, document_id=w.anhaenge[d].id)),
        _bearbeiten_ohne_sperre,
    ),
    Weg(
        "teilen",
        "post",
        lambda w, d: reverse("work:document_share_update", kwargs=_k(w, d)),
        lambda p, d: soll_verwalten(p, d) and _mitglied_mit(p, "motions.share"),
        lambda w, d: {"visibility": DOKUMENTE[d][0]},
    ),
    Weg(
        "freigabe_entziehen",
        "post",
        lambda w, d: reverse(
            "work:document_share_remove", kwargs={"org_slug": w.organization.slug, "share_id": w.freigaben[d].id}
        ),
        lambda p, d: soll_verwalten(p, d) and _mitglied_mit(p, "motions.share"),
    ),
    Weg(
        "version_wiederherstellen",
        "post",
        lambda w, d: reverse("work:document_revision_restore", kwargs=_k(w, d, revision_id=w.versionen[d].id)),
        lambda p, d: _mitglied_mit(p, "motions.edit")
        and soll_verwalten(p, d)
        and soll_stufe(p, d) in ("edit", "admin"),
    ),
    Weg(
        "aus_papierkorb_holen",
        "post",
        lambda w, d: reverse("work:document_restore", kwargs=_k(w, d)),
        _bearbeiten_ohne_sperre,
        nur=_geloescht,
    ),
    Weg(
        "endgueltig_loeschen",
        "post",
        lambda w, d: reverse("work:document_permanent_delete", kwargs=_k(w, d)),
        lambda p, d: _bearbeiten_ohne_sperre(p, d) and soll_loeschen(p, d),
        nur=_geloescht,
    ),
)

ERLAUBT = (200, 204)
ABGEWIESEN = (302, 403, 404)


def _anfrage(welt: Welt, person: str, weg: Weg, dok: str) -> HttpResponse:
    client = welt.clients[person]
    assert weg.url is not None
    url = weg.url(welt, dok)
    daten = weg.daten(welt, dok)
    if weg.datei:
        daten["file"] = SimpleUploadedFile("neu.pdf", PDF_BYTES, content_type="application/pdf")
    cookies = copy.deepcopy(client.cookies)
    try:
        with transaction.atomic():
            if weg.method == "get":
                antwort = client.get(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            else:
                antwort = client.post(url, daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            transaction.set_rollback(True)
    finally:
        client.cookies = cookies
    return cast(HttpResponse, antwort)


def _tabelle(abweichungen: list[tuple[str, str, str, str]]) -> str:
    zeilen = [f"  {person:<20} {dok:<22} soll={soll:<10} ist={ist}" for person, dok, soll, ist in abweichungen]
    return f"{len(abweichungen)} Abweichungen:\n" + "\n".join(zeilen)


@pytest.mark.parametrize("weg", WEGE, ids=[weg.name for weg in WEGE])
def test_weg_folgt_der_regel(welt: Welt, weg: Weg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export_service.motion_export_service, "export_to_pdf", lambda motion: PDF_BYTES)
    abweichungen = []
    for person in PERSONEN:
        for dok in DOKUMENTE:
            if not weg.nur(dok):
                continue
            soll = weg.soll(person, dok)
            code = _anfrage(welt, person, weg, dok).status_code
            if code not in ERLAUBT + ABGEWIESEN:
                abweichungen.append((person, dok, "erlaubt" if soll else "abgewiesen", f"Fehler {code}"))
            elif (code in ERLAUBT) != soll:
                abweichungen.append((person, dok, "erlaubt" if soll else "abgewiesen", f"{code}"))
    assert not abweichungen, f"{weg.name}: " + _tabelle(abweichungen)


def test_liste_stimmt_mit_der_regel_ueberein(welt: Welt) -> None:
    """Dokumentliste, Papierkorb, Dashboard und Auswahlfelder rechnen über ``Motion.visible_to``."""
    abweichungen = []
    for person in PERSONEN:
        membership = welt.mitglieder[person]
        mit_papierkorb = set(
            cast(Any, Motion).visible_to(membership, include_deleted=True).values_list("id", flat=True)
        )
        ohne_papierkorb = set(cast(Any, Motion).visible_to(membership).values_list("id", flat=True))
        for dok, motion in welt.dokumente.items():
            soll = _zugang(person, dok)
            if (motion.id in mit_papierkorb) != soll:
                abweichungen.append((person, dok, str(soll), str(motion.id in mit_papierkorb)))
            if motion.id in ohne_papierkorb and _geloescht(dok):
                abweichungen.append((person, dok, "nicht in der Liste", "gelöschtes Dokument in der Liste"))
    assert not abweichungen, "Liste: " + _tabelle(abweichungen)


def test_stufen_im_modell(welt: Welt) -> None:
    """Editor und Live-Kollaboration nutzen dieselbe Stufe; can_* leiten sich daraus ab."""
    abweichungen = []
    for person in PERSONEN:
        membership = welt.mitglieder[person]
        for dok, motion in welt.dokumente.items():
            stufe = soll_stufe(person, dok)
            ist = {
                "editor": motion.editor_access_level(membership),
                "kollaboration": motion.get_collab_access_level(membership),
                "can_access": motion.can_access(membership),
                "can_comment": motion.can_comment(membership),
                "can_edit": motion.can_edit(membership),
                "can_share": motion.can_share(membership),
                "can_delete": motion.can_delete(membership),
            }
            soll = {
                "editor": stufe,
                "kollaboration": None if stufe == "none" else ("edit" if stufe == "admin" else stufe),
                "can_access": _zugang(person, dok),
                "can_comment": stufe in ("comment", "edit", "admin"),
                "can_edit": soll_stufe(person, dok, sperre=False) in ("edit", "admin"),
                "can_share": soll_verwalten(person, dok) and _mitglied_mit(person, "motions.share"),
                "can_delete": soll_loeschen(person, dok),
            }
            for schluessel, wert in soll.items():
                if ist[schluessel] != wert:
                    abweichungen.append((person, dok, f"{schluessel}={wert}", str(ist[schluessel])))
    assert not abweichungen, "Modell: " + _tabelle(abweichungen)
