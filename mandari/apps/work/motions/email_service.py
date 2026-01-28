# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Email service for sending motions.

Provides functionality to send motions to:
- Administration (Verwaltung)
- Coalition partners
- Individual parties
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from django.core.mail import EmailMessage
from django.template.loader import render_to_string

if TYPE_CHECKING:
    from apps.tenants.models import CouncilParty, Organization
    from .models import Motion

logger = logging.getLogger(__name__)


@dataclass
class EmailResult:
    """Result of an email send operation."""
    success: bool
    recipient: str
    error: str = ""


class MotionEmailService:
    """
    Service for sending motions via email.

    Handles email composition, PDF attachment, and delivery.
    """

    def __init__(self, organization: "Organization"):
        """
        Initialize the email service.

        Args:
            organization: The organization sending the motion
        """
        self.organization = organization

    def send_to_administration(
        self,
        motion: "Motion",
        attach_pdf: bool = True,
        custom_message: str = ""
    ) -> EmailResult:
        """
        Send a motion to the administration.

        Args:
            motion: The motion to send
            attach_pdf: Whether to attach the motion as PDF
            custom_message: Optional custom message to include

        Returns:
            EmailResult indicating success or failure
        """
        if not self.organization.administration_email:
            return EmailResult(
                success=False,
                recipient="",
                error="Keine Verwaltungs-E-Mail konfiguriert"
            )

        return self._send_motion(
            motion=motion,
            to_email=self.organization.administration_email,
            subject_prefix="Neuer Antrag",
            attach_pdf=attach_pdf,
            custom_message=custom_message
        )

    def send_to_coalition(
        self,
        motion: "Motion",
        attach_pdf: bool = True,
        custom_message: str = ""
    ) -> list[EmailResult]:
        """
        Send a motion to all coalition partners.

        Args:
            motion: The motion to send
            attach_pdf: Whether to attach the motion as PDF
            custom_message: Optional custom message to include

        Returns:
            List of EmailResult for each coalition partner
        """
        from apps.tenants.models import CouncilParty

        parties = CouncilParty.objects.filter(
            organization=self.organization,
            is_coalition_member=True,
            is_active=True,
        ).exclude(email="")

        results = []
        for party in parties:
            result = self._send_motion(
                motion=motion,
                to_email=party.email,
                subject_prefix=f"Koalitionsabstimmung: {self.organization.name}",
                attach_pdf=attach_pdf,
                custom_message=custom_message,
                party_name=party.name
            )
            results.append(result)

        return results

    def send_to_party(
        self,
        motion: "Motion",
        party: "CouncilParty",
        attach_pdf: bool = True,
        custom_message: str = ""
    ) -> EmailResult:
        """
        Send a motion to a specific party.

        Args:
            motion: The motion to send
            party: The party to send to
            attach_pdf: Whether to attach the motion as PDF
            custom_message: Optional custom message to include

        Returns:
            EmailResult indicating success or failure
        """
        if not party.email:
            return EmailResult(
                success=False,
                recipient=party.name,
                error=f"Keine E-Mail für {party.name} konfiguriert"
            )

        return self._send_motion(
            motion=motion,
            to_email=party.email,
            subject_prefix=f"Antrag von {self.organization.name}",
            attach_pdf=attach_pdf,
            custom_message=custom_message,
            party_name=party.name
        )

    def _send_motion(
        self,
        motion: "Motion",
        to_email: str,
        subject_prefix: str,
        attach_pdf: bool,
        custom_message: str,
        party_name: Optional[str] = None
    ) -> EmailResult:
        """
        Internal method to send a motion email.

        Args:
            motion: The motion to send
            to_email: Recipient email address
            subject_prefix: Prefix for email subject
            attach_pdf: Whether to attach PDF
            custom_message: Custom message to include
            party_name: Optional party name for personalization

        Returns:
            EmailResult indicating success or failure
        """
        try:
            subject = f"{subject_prefix}: {motion.title}"

            # Render HTML email body
            context = {
                "motion": motion,
                "organization": self.organization,
                "custom_message": custom_message,
                "party_name": party_name,
            }

            # Try to render template, fall back to simple text
            try:
                html_body = render_to_string(
                    "work/motions/email/motion_email.html",
                    context
                )
            except Exception:
                # Fallback to simple text email
                html_body = self._generate_simple_email(motion, custom_message)

            # Create email
            email = EmailMessage(
                subject=subject,
                body=html_body,
                to=[to_email],
            )
            email.content_subtype = "html"

            # Attach PDF if requested
            if attach_pdf:
                try:
                    from .export_service import motion_export_service
                    pdf_content = motion_export_service.export_to_pdf(motion)

                    # Create safe filename
                    safe_title = "".join(
                        c for c in motion.title[:50]
                        if c.isalnum() or c in (' ', '-', '_')
                    ).strip()
                    filename = f"{safe_title}.pdf"

                    email.attach(filename, pdf_content, "application/pdf")
                except Exception as e:
                    logger.warning(f"Could not attach PDF: {e}")

            # Send email
            sent = email.send(fail_silently=False)

            if sent:
                logger.info(f"Motion email sent to {to_email}")
                return EmailResult(success=True, recipient=to_email)
            else:
                return EmailResult(
                    success=False,
                    recipient=to_email,
                    error="E-Mail konnte nicht gesendet werden"
                )

        except Exception as e:
            logger.error(f"Failed to send motion email to {to_email}: {e}")
            return EmailResult(
                success=False,
                recipient=to_email,
                error=str(e)
            )

    def _generate_simple_email(self, motion: "Motion", custom_message: str) -> str:
        """
        Generate a simple HTML email without template.

        Args:
            motion: The motion to include
            custom_message: Custom message to include

        Returns:
            Simple HTML email body
        """
        return f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6;">
            <h2>{motion.title}</h2>

            <p><strong>Von:</strong> {self.organization.name}</p>
            <p><strong>Typ:</strong> {motion.get_type_display()}</p>
            <p><strong>Status:</strong> {motion.get_status_display()}</p>

            {f'<p style="margin: 20px 0; padding: 15px; background: #f5f5f5; border-radius: 4px;">{custom_message}</p>' if custom_message else ''}

            <hr style="margin: 20px 0;">

            <div style="white-space: pre-wrap;">
                {motion.content or 'Kein Inhalt'}
            </div>

            <hr style="margin: 20px 0;">

            <p style="color: #666; font-size: 12px;">
                Diese E-Mail wurde automatisch von Mandari versendet.<br>
                {self.organization.name}
            </p>
        </body>
        </html>
        """

    def get_coalition_parties(self) -> list:
        """
        Get all coalition parties for this organization.

        Returns:
            List of CouncilParty objects
        """
        from apps.tenants.models import CouncilParty

        return list(CouncilParty.objects.filter(
            organization=self.organization,
            is_coalition_member=True,
            is_active=True,
        ).order_by("coalition_order"))

    def get_all_parties(self) -> list:
        """
        Get all parties for this organization.

        Returns:
            List of CouncilParty objects
        """
        from apps.tenants.models import CouncilParty

        return list(CouncilParty.objects.filter(
            organization=self.organization,
            is_active=True,
        ).order_by("coalition_order", "name"))


def get_email_service(organization: "Organization") -> MotionEmailService:
    """
    Factory function to get an email service for an organization.

    Args:
        organization: The organization

    Returns:
        MotionEmailService instance
    """
    return MotionEmailService(organization)
