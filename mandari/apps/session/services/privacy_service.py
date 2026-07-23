# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DSGVO-Paket für das Session RIS (Issue #43).

- **Aufbewahrungsfristen** je Datenart in den Mandanten-Einstellungen
  (0 = Frist deaktiviert, es wird nichts gelöscht):
  * persons_years: Kontakt-/Bankdaten ausgeschiedener Mandatsträger
  * audit_years: Audit-Log-Einträge
  * np_content_years: nicht-öffentliche Inhalte (NÖ-Protokollteil,
    interne Sitzungsnotizen)
- **Anonymisierungs-/Löschlauf** (:func:`run_privacy_purge`): entfernt
  nach Fristablauf Kontakt- und Bankdaten ausgeschiedener Personen —
  der NAME bleibt erhalten, damit historische Beschlüsse und Protokolle
  nachvollziehbar bleiben. Jeder Lauf wird nachweisbar auditiert.
- **Betroffenenauskunft** (:func:`subject_access_export`): Export aller
  zu einer Person gespeicherten Daten (Art. 15 DSGVO). Bankdaten werden
  nur mit der Berechtigung manage_allowances entschlüsselt.

Dokumentation: docs/DSGVO_LOESCHKONZEPT.md, docs/DSGVO_AVV_MUSTER.md,
docs/DSGVO_TOM.md.
"""

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Defaults (Jahre); 0 = deaktiviert. Orientierung: gängige kommunale
# Aufbewahrungsempfehlungen — verbindlich sind die örtlichen Satzungen.
PRIVACY_DEFAULTS = {
    "persons_years": 0,
    "audit_years": 0,
    "np_content_years": 0,
    "notice": "",
}


def get_privacy_settings(tenant) -> dict:
    """Datenschutz-Einstellungen des Mandanten (mit Defaults)."""
    stored = (tenant.settings or {}).get("privacy", {})
    result = dict(PRIVACY_DEFAULTS)
    for key in ("persons_years", "audit_years", "np_content_years"):
        try:
            result[key] = max(0, min(int(stored.get(key, result[key])), 100))
        except (TypeError, ValueError):
            pass
    result["notice"] = str(stored.get("notice", "") or "")
    return result


# =============================================================================
# Anonymisierungs-/Löschlauf
# =============================================================================


def _anonymize_person(person) -> list[str]:
    """
    Kontakt- und Bankdaten einer ausgeschiedenen Person entfernen.

    Der Name bleibt erhalten (historische Beschlüsse/Protokolle).
    Returns: Liste der geleerten Datenarten.
    """
    cleared = []
    if person.email:
        person.email = ""
        cleared.append("E-Mail")
    if person.get_phone_decrypted():
        person.set_phone_encrypted("")
        cleared.append("Telefon")
    if person.get_address_decrypted():
        person.set_address_encrypted("")
        cleared.append("Adresse")
    if (
        person.get_bank_iban_decrypted()
        or person.get_bank_bic_decrypted()
        or person.get_bank_account_holder_decrypted()
    ):
        person.set_bank_account_holder_encrypted("")
        person.set_bank_iban_encrypted("")
        person.set_bank_bic_encrypted("")
        cleared.append("Bankdaten")
    if cleared:
        person.save()
    return cleared


def run_privacy_purge(tenant, *, now=None, dry_run=False, user=None, request=None) -> dict:
    """
    Anonymisierungs-/Löschlauf gemäß den konfigurierten Fristen (Issue #43).

    Nachweisbarkeit: Jede Anonymisierung und der Lauf selbst werden im
    Audit-Log dokumentiert (ohne Klartext-Werte).

    Returns:
        dict: persons_anonymized, audit_deleted, np_meetings_cleared,
              skipped (Liste deaktivierter Datenarten), dry_run
    """
    from apps.session import audit
    from apps.session.models import SessionAuditLog, SessionMeeting, SessionPerson, SessionProtocol

    now = now or timezone.now()
    today = now.date()
    settings = get_privacy_settings(tenant)
    stats = {
        "persons_anonymized": 0,
        "audit_deleted": 0,
        "np_meetings_cleared": 0,
        "skipped": [],
        "dry_run": dry_run,
    }

    # 1) Ausgeschiedene Mandatsträger: Kontakt-/Bankdaten nach Frist
    if settings["persons_years"] > 0:
        cutoff = today - timedelta(days=365 * settings["persons_years"])
        persons = SessionPerson.objects.filter(
            tenant=tenant, is_active=False, end_date__isnull=False, end_date__lte=cutoff
        )
        for person in persons:
            if dry_run:
                has_data = bool(
                    person.email
                    or person.get_phone_decrypted()
                    or person.get_address_decrypted()
                    or person.get_bank_iban_decrypted()
                    or person.get_bank_account_holder_decrypted()
                )
                if has_data:
                    stats["persons_anonymized"] += 1
                continue
            cleared = _anonymize_person(person)
            if cleared:
                stats["persons_anonymized"] += 1
                audit.log_event(
                    "update",
                    person,
                    tenant=tenant,
                    user=user,
                    request=request,
                    changes={"dsgvo_anonymisiert": cleared, "mandatsende": person.end_date.isoformat()},
                )
    else:
        stats["skipped"].append("Personendaten (Frist deaktiviert)")

    # 2) Nicht-öffentliche Inhalte: NÖ-Protokollteil + interne Notizen
    if settings["np_content_years"] > 0:
        cutoff_dt = now - timedelta(days=365 * settings["np_content_years"])
        meetings = SessionMeeting.objects.filter(tenant=tenant, start__lte=cutoff_dt)
        for meeting in meetings:
            cleared = []
            protocol = SessionProtocol.objects.filter(meeting=meeting).first()
            if protocol is not None and protocol.get_content_decrypted():
                cleared.append("NÖ-Protokollteil")
                if not dry_run:
                    protocol.set_content_encrypted("")
                    protocol.save(update_fields=["content_encrypted", "updated_at"])
            if meeting.get_internal_notes_decrypted():
                cleared.append("Interne Notizen")
                if not dry_run:
                    meeting.set_internal_notes_encrypted("")
                    meeting.save(update_fields=["internal_notes_encrypted", "updated_at"])
            if cleared:
                stats["np_meetings_cleared"] += 1
                if not dry_run:
                    audit.log_event(
                        "update",
                        meeting,
                        tenant=tenant,
                        user=user,
                        request=request,
                        changes={"dsgvo_geloescht": cleared},
                    )
    else:
        stats["skipped"].append("NÖ-Inhalte (Frist deaktiviert)")

    # 3) Audit-Log nach Frist (QuerySet-Delete — Einzellöschung ist gesperrt)
    if settings["audit_years"] > 0:
        cutoff_dt = now - timedelta(days=365 * settings["audit_years"])
        old_entries = SessionAuditLog.objects.filter(tenant=tenant, created_at__lte=cutoff_dt)
        if dry_run:
            stats["audit_deleted"] = old_entries.count()
        else:
            stats["audit_deleted"] = old_entries.count()
            old_entries._raw_delete(old_entries.db)
    else:
        stats["skipped"].append("Audit-Log (Frist deaktiviert)")

    # Lauf selbst nachweisbar dokumentieren (auch im Dry-Run)
    if not dry_run:
        audit.log_event(
            "delete",
            tenant,
            tenant=tenant,
            user=user,
            request=request,
            changes={
                "dsgvo_loeschlauf": {
                    "personen_anonymisiert": stats["persons_anonymized"],
                    "noe_inhalte_geleert": stats["np_meetings_cleared"],
                    "audit_geloescht": stats["audit_deleted"],
                    "fristen": {
                        "personen_jahre": settings["persons_years"],
                        "noe_jahre": settings["np_content_years"],
                        "audit_jahre": settings["audit_years"],
                    },
                }
            },
        )
    return stats


# =============================================================================
# Betroffenenauskunft (Art. 15 DSGVO)
# =============================================================================


def subject_access_export(tenant, person, *, include_bank=False) -> dict:
    """
    Alle zu einer Person gespeicherten Daten als Export-Dict (Issue #43).

    Bankdaten werden nur mit include_bank=True (manage_allowances)
    entschlüsselt — sonst wird lediglich vermerkt, OB Daten vorliegen.
    """
    from apps.session.models import SessionAllowance, SessionPaper

    data = {
        "auskunft": {
            "mandant": tenant.name,
            "erstellt_am": timezone.localtime().isoformat(),
            "rechtsgrundlage": "Art. 15 DSGVO (Auskunftsrecht der betroffenen Person)",
        },
        "stammdaten": {
            "anrede": person.form_of_address,
            "titel": person.title,
            "vorname": person.given_name,
            "nachname": person.family_name,
            "e_mail": person.email,
            "telefon": person.get_phone_decrypted() or "",
            "adresse": person.get_address_decrypted() or "",
            "aktiv": person.is_active,
            "mandatsbeginn": person.start_date.isoformat() if person.start_date else None,
            "mandatsende": person.end_date.isoformat() if person.end_date else None,
        },
    }

    if include_bank:
        data["bankdaten"] = {
            "kontoinhaber": person.get_bank_account_holder_decrypted() or "",
            "iban": person.get_bank_iban_decrypted() or "",
            "bic": person.get_bank_bic_decrypted() or "",
        }
    else:
        has_bank = bool(person.get_bank_iban_decrypted() or person.get_bank_account_holder_decrypted())
        data["bankdaten"] = {
            "hinweis": "Bankdaten sind verschlüsselt gespeichert"
            + (" und vorhanden." if has_bank else "; es sind keine hinterlegt.")
        }

    data["gremienmitgliedschaften"] = [
        {
            "gremium": m.organization.name,
            "funktion": m.get_role_display(),
            "stimmberechtigt": m.has_voting_rights,
            "von": m.start_date.isoformat() if m.start_date else None,
            "bis": m.end_date.isoformat() if m.end_date else None,
            "wahlperiode": m.legislative_term.name if m.legislative_term else None,
        }
        for m in person.memberships.select_related("organization", "legislative_term")
    ]

    data["anwesenheiten"] = [
        {
            "sitzung": a.meeting.name,
            "gremium": a.meeting.organization.name,
            "datum": timezone.localtime(a.meeting.start).date().isoformat(),
            "status": a.get_status_display(),
            "funktion": a.get_role_display(),
        }
        for a in person.attendances.select_related("meeting__organization").order_by("meeting__start")
    ]

    data["sitzungsgelder"] = [
        {
            "sitzung": allowance.attendance.meeting.name,
            "datum": timezone.localtime(allowance.attendance.meeting.start).date().isoformat(),
            "betrag": str(allowance.amount),
            "waehrung": allowance.currency,
            "status": allowance.get_status_display(),
            "export_referenz": allowance.export_reference,
        }
        for allowance in SessionAllowance.objects.filter(attendance__person=person).select_related(
            "attendance__meeting"
        )
    ]

    data["vorlagen_als_verfasser"] = [
        {"referenz": paper.reference, "name": paper.name, "datum": paper.date.isoformat() if paper.date else None}
        for paper in SessionPaper.objects.filter(tenant=tenant, originator_person=person)
    ]

    return data
