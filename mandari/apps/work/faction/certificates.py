# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teilnahmenachweis-PDF mit QR-Verifikation und Sammel-Export (Issue #68).

Grundlage sind ausschließlich die vom Vorstand FINAL BESTÄTIGTEN
Teilnahmen (Issue #67: ``confirmed_final_at``/``confirmed_final_by`` je
Teilnahme, Anwesenheitsstatus "present"). Jede Ausstellung wird als
:class:`FactionAttendanceCertificate` dokumentiert (Token, Prüfsumme,
Zeitraum, Aussteller) und auditiert (Issue #66).

Datenschutz (Akzeptanzkriterium):
- Der Prüfcode ist ein opakes Zufalls-Token ohne Personenbezug.
- Die öffentliche Verifikations-Seite zeigt nur: gültig/ungültig,
  Ausstellungsdatum, Organisation, Anzahl bestätigter Teilnahmen und
  Zeitraum. Der Name steht ausschließlich im PDF selbst.
"""

import csv
import hashlib
import io
import logging

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from apps.common.pdf import html_to_pdf

from .models import FactionAttendance, FactionAttendanceCertificate

logger = logging.getLogger(__name__)


# =============================================================================
# Bestätigte Teilnahmen
# =============================================================================


def confirmed_attendances(membership, period_start, period_end):
    """
    Vom Vorstand final bestätigte Anwesenheiten eines Mitglieds im Zeitraum.

    Nur Teilnahmen mit Status "present" UND gesetzter finaler Bestätigung
    (Issue #67) — unbestätigte oder bloß zugesagte Teilnahmen zählen nicht.
    """
    return (
        FactionAttendance.objects.filter(
            membership=membership,
            meeting__organization=membership.organization,
            status="present",
            confirmed_final_at__isnull=False,
            meeting__start__date__gte=period_start,
            meeting__start__date__lte=period_end,
        )
        .select_related("meeting", "confirmed_final_by__user")
        .order_by("meeting__start")
    )


def bulk_confirmed_attendances(organization, period_start, period_end):
    """Alle final bestätigten Anwesenheiten der Organisation im Zeitraum (je Person)."""
    return (
        FactionAttendance.objects.filter(
            meeting__organization=organization,
            membership__isnull=False,
            status="present",
            confirmed_final_at__isnull=False,
            meeting__start__date__gte=period_start,
            meeting__start__date__lte=period_end,
        )
        .select_related("membership__user", "meeting", "confirmed_final_by__user")
        .order_by("membership__user__last_name", "membership__user__email", "meeting__start")
    )


# =============================================================================
# Ausstellung (dokumentiert + auditiert)
# =============================================================================


def _certificate_checksum(organization, membership, period_start, period_end, attendances) -> str:
    """SHA-256-Prüfsumme über die bestätigten Teilnahmen (Manipulationsschutz)."""
    parts = [
        str(organization.pk),
        str(membership.pk),
        period_start.isoformat(),
        period_end.isoformat(),
    ]
    for attendance in attendances:
        parts.append(
            f"{attendance.meeting_id}|{attendance.meeting.start.isoformat()}|{attendance.confirmed_final_at.isoformat()}"
        )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def issue_certificate(membership, period_start, period_end, issued_by=None):
    """
    Teilnahmenachweis ausstellen: dokumentierte Ausstellung + Teilnahmenliste.

    Returns:
        Tuple (FactionAttendanceCertificate | None, list[FactionAttendance]) —
        ohne bestätigte Teilnahmen wird KEIN Nachweis ausgestellt (None).
    """
    attendances = list(confirmed_attendances(membership, period_start, period_end))
    if not attendances:
        return None, []

    organization = membership.organization
    certificate = FactionAttendanceCertificate.objects.create(
        organization=organization,
        membership=membership,
        issued_by=issued_by or membership,
        period_start=period_start,
        period_end=period_end,
        attendance_count=len(attendances),
        checksum=_certificate_checksum(organization, membership, period_start, period_end, attendances),
    )

    from .audit import log_event

    log_event(
        "certificate_issued",
        certificate,
        organization=organization,
        membership=issued_by or membership,
        is_internal=False,
        changes={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "attendance_count": len(attendances),
        },
    )
    return certificate, attendances


def certificate_verify_url(certificate) -> str:
    """Öffentliche Verifikations-URL (opakes Token, kein Personenbezug)."""
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{base}/nachweis/{certificate.token}/"


def qr_data_uri(url: str) -> str:
    """
    QR-Code als PNG-Data-URI (reines Python via segno).

    Fällt segno aus (nicht installiert o. Ä.), bleibt der Prüfcode als
    Text + URL im PDF — der Nachweis ist weiterhin verifizierbar.
    """
    try:
        import segno

        return segno.make(url, error="m").png_data_uri(scale=4)
    except Exception:
        logger.warning("QR-Code konnte nicht erzeugt werden — Prüfcode bleibt als Text/URL im PDF")
        return ""


# =============================================================================
# PDF / CSV
# =============================================================================


def build_certificate_pdf(certificate, attendances) -> bytes:
    """Teilnahmenachweis-PDF für ein Mitglied erzeugen."""
    verify_url = certificate_verify_url(certificate)
    context = {
        "certificate": certificate,
        "organization": certificate.organization,
        "holder_name": certificate.membership.user.get_display_name(),
        "attendances": attendances,
        "verify_url": verify_url,
        "qr_data_uri": qr_data_uri(verify_url),
        "generated_at": timezone.localtime(),
    }
    html = render_to_string("work/faction/pdf/attendance_certificate.html", context)
    return html_to_pdf(html)


def _group_by_member(attendances):
    """Teilnahmen je Mitglied gruppieren (für den Sammel-Export)."""
    groups: dict = {}
    for attendance in attendances:
        groups.setdefault(attendance.membership_id, {"membership": attendance.membership, "attendances": []})
        groups[attendance.membership_id]["attendances"].append(attendance)
    return list(groups.values())


def build_bulk_export_pdf(organization, period_start, period_end, attendances) -> bytes:
    """Sammel-Export als PDF: alle bestätigten Teilnahmen je Person im Zeitraum."""
    context = {
        "organization": organization,
        "period_start": period_start,
        "period_end": period_end,
        "groups": _group_by_member(attendances),
        "total_count": len(attendances),
        "generated_at": timezone.localtime(),
    }
    html = render_to_string("work/faction/pdf/attendance_export.html", context)
    return html_to_pdf(html)


def build_bulk_export_csv(organization, period_start, period_end, attendances) -> str:
    """Sammel-Export als CSV (Semikolon-getrennt, für die Verwaltung)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Name",
            "E-Mail",
            "Sitzung",
            "Datum",
            "Teilnahmeart",
            "Bestätigt durch",
            "Bestätigt am",
        ]
    )
    for attendance in attendances:
        confirmed_by = ""
        if attendance.confirmed_final_by_id and attendance.confirmed_final_by:
            confirmed_by = attendance.confirmed_final_by.user.get_display_name()
        writer.writerow(
            [
                attendance.membership.user.get_display_name(),
                attendance.membership.user.email,
                attendance.meeting.title,
                timezone.localtime(attendance.meeting.start).strftime("%d.%m.%Y"),
                attendance.get_participation_type_display(),
                confirmed_by,
                timezone.localtime(attendance.confirmed_final_at).strftime("%d.%m.%Y %H:%M"),
            ]
        )
    return buffer.getvalue()
