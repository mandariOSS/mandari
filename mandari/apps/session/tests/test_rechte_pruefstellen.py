# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Heutige Prüfstellen der Session-Views und ihre Katalogrechte (Issue #772, Grundlage für #773).

Jede geroutete Session-View prüft heute Häkchen (``permission_required``, je Aktion ``ACTION_PERMS``). Hier steht,
welchen Rechten des Katalogs diese Stellen entsprechen. Regel: Die Herkünfte der zugeordneten Rechte sind **genau**
die Häkchen, die die Stelle heute prüft. Dann behält jede Rolle ihre Funktionen, wenn die Prüfstellen auf den
Katalog umgestellt werden und Rollen Katalogrechte speichern (#773, #775) – etwa die Rücknahme einer Vorlage im
Bearbeiten-Formular (``edit_papers``) oder die Absage einer Sitzung (``edit_meetings``).

Ohne Eintrag in :data:`ZUORDNUNG` entspricht eine Stelle den Leitrechten ihrer Häkchen. Eingetragen ist, was eine
Stelle darüber hinaus oder statt des Leitrechts erlaubt. Außerdem: Jedes Recht mit Herkunft hat eine Prüfstelle
oder steht mit Begründung in :data:`OHNE_VIEW`; jedes Recht ohne Herkunft steht in :data:`NEUE_FUNKTIONEN`, und
keine heutige Stelle entspricht ihm.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.urls import URLResolver, get_resolver

from apps.session.permissions import SessionPermissionMixin
from apps.session.rechte import katalog

#: Stelle (``modul.View`` bzw. ``modul.View:aktion``) → Rechte, die sie heute gewährt
ZUORDNUNG: dict[str, set[str]] = {
    # Sitzung
    "meetings.MeetingUpdateView": {"sitzung.bearbeiten", "sitzung.absagen"},
    "calendar.MeetingPlanView": {"sitzung.planen"},
    "invitations.MeetingInvitationView": {"sitzung.laden"},
    "invitation_responses.MeetingSerialLetterView": {"sitzung.laden"},
    "invitation_responses.MeetingInvitationReminderView": {"sitzung.laden"},
    "invitation_responses.MeetingLettersSentView": {"sitzung.laden"},
    "invitation_responses.MeetingInvitationStatusView": {"sitzung.laden"},
    "invitation_responses.MeetingInvitationProofView": {"sitzung.laden"},
    "invitation_responses.MeetingResponseEntryView": {"sitzung.laden", "sitzung.anwesenheit_fuehren"},
    # Tagesordnung
    "agenda.AgendaItemCreateView": {"tagesordnung.bearbeiten"},
    "agenda.AgendaItemUpdateView": {"tagesordnung.bearbeiten"},
    "agenda.AgendaItemDeleteView": {"tagesordnung.bearbeiten"},
    "agenda.AgendaItemMoveView": {"tagesordnung.bearbeiten"},
    "agenda.AgendaItemWithdrawView": {"tagesordnung.bearbeiten"},
    "agenda.AgendaReorderView": {"tagesordnung.bearbeiten"},
    # Vorlage: Rücknahme und Rückkehr in den Entwurf im Bearbeiten-Formular (Status „Zurückgezogen“)
    "papers.PaperUpdateView": {"vorlage.bearbeiten", "vorlage.zurueckziehen"},
    "papers.PaperWorkflowView:submit": {"vorlage.einreichen"},
    "papers.PaperWorkflowView:approve": {"vorlage.freigeben"},
    "papers.PaperWorkflowView:reject": {"vorlage.zurueckweisen"},
    "consultations.ConsultationScheduleView": {"vorlage.terminieren"},
    "consultations.ConsultationForwardView": {"vorlage.terminieren"},
    "cosign.CosignatureActionView": {"vorlage.mitzeichnen"},
    "cosign.MyCosignaturesView": {"vorlage.mitzeichnen"},
    # Antrag, Anfrage: Status, Zielgremium und Vermerke
    "applications.ApplicationProcessView": {"antrag.eingang_bearbeiten", "antrag.zuweisen"},
    # Niederschrift
    "protocols.ProtocolWorkflowView:submit": {"niederschrift.bearbeiten"},
    "protocols.ProtocolWorkflowView:reject": {"niederschrift.genehmigen"},
    "protocols.ProtocolWorkflowView:approve": {"niederschrift.genehmigen"},
    "protocols.ProtocolWorkflowView:publish": {"niederschrift.veroeffentlichen"},
    "protocols.ProtocolWorkflowView:unpublish": {"niederschrift.veroeffentlichen"},
    "protocols.ProtocolCorrectionView": {"niederschrift.berichtigen"},
    "protocols.ProtocolCorrectionDecisionView": {"niederschrift.berichtigen"},
    # Beschluss
    "resolutions.ResolutionRegisterView": {"beschluss.sehen"},
    "resolutions.ResolutionCsvExportView": {"beschluss.sehen"},
    "resolutions.ResolutionExtractPdfView": {"beschluss.sehen"},
    "resolutions.ResolutionMeetingPdfView": {"beschluss.sehen"},
    "resolutions.ResolutionTrackingUpdateView": {"beschluss.umsetzung_bearbeiten"},
    "resolutions.ResolutionForwardingCreateView": {"beschluss.auszug_uebergeben"},
    # Umlaufverfahren
    "voting.CircularListView": {"umlauf.sehen"},
    "voting.CircularDetailView": {"umlauf.sehen"},
    "voting.CircularCreateView": {"umlauf.anlegen"},
    "voting.CircularVoteView": {"umlauf.stimmen_erfassen"},
    "voting.CircularCloseView": {"umlauf.feststellen"},
    # Person, Besetzung, Gremium, Körperschaft
    "persons.PersonListView": {"person.sehen"},
    "persons.PersonDetailView": {"person.sehen", "person.kontaktdaten_sehen"},
    "persons.PersonCreateView": {"person.bearbeiten"},
    "persons.PersonUpdateView": {"person.bearbeiten"},
    "persons.PersonDeactivateView": {"person.bearbeiten"},
    "memberships.MembershipCreateView": {"person.bearbeiten"},
    "memberships.MembershipUpdateView": {"person.bearbeiten"},
    "memberships.MembershipEndView": {"person.bearbeiten"},
    "memberships.MembershipSuccessionView": {"person.bearbeiten"},
    "organizations.OrganizationListView": {"gremium.sehen"},
    "organizations.OrganizationDetailView": {"gremium.sehen"},
    "bodies.BodyListView": {"koerperschaft.verwalten"},
    "bodies.BodyCreateView": {"koerperschaft.verwalten"},
    "bodies.BodyUpdateView": {"koerperschaft.verwalten"},
    "bodies.BodyDefaultView": {"koerperschaft.verwalten"},
    # Sitzungsgeld, Pauschalen
    "allowances.AllowanceRateSaveView": {"sitzungsgeld.saetze_verwalten"},
    "allowances.AllowanceRateDeleteView": {"sitzungsgeld.saetze_verwalten"},
    "allowances.AllowanceDebtorSaveView": {"sitzungsgeld.saetze_verwalten"},
    "allowances.AllowanceApproveView": {"sitzungsgeld.anordnen"},
    "allowances.AllowanceCsvExportView": {"sitzungsgeld.exportieren"},
    "allowances.AllowanceSepaExportView": {"sitzungsgeld.exportieren"},
    "monthly_allowances.MonthlyRateSaveView": {"sitzungsgeld.saetze_verwalten"},
    "monthly_allowances.MonthlyRateDeleteView": {"sitzungsgeld.saetze_verwalten"},
    "monthly_allowances.MonthlyApproveView": {"sitzungsgeld.anordnen"},
    "monthly_allowances.MonthlyCsvExportView": {"sitzungsgeld.exportieren"},
    "monthly_allowances.MonthlySepaExportView": {"sitzungsgeld.exportieren"},
    # Abläufe
    "approvals.FourEyesSettingsView": {"ablauf.verwalten"},
    "approvals.ProtocolApprovalSettingsView": {"ablauf.verwalten"},
    "cosign.CosignSettingsView": {"ablauf.verwalten"},
    "cosign.CosignRuleManageView": {"ablauf.verwalten"},
    # Rollen, Konten, Vertretungen (Amtszuordnung der Mitzeichnung wird mit #775 eine Zuweisung)
    "roles.RoleListView": {"konto.rollen_verwalten"},
    "roles.RoleSaveView": {"konto.rollen_verwalten"},
    "roles.RoleDeleteView": {"konto.rollen_verwalten"},
    "settings.UserRolesUpdateView": {"konto.rollen_verwalten"},
    "cosign.DepartmentAssignmentView": {"konto.rollen_verwalten"},
    "approvals.DelegationListView": {"konto.vertretungen_verwalten"},
    "approvals.DelegationCreateView": {"konto.vertretungen_verwalten"},
    "approvals.DelegationRevokeView": {"konto.vertretungen_verwalten"},
    # Einstellungen, Schnittstellen, Veröffentlichung
    "api_tokens.APITokenListView": {"einstellung.schnittstellen_verwalten"},
    "api_tokens.APITokenCreateView": {"einstellung.schnittstellen_verwalten"},
    "api_tokens.APITokenRevokeView": {"einstellung.schnittstellen_verwalten"},
    "settings.OParlAccessView": {"einstellung.schnittstellen_verwalten"},
    "settings.OParlLicenseView": {"einstellung.schnittstellen_verwalten"},
    "settings.ImplementationPublishView": {"einstellung.veroeffentlichung_verwalten"},
    "settings.InsightPublishView": {"einstellung.veroeffentlichung_verwalten"},
    "settings.PortalPublicationEndView": {"einstellung.veroeffentlichung_verwalten"},
}

#: Geroutete Views ohne eigenes Häkchen: Sie prüfen das Objekt, zu dem sie gehören
OHNE_KLASSENRECHT: dict[str, str] = {
    "files.FileVersionListView": "Verlauf einer Anlage: sichtbar genau wie die Anlage selbst",
    "files.FileVersionDownloadView": "frühere Fassung einer Anlage: sichtbar genau wie die Anlage selbst",
}

#: Rechte mit Herkunft ohne eigene View-Prüfstelle → wo sie heute wirken
OHNE_VIEW: dict[str, str] = {
    "sitzung.noe_sehen": "Prüfungen in Views, Diensten und visible_to() (view_non_public_meetings)",
    "vorlage.noe_sehen": "Prüfungen in Views, Diensten und visible_to() (view_non_public_papers)",
    "niederschrift.noe_sehen": "nichtöffentliche Teile der Niederschrift (view_non_public_meetings)",
    "person.bankdaten_sehen": "Bankdaten in Person und Betroffenenauskunft (manage_allowances)",
    "einstellung.api_nutzen": "Session-API (access_api)",
    "einstellung.oparl_lesen": "ohne Wirkung: die OParl-Schnittstelle ist öffentlich",
    "sitzung.loeschen": "keine Prüfstelle: das Häkchen prüft heute keine Stelle",
    "vorlage.loeschen": "keine Prüfstelle: das Häkchen prüft heute keine Stelle",
    "antrag.beantworten": "Funktion folgt als Station; wer heute Anträge bearbeitet, erhält sie",
    "sitzungsgeld.pruefen": "Station; heute Teil der Genehmigung mit Vier-Augen-Prinzip",
}

#: Rechte ohne Herkunft: Die Funktion gibt es noch nicht; bis zur Rollenmatrix nur in der Administrator-Vollmacht
NEUE_FUNKTIONEN: dict[str, str] = {
    "tagesordnung.benehmen_erklaeren": "Benehmen bzw. Einvernehmen zur Tagesordnung (Ablauf-Regelwerk)",
    "tagesordnung.freigeben": "Freigabe der Tagesordnung als Station (Ablauf-Regelwerk)",
    "vorlage.veroeffentlichen": "keine eigene Aktion: öffentlich wird eine Vorlage mit der Freigabe",
    "niederschrift.unterzeichnen": "Unterzeichnung der Niederschrift als Station",
    "ablauf.station_uebersteuern": "Station eines Ablaufs übersteuern (Ablauf-Regelwerk)",
    "konto.rechteauskunft": "Rechteauskunft (#776)",
}


def _views() -> dict[str, type[SessionPermissionMixin]]:
    """Geroutete Views mit Häkchenprüfung, Schlüssel ``modul.View``."""
    gefunden: dict[str, type[SessionPermissionMixin]] = {}

    def laufen(muster: list[Any]) -> None:
        for eintrag in muster:
            if isinstance(eintrag, URLResolver):
                laufen(eintrag.url_patterns)
                continue
            klasse = getattr(eintrag.callback, "view_class", None)
            if klasse is not None and issubclass(klasse, SessionPermissionMixin):
                assert klasse.__module__.startswith("apps.session.views."), klasse
                gefunden[f"{klasse.__module__.rsplit('.', 1)[1]}.{klasse.__name__}"] = klasse

    laufen(get_resolver().url_patterns)
    return gefunden


def _pruefstellen() -> dict[str, set[str]]:
    """Stelle → Häkchen, die sie heute prüft (bei Aktionen je Aktion)."""
    stellen: dict[str, set[str]] = {}
    for name, klasse in _views().items():
        aktionen = getattr(klasse, "ACTION_PERMS", None)
        if aktionen:
            stellen.update({f"{name}:{aktion}": {haekchen} for aktion, haekchen in aktionen.items()})
            continue
        haekchen = klasse.permission_required
        if haekchen is None:
            continue
        stellen[name] = {haekchen} if isinstance(haekchen, str) else set(haekchen)
    return stellen


def _rechte_der_stelle(stelle: str, haekchen: set[str]) -> set[str]:
    return ZUORDNUNG.get(stelle) or {katalog.LEITRECHTE[name] for name in haekchen}


@pytest.fixture(scope="module")
def pruefstellen() -> dict[str, set[str]]:
    return _pruefstellen()


def test_views_gefunden(pruefstellen: dict[str, set[str]]) -> None:
    assert len(pruefstellen) > 150
    assert {"papers.PaperUpdateView", "meetings.MeetingUpdateView", "papers.PaperWorkflowView:approve"} <= set(
        pruefstellen
    )


def test_herkunft_ist_genau_das_gepruefte_haekchen(pruefstellen: dict[str, set[str]]) -> None:
    abweichend = {}
    for stelle, haekchen in sorted(pruefstellen.items()):
        rechte = _rechte_der_stelle(stelle, haekchen)
        herkunft = {katalog.KATALOG[recht].herkunft for recht in rechte}
        if herkunft != haekchen:
            abweichend[stelle] = (sorted(haekchen), {recht: katalog.KATALOG[recht].herkunft for recht in rechte})
    assert not abweichend


def test_zuordnung_ohne_veraltete_eintraege(pruefstellen: dict[str, set[str]]) -> None:
    assert set(ZUORDNUNG) <= set(pruefstellen)
    for rechte in ZUORDNUNG.values():
        assert rechte and rechte <= set(katalog.KATALOG)


def test_views_ohne_klassenrecht_sind_benannt() -> None:
    ohne = {
        name
        for name, klasse in _views().items()
        if klasse.permission_required is None and not getattr(klasse, "ACTION_PERMS", None)
    }
    assert ohne == set(OHNE_KLASSENRECHT)


def test_jedes_recht_mit_herkunft_hat_eine_pruefstelle(pruefstellen: dict[str, set[str]]) -> None:
    genutzt = set().union(*(_rechte_der_stelle(stelle, haekchen) for stelle, haekchen in pruefstellen.items()))
    mit_herkunft = {recht.kennung for recht in katalog.RECHTE if recht.herkunft}
    assert not genutzt & set(OHNE_VIEW)
    assert genutzt | set(OHNE_VIEW) == mit_herkunft


def test_rechte_ohne_herkunft_gibt_es_heute_nicht(pruefstellen: dict[str, set[str]]) -> None:
    ohne_herkunft = {recht.kennung for recht in katalog.RECHTE if recht.herkunft is None}
    assert ohne_herkunft == set(NEUE_FUNKTIONEN)
    genutzt = set().union(*(_rechte_der_stelle(stelle, haekchen) for stelle, haekchen in pruefstellen.items()))
    assert not genutzt & ohne_herkunft


def test_ruecknahme_und_absage_folgen_aus_dem_bearbeiten() -> None:
    """Rücknahme einer Vorlage und Absage einer Sitzung gibt es heute mit dem Bearbeiten-Häkchen."""
    assert katalog.KATALOG["vorlage.zurueckziehen"].herkunft == "edit_papers"
    assert katalog.KATALOG["sitzung.absagen"].herkunft == "edit_meetings"
    assert katalog.KATALOG["sitzung.loeschen"].herkunft == "delete_meetings"
    assert {"vorlage.zurueckziehen"} <= katalog.rechte_aus_haekchen(["edit_papers"])
    assert {"sitzung.absagen", "sitzung.planen"} <= katalog.rechte_aus_haekchen(["edit_meetings"])
    assert "sitzung.absagen" not in katalog.rechte_aus_haekchen(["delete_meetings"])
