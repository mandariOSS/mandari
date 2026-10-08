# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neuer Antragseditor (Teil von #856): Schalter je Organisation, Ablauf Entwurf → Abstimmung → Freigabe → Einreichung
über die vorhandenen Status und Freigabe-Anfragen, Vorauswahl der Stimmberechtigten. Der bisherige Editor bleibt
Standard und über ``?ansicht=bisher`` erreichbar; Seitenaufrufe ändern keine Daten.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, cast

import pytest
from django.utils import timezone

from apps.work.motions import ablauf, editor_neu
from apps.work.motions.models import Motion, MotionApproval, MotionComment

EDIT = ["motions.view", "motions.view_drafts", "motions.edit", "motions.comment"]
CONFIG_RE = re.compile(r'<script[^>]*id="document-editor-config"[^>]*>(.*?)</script>', re.S)
VORAUSWAHL_RE = re.compile(r'<script[^>]*id="editor-abstimmung-vorauswahl"[^>]*>(.*?)</script>', re.S)


@pytest.fixture
def neues_design(org: Any) -> None:
    """Schalter „Neues Erscheinungsbild“ (Organization.work_new_design, #852) für die Organisation an."""
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, [*EDIT, "voting.participate"], email="autorin@example.org")


@pytest.fixture
def motion(org: Any, autorin: Any) -> Motion:
    m = Motion.objects.create(organization=org, author=autorin, title="Antrag Musterweg", visibility="organization")
    cast(Any, m).set_content_encrypted('<h2>Begründung</h2><p>Text mit <span data-comment-id="m1">Stelle</span>.</p>')
    m.save()
    return m


def url(org: Any, motion: Motion, query: str = "") -> str:
    return f"/work/{org.slug}/documents/{motion.id}/{query}"


def config_aus(html: str) -> dict[str, Any]:
    match = CONFIG_RE.search(html)
    assert match
    result: dict[str, Any] = json.loads(match.group(1))
    return result


# ---- Ablauf ------------------------------------------------------------------------------------------------------


class _Approval:
    def __init__(self, approved: bool | None) -> None:
        self.approved = approved


@pytest.mark.django_db
class TestAblauf:
    def test_entwurf_naechster_schritt_abstimmung(self, motion: Motion) -> None:
        stand = ablauf.ablauf_fuer(motion, approvals=[], offene_kommentare=2, darf_steuern=True)
        assert stand.aktuell == ablauf.ENTWURF
        assert [s.zustand for s in stand.stufen] == ["aktuell", "offen", "offen", "offen"]
        assert stand.stufen[0].unter == "Offen: 2 Kommentare"
        assert stand.aktion == "abstimmung"
        # Die Abstimmung ist ein eigener Schritt (Klick auf die Stufe), keine Infozeile beim Schreiben
        assert stand.stufen[1].dialog == "abstimmung"
        assert stand.info_titel == ""

    def test_ohne_recht_kein_schritt(self, motion: Motion) -> None:
        stand = ablauf.ablauf_fuer(motion, approvals=[], offene_kommentare=0, darf_steuern=False)
        assert stand.aktion == ""
        assert all(not s.dialog for s in stand.stufen)

    def test_abstimmung_zaehlt_freigabe_anfragen(self, motion: Motion) -> None:
        motion.status = "internal_review"
        approvals = [_Approval(True), _Approval(True), _Approval(False), _Approval(None)]
        stand = ablauf.ablauf_fuer(motion, approvals=approvals, offene_kommentare=0, darf_steuern=True)
        assert stand.aktuell == ablauf.ABSTIMMUNG
        assert stand.stufen[1].unter == "2 von 4 zugestimmt"
        assert stand.aktion == "freigeben"
        assert stand.info_titel == "Interne Absprache."
        assert stand.info_text == "2 von 4 haben zugestimmt, 1 abgelehnt, 1 offen."

    def test_freigegeben_einreichung_ist_dran(self, motion: Motion) -> None:
        motion.status = "approved"
        motion.due_date = dt.date(2026, 10, 26)
        stand = ablauf.ablauf_fuer(motion, approvals=[], offene_kommentare=0, darf_steuern=True)
        assert stand.aktuell == ablauf.EINREICHUNG
        assert [s.zustand for s in stand.stufen] == ["erledigt", "erledigt", "erledigt", "aktuell"]
        assert stand.aktion == "einreichen"
        assert stand.info_text == "Nächster Schritt: bei der Verwaltung einreichen, Frist 26.10.2026."

    def test_eingereicht_alle_stufen_erledigt(self, motion: Motion) -> None:
        motion.status = "submitted"
        motion.submitted_at = timezone.make_aware(dt.datetime(2026, 10, 22, 10, 0))
        stand = ablauf.ablauf_fuer(motion, approvals=[], offene_kommentare=0, darf_steuern=True)
        assert stand.aktuell == ablauf.FERTIG
        assert stand.jetzt == 3
        assert stand.stufen[3].unter == "am 22.10.2026"
        assert (stand.info_titel, stand.info_text) == ("Eingereicht", "am 22.10.2026.")
        assert stand.aktion == ""

    @pytest.mark.parametrize(("eingereicht", "erwartet"), [(False, ablauf.ENTWURF), (True, ablauf.FERTIG)])
    def test_abgelehnt_je_nach_einreichung(self, motion: Motion, eingereicht: bool, erwartet: int) -> None:
        motion.status = "rejected"
        motion.submitted_at = timezone.now() if eingereicht else None
        stand = ablauf.ablauf_fuer(motion, approvals=[], offene_kommentare=0, darf_steuern=True)
        assert stand.aktuell == erwartet
        # Aus „Abgelehnt“ führt kein Schritt des Ablaufs; Statuswechsel bleiben im Menü „Ablauf“
        assert stand.aktion == ""


@pytest.mark.django_db
def test_stimmberechtigte_nach_rechten(org: Any, make_member: Any) -> None:
    mit = make_member(org, ["voting.participate"], email="mit@example.org")
    admin = make_member(org, [], email="admin@example.org", is_admin=True)
    ohne = make_member(org, ["motions.view"], email="ohne@example.org")
    verweigert = make_member(org, ["voting.participate"], email="verweigert@example.org")
    from apps.tenants.models import Permission

    verweigert.denied_permissions.add(Permission.objects.get_or_create(codename="voting.participate")[0])
    gast = make_member(org, ["voting.participate"], email="gast@example.org")
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])

    ids = ablauf.stimmberechtigte_ids(org)
    assert mit.id in ids
    assert admin.id in ids
    assert ohne.id not in ids
    assert verweigert.id not in ids
    assert gast.id not in ids


# ---- Wer zur Abstimmung angefragt werden kann ------------------------------------------------------------------

OHNE_ZUGANG_RE = re.compile(r'<script[^>]*id="editor-abstimmung-ohne-zugang"[^>]*>(.*?)</script>', re.S)


def _kreis_mitglieder(org: Any, make_member: Any, motion: Motion) -> dict[str, Any]:
    """Mitglieder mit allen Zugangswegen: Leserecht ohne Entwürfe, ohne Leserecht, Mitarbeit, Freigabe, Admin …"""
    from apps.work.motions.models import MotionShare

    mitglieder = {
        "leser": make_member(org, ["motions.view", "voting.participate"], email="kreis-leser@example.org"),
        "ohne_leserecht": make_member(org, ["voting.participate"], email="kreis-ohne@example.org"),
        "mitarbeit": make_member(org, [*EDIT, "voting.participate"], email="kreis-mitarbeit@example.org"),
        "freigabe": make_member(
            org, ["motions.view", "motions.comment", "voting.participate"], email="kreis-freigabe@example.org"
        ),
        "admin": make_member(org, [], email="kreis-admin@example.org", is_admin=True),
        "vereidigt": make_member(
            org, ["motions.view", "voting.participate", "faction.view_non_public"], email="kreis-vereidigt@example.org"
        ),
        "beratend": make_member(org, ["motions.view"], email="kreis-beratend@example.org"),
    }
    mitglieder["vereidigt"].is_sworn_in = True
    mitglieder["vereidigt"].save(update_fields=["is_sworn_in"])
    motion.contributors.add(mitglieder["mitarbeit"])
    if not motion.is_sworn_in_only():  # Nichtöffentliches lässt sich nicht freigeben (MotionShare.save)
        MotionShare.objects.create(
            motion=motion,
            scope="user",
            user=mitglieder["freigabe"].user,
            level="comment",
            created_by=mitglieder["mitarbeit"].user,
        )
    return mitglieder


@pytest.mark.django_db
@pytest.mark.parametrize("sichtbarkeit", ["organization", "shared", "private"])
@pytest.mark.parametrize("darf_teilen", [False, True])
@pytest.mark.parametrize("nichtoeffentlich", [False, True])
def test_abstimmung_kreis_folgt_der_zugriffsregel(
    org: Any, make_member: Any, sichtbarkeit: str, darf_teilen: bool, nichtoeffentlich: bool
) -> None:
    """Liste, Vorauswahl und „ohne Zugang“ entsprechen Motion.can_access nach dem Start der Abstimmung."""
    from apps.work.motions.models import DocumentFolder

    rechte = [*EDIT, "voting.participate", "faction.view_non_public", *(["motions.share"] if darf_teilen else [])]
    autorin = make_member(org, rechte, email="kreis-autorin@example.org")
    autorin.is_sworn_in = True
    autorin.save(update_fields=["is_sworn_in"])
    ordner = (
        DocumentFolder.objects.create(organization=org, name="Nichtöffentlich", sworn_in_only=True)
        if nichtoeffentlich
        else None
    )
    motion = Motion.objects.create(
        organization=org, author=autorin, title="Antrag Kreis", visibility=sichtbarkeit, status="draft", folder=ordner
    )
    mitglieder = _kreis_mitglieder(org, make_member, motion)
    inaktiv = make_member(org, ["motions.view", "voting.participate"], email="kreis-inaktiv@example.org")
    inaktiv.is_active = False
    inaktiv.save(update_fields=["is_active"])

    kreis = ablauf.abstimmung_kreis(motion, autorin)
    angezeigt = {m.id for m in kreis.mitglieder}
    stimmrecht = ablauf.stimmberechtigte_ids(org)
    teilen = motion.can_share(autorin)
    nach_start = Motion.objects.get(pk=motion.pk)
    nach_start.status = ablauf.START_STATUS
    for name, mitglied in mitglieder.items():
        sieht = nach_start.can_access(mitglied)
        if sieht:
            assert mitglied.id in angezeigt and mitglied.id not in kreis.ohne_zugang, name
        elif teilen and mitglied.has_permission("motions.view"):
            # Die Anfrage gibt das Dokument frei (Freigabe-Weg wie bisher), der Dialog sagt es
            assert mitglied.id in angezeigt and mitglied.id in kreis.ohne_zugang, name
        else:
            assert mitglied.id not in angezeigt, name
        # Vorausgewählt werden nur Stimmberechtigte, die das Dokument ohnehin sehen dürfen
        assert (mitglied.id in kreis.vorauswahl) == (sieht and mitglied.id in stimmrecht), name
    assert autorin.id not in angezeigt
    assert inaktiv.id not in angezeigt
    if nichtoeffentlich:
        # Nichtöffentliches lässt sich nie teilen: nur Vereidigte, niemand „ohne Zugang“
        assert angezeigt == {mitglieder["vereidigt"].id}
        assert not kreis.ohne_zugang


@pytest.mark.django_db
def test_abstimmung_kreis_ohne_abfrage_je_mitglied(
    org: Any, make_member: Any, django_assert_max_num_queries: Any
) -> None:
    autorin = make_member(org, [*EDIT, "voting.participate", "motions.share"], email="kreis-zahl@example.org")
    motion = Motion.objects.create(organization=org, author=autorin, title="Antrag", visibility="shared")
    for nummer in range(12):
        make_member(org, ["motions.view", "motions.edit", "voting.participate"], email=f"kreis-{nummer}@example.org")
    motion = Motion.objects.get(pk=motion.pk)
    autorin = type(autorin).objects.get(pk=autorin.pk)
    with django_assert_max_num_queries(15):
        kreis = ablauf.abstimmung_kreis(motion, autorin)
    assert len(kreis.mitglieder) == 12


@pytest.mark.django_db
class TestAbstimmungPrivat:
    """Private Dokumente: Vorausgewählt wird nur, wer es sieht; wer es erst durch die Anfrage sieht, erfährt man."""

    def test_ohne_teilen_recht_nur_wer_es_sieht(
        self, org: Any, autorin: Any, make_member: Any, client_for: Any, neues_design: None
    ) -> None:
        motion = Motion.objects.create(organization=org, author=autorin, title="Privat", visibility="private")
        stimme = make_member(org, ["motions.view", "voting.participate"], email="privat-stimme@example.org")
        mitarbeit = make_member(org, [*EDIT, "voting.participate"], email="privat-mitarbeit@example.org")
        motion.contributors.add(mitarbeit)
        html = client_for(autorin.user).get(url(org, motion)).content.decode()
        vorauswahl = VORAUSWAHL_RE.search(html)
        ohne_zugang = OHNE_ZUGANG_RE.search(html)
        assert vorauswahl and ohne_zugang
        assert json.loads(vorauswahl.group(1)) == [str(mitarbeit.id)]
        assert json.loads(ohne_zugang.group(1)) == []
        dialog = html[html.index('data-testid="dialog-abstimmung"') :]
        assert f'value="{mitarbeit.id}"' in dialog
        assert f'value="{stimme.id}"' not in dialog
        assert 'data-testid="abstimmung-freigabe-hinweis"' not in html

    def test_mit_teilen_recht_auswahl_moeglich_mit_hinweis(
        self, org: Any, make_member: Any, client_for: Any, neues_design: None
    ) -> None:
        teilende = make_member(org, [*EDIT, "motions.share"], email="privat-teilende@example.org")
        motion = Motion.objects.create(organization=org, author=teilende, title="Privat", visibility="private")
        stimme = make_member(org, ["motions.view", "voting.participate"], email="privat-stimme2@example.org")
        html = client_for(teilende.user).get(url(org, motion)).content.decode()
        vorauswahl = VORAUSWAHL_RE.search(html)
        ohne_zugang = OHNE_ZUGANG_RE.search(html)
        assert vorauswahl and ohne_zugang
        # Nicht vorausgewählt (sonst gäbe ein Klick das private Dokument allen Stimmberechtigten frei) …
        assert json.loads(vorauswahl.group(1)) == []
        # … aber wählbar, und der Dialog sagt, was die Auswahl bewirkt
        assert json.loads(ohne_zugang.group(1)) == [str(stimme.id)]
        dialog = html[html.index('data-testid="dialog-abstimmung"') :]
        assert f'value="{stimme.id}"' in dialog
        assert 'data-testid="abstimmung-freigabe-hinweis"' in dialog
        assert "nicht mehr privat" in dialog


# ---- Seite und Schalter -----------------------------------------------------------------------------------------


@pytest.mark.django_db
class TestSchalter:
    def test_standard_bisheriger_editor(self, org: Any, motion: Motion, autorin: Any, client_for: Any) -> None:
        response = client_for(autorin.user).get(url(org, motion))
        assert response.status_code == 200
        assert [t.name for t in response.templates][0] == "work/motions/editor.html"
        assert config_aus(response.content.decode())["ansicht"] == "bisher"
        assert "ablauf" not in response.context

    def test_neuer_editor_bei_eingeschaltetem_schalter(
        self, org: Any, motion: Motion, autorin: Any, client_for: Any, neues_design: None
    ) -> None:
        response = client_for(autorin.user).get(url(org, motion))
        html = response.content.decode()
        assert response.status_code == 200
        assert [t.name for t in response.templates][0] == "work/motions/editor_neu.html"
        assert 'data-ansicht="neu"' in html
        config = config_aus(html)
        assert config["ansicht"] == "neu"
        assert config["status"] == "draft"
        assert config["urls"]["approvalRequest"] == f"/work/{org.slug}/documents/{motion.id}/approvals/request/"
        assert config["membershipId"] == str(autorin.id)
        # Kopfzeile: Menüleiste und Ablaufleiste mit vier Stufen
        for label in ("Datei", "Bearbeiten", "Ansicht", "Einfügen", "Format", "Ablauf"):
            assert f">{label}</button>" in html
        assert html.count('class="ke-stufe-knopf"') == 4
        assert "<script>" not in html.split("document-editor-config")[1].split("</script>", 1)[1]

    def test_bisherige_ansicht_bleibt_erreichbar(
        self, org: Any, motion: Motion, autorin: Any, client_for: Any, neues_design: None
    ) -> None:
        response = client_for(autorin.user).get(url(org, motion, "?ansicht=bisher"))
        assert [t.name for t in response.templates][0] == "work/motions/editor.html"

    def test_vorauswahl_stimmberechtigte_ohne_eigene_person(
        self, org: Any, motion: Motion, autorin: Any, make_member: Any, client_for: Any, neues_design: None
    ) -> None:
        stimme = make_member(org, ["voting.participate", "motions.view"], email="stimme@example.org")
        make_member(org, ["motions.view"], email="beratend@example.org")
        html = client_for(autorin.user).get(url(org, motion)).content.decode()
        match = VORAUSWAHL_RE.search(html)
        assert match
        assert json.loads(match.group(1)) == [str(stimme.id)]
        assert 'data-testid="dialog-abstimmung"' in html

    def test_lesezugriff_ohne_editor_und_ohne_schritte(
        self, org: Any, motion: Motion, make_member: Any, client_for: Any, neues_design: None
    ) -> None:
        motion.status = "submitted"
        motion.submitted_at = timezone.now()
        motion.save(update_fields=["status", "submitted_at"])
        leser = make_member(org, ["motions.view", "motions.view_drafts"], email="leser@example.org")
        html = client_for(leser.user).get(url(org, motion)).content.decode()
        assert 'class="content-readonly" data-ke-text' in html
        assert 'id="editor-container"' not in html
        assert "Der Text ist nur noch lesbar." in html
        assert 'data-testid="dialog-' not in html

    def test_seitenaufruf_aendert_keine_daten(
        self, org: Any, motion: Motion, autorin: Any, client_for: Any, neues_design: None
    ) -> None:
        kommentar = MotionComment.objects.create(motion=motion, author=autorin, content="Bitte prüfen", mark_id=None)
        MotionApproval.objects.create(motion=motion, approver=autorin, approval_type="council")
        vorher = (
            cast(Any, motion).get_content_decrypted(),
            motion.status,
            MotionComment.objects.count(),
            MotionApproval.objects.count(),
        )
        client_for(autorin.user).get(url(org, motion))
        client_for(autorin.user).get(url(org, motion, "?ansicht=bisher"))
        motion.refresh_from_db()
        kommentar.refresh_from_db()
        nachher = (
            cast(Any, motion).get_content_decrypted(),
            motion.status,
            MotionComment.objects.count(),
            MotionApproval.objects.count(),
        )
        assert vorher == nachher
        assert kommentar.is_resolved is False


@pytest.mark.django_db
class TestNachDerFreigabe:
    """Der Text ist nach der Freigabe gesperrt, der Ablauf (Status, Einreichung) bleibt steuerbar."""

    def test_freigegeben_einreichen_bleibt_moeglich(
        self, org: Any, motion: Motion, autorin: Any, client_for: Any, neues_design: None
    ) -> None:
        motion.status = "approved"
        motion.save(update_fields=["status"])
        html = client_for(autorin.user).get(url(org, motion)).content.decode()
        info = html[html.index('data-testid="ablauf-info"') :]
        # Knopf der Infozeile (die Dialoge folgen erst danach und haben keinen solchen Knopf)
        assert "Einreichen …</button>" in info
        assert 'data-testid="dialog-einreichen"' in html
        assert "Status ändern (aktuell: Freigegeben)" in html
        # Der Text selbst ist gesperrt
        assert 'id="editor-container"' not in html

    def test_eingangsnummer_der_verwaltung_in_der_infozeile(
        self,
        org: Any,
        make_member: Any,
        client_for: Any,
        neues_design: None,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        from apps.session.models import SessionAPIToken, SessionTenant
        from apps.work.motions import ris_submission

        autor = make_member(org, [*EDIT, "motions.submit_to_ris", "motions.approve"], email="einreichen@example.org")
        tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")
        _token, raw = SessionAPIToken.create_token(tenant, "Fraktion Test", can_submit_applications=True)
        ris_submission.connect_with_token(org, raw, autor)
        antrag = Motion.objects.create(
            organization=org, author=autor, title="Radweg Musterweg", visibility="organization", status="approved"
        )
        cast(Any, antrag).set_content_encrypted(
            "<h2>Beschlussvorschlag</h2><p>Radweg.</p><h2>Begründung</h2><p>Ja.</p>"
        )
        antrag.save()
        with django_capture_on_commit_callbacks(execute=True):
            eingang = ris_submission.submit_motion(antrag, autor, ris_submission.build_prefill(antrag))
        assert eingang is not None and eingang.reference

        html = client_for(autor.user).get(url(org, antrag)).content.decode()
        info = html[html.index('data-testid="ablauf-info"') :]
        # Nach der Einreichung ist der Text gesperrt: ohne „alle bearbeiten“ wie im bisherigen Kopf nur der Stand
        assert 'title="Stand bei der Verwaltung"' in info
        assert str(eingang.reference) in info


def test_menue_eintraege_fest() -> None:
    assert [key for key, _ in editor_neu.MENUE_LEISTE] == [
        "datei",
        "bearbeiten",
        "ansicht",
        "einfuegen",
        "format",
        "ablauf",
    ]
    assert editor_neu.ABSTIMMUNG_ART in dict(MotionApproval.APPROVAL_TYPE_CHOICES)
