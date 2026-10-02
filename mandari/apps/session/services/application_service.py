# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Application service for handling application submissions.

This service provides the integration between Work module and Session RIS,
enabling political organizations to submit applications (Anträge) directly.
"""

import logging
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.session.models import (
    SessionApplication,
    SessionOrganization,
    SessionPaper,
    SessionTenant,
)

logger = logging.getLogger(__name__)

#: Antragsart → Vorlagenart (bestimmt den Nummernkreis, z. B. „AN/…“ für Anträge der Politik)
PAPER_TYPE_FOR_APPLICATION = {"inquiry": "inquiry", "amendment": "amendment", "resolution": "resolution"}

#: Status, den nur die Umwandlung setzt – nie von Hand
CONVERTED = "converted"
#: Status, aus denen sich ein Antrag umwandeln lässt (nicht abgelehnt, nicht zurückgezogen)
CONVERTIBLE_STATUSES = frozenset({"submitted", "received", "in_review", "accepted"})
#: Angaben im Feld „Finanzielle Auswirkungen“, die „keine“ bedeuten
NO_FINANCIAL_IMPACT = frozenset(
    {"keine", "nein", "-", "–", "—", "0", "entfällt", "keine kosten", "keine finanziellen auswirkungen"}
)


class ConversionError(ValueError):
    """Umwandlung in eine Vorlage nicht möglich (Text ist für Nutzer:innen gedacht)."""


def default_paper_type(application: SessionApplication) -> str:
    return PAPER_TYPE_FOR_APPLICATION.get(application.application_type, "motion")


def conversion_blocker(application: SessionApplication) -> str | None:
    """Grund, warum sich der Antrag nicht in eine Vorlage umwandeln lässt; None, wenn es geht."""
    if application.status in CONVERTIBLE_STATUSES:
        return None
    return f"Ein Antrag im Status „{application.get_status_display()}“ wird nicht in eine Vorlage umgewandelt."


def manual_statuses(application: SessionApplication) -> set[str]:
    """
    Status, die die Antragsbearbeitung von Hand setzen darf.

    „In Vorlage umgewandelt“ setzt nur die Umwandlung. Solange die Vorlage dazu besteht, bleibt ein
    umgewandelter Antrag dabei – sonst stünde der Antrag wieder zur Umwandlung, obwohl es die Vorlage gibt.
    """
    if application.status == CONVERTED and application.created_papers.exists():
        return {CONVERTED}
    alle = {value for value, _ in SessionApplication._meta.get_field("status").flatchoices}
    return (alle - {CONVERTED}) | {application.status}


def financial_impact(application: SessionApplication) -> tuple[bool | None, str]:
    """
    Angabe „Finanzielle Auswirkungen“ für die Vorlage: (Ja/Nein/keine Angabe, Text).

    Der Antrag hat dafür nur ein Freitextfeld. Leer heißt „keine Angabe“, eindeutige Verneinungen
    wie „keine“ heißen „Nein“, alles andere „Ja“ – der Text kommt in jedem Fall mit.
    """
    text = (application.financial_impact or "").strip()
    if not text:
        return None, ""
    return text.lower().rstrip(".!").strip() not in NO_FINANCIAL_IMPACT, text


def convert_to_paper(
    application: SessionApplication,
    *,
    session_user: Any = None,
    name: str = "",
    paper_type: str = "",
    main_organization_id: object = None,
) -> tuple[SessionPaper, bool]:
    """
    Antrag in eine Vorlage umwandeln (Issue #316: ein Weg für Portal und Admin).

    Die Vorlage entsteht öffentlich **im Entwurf** (Issue #721): Sie durchläuft Mitzeichnung, Vier-Augen-Prüfung
    und Freigabe wie jede Vorlage, bevor sie auf eine Tagesordnung kommt. Die Nummer vergibt der Nummernkreis
    atomar beim Speichern, je nach Einstellung schon jetzt oder erst bei der Freigabe (Issue #150). Der Antrag
    wechselt per Einzel-Speichern auf „In Vorlage umgewandelt“ – so laufen Audit-Log und die Rückmeldung an die
    einreichende Fraktion über die Signale; die Drucksachennummer erfährt die Fraktion mit der Veröffentlichung.

    Returns:
        (Vorlage, neu angelegt?) – ein bereits umgewandelter Antrag liefert seine Vorlage zurück.

    Raises:
        ConversionError: Antrag ist abgelehnt oder zurückgezogen, oder das federführende Gremium gehört
            nicht zum Mandanten.
        NumberingError: Der Nummernkreis kann keine Nummer vergeben.
    """
    tenant = application.tenant
    existing = SessionPaper.objects.filter(tenant=tenant, source_application=application).first()
    if existing is not None:
        return existing, False
    blocker = conversion_blocker(application)
    if blocker:
        raise ConversionError(blocker)

    valid_types = {value for value, _ in SessionPaper._meta.get_field("paper_type").flatchoices}
    if paper_type not in valid_types:
        paper_type = default_paper_type(application)

    main_organization = None
    if main_organization_id:
        try:
            main_organization = SessionOrganization.objects.filter(pk=main_organization_id, tenant=tenant).first()
        except (ValueError, ValidationError):
            main_organization = None
        if main_organization is None:
            raise ConversionError("Das gewählte federführende Gremium wurde nicht gefunden.")

    has_financial_impact, financial_impact_note = financial_impact(application)
    with transaction.atomic():
        paper = SessionPaper.objects.create(
            tenant=tenant,
            name=(name or "").strip()[:500] or application.title,
            paper_type=paper_type,
            main_text=application.justification,
            resolution_text=application.resolution_proposal,
            has_financial_impact=has_financial_impact,
            financial_impact_note=financial_impact_note,
            is_public=True,
            date=timezone.localdate(),
            main_organization=main_organization,
            source_application=application,
            created_by=session_user,
            # Entwurf statt „Freigegeben“ (Issue #721): keine Freigabe am Freigabelauf vorbei
            status="draft",
        )
        application.status = "converted"
        application.save(update_fields=["status", "updated_at"])
        # Anhänge des Antrags hängen jetzt auch an der Vorlage – nichtöffentlich, bis die
        # Verwaltung sie freigibt (#584)
        application.files.filter(paper__isnull=True).update(paper=paper, is_public=False)
    return paper, True


def attachment_accepted(name: str, size: int) -> str | None:
    """Warum eine Datei nicht als Antrags-Anhang angenommen wird – oder ``None`` (#584)."""
    from . import file_service

    ext = PurePosixPath(name or "").suffix.lower()
    if ext not in file_service.ALLOWED_EXTENSIONS:
        return "Dateityp wird von der Verwaltung nicht angenommen"
    if size > file_service.MAX_FILE_SIZE_MB * 1024 * 1024:
        return f"größer als {file_service.MAX_FILE_SIZE_MB} MB"
    return None


def attach_application_files(application: SessionApplication, files: list[tuple[str, Any]]) -> list[str]:
    """
    Anhänge eines eingereichten Antrags speichern (Work → Session, #584).

    ``files``: Paare aus Anzeigename und Django-``File`` (Dateiname ohne Pfad). Geprüft wird wie bei Anlagen der
    Verwaltung (Dateityp, Größe, Virenscan-Hook); gespeichert über die Fassungsablage
    (Deduplizierung je Mandant). Anhänge bleiben nichtöffentlich.

    Returns:
        Namen der nicht angenommenen Dateien (mit Grund).
    """
    from apps.session.models import SessionFile

    from . import file_service, file_version_service

    skipped: list[str] = []
    for name, content in files:
        reason = attachment_accepted(name, int(getattr(content, "size", 0) or 0))
        if reason is None:
            try:
                file_service.scan_upload(content)
            except Exception:  # Befund oder Prüfung nicht verfügbar – der Hook wirft beliebige Ausnahmen
                logger.warning("Antrags-Anhang abgelehnt (Virenprüfung): Antrag %s", application.pk)
                reason = "Virenprüfung nicht bestanden oder nicht verfügbar"
        if reason is not None:
            skipped.append(f"{name} ({reason})")
            continue
        mime_type = file_service.mime_type_for_name(name)
        session_file = SessionFile(
            tenant=application.tenant,
            application=application,
            name=name[:500],
            mime_type=mime_type,
            is_public=False,
            text_content=_search_text(content, mime_type, name),
        )
        file_version_service.attach_upload(session_file, content, user=None)
    return skipped


def _search_text(content: Any, mime_type: str, name: str) -> str:
    """Text für die Session-Suche wie beim Upload der Verwaltung (best effort, Größengrenze)."""
    from . import file_service

    if int(getattr(content, "size", 0) or 0) > file_service.TEXT_EXTRACTION_MAX_SIZE_MB * 1024 * 1024:
        return ""
    content.seek(0)
    data = content.read()
    content.seek(0)
    return file_service.extract_text(data, mime_type, name)


#: Antworttext der APIs bei abweichender Organisation (fester Text, keine Ausnahme-Details nach außen)
SUBMITTING_ORGANIZATION_MISMATCH = (
    "Die einreichende Organisation passt nicht zu diesem Token. Anträge werden der Organisation "
    "zugeordnet, die den Token in mandari Work verbunden hat."
)


class SubmittingOrganizationMismatchError(PermissionError):
    """Die angegebene einreichende Organisation passt nicht zur Verbindung des Tokens."""


def submitting_organization_for_token(token: Any, claimed_id: object = None) -> Any:
    """
    Einreichende Organisation eines API-Tokens.

    Maßgeblich ist die Verbindung, die eine Organisation in Work mit diesem Token hergestellt
    hat (``AdministrationConnection``). Eine in der Anfrage genannte Organisation wird nur
    akzeptiert, wenn sie dazu passt – sonst könnte ein Tokeninhaber im Namen einer anderen
    Fraktion einreichen. Ohne Verbindung wird keine Organisation zugeordnet.
    """
    from apps.work.motions.models import AdministrationConnection

    connection = (
        AdministrationConnection.objects.filter(token_hash=token.token, tenant=token.tenant, is_active=True)
        .select_related("organization")
        .first()
    )
    bound = connection.organization if connection else None
    if claimed_id and (bound is None or str(bound.pk) != str(claimed_id)):
        raise SubmittingOrganizationMismatchError(SUBMITTING_ORGANIZATION_MISMATCH)
    return bound


class ApplicationService:
    """
    Service for managing application submissions.

    This is the main integration point between Work and Session.
    """

    @staticmethod
    def get_target_organizations(tenant: SessionTenant):
        """
        Get available target organizations for applications.

        Args:
            tenant: The SessionTenant

        Returns:
            QuerySet of SessionOrganizations that can receive applications.
        """
        return SessionOrganization.objects.filter(
            tenant=tenant,
            is_active=True,
            organization_type__in=["committee", "council", "advisory"],
        ).order_by("name")

    @staticmethod
    @transaction.atomic
    def submit_application(
        tenant: SessionTenant,
        title: str,
        justification: str,
        resolution_proposal: str,
        submitter_name: str,
        submitter_email: str,
        application_type: str = "motion",
        submitting_organization=None,
        target_organization_id: UUID | None = None,
        submitter_phone: str = "",
        co_signers: str = "",
        financial_impact: str = "",
        is_urgent: bool = False,
        urgency_reason: str = "",
        deadline=None,
        submitted_via_token=None,
    ) -> SessionApplication:
        """
        Submit a new application from Work module.

        This is the primary method for Work → Session integration.

        Args:
            tenant: Target SessionTenant
            title: Application title
            justification: Why this should be approved
            resolution_proposal: What should be decided
            submitter_name: Name of the person submitting
            submitter_email: Email of the submitter
            application_type: Type of application (motion, inquiry, etc.)
            submitting_organization: Work Organization (optional)
            target_organization_id: Target SessionOrganization UUID (optional)
            submitter_phone: Phone number (optional)
            co_signers: List of co-signers (optional)
            financial_impact: Financial implications (optional)
            is_urgent: Whether this is urgent (optional)
            urgency_reason: Reason for urgency (optional)
            deadline: Requested decision deadline (optional)
            submitted_via_token: SessionAPIToken der Einreichung (für den Abruf des Rückmeldestands)

        Returns:
            Created SessionApplication

        Raises:
            ValueError: If required fields are missing or invalid
        """
        # Validate required fields
        if not title or not title.strip():
            raise ValueError("Titel ist erforderlich")
        if not justification or not justification.strip():
            raise ValueError("Begründung ist erforderlich")
        if not resolution_proposal or not resolution_proposal.strip():
            raise ValueError("Beschlussvorschlag ist erforderlich")
        if not submitter_name or not submitter_name.strip():
            raise ValueError("Name des Einreichers ist erforderlich")
        if not submitter_email or not submitter_email.strip():
            raise ValueError("E-Mail des Einreichers ist erforderlich")

        # Antragsart gegen die Model-Choices validieren (Model ist führend)
        valid_types = {choice[0] for choice in SessionApplication._meta.get_field("application_type").choices}
        if application_type not in valid_types:
            raise ValueError(f"Ungültige Antragsart: {application_type}. Gültig: {', '.join(sorted(valid_types))}")

        # Get target organization if specified
        target_org = None
        if target_organization_id:
            try:
                target_org = SessionOrganization.objects.get(
                    id=target_organization_id,
                    tenant=tenant,
                    is_active=True,
                )
            except SessionOrganization.DoesNotExist:
                raise ValueError("Zielgremium nicht gefunden") from None

        # Create application
        return SessionApplication.objects.create(
            tenant=tenant,
            title=title.strip(),
            application_type=application_type,
            justification=justification.strip(),
            resolution_proposal=resolution_proposal.strip(),
            financial_impact=financial_impact.strip() if financial_impact else "",
            submitting_organization=submitting_organization,
            submitter_name=submitter_name.strip(),
            submitter_email=submitter_email.strip().lower(),
            submitter_phone=submitter_phone.strip() if submitter_phone else "",
            co_signers=co_signers.strip() if co_signers else "",
            target_organization=target_org,
            is_urgent=is_urgent,
            urgency_reason=urgency_reason.strip() if urgency_reason else "",
            deadline=deadline,
            submitted_via_token=submitted_via_token,
            status="submitted",
        )
