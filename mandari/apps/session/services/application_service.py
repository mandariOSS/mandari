# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Application service for handling application submissions.

This service provides the integration between Work module and Session RIS,
enabling political organizations to submit applications (Anträge) directly.
"""

from uuid import UUID

from django.db import transaction

from apps.session.models import (
    SessionApplication,
    SessionOrganization,
    SessionTenant,
)


class ApplicationService:
    """
    Service for managing application submissions.

    This is the main integration point between Work and Session.
    """

    @staticmethod
    def get_available_tenants():
        """
        Get all Session tenants that accept applications.

        Returns:
            QuerySet of active SessionTenants with OParl body connections.
        """
        return SessionTenant.objects.filter(
            is_active=True,
            oparl_body__isnull=False,
        ).select_related("oparl_body")

    @staticmethod
    def get_tenant_for_body(oparl_body_id: UUID) -> SessionTenant | None:
        """
        Get the Session tenant for an OParl Body.

        Args:
            oparl_body_id: UUID of the OParl Body

        Returns:
            SessionTenant if found, None otherwise
        """
        try:
            return SessionTenant.objects.get(
                oparl_body_id=oparl_body_id,
                is_active=True,
            )
        except SessionTenant.DoesNotExist:
            return None

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
                raise ValueError("Zielgremium nicht gefunden")

        # Create application
        application = SessionApplication.objects.create(
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
            status="submitted",
        )

        return application

    @staticmethod
    def get_application_status(application_id: UUID, tenant: SessionTenant) -> dict:
        """
        Get the current status of an application.

        Args:
            application_id: UUID of the application
            tenant: SessionTenant for security check

        Returns:
            Dict with application status info

        Raises:
            ValueError: If application not found
        """
        try:
            app = SessionApplication.objects.select_related(
                "target_organization",
                "received_by__user",
            ).get(
                id=application_id,
                tenant=tenant,
            )
        except SessionApplication.DoesNotExist:
            raise ValueError("Antrag nicht gefunden")

        return {
            "id": str(app.id),
            "reference": app.reference,
            "title": app.title,
            "status": app.status,
            "status_display": app.get_status_display(),
            "submitted_at": app.submitted_at,
            "received_at": app.received_at,
            "target_organization": (
                {
                    "id": str(app.target_organization_id),
                    "name": app.target_organization.name,
                }
                if app.target_organization
                else None
            ),
            "processing_notes": app.processing_notes if app.processing_notes else None,
            "is_urgent": app.is_urgent,
        }

    @staticmethod
    def get_applications_for_organization(
        work_organization,
        tenant: SessionTenant,
        status: str | None = None,
    ):
        """
        Get all applications submitted by a Work organization.

        Args:
            work_organization: The Work Organization
            tenant: SessionTenant
            status: Filter by status (optional)

        Returns:
            QuerySet of SessionApplications
        """
        qs = SessionApplication.objects.filter(
            tenant=tenant,
            submitting_organization=work_organization,
        ).order_by("-submitted_at")

        if status:
            qs = qs.filter(status=status)

        return qs
