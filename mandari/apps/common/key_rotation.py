# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schlüsselwechsel für alle verschlüsselt gespeicherten Werte.

Grundlage ist das Verzeichnis ``apps/common/crypto_registry.py``; was dort steht, wird
erfasst – und nur das. Der Wechsel hat drei Stufen:

1. **Hauptschlüssel-Ebene** – eingepackte Mandantenschlüssel, plattformweite
   Zugangsdaten und der zweite Faktor. Alles, was noch mit einem vorherigen
   Hauptschlüssel verschlüsselt ist, wird in *einer* Transaktion auf den aktuellen
   umgeschrieben. Scheitert ein Wert, bleibt der gesamte alte Stand erhalten.
2. **Neue Mandantenschlüssel** – je Mandant in *einer* Transaktion: neuer Schlüssel,
   alle Inhalte des Mandanten neu verschlüsseln. Der bisherige Schlüssel bleibt als
   ``encryption_key_previous`` zum Lesen erhalten; was die laufende Anwendung in der
   Zwischenzeit noch mit ihm schreibt, bleibt dadurch lesbar. Scheitert ein Wert, bleibt
   der Mandant vollständig beim alten Schlüssel.
3. **Abschluss** – nach einem Neustart der Anwendung: Nachzügler neu verschlüsseln und den
   vorherigen Mandantenschlüssel entfernen, je Mandant in einer Transaktion.

Wiederaufnahme: Ein Wert, der schon mit dem aktuellen Schlüssel lesbar ist, bleibt
unberührt. Mandanten mit vorherigem Schlüssel gelten als begonnen und erhalten keinen
weiteren neuen Schlüssel. Ein abgebrochener Lauf wird deshalb einfach wiederholt.

Geschrieben wird mit gezielten Updates, die den gelesenen Wert als Bedingung enthalten:
Hat die Anwendung einen Wert zwischenzeitlich geändert, bleibt ihre Fassung stehen.
Ausgegeben und protokolliert werden nur Anzahlen und Datensatzkennungen, nie Werte oder
Schlüssel.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import models, transaction

from apps.common.crypto_registry import (
    ENCRYPTED_FIELDS,
    SELF,
    TENANT_MODELS,
    EncryptedField,
    KeyKind,
    fields_of_kind,
)
from apps.common.encryption import (
    aes_open,
    aes_seal,
    decode_master_key,
    generate_key,
    previous_master_key_materials,
    two_factor_fernet,
    two_factor_material_variants,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
#: So beginnt jedes Fernet-Token (Version 0x80, Zeitstempel); alles andere ist Altbestand im Klartext
FERNET_PREFIX = b"gAAAAA"
#: Höchstzahl gemeldeter Kennungen unlesbarer Datensätze je Feld
MAX_REPORTED_IDS = 5

MASTER_LEVEL = (KeyKind.TENANT_KEY, KeyKind.MASTER, KeyKind.TWO_FACTOR)

TenantRef = tuple[str, str]  # (Modell-Label, Primärschlüssel als Text)


class Status(StrEnum):
    CURRENT = "aktuell"
    PREVIOUS = "alter Schlüssel"
    PLAINTEXT = "Klartext"
    UNREADABLE = "unlesbar"
    UNASSIGNED = "ohne Schlüsselart"


class RotationError(Exception):
    """Abbruch des Wechsels. Die Meldung nennt nur Felder und Kennungen, nie Werte."""


@dataclass
class FieldReport:
    entry: EncryptedField
    counts: Counter[Status] = field(default_factory=Counter)
    unreadable_ids: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def add(self, status: Status, pk: Any) -> None:
        self.counts[status] += 1
        if status in (Status.UNREADABLE, Status.UNASSIGNED) and len(self.unreadable_ids) < MAX_REPORTED_IDS:
            self.unreadable_ids.append(str(pk))


@dataclass
class ScanResult:
    fields: list[FieldReport]
    #: Werte je Mandant und Status (nur Mandantendaten)
    per_tenant: dict[TenantRef, Counter[Status]]
    #: Mandanten mit Schlüssel: (Label, pk) → hat einen vorherigen Schlüssel (Wechsel begonnen)
    tenants: dict[TenantRef, bool]

    def count(self, *statuses: Status, kinds: Collection[KeyKind] = tuple(KeyKind)) -> int:
        return sum(report.counts[status] for report in self.fields if report.entry.kind in kinds for status in statuses)

    def tenant_count(self, tenant: TenantRef, *statuses: Status) -> int:
        counts = self.per_tenant.get(tenant, Counter())
        return sum(counts[status] for status in statuses)


@dataclass
class RotationResult:
    master_values_rewritten: int = 0
    tenants_switched: int = 0
    tenants_resumed: int = 0
    tenant_values_rewritten: int = 0
    tenants_finalized: int = 0
    skipped_unreadable: int = 0


@dataclass(frozen=True)
class _TenantKeys:
    current: bytes | None
    previous: bytes | None


def _bytes(value: Any) -> bytes:
    return bytes(value) if value else b""


def _model(label: str) -> type[models.Model]:
    return apps.get_model(label)


def tenant_target(model: type[models.Model], path: str) -> str:
    """Label des Mandantenmodells, bei dem ein Lookup-Pfad endet (nur Vorwärts-Fremdschlüssel)."""
    if path == SELF:
        return model._meta.label
    current: type[models.Model] = model
    for part in path.split("__"):
        relation = current._meta.get_field(part)
        related = getattr(relation, "related_model", None)
        forward = getattr(relation, "concrete", False) and (
            getattr(relation, "many_to_one", False) or getattr(relation, "one_to_one", False)
        )
        if not forward or related is None or isinstance(related, str):
            raise ImproperlyConfigured(f"{model._meta.label}: '{path}' ist kein Pfad über Fremdschlüssel.")
        current = cast(type[models.Model], related)
    return current._meta.label


def _tenant_paths(entry: EncryptedField) -> list[tuple[str, str]]:
    """(Pfad, Label des Mandantenmodells) für jeden Pfad des Eintrags."""
    model = _model(entry.model)
    return [(path, tenant_target(model, path)) for path in entry.tenant_paths]


def _batches(
    model: type[models.Model], field_name: str, extra: Sequence[str] = (), where: models.Q | None = None
) -> Iterator[list[tuple[Any, ...]]]:
    """Zeilen mit gesetztem Feld, seitenweise nach Primärschlüssel (kein offener Cursor, wenig Speicher)."""
    queryset = model._base_manager.filter(**{f"{field_name}__isnull": False})
    if where is not None:
        queryset = queryset.filter(where)
    queryset = queryset.order_by("pk")
    last: Any = None
    while True:
        page = queryset if last is None else queryset.filter(pk__gt=last)
        rows = list(page.values_list("pk", field_name, *extra)[:BATCH_SIZE])
        if not rows:
            return
        yield rows
        last = rows[-1][0]


def _store(model: type[models.Model], pk: Any, field_name: str, old: bytes, new: bytes) -> int:
    """Nur schreiben, wenn der Wert noch der gelesene ist – eine zwischenzeitliche Änderung gewinnt."""
    return model._base_manager.filter(pk=pk, **{field_name: old}).update(**{field_name: new})


class KeyRotation:
    """
    Schlüsselwechsel mit dem aktuellen Hauptschlüssel als Ziel.

    Vorherige Hauptschlüssel stammen aus ``ENCRYPTION_MASTER_KEY_PREVIOUS`` und zusätzlich
    aus ``extra_master_keys`` (z. B. einer Datenbank, die von einem anderen Server stammt).
    """

    def __init__(
        self,
        *,
        extra_master_keys: Sequence[str] = (),
        tenant_types: Collection[str] = tuple(TENANT_MODELS.values()),
        ignore_unreadable: bool = False,
    ) -> None:
        material = str(getattr(settings, "ENCRYPTION_MASTER_KEY", "") or "")
        if not material:
            raise ImproperlyConfigured("ENCRYPTION_MASTER_KEY ist nicht gesetzt.")
        self.master = decode_master_key(material)
        materials = [*previous_master_key_materials(), *extra_master_keys]
        previous: list[bytes] = []
        for number, previous_material in enumerate(materials, start=1):
            key = decode_master_key(previous_material, f"Vorheriger Hauptschlüssel Nr. {number}")
            if key != self.master and key not in previous:
                previous.append(key)
        self.previous_masters: tuple[bytes, ...] = tuple(previous)
        self.fernet = two_factor_fernet(material)
        self.previous_fernets: tuple[Fernet, ...] = tuple(
            two_factor_fernet(variant) for item in materials for variant in two_factor_material_variants(item)
        )
        self.tenant_types = frozenset(tenant_types)
        self.ignore_unreadable = ignore_unreadable

    # -- Entschlüsseln und Verschlüsseln je Schlüsselart ---------------------------------------

    def _open_master(self, data: bytes) -> tuple[Status, bytes | None]:
        for status, key in ((Status.CURRENT, self.master), *((Status.PREVIOUS, k) for k in self.previous_masters)):
            try:
                return status, aes_open(key, data)
            except (InvalidTag, ValueError):
                continue
        return Status.UNREADABLE, None

    def _open_two_factor(self, data: bytes) -> tuple[Status, bytes | None]:
        for status, fernet in ((Status.CURRENT, self.fernet), *((Status.PREVIOUS, f) for f in self.previous_fernets)):
            try:
                return status, fernet.decrypt(data)
            except InvalidToken:
                continue
        if not data.startswith(FERNET_PREFIX):
            # Altbestand aus der Zeit vor der Verschlüsselung; die Anwendung liest ihn als Text
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                return Status.UNREADABLE, None
            return Status.PLAINTEXT, data
        return Status.UNREADABLE, None

    @staticmethod
    def _open_tenant(data: bytes, keys: _TenantKeys | None) -> tuple[Status, bytes | None]:
        if keys is not None:
            for status, key in ((Status.CURRENT, keys.current), (Status.PREVIOUS, keys.previous)):
                if key is None:
                    continue
                try:
                    return status, aes_open(key, data)
                except (InvalidTag, ValueError):
                    continue
        return Status.UNREADABLE, None

    def _open(self, kind: KeyKind, data: bytes) -> tuple[Status, bytes | None]:
        if kind is KeyKind.TWO_FACTOR:
            return self._open_two_factor(data)
        if kind in (KeyKind.TENANT_KEY, KeyKind.MASTER):
            return self._open_master(data)
        return Status.UNASSIGNED, None

    def _seal(self, kind: KeyKind, plaintext: bytes) -> bytes:
        if kind is KeyKind.TWO_FACTOR:
            return self.fernet.encrypt(plaintext)
        return aes_seal(self.master, plaintext)

    def _unwrap(self, wrapped: Any) -> bytes | None:
        data = _bytes(wrapped)
        return self._open_master(data)[1] if data else None

    def _load_tenant_keys(self, label: str, pk: Any | None = None) -> dict[TenantRef, _TenantKeys]:
        queryset = _model(label)._base_manager.all()
        if pk is not None:
            queryset = queryset.filter(pk=pk)
        return {
            (label, str(row_pk)): _TenantKeys(self._unwrap(wrapped), self._unwrap(previous))
            for row_pk, wrapped, previous in queryset.values_list("pk", "encryption_key", "encryption_key_previous")
        }

    # -- Bestandsaufnahme -----------------------------------------------------------------------

    def scan(self) -> ScanResult:
        """Alle Werte einordnen, ohne etwas zu ändern."""
        tenant_keys: dict[TenantRef, _TenantKeys] = {}
        tenants: dict[TenantRef, bool] = {}
        for label in TENANT_MODELS:
            tenant_keys.update(self._load_tenant_keys(label))
            with_key = _model(label)._base_manager.filter(encryption_key__isnull=False)
            for pk, wrapped, previous in with_key.values_list("pk", "encryption_key", "encryption_key_previous"):
                if wrapped:
                    tenants[(label, str(pk))] = bool(previous)

        reports: list[FieldReport] = []
        per_tenant: dict[TenantRef, Counter[Status]] = {}
        for entry in ENCRYPTED_FIELDS:
            report = FieldReport(entry)
            model = _model(entry.model)
            if entry.kind is KeyKind.TENANT:
                paths = _tenant_paths(entry)
                extra = [path for path, _target in paths if path != SELF]
                for rows in _batches(model, entry.field, extra):
                    for row in rows:
                        data = _bytes(row[1])
                        if not data:
                            continue
                        tenant = self._tenant_of(paths, row)
                        status = self._open_tenant(data, tenant_keys.get(tenant) if tenant else None)[0]
                        report.add(status, row[0])
                        if tenant is not None:
                            per_tenant.setdefault(tenant, Counter())[status] += 1
            else:
                for rows in _batches(model, entry.field):
                    for pk, value in rows:
                        data = _bytes(value)
                        if data:
                            report.add(self._open(entry.kind, data)[0], pk)
            reports.append(report)
        return ScanResult(reports, per_tenant, tenants)

    @staticmethod
    def _tenant_of(paths: list[tuple[str, str]], row: tuple[Any, ...]) -> TenantRef | None:
        values = iter(row[2:])
        for path, target in paths:
            value = row[0] if path == SELF else next(values)
            if value is not None:
                return (target, str(value))
        return None

    def check_preconditions(self, scan: ScanResult) -> None:
        """Vor jeder Änderung: keine Werte ohne Schlüsselart, keine unlesbaren (außer ausdrücklich erlaubt)."""
        unassigned = [report for report in scan.fields if report.counts[Status.UNASSIGNED]]
        if unassigned:
            names = ", ".join(report.entry.label for report in unassigned)
            raise RotationError(
                f"Felder ohne festgelegte Schlüsselart enthalten Werte: {names}. "
                "Bitte zuerst in apps/common/crypto_registry.py eintragen."
            )
        unreadable = [report for report in scan.fields if report.counts[Status.UNREADABLE]]
        if unreadable and not self.ignore_unreadable:
            details = "; ".join(
                f"{report.entry.label}: {report.counts[Status.UNREADABLE]} (z. B. {', '.join(report.unreadable_ids)})"
                for report in unreadable
            )
            raise RotationError(
                f"Unlesbare Werte gefunden – {details}. Stimmen die Schlüssel? Mit --ignore-unreadable "
                "werden sie unverändert gelassen."
            )

    # -- Stufe 1: Hauptschlüssel-Ebene ----------------------------------------------------------

    def rewrite_master_level(self) -> tuple[int, int]:
        """Alles mit vorherigem Hauptschlüssel (und 2FA-Klartext) auf den aktuellen – in einer Transaktion."""
        rewritten = skipped = 0
        touched: set[type[models.Model]] = set()
        with transaction.atomic():
            for entry in fields_of_kind(*MASTER_LEVEL):
                model = _model(entry.model)
                for rows in _batches(model, entry.field):
                    for pk, value in rows:
                        data = _bytes(value)
                        if not data:
                            continue
                        status, plaintext = self._open(entry.kind, data)
                        if status is Status.UNREADABLE:
                            if not self.ignore_unreadable:
                                raise RotationError(f"{entry.label}: Wert nicht lesbar (Datensatz {pk}).")
                            skipped += 1
                        elif status in (Status.PREVIOUS, Status.PLAINTEXT) and plaintext is not None:
                            rewritten += _store(model, pk, entry.field, data, self._seal(entry.kind, plaintext))
                            touched.add(model)
        for model in touched:
            cache_key = getattr(model, "CACHE_KEY", None)
            if isinstance(cache_key, str):
                cache.delete(cache_key)  # zwischengespeicherte Instanzen tragen noch den alten Geheimtext
        return rewritten, skipped

    # -- Stufe 2 und 3: Mandantenschlüssel ------------------------------------------------------

    def _tenant_entries(self, label: str) -> list[tuple[EncryptedField, list[tuple[str, str]], list[str]]]:
        """Mandantenfelder mit den Pfaden, die bei diesem Mandantenmodell enden."""
        result = []
        for entry in fields_of_kind(KeyKind.TENANT):
            paths = _tenant_paths(entry)
            matching = [path for path, target in paths if target == label]
            if matching:
                result.append((entry, paths, matching))
        return result

    def _reencrypt_tenant(self, label: str, pk: Any) -> tuple[int, int]:
        """Alle noch mit dem vorherigen Schlüssel verschlüsselten Werte eines Mandanten auf den aktuellen."""
        keys = self._load_tenant_keys(label, pk).get((label, str(pk)))
        if keys is None or keys.current is None:
            return 0, 0
        rewritten = skipped = 0
        for entry, _paths, matching in self._tenant_entries(label):
            model = _model(entry.model)
            where = models.Q()
            for path in matching:
                where |= models.Q(**{path: pk})
            for rows in _batches(model, entry.field, where=where):
                for row_pk, value in rows:
                    data = _bytes(value)
                    if not data:
                        continue
                    status, plaintext = self._open_tenant(data, keys)
                    if status is Status.UNREADABLE:
                        if not self.ignore_unreadable:
                            raise RotationError(f"{entry.label}: Wert nicht lesbar (Datensatz {row_pk}).")
                        skipped += 1
                    elif status is Status.PREVIOUS and plaintext is not None:
                        rewritten += _store(model, row_pk, entry.field, data, aes_seal(keys.current, plaintext))
        return rewritten, skipped

    def _selected_tenants(self, *, in_progress: bool | None = None) -> Iterator[tuple[str, Any]]:
        for label, short in TENANT_MODELS.items():
            if short not in self.tenant_types:
                continue
            queryset = _model(label)._base_manager.filter(encryption_key__isnull=False).exclude(encryption_key=b"")
            if in_progress is True:
                queryset = queryset.filter(encryption_key_previous__isnull=False).exclude(encryption_key_previous=b"")
            for pk in list(queryset.order_by("pk").values_list("pk", flat=True)):
                yield label, pk

    def rotate_tenant_keys(self, result: RotationResult) -> None:
        """Neuer Schlüssel je Mandant und alle Inhalte neu verschlüsseln – je Mandant eine Transaktion."""
        for label, pk in self._selected_tenants():
            model = _model(label)
            with transaction.atomic():
                row = (
                    model._base_manager.select_for_update()
                    .filter(pk=pk)
                    .values_list("encryption_key", "encryption_key_previous")
                    .first()
                )
                if row is None:
                    continue
                wrapped, previous = _bytes(row[0]), _bytes(row[1])
                if previous:
                    result.tenants_resumed += 1  # begonnener Wechsel: fortsetzen, kein weiterer neuer Schlüssel
                else:
                    status = self._open_master(wrapped)[0]
                    if status is not Status.CURRENT:
                        # Nach Stufe 1 ist jeder lesbare Schlüssel beim aktuellen Hauptschlüssel
                        if not self.ignore_unreadable:
                            raise RotationError(f"{label}: Mandantenschlüssel nicht lesbar (Datensatz {pk}).")
                        result.skipped_unreadable += 1
                        continue
                    model._base_manager.filter(pk=pk).update(
                        encryption_key=aes_seal(self.master, generate_key()),
                        encryption_key_previous=wrapped,
                    )
                    result.tenants_switched += 1
                rewritten, skipped = self._reencrypt_tenant(label, pk)
                result.tenant_values_rewritten += rewritten
                result.skipped_unreadable += skipped

    def finalize_tenants(self, result: RotationResult) -> None:
        """Nachzügler neu verschlüsseln und den vorherigen Mandantenschlüssel entfernen."""
        for label, pk in self._selected_tenants(in_progress=True):
            model = _model(label)
            with transaction.atomic():
                model._base_manager.select_for_update().filter(pk=pk).values_list("pk").first()
                rewritten, skipped = self._reencrypt_tenant(label, pk)
                model._base_manager.filter(pk=pk).update(encryption_key_previous=None)
            result.tenant_values_rewritten += rewritten
            result.skipped_unreadable += skipped
            result.tenants_finalized += 1

    # -- Gesamtablauf ---------------------------------------------------------------------------

    def run(self, *, master_only: bool = False, finalize: bool = False) -> RotationResult:
        """
        Standard: Hauptschlüssel-Ebene umschreiben, neue Mandantenschlüssel, Inhalte neu verschlüsseln.

        ``master_only``: nur die Hauptschlüssel-Ebene. ``finalize``: statt neuer Mandantenschlüssel
        den begonnenen Wechsel abschließen.
        """
        self.check_preconditions(self.scan())
        result = RotationResult()
        result.master_values_rewritten, result.skipped_unreadable = self.rewrite_master_level()
        if finalize:
            self.finalize_tenants(result)
        elif not master_only:
            self.rotate_tenant_keys(result)
        logger.warning(
            "Schlüsselwechsel ausgeführt",
            extra={
                "mode": "finalize" if finalize else "master_only" if master_only else "full",
                "master_values_rewritten": result.master_values_rewritten,
                "tenants_switched": result.tenants_switched,
                "tenants_resumed": result.tenants_resumed,
                "tenant_values_rewritten": result.tenant_values_rewritten,
                "tenants_finalized": result.tenants_finalized,
                "skipped_unreadable": result.skipped_unreadable,
            },
        )
        return result
