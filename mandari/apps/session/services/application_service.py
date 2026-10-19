# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Application service for handling application submissions.

This service provides the integration between Work module and Session RIS,
enabling political organizations to submit applications (Anträge) directly.
"""

import logging
import re
from pathlib import PurePosixPath
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.session.models import (
    SessionApplication,
    SessionFile,
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
    from apps.session import hub_events

    # Drehscheibe (Issue #534): ris.paper.created mit der Einreichung, Anhänge nichtöffentlich an der Vorlage
    with transaction.atomic(), hub_events.track(tenant) as tracked:
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
        tracked.paper(paper, created=True)
        application.status = "converted"
        application.save(update_fields=["status", "updated_at"])
        # Anhänge des Antrags hängen jetzt auch an der Vorlage – nichtöffentlich, bis die
        # Verwaltung sie freigibt (#584). Die automatisch erzeugte Antragsfassung ist der Antrag selbst und
        # wird mit der Vorlage öffentlich; im Bürgerportal erscheint sie erst mit deren Freigabe.
        files = application.files.filter(paper__isnull=True)
        files.filter(is_application_pdf=True).update(paper=paper, is_public=True)
        files.exclude(is_application_pdf=True).update(paper=paper, is_public=False)
    return paper, True


# =============================================================================
# Antragsfassung als PDF (Teil von #584)
# =============================================================================


def application_pdf_name(application: SessionApplication) -> str:
    """Dateiname der Antragsfassung, z. B. „Antrag A-2026-0003.pdf“ (ohne Schrägstriche)."""
    reference = re.sub(r"[\\/]+", "-", (application.reference or "").strip()).strip("-")
    return f"Antrag {reference}.pdf" if reference else "Antrag.pdf"


def _application_pdf_text(application: SessionApplication) -> str:
    """Suchtext der Antragsfassung – derselbe Inhalt wie im PDF, ohne Umweg über die Texterkennung."""
    parts = [
        f"{application.get_application_type_display()}: {application.title}",
        application.reference,
        application.resolution_proposal,
        application.justification,
        application.financial_impact,
        application.urgency_reason if application.is_urgent else "",
        application.co_signers,
    ]
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def build_application_pdf(application: SessionApplication) -> bytes:
    """
    Antragsfassung als schlichtes A4-PDF: genau die eingereichten Angaben (Teil von #584).

    Die Texte sind Klartext aus dem Einreichungsformular und werden im Template escaped – es wird kein HTML
    übernommen. Keine E-Mail-Adresse und keine Telefonnummer: Die Datei wird mit der Vorlage öffentlich.
    """
    from django.template.loader import render_to_string

    from apps.common.pdf import html_to_pdf

    organization = application.submitting_organization
    # Ohne hinterlegten Namen steht beim Einreichen die E-Mail-Adresse im Namensfeld – die bleibt draußen
    submitter = (application.submitter_name or "").strip()
    if "@" in submitter:
        submitter = ""
    context = {
        "tenant": application.tenant,
        "application": application,
        "organization_name": organization.name if organization is not None else "",
        "submitter": submitter,
        "generated_at": timezone.localtime(),
    }
    return html_to_pdf(render_to_string("session/pdf/application.html", context))


def attach_application_pdf(application: SessionApplication) -> SessionFile:
    """
    Antragsfassung erzeugen und als Anlage an den eingegangenen Antrag hängen (nichtöffentlich wie jeder Anhang,
    bis die Umwandlung in eine Vorlage sie öffentlich macht).

    Kein Virenscan: Das PDF entsteht auf dem Server aus escaptem Klartext, es enthält keine hochgeladenen Inhalte.
    Ein eingerichteter, aber gerade nicht erreichbarer Scanner würde sonst die eigene Antragsfassung verwerfen.

    Raises:
        Exception: Erzeugung oder Ablage gescheitert – der Aufrufer entscheidet, ob ohne PDF eingereicht wird.
    """
    from django.core.files.base import ContentFile

    from . import file_version_service

    data = build_application_pdf(application)
    if not data.startswith(b"%PDF"):
        raise ValueError("Antragsfassung: Ergebnis ist kein PDF")
    name = application_pdf_name(application)
    session_file = SessionFile(
        tenant=application.tenant,
        application=application,
        name=name,
        mime_type="application/pdf",
        is_public=False,
        is_application_pdf=True,
        text_content=_application_pdf_text(application),
    )
    file_version_service.attach_upload(session_file, ContentFile(data, name=name), user=None)
    return session_file


#: Gründe, aus denen eine Anlage nicht angenommen wird; die Codes stehen in der Quittung von ``submission.submit``.
ATTACHMENT_TYPE = "dateityp"
ATTACHMENT_SIZE = "groesse"
ATTACHMENT_SCAN = "virenpruefung"


def attachment_reason_text(code: str) -> str:
    """Text für Nutzer:innen zu einem Grund (``ATTACHMENT_*``)."""
    from . import file_service

    return {
        ATTACHMENT_TYPE: "Dateityp wird von der Verwaltung nicht angenommen",
        ATTACHMENT_SIZE: f"größer als {file_service.MAX_FILE_SIZE_MB} MB",
        ATTACHMENT_SCAN: "Virenprüfung nicht bestanden oder nicht verfügbar",
    }.get(code, "nicht angenommen")


def attachment_rejection(name: str, size: int) -> str | None:
    """Code, aus dem eine Datei nicht als Antrags-Anhang angenommen wird (Dateityp, Größe) – oder ``None``."""
    from . import file_service

    ext = PurePosixPath(name or "").suffix.lower()
    if ext not in file_service.ALLOWED_EXTENSIONS:
        return ATTACHMENT_TYPE
    if size > file_service.MAX_FILE_SIZE_MB * 1024 * 1024:
        return ATTACHMENT_SIZE
    return None


def attachment_accepted(name: str, size: int) -> str | None:
    """Warum eine Datei nicht als Antrags-Anhang angenommen wird – oder ``None`` (#584)."""
    code = attachment_rejection(name, size)
    return attachment_reason_text(code) if code else None


def attach_application_files(application: SessionApplication, files: list[tuple[str, Any]]) -> list[tuple[int, str]]:
    """
    Anhänge eines eingereichten Antrags speichern (Befehl ``submission.submit``, #584, #540).

    ``files``: Paare aus Anzeigename und Django-``File`` (Dateiname ohne Pfad). Geprüft wird wie bei Anlagen der
    Verwaltung (Dateityp, Größe, Virenscan-Hook); gespeichert über die Fassungsablage
    (Deduplizierung je Mandant). Anhänge bleiben nichtöffentlich.

    Returns:
        Stelle in ``files`` und Code (``ATTACHMENT_*``) je nicht angenommener Datei.
    """
    from apps.session.models import SessionFile

    from . import file_service, file_version_service

    skipped: list[tuple[int, str]] = []
    for index, (name, content) in enumerate(files):
        reason = attachment_rejection(name, int(getattr(content, "size", 0) or 0))
        if reason is None:
            try:
                file_service.scan_upload(content)
            except Exception:  # Befund oder Prüfung nicht verfügbar – der Hook wirft beliebige Ausnahmen
                logger.warning("Antrags-Anhang abgelehnt (Virenprüfung): Antrag %s", application.pk)
                reason = ATTACHMENT_SCAN
        if reason is not None:
            skipped.append((index, reason))
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


def connected_token(tenant: SessionTenant, organization_id: object) -> Any:
    """
    Einreichungs-Token, mit dem eine Organisation in Work mit diesem Mandanten verbunden ist – oder ``None``.

    Grundlage ist dieselbe Verbindung (``AdministrationConnection``) wie bei ``submitting_organization_for_token``.
    """
    from apps.session.models import SessionAPIToken
    from apps.work.motions.models import AdministrationConnection

    connection = AdministrationConnection.objects.filter(
        organization_id=organization_id, tenant=tenant, is_active=True
    ).first()
    if connection is None:
        return None
    return SessionAPIToken.objects.filter(token=connection.token_hash, tenant=tenant).first()


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


def submit_with_token(
    tenant: SessionTenant,
    token: Any,
    fields: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> Any:
    """
    Antrag mit API-Token einreichen (Session-API v1 und abgekündigter Pfad) über den Befehl ``submission.submit``.

    ``fields`` sind die Felder der API (``title``, ``submitter_name``, ``co_signers`` als Text bis 5000 Zeichen,
    ``deadline`` als Datum, ``target_organization_id`` …). Geprüft wird im Handler (``apps.session.commands``). Die
    Mitunterzeichnenden gehen unverändert als ``co_signers_text`` mit. Ein Zielgremium, das es beim Mandanten nicht
    gibt oder das inaktiv ist, bleibt unbeachtet. Ohne ``idempotency_key`` gilt jede Anfrage als neue Einreichung.

    Returns:
        Quittung (``hub.commands.Receipt``); ``CommandError`` mit Problem nach RFC 9457, wenn der Befehl scheitert.
    """
    import uuid

    from hub.commands import Command, dispatch

    from ..commands import SUBMIT, tenant_ref, token_actor_ref

    submitter: dict[str, Any] = {"name": fields["submitter_name"], "email": fields["submitter_email"]}
    if fields.get("submitter_phone"):
        submitter["phone"] = fields["submitter_phone"]
    body: dict[str, Any] = {
        "application_type": fields.get("application_type") or "motion",
        "title": fields["title"],
        "resolution_proposal": fields["resolution_proposal"],
        "justification": fields["justification"],
        "submitter": submitter,
        "is_urgent": bool(fields.get("is_urgent")),
    }
    for name in ("financial_impact", "urgency_reason"):
        if fields.get(name):
            body[name] = fields[name]
    if (fields.get("co_signers") or "").strip():
        body["co_signers_text"] = fields["co_signers"]
    if fields.get("deadline"):
        body["deadline"] = fields["deadline"].isoformat()
    if fields.get("submitting_organization_id"):
        body["submitting_organization"] = str(fields["submitting_organization_id"])
    target_id = fields.get("target_organization_id")
    if target_id and SessionOrganization.objects.filter(pk=target_id, tenant=tenant, is_active=True).exists():
        body["target_organization"] = str(target_id)
    return dispatch(
        Command(
            name=SUBMIT,
            body=body,
            idempotency_key=idempotency_key or f"api.{uuid.uuid4()}",
            tenant_ref=tenant_ref(tenant),
            actor_ref=token_actor_ref(token),
        )
    )


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
