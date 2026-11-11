# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsgeld-Abrechnung (Issue #38).

Abrechnungslauf, Genehmigung (Vier-Augen-Prinzip), Exporte und
Jahresübersicht:

- **Sätze**: je Gremium/Funktion (SessionAllowanceRate), Fallback ist das
  Standard-Sitzungsgeld des Gremiums (SessionOrganization.allowance_amount).
- **Abrechnungslauf**: erzeugt je anrechenbarer Anwesenheit (Status
  present/joined_late/left_early, keine Gäste) im Zeitraum genau eine
  Sitzungsgeld-Position (OneToOne auf die Anwesenheit — idempotent).
- **Vier-Augen-Prinzip**: Wer die Positionen erzeugt hat, darf sie nicht
  selbst genehmigen (je Mandant schaltbar, Standard: an – Issue #222).
- **Exporte**: generisches CSV fürs Finanzverfahren und SEPA-pain.001-XML
  (Überweisungs-Datei); Abrechnungsmitteilung als PDF je Empfänger.
- **Bankdaten**: IBAN/BIC/Kontoinhaber werden ausschließlich über die
  verschlüsselten Person-Accessoren gelesen und nur in Export-Pfaden mit
  der Berechtigung ``manage_allowances`` verwendet.
"""

import io
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.sax.saxutils import escape  # noqa: F401  (Doku: Escaping via ElementTree)

from django.db import transaction
from django.utils import timezone

from apps.common import csv_safety

logger = logging.getLogger(__name__)

# Anwesenheits-Status, die Sitzungsgeld auslösen (Issue #38):
# verspätetes Kommen / vorzeitiges Gehen gilt als Teilnahme
ELIGIBLE_STATUSES = ("present", "joined_late", "left_early")

# Funktionen ohne Sitzungsgeld
EXCLUDED_ROLES = ("guest",)


# =============================================================================
# Sätze
# =============================================================================


def rate_for(organization, role, rates_map=None) -> Decimal:
    """
    Entschädigungssatz für eine Funktion im Gremium.

    Vorrang: Satz je Funktion (SessionAllowanceRate), sonst das
    Standard-Sitzungsgeld des Gremiums.
    """
    if rates_map is None:
        rates_map = {(r.organization_id, r.role): r.amount for r in organization.allowance_rates.all()}
    specific = rates_map.get((organization.pk, role))
    if specific is not None:
        return specific
    return organization.allowance_amount or Decimal("0.00")


# =============================================================================
# Abrechnungslauf
# =============================================================================


#: Bezeichnung einer nichtöffentlichen Sitzung in der Abrechnung für Konten ohne das Sichtrecht dafür
NON_PUBLIC_MEETING_LABEL = "Nichtöffentliche Sitzung"


def meeting_label_expression(permissions: Any, prefix: str = "attendance__meeting__") -> Any:
    """
    Ausdruck für den Sitzungsnamen einer Position (Annotation ``meeting_label``).

    Ausnahme von der Sichtbarkeitsregel für Sitzungen (``visibility.meeting_q``): Die Abrechnung zeigt auch
    Positionen nichtöffentlicher Sitzungen, sonst wäre sie unvollständig. Ohne ``view_non_public_meetings`` steht
    statt des Namens eine neutrale Bezeichnung; Gremium, Datum, Funktion und Betrag bleiben.
    """
    from django.db.models import Case, CharField, F, Value, When

    from apps.session.visibility import NON_PUBLIC_MEETINGS

    name = F(f"{prefix}name")
    if NON_PUBLIC_MEETINGS in permissions:
        return name
    return Case(
        When(**{f"{prefix}is_public": False}, then=Value(NON_PUBLIC_MEETING_LABEL)),
        default=name,
        output_field=CharField(),
    )


def meeting_label(allowance: Any) -> str:
    """Sitzungsname einer Position: die Annotation der Abrechnungsansichten, sonst der Name der Sitzung."""
    label = getattr(allowance, "meeting_label", None)
    return str(label if label is not None else allowance.attendance.meeting.name)


#: Meldung, wenn eine Anwesenheitszeile wegen ihres Sitzungsgelds bestehen bleibt
ATTENDANCE_HAS_ALLOWANCE = (
    "Für diese Anwesenheit ist Sitzungsgeld genehmigt, exportiert oder ausgezahlt. Die Zeile bleibt bestehen; "
    "die Position lässt sich nur in der Abrechnung stornieren."
)


def _removal_block(allowance: Any) -> str:
    """
    Grund, aus dem eine Anwesenheitszeile mit dieser Position nicht entfernt werden darf (leer: sie darf).

    Das Sitzungsgeld hängt an der Anwesenheit und verschwände mit ihr. Genehmigte, exportierte oder ausgezahlte
    Positionen sind geprüft bzw. überwiesen: Sie bleiben, auch wenn die Niederschrift noch offen ist, und lassen
    sich nur in der Abrechnung (``manage_allowances``) stornieren. Ausstehende und stornierte Positionen entfallen
    mit der Zeile, wie sie auch der nächste Abrechnungslauf mangels Grundlage stornieren würde.
    """
    if allowance is None:
        return ""
    if allowance.status in ("approved", "paid") or allowance.export_reference:
        return ATTENDANCE_HAS_ALLOWANCE
    return ""


def remove_attendance(attendance: Any) -> str:
    """
    Anwesenheitszeile entfernen, wenn kein geprüftes Sitzungsgeld daran hängt; sonst der Grund (leer: entfernt).

    Prüfung und Löschen laufen in einer Transaktion unter Zeilensperren: Die Sperre der Anwesenheit hält auch eine
    Position auf, die gerade erst entsteht; die Sperre der Position wartet eine laufende Genehmigung oder einen
    laufenden Export ab und liest danach deren Ergebnis. So verschwindet keine Position, die zwischen Prüfung und
    Löschen genehmigt oder exportiert wurde.
    """
    from apps.session.models import SessionAllowance, SessionAttendance

    with transaction.atomic():
        locked = SessionAttendance.objects.select_for_update().filter(pk=attendance.pk).first()
        if locked is None:
            return ""
        reason = _removal_block(SessionAllowance.objects.select_for_update().filter(attendance=locked).first())
        if reason:
            return reason
        locked.delete()
    return ""


def generate_allowances(tenant, period_start, period_end, *, organization=None, created_by=None) -> dict:
    """
    Abrechnungslauf: Sitzungsgeld-Positionen aus Anwesenheiten erzeugen.

    Idempotent — Anwesenheiten mit vorhandener Position werden übersprungen
    (OneToOne). Abgesagte Sitzungen zählen nicht.

    Returns:
        dict: created, skipped_existing, skipped_zero, total (Decimal)
    """
    from apps.session.models import SessionAllowance, SessionAllowanceRate, SessionAttendance

    attendances = (
        SessionAttendance.objects.filter(
            meeting__tenant=tenant,
            meeting__cancelled=False,
            meeting__start__date__gte=period_start,
            meeting__start__date__lte=period_end,
            status__in=ELIGIBLE_STATUSES,
        )
        .exclude(role__in=EXCLUDED_ROLES)
        .select_related("meeting__organization", "person")
    )
    if organization is not None:
        attendances = attendances.filter(meeting__organization=organization)

    rates_map = {
        (r.organization_id, r.role): r.amount for r in SessionAllowanceRate.objects.filter(organization__tenant=tenant)
    }

    stats = {"created": 0, "skipped_existing": 0, "skipped_zero": 0, "total": Decimal("0.00")}
    existing_ids = set(
        SessionAllowance.objects.filter(attendance__meeting__tenant=tenant).values_list("attendance_id", flat=True)
    )

    for attendance in attendances:
        if attendance.pk in existing_ids:
            stats["skipped_existing"] += 1
            continue
        org = attendance.meeting.organization
        amount = rate_for(org, attendance.role, rates_map)
        if amount <= 0:
            stats["skipped_zero"] += 1
            continue
        SessionAllowance.objects.create(
            attendance=attendance,
            amount=amount,
            currency=org.allowance_currency or "EUR",
            status="pending",
            created_by=created_by,
        )
        stats["created"] += 1
        stats["total"] += amount

    return stats


# =============================================================================
# Genehmigung (Vier-Augen-Prinzip)
# =============================================================================


def approve_allowances(allowances, approver, *, four_eyes: bool = True) -> dict:
    """
    Positionen genehmigen — Vier-Augen-Prinzip (Issue #38).

    Positionen, die der/die Genehmigende selbst erzeugt hat, werden
    NICHT genehmigt (blocked_four_eyes). ``four_eyes=False`` nur, wenn der
    Mandant das Vier-Augen-Prinzip für Sitzungsgeld abgeschaltet hat (Issue #222).

    Returns:
        dict: approved, blocked_four_eyes
    """
    stats = {"approved": 0, "blocked_four_eyes": 0}
    now = timezone.now()
    for allowance in allowances:
        if allowance.status != "pending":
            continue
        if four_eyes and allowance.created_by_id is not None and allowance.created_by_id == approver.pk:
            stats["blocked_four_eyes"] += 1
            continue
        allowance.status = "approved"
        allowance.approved_by = approver
        allowance.approved_at = now
        allowance.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        stats["approved"] += 1
    return stats


# =============================================================================
# Exporte
# =============================================================================


def next_export_reference(tenant) -> str:
    """Nächste fortlaufende Export-Referenz, z. B. SG-2026-0003."""
    from apps.session.models import SessionAllowance

    year = timezone.localdate().year
    prefix = f"SG-{year}-"
    max_num = 0
    refs = SessionAllowance.objects.filter(
        attendance__meeting__tenant=tenant, export_reference__startswith=prefix
    ).values_list("export_reference", flat=True)
    for ref in refs:
        try:
            max_num = max(max_num, int(ref.rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            continue
    return f"{prefix}{max_num + 1:04d}"


def build_export_csv(allowances) -> str:
    """
    Generisches CSV fürs Finanzverfahren (Semikolon, CRLF, UTF-8).

    Enthält Bankdaten (über die verschlüsselten Accessoren) — der Aufruf
    ist auf die Berechtigung manage_allowances beschränkt.
    """
    buffer = io.StringIO()
    writer = csv_safety.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Name",
            "Gremium",
            "Sitzung",
            "Datum",
            "Funktion",
            "Betrag",
            "Waehrung",
            "Status",
            "Kontoinhaber",
            "IBAN",
            "BIC",
            "Export-Referenz",
        ]
    )
    for allowance in allowances:
        attendance = allowance.attendance
        person = attendance.person
        writer.writerow(
            [
                person.display_name,
                attendance.meeting.organization.name,
                meeting_label(allowance),
                timezone.localtime(attendance.meeting.start).strftime("%d.%m.%Y"),
                attendance.get_role_display(),
                f"{allowance.amount:.2f}".replace(".", ","),
                allowance.currency,
                allowance.get_status_display(),
                person.get_bank_account_holder_decrypted() or "",
                person.get_bank_iban_decrypted() or "",
                person.get_bank_bic_decrypted() or "",
                allowance.export_reference,
            ]
        )
    return buffer.getvalue()


def _sepa_text(value: str, max_length: int = 70) -> str:
    """SEPA-erlaubter Zeichensatz (EPC-Empfehlung), gekürzt."""
    value = value or ""
    replacements = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    value = re.sub(r"[^A-Za-z0-9/\-?:().,'+ ]", " ", value)
    return value.strip()[:max_length] or "-"


def build_sepa_xml(tenant, allowances, *, debtor_name, debtor_iban, debtor_bic="", reference="", execution_date=None):
    """
    SEPA-pain.001.001.03-Überweisungsdatei (Issue #38).

    Je Person eine Sammel-Transaktion (Summe ihrer Positionen). Personen
    ohne hinterlegte IBAN werden übersprungen und namentlich zurückgemeldet.

    Returns:
        Tuple (xml_bytes, transaction_count, total (Decimal), skipped_names)
    """
    execution_date = execution_date or timezone.localdate()

    # Je Person summieren (Bankdaten über die verschlüsselten Accessoren)
    per_person: dict = {}
    for allowance in allowances:
        # Sitzungsgelder hängen an der Anwesenheit, Monats-Pauschalen
        # direkt an der Person — beide Typen werden unterstützt.
        person = getattr(allowance, "person", None) or allowance.attendance.person
        entry = per_person.setdefault(person.pk, {"person": person, "amount": Decimal("0.00"), "count": 0})
        entry["amount"] += allowance.amount
        entry["count"] += 1

    transactions = []
    skipped = []
    for entry in per_person.values():
        person = entry["person"]
        iban = re.sub(r"\s+", "", person.get_bank_iban_decrypted() or "")
        if not iban:
            skipped.append(person.display_name)
            continue
        transactions.append(
            {
                "name": person.get_bank_account_holder_decrypted() or person.display_name,
                "iban": iban.upper(),
                "bic": re.sub(r"\s+", "", person.get_bank_bic_decrypted() or "").upper(),
                "amount": entry["amount"],
                "count": entry["count"],
            }
        )

    total = sum((t["amount"] for t in transactions), Decimal("0.00"))
    now = timezone.localtime()
    msg_id = _sepa_text(reference or f"SG-{now:%Y%m%d%H%M%S}", 35)

    ns = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
    root = Element("Document", {"xmlns": ns})
    cstmr = SubElement(root, "CstmrCdtTrfInitn")

    # Group Header
    grp = SubElement(cstmr, "GrpHdr")
    SubElement(grp, "MsgId").text = msg_id
    SubElement(grp, "CreDtTm").text = now.strftime("%Y-%m-%dT%H:%M:%S")
    SubElement(grp, "NbOfTxs").text = str(len(transactions))
    SubElement(grp, "CtrlSum").text = f"{total:.2f}"
    initg = SubElement(grp, "InitgPty")
    SubElement(initg, "Nm").text = _sepa_text(debtor_name or tenant.name)

    # Payment Info
    pmt = SubElement(cstmr, "PmtInf")
    SubElement(pmt, "PmtInfId").text = msg_id
    SubElement(pmt, "PmtMtd").text = "TRF"
    SubElement(pmt, "NbOfTxs").text = str(len(transactions))
    SubElement(pmt, "CtrlSum").text = f"{total:.2f}"
    pmt_tp = SubElement(pmt, "PmtTpInf")
    svc_lvl = SubElement(pmt_tp, "SvcLvl")
    SubElement(svc_lvl, "Cd").text = "SEPA"
    SubElement(pmt, "ReqdExctnDt").text = execution_date.isoformat()
    dbtr = SubElement(pmt, "Dbtr")
    SubElement(dbtr, "Nm").text = _sepa_text(debtor_name or tenant.name)
    dbtr_acct = SubElement(pmt, "DbtrAcct")
    dbtr_acct_id = SubElement(dbtr_acct, "Id")
    SubElement(dbtr_acct_id, "IBAN").text = re.sub(r"\s+", "", debtor_iban or "").upper()
    dbtr_agt = SubElement(pmt, "DbtrAgt")
    fin_inst = SubElement(dbtr_agt, "FinInstnId")
    if debtor_bic:
        SubElement(fin_inst, "BIC").text = re.sub(r"\s+", "", debtor_bic).upper()
    else:
        othr = SubElement(fin_inst, "Othr")
        SubElement(othr, "Id").text = "NOTPROVIDED"
    SubElement(pmt, "ChrgBr").text = "SLEV"

    for index, txn in enumerate(transactions, start=1):
        cdt = SubElement(pmt, "CdtTrfTxInf")
        pmt_id = SubElement(cdt, "PmtId")
        SubElement(pmt_id, "EndToEndId").text = _sepa_text(f"{msg_id}-{index:04d}", 35)
        amt = SubElement(cdt, "Amt")
        instd = SubElement(amt, "InstdAmt", {"Ccy": "EUR"})
        instd.text = f"{txn['amount']:.2f}"
        if txn["bic"]:
            cdtr_agt = SubElement(cdt, "CdtrAgt")
            cdtr_fin = SubElement(cdtr_agt, "FinInstnId")
            SubElement(cdtr_fin, "BIC").text = txn["bic"]
        cdtr = SubElement(cdt, "Cdtr")
        SubElement(cdtr, "Nm").text = _sepa_text(txn["name"])
        cdtr_acct = SubElement(cdt, "CdtrAcct")
        cdtr_acct_id = SubElement(cdtr_acct, "Id")
        SubElement(cdtr_acct_id, "IBAN").text = txn["iban"]
        rmt = SubElement(cdt, "RmtInf")
        SubElement(rmt, "Ustrd").text = _sepa_text(f"Sitzungsgeld {txn['count']} Sitzung(en) {reference}".strip(), 140)

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")
    return xml_bytes, len(transactions), total, skipped


def mark_exported(allowances, reference, *, mark_paid=True) -> int:
    """Positionen als exportiert (und ausgezahlt) markieren."""
    now = timezone.now()
    count = 0
    for allowance in allowances:
        allowance.export_reference = reference
        allowance.export_date = now
        update_fields = ["export_reference", "export_date", "updated_at"]
        if mark_paid and allowance.status == "approved":
            allowance.status = "paid"
            allowance.paid_at = now
            update_fields += ["status", "paid_at"]
        allowance.save(update_fields=update_fields)
        count += 1
    return count


# =============================================================================
# Jahresübersicht
# =============================================================================


def year_summary(tenant, year) -> list[dict]:
    """
    Jahresübersicht je Person (Grundlage Steuerbescheinigung, Issue #38).

    Returns:
        Liste von dicts: person, count, total, paid, approved, pending
    """
    from apps.session.models import SessionAllowance

    allowances = (
        SessionAllowance.objects.filter(
            attendance__meeting__tenant=tenant,
            attendance__meeting__start__year=year,
        )
        .exclude(status="cancelled")
        .select_related("attendance__person", "attendance__meeting")
    )
    per_person: dict = {}
    for allowance in allowances:
        # Sitzungsgelder hängen an der Anwesenheit, Monats-Pauschalen
        # direkt an der Person — beide Typen werden unterstützt.
        person = getattr(allowance, "person", None) or allowance.attendance.person
        entry = per_person.setdefault(
            person.pk,
            {
                "person": person,
                "count": 0,
                "total": Decimal("0.00"),
                "paid": Decimal("0.00"),
                "approved": Decimal("0.00"),
                "pending": Decimal("0.00"),
            },
        )
        entry["count"] += 1
        entry["total"] += allowance.amount
        if allowance.status == "paid":
            entry["paid"] += allowance.amount
        elif allowance.status == "approved":
            entry["approved"] += allowance.amount
        else:
            entry["pending"] += allowance.amount
    return sorted(per_person.values(), key=lambda e: (e["person"].family_name, e["person"].given_name))


def year_summary_csv(rows, year) -> str:
    """Jahresübersicht als CSV (Semikolon, CRLF)."""
    buffer = io.StringIO()
    writer = csv_safety.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Jahr", "Name", "Positionen", "Summe", "Ausgezahlt", "Genehmigt", "Ausstehend"])
    for row in rows:
        writer.writerow(
            [
                year,
                row["person"].display_name,
                row["count"],
                f"{row['total']:.2f}".replace(".", ","),
                f"{row['paid']:.2f}".replace(".", ","),
                f"{row['approved']:.2f}".replace(".", ","),
                f"{row['pending']:.2f}".replace(".", ","),
            ]
        )
    return buffer.getvalue()


# =============================================================================
# Abrechnungsmitteilung (PDF)
# =============================================================================


def build_notice_pdf(tenant, person, allowances, period_start, period_end) -> bytes:
    """Abrechnungsmitteilung als PDF je Empfänger (Issue #38)."""
    from django.template.loader import render_to_string

    from apps.common.pdf import html_to_pdf

    total = sum((a.amount for a in allowances), Decimal("0.00"))
    context = {
        "tenant": tenant,
        "person": person,
        "allowances": allowances,
        "period_start": period_start,
        "period_end": period_end,
        "total": total,
        "generated_at": timezone.localtime(),
    }
    html = render_to_string("session/pdf/allowance_notice.html", context)
    return html_to_pdf(html)


def parse_period(raw_from, raw_to):
    """Zeitraum aus Request-Parametern lesen (Default: laufender Monat)."""
    today = timezone.localdate()
    period_start = today.replace(day=1)
    period_end = today
    try:
        if raw_from:
            period_start = datetime.strptime(raw_from, "%Y-%m-%d").date()
        if raw_to:
            period_end = datetime.strptime(raw_to, "%Y-%m-%d").date()
    except ValueError:
        return None, None
    if period_start > period_end:
        return None, None
    return period_start, period_end


# =============================================================================
# Monatliche Pauschalen (EntschVO NRW)
# =============================================================================


def generate_monthly_allowances(tenant, year: int, month: int, *, created_by=None) -> dict:
    """
    Monatslauf: Für alle aktiven Pauschalen-Zuordnungen des Mandanten einen
    Abrechnungsposten für den Monat erzeugen (idempotent — vorhandene
    Posten bleiben unverändert).
    """
    from datetime import date

    from ..models import SessionMonthlyAllowance, SessionPersonMonthlyRate

    period = date(year, month, 1)
    created = 0
    skipped = 0
    assignments = (
        SessionPersonMonthlyRate.objects.filter(person__tenant=tenant, person__is_active=True, rate__is_active=True)
        .select_related("person", "rate")
        .order_by("person__family_name")
    )
    for assignment in assignments:
        if not assignment.active_in_month(period):
            continue
        _, was_created = SessionMonthlyAllowance.objects.get_or_create(
            person=assignment.person,
            rate=assignment.rate,
            period=period,
            defaults={
                "tenant": tenant,
                "amount": assignment.rate.amount,
                "created_by": created_by,
            },
        )
        if was_created:
            created += 1
        else:
            skipped += 1
    return {"created": created, "skipped": skipped, "period": period}


def approve_monthly_allowances(allowances, approver, *, four_eyes: bool = True) -> dict:
    """Monats-Pauschalen genehmigen (nur Status „Ausstehend") — Vier-Augen-Prinzip.

    Posten, die der/die Genehmigende selbst erzeugt hat (Monatslauf), werden
    NICHT genehmigt (blocked_four_eyes), analog zu ``approve_allowances``.
    """
    approved = 0
    blocked = 0
    now = timezone.now()
    for allowance in allowances:
        if allowance.status != "pending":
            continue
        if four_eyes and allowance.created_by_id is not None and allowance.created_by_id == approver.pk:
            blocked += 1
            continue
        allowance.status = "approved"
        allowance.approved_by = approver
        allowance.approved_at = now
        allowance.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        approved += 1
    return {"approved": approved, "blocked_four_eyes": blocked}


def build_monthly_export_csv(allowances) -> str:
    """CSV der Monats-Pauschalen fürs Finanzverfahren (analog Sitzungsgeld-CSV)."""
    buffer = io.StringIO()
    writer = csv_safety.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Name",
            "Pauschale",
            "Rechtsgrundlage",
            "Monat",
            "Betrag",
            "Status",
            "Kontoinhaber",
            "IBAN",
            "BIC",
            "Export-Referenz",
        ]
    )
    for allowance in allowances:
        person = allowance.person
        writer.writerow(
            [
                person.display_name,
                allowance.rate.name,
                allowance.rate.legal_basis,
                allowance.period.strftime("%m/%Y"),
                f"{allowance.amount:.2f}".replace(".", ","),
                allowance.get_status_display(),
                person.get_bank_account_holder_decrypted() or "",
                person.get_bank_iban_decrypted() or "",
                person.get_bank_bic_decrypted() or "",
                allowance.export_reference,
            ]
        )
    return buffer.getvalue()


def mark_monthly_exported(allowances, reference, *, mark_paid=True) -> int:
    """Monats-Pauschalen nach dem Export als ausgezahlt kennzeichnen."""
    count = 0
    now = timezone.now()
    for allowance in allowances:
        allowance.export_reference = reference
        allowance.export_date = now
        if mark_paid:
            allowance.status = "paid"
            allowance.paid_at = now
        allowance.save(update_fields=["export_reference", "export_date", "status", "paid_at", "updated_at"])
        count += 1
    return count
