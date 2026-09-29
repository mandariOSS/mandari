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
  (Überweisungs-Datei); Abrechnungsmitteilung als PDF je Empfänger. Der SEPA-Export
  (:func:`export_sepa`) läuft für Sitzungsgeld und Monatspauschalen gleich: in einer Transaktion,
  mit gesperrten Positionen, nur für genehmigte und noch nicht exportierte Positionen, mit einer
  Referenz aus dem gemeinsamen Zähler je Mandant und Jahr (Issue #428).
- **Bankdaten**: IBAN/BIC/Kontoinhaber werden ausschließlich über die
  verschlüsselten Person-Accessoren gelesen und nur in Export-Pfaden mit
  der Berechtigung ``manage_allowances`` verwendet.
"""

import io
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.sax.saxutils import escape  # noqa: F401  (Doku: Escaping via ElementTree)

from django.db import transaction
from django.db.models import QuerySet
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


#: Arten des SEPA-Exports (Verwendungszweck und Kennzeichnung der Positionen)
KIND_SESSION = "sitzungsgeld"
KIND_MONTHLY = "pauschale"


def _max_used_reference(tenant: Any, prefix: str) -> int:
    """Höchste bereits vergebene Nummer mit diesem Präfix – Sitzungsgeld und Pauschalen zusammen."""
    from apps.session.models import SessionAllowance, SessionMonthlyAllowance

    refs = set(
        SessionAllowance.objects.filter(
            attendance__meeting__tenant=tenant, export_reference__startswith=prefix
        ).values_list("export_reference", flat=True)
    )
    refs.update(
        SessionMonthlyAllowance.objects.filter(tenant=tenant, export_reference__startswith=prefix).values_list(
            "export_reference", flat=True
        )
    )
    max_num = 0
    for ref in refs:
        try:
            max_num = max(max_num, int(ref.rsplit("-", 1)[-1]))
        except (TypeError, ValueError):
            continue
    return max_num


def next_export_reference(tenant: Any) -> str:
    """
    Nächste fortlaufende Export-Referenz, z. B. SG-2026-0003 (Issue #428).

    Ein Zähler je Mandant und Jahr für Sitzungsgeld und Monatspauschalen. Die Zählerzeile bleibt
    bis zum Ende der umgebenden Transaktion gesperrt; bricht der Export ab, ist auch die Nummer
    nicht verbraucht. Der Zähler setzt mindestens auf der höchsten bereits vergebenen Nummer auf –
    so entstehen auch nach Exporten aus älteren Ständen keine Doppelungen.
    """
    from apps.session.models import SessionExportCounter

    year = timezone.localdate().year
    prefix = f"SG-{year}-"
    with transaction.atomic():
        counter, _ = SessionExportCounter.objects.select_for_update().get_or_create(tenant=tenant, year=year)
        counter.value = max(counter.value, _max_used_reference(tenant, prefix)) + 1
        counter.save(update_fields=["value", "updated_at"])
    return f"{prefix}{counter.value:04d}"


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
                attendance.meeting.name,
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


def _person_of(allowance: Any) -> Any:
    """Empfänger einer Position: Sitzungsgeld hängt an der Anwesenheit, Pauschalen an der Person."""
    return getattr(allowance, "person", None) or allowance.attendance.person


def _iban_of(person: Any) -> str:
    """IBAN ohne Leerraum – leer heißt: nicht überweisbar (gleiche Regel für XML und Kennzeichnung)."""
    return re.sub(r"\s+", "", person.get_bank_iban_decrypted() or "")


def _purpose(kind: str, entry: dict[str, Any], reference: str) -> str:
    """Verwendungszweck je Art des Exports (Issue #428)."""
    if kind == KIND_MONTHLY:
        months = ", ".join(f"{period:%m/%Y}" for period in sorted(entry["periods"]))
        return f"Monatspauschale {months} {reference}".strip()
    return f"Sitzungsgeld {entry['count']} Sitzung(en) {reference}".strip()


def build_sepa_xml(
    tenant: Any,
    allowances: Iterable[Any],
    *,
    debtor_name: str,
    debtor_iban: str,
    debtor_bic: str = "",
    reference: str = "",
    execution_date: date | None = None,
    kind: str = KIND_SESSION,
) -> tuple[bytes, int, Decimal, list[str]]:
    """
    SEPA-pain.001.001.03-Überweisungsdatei (Issue #38).

    Je Person eine Sammel-Transaktion (Summe ihrer Positionen). Personen
    ohne hinterlegte IBAN werden übersprungen und namentlich zurückgemeldet.
    ``kind`` bestimmt den Verwendungszweck (Sitzungsgeld oder Monatspauschale).

    Returns:
        Tuple (xml_bytes, transaction_count, total (Decimal), skipped_names)
    """
    execution_date = execution_date or timezone.localdate()

    # Je Person summieren (Bankdaten über die verschlüsselten Accessoren)
    per_person: dict[Any, dict[str, Any]] = {}
    for allowance in allowances:
        person = _person_of(allowance)
        entry = per_person.setdefault(
            person.pk, {"person": person, "amount": Decimal("0.00"), "count": 0, "periods": set()}
        )
        entry["amount"] += allowance.amount
        entry["count"] += 1
        if getattr(allowance, "period", None) is not None:
            entry["periods"].add(allowance.period)

    transactions: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in per_person.values():
        person = entry["person"]
        iban = _iban_of(person)
        if not iban:
            skipped.append(person.display_name)
            continue
        transactions.append(
            {
                "name": person.get_bank_account_holder_decrypted() or person.display_name,
                "iban": iban.upper(),
                "bic": re.sub(r"\s+", "", person.get_bank_bic_decrypted() or "").upper(),
                "amount": entry["amount"],
                "purpose": _purpose(kind, entry, reference),
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
        SubElement(rmt, "Ustrd").text = _sepa_text(txn["purpose"], 140)

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


@dataclass
class SepaExport:
    """Ergebnis von :func:`export_sepa`."""

    #: Export-Referenz; leer, wenn nichts exportiert wurde
    reference: str = ""
    xml: bytes = b""
    transaction_count: int = 0
    total: Decimal = Decimal("0.00")
    #: Personen ohne IBAN (ihre Positionen bleiben genehmigt und offen)
    skipped: list[str] = field(default_factory=list)
    #: als exportiert gekennzeichnete Positionen
    exported: int = 0


def export_sepa(
    tenant: Any,
    allowances: QuerySet[Any],
    *,
    kind: str,
    debtor_name: str,
    debtor_iban: str,
    debtor_bic: str = "",
) -> SepaExport:
    """
    SEPA-Export in einem Zug für Sitzungsgeld oder Monatspauschalen (Issue #428).

    ``allowances`` ist das QuerySet der Auswahl (Zeitraum, Gremium bzw. Monat). In einer
    Transaktion werden die Positionen gesperrt und nur die genehmigten, noch nicht exportierten
    übernommen; Referenz, Datei und Kennzeichnung entstehen unter dieser Sperre. Ein zweiter,
    gleichzeitiger Export derselben Auswahl wartet und findet danach nichts mehr – keine Position
    wird zweimal ausgegeben. Beträge und Summen berechnet :func:`build_sepa_xml` unverändert.

    Ohne überweisbare Position (niemand mit IBAN) wird nichts gekennzeichnet und keine Referenz
    verbraucht; ``skipped`` nennt dann die Personen.
    """
    with transaction.atomic():
        positions = list(allowances.select_for_update(of=("self",)).filter(status="approved", export_reference=""))
        if not positions:
            return SepaExport()
        exportable = [a for a in positions if _iban_of(_person_of(a))]
        if not exportable:
            names = {_person_of(a).pk: _person_of(a).display_name for a in positions}
            return SepaExport(skipped=list(names.values()))

        reference = next_export_reference(tenant)
        xml_bytes, txn_count, total, skipped = build_sepa_xml(
            tenant,
            positions,
            debtor_name=debtor_name,
            debtor_iban=debtor_iban,
            debtor_bic=debtor_bic,
            reference=reference,
            kind=kind,
        )
        mark = mark_monthly_exported if kind == KIND_MONTHLY else mark_exported
        exported = mark(exportable, reference, mark_paid=True)
    return SepaExport(
        reference=reference,
        xml=xml_bytes,
        transaction_count=txn_count,
        total=total,
        skipped=skipped,
        exported=exported,
    )


def last_export_reference(allowances: QuerySet[Any]) -> str:
    """Zuletzt vergebene Export-Referenz einer Auswahl – für den Hinweis „bereits exportiert“."""
    return (
        allowances.exclude(export_reference="")
        .order_by("-export_date", "-export_reference")
        .values_list("export_reference", flat=True)
        .first()
        or ""
    )


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
        # Wie beim Sitzungsgeld: „ausgezahlt“ nur aus „genehmigt“
        if mark_paid and allowance.status == "approved":
            allowance.status = "paid"
            allowance.paid_at = now
        allowance.save(update_fields=["export_reference", "export_date", "status", "paid_at", "updated_at"])
        count += 1
    return count
