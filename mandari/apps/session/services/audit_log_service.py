# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokoll des Session RIS: Export, Prüfung und Archivierung (Issue #221).

- :func:`build_export`: Zeitraum als CSV, JSON (mit Prüfsumme im Umschlag) oder ZIP (beides plus
  ``SHA256SUMS`` und Kettenanker). Jeder Export bekommt eine SHA-256-Prüfsumme; sie steht im
  Antwortkopf ``X-Checksum-SHA256`` und im Protokolleintrag des Exports.
- :func:`verify_tenant`: Hash-Kette des Mandanten prüfen.
- :func:`archive_expired`: vor der fristgerechten Löschung ein Archivpaket schreiben, dann löschen
  und den Kettenanker setzen (aufgerufen vom DSGVO-Löschlauf).

Der generische Teil (Kette, Dateiformate, Archivspeicher) liegt in :mod:`apps.common.audit_chain`
und :mod:`apps.common.audit_archive`.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from apps.common import audit_archive, audit_chain
from apps.session.models import SessionAuditLog, SessionTenant, SessionUser

SPEC = audit_chain.SESSION
EXPORT_FORMATS = ("csv", "json", "zip")
#: Anzeigefelder im Export (nicht Teil des Hashes)
EXTRA_COLUMNS = ("benutzer", "in_vertretung_fuer", "aktion")
#: Obergrenze einer Löschung aus der Oberfläche; der Befehl session_privacy_purge kennt keine
UI_PURGE_LIMIT = 50_000
DELETED_ACCOUNT = "gelöschtes Konto"


def user_extras(entries: list[Any]) -> dict[Any, dict[str, Any]]:
    """Anzeige je Eintrag: E-Mail der handelnden bzw. vertretenen Person und Aktionsbezeichnung."""
    refs = {e.user_ref for e in entries if e.user_ref} | {e.on_behalf_of_ref for e in entries if e.on_behalf_of_ref}
    emails = dict(SessionUser.objects.filter(pk__in=refs).values_list("pk", "user__email")) if refs else {}
    labels = dict(SessionAuditLog.ACTION_CHOICES)

    def _who(ref: Any) -> str:
        if not ref:
            return ""
        return str(emails.get(ref, DELETED_ACCOUNT))

    return {
        e.pk: {
            "benutzer": _who(e.user_ref),
            "in_vertretung_fuer": _who(e.on_behalf_of_ref),
            "aktion": labels.get(e.action, e.action),
        }
        for e in entries
    }


def _bounds(date_from: dt.date | None, date_to: dt.date | None) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Tagesgrenzen in der Zeitzone der Anwendung (indexfreundlich statt created_at__date)."""
    start = timezone.make_aware(dt.datetime.combine(date_from, dt.time.min)) if date_from else None
    end = timezone.make_aware(dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min)) if date_to else None
    return start, end


def period_queryset(tenant: SessionTenant, date_from: dt.date | None, date_to: dt.date | None) -> QuerySet[Any]:
    """Einträge des Mandanten im Zeitraum (Tage einschließlich), zeitlich sortiert."""
    qs = SessionAuditLog.objects.filter(tenant=tenant)
    start, end = _bounds(date_from, date_to)
    if start is not None:
        qs = qs.filter(created_at__gte=start)
    if end is not None:
        qs = qs.filter(created_at__lt=end)
    return qs.order_by("created_at", "seq", "id")


def build_export(
    tenant: SessionTenant,
    *,
    fmt: str,
    date_from: dt.date | None,
    date_to: dt.date | None,
    requested_by: str,
) -> audit_archive.ExportFile:
    """Export eines Zeitraums als CSV, JSON oder ZIP (mit Prüfsummen)."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unbekanntes Exportformat: {fmt}")
    meta = {
        "format": audit_archive.FORMAT_EXPORT,
        "version": audit_archive.FORMAT_VERSION,
        "mandant": {"name": tenant.name, "slug": tenant.slug},
        "zeitraum": {
            "von": date_from.isoformat() if date_from else None,
            "bis": date_to.isoformat() if date_to else None,
        },
        "erstellt_am": timezone.now().isoformat(),
        "erstellt_von": requested_by,
        "kette": audit_archive.chain_info(SPEC, tenant.pk),
    }
    entries = period_queryset(tenant, date_from, date_to).iterator(chunk_size=2000)
    records = audit_archive.iter_records(SPEC, entries, user_extras)
    base_name = "protokoll-{}-{}-{}".format(
        tenant.slug,
        date_from.isoformat() if date_from else "anfang",
        date_to.isoformat() if date_to else "heute",
    )
    columns = audit_archive.csv_columns(SPEC, EXTRA_COLUMNS)
    if fmt == "json":
        return audit_archive.write_json(records, meta, name=f"{base_name}.json")
    if fmt == "csv":
        return audit_archive.write_csv(records, columns, name=f"{base_name}.csv")
    json_file, csv_file = audit_archive.write_json_and_csv(records, meta, columns, base_name=base_name)
    return audit_archive.write_zip(
        [json_file, csv_file],
        name=f"{base_name}.zip",
        extra_files={"kette.json": json.dumps(meta["kette"], ensure_ascii=False, indent=2) + "\n"},
    )


def verify_tenant(tenant: SessionTenant) -> audit_chain.VerifyResult:
    """Hash-Kette des Mandanten vollständig prüfen."""
    return audit_chain.verify(SPEC, tenant.pk)


def chain_status(tenant: SessionTenant) -> dict[str, Any]:
    """Kurzstatus für die Protokollansicht (eine Abfrage)."""
    info = audit_archive.chain_info(SPEC, tenant.pk)
    head = audit_chain.get_head(SPEC, tenant.pk)
    info["anker_archiv"] = head.anchor_archive if head else ""
    info["anker_am"] = head.anchor_at if head else None
    return info


# =============================================================================
# Archivierung vor der fristgerechten Löschung
# =============================================================================


@dataclass
class PurgeOutcome:
    """Ergebnis der Protokoll-Löschung im DSGVO-Löschlauf."""

    deleted: int = 0
    archive_name: str = ""
    archive_sha256: str = ""
    deferred: int = 0
    not_chained: bool = False
    error: str = ""


def ensure_chained(tenant: SessionTenant, *, allow_backfill: bool) -> bool:
    """Ist die Kette des Mandanten scharf? Verkettet den Altbestand, wenn erlaubt (Befehlszeile)."""
    head = audit_chain.get_head(SPEC, tenant.pk)
    if head is not None and head.initialized:
        return True
    if not allow_backfill:
        return not SessionAuditLog.objects.filter(tenant=tenant).exists()
    audit_chain.backfill(SPEC, tenant.pk)
    head = audit_chain.get_head(SPEC, tenant.pk)
    return head is None or bool(head.initialized)


def count_expired(tenant: SessionTenant, cutoff: dt.datetime) -> int:
    """Wie viele Einträge ein Löschlauf jetzt archivieren und löschen würde (Probelauf)."""
    found = audit_archive.expired_range(SPEC, tenant.pk, cutoff)
    if found is None:
        return 0
    first, last, _deferred = found
    return last - first + 1


def archive_expired(
    tenant: SessionTenant,
    cutoff: dt.datetime,
    *,
    user: Any = None,
    request: Any = None,
    limit: int | None = None,
    allow_backfill: bool = False,
) -> PurgeOutcome:
    """Abgelaufene Einträge archivieren, löschen und den Vorgang selbst protokollieren."""
    from apps.session import audit

    if not ensure_chained(tenant, allow_backfill=allow_backfill):
        return PurgeOutcome(not_chained=True)
    try:
        result = audit_archive.archive_and_delete(
            SPEC,
            tenant.pk,
            cutoff,
            scope_label=tenant.slug,
            meta={"mandant": {"name": tenant.name, "slug": tenant.slug}, "frist_bis": cutoff.isoformat()},
            extras=user_extras,
            extra_columns=EXTRA_COLUMNS,
            limit=limit,
        )
    except audit_chain.ChainError as exc:
        return PurgeOutcome(error=str(exc))
    if result is None:
        return PurgeOutcome()
    audit.log_event(
        "audit_archive",
        tenant,
        tenant=tenant,
        user=user,
        request=request,
        changes={
            "archiv": result.archive_name,
            "sha256": result.archive_sha256,
            "von_nr": result.first_seq,
            "bis_nr": result.last_seq,
            "anzahl": result.deleted,
            "anker": result.anchor_hash,
            "frist_bis": cutoff.isoformat(),
        },
    )
    return PurgeOutcome(
        deleted=result.deleted,
        archive_name=result.archive_name,
        archive_sha256=result.archive_sha256,
        deferred=result.deferred,
    )
