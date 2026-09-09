# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schema-Contract zwischen Django (``insight_core.models``) und dem Ingestor
(``ingestor/src/storage/models.py``, SQLAlchemy).

Beide Seiten beschreiben dieselben Tabellen unabhängig voneinander. Die Datenbank wird von
Django-Migrationen erzeugt; der Ingestor schreibt per SQLAlchemy hinein. Dieses Modul normalisiert
beide Beschreibungen auf ein gemeinsames Format und meldet Abweichungen, die den Ingestor zur
Laufzeit brechen würden (fehlende Spalten, NOT-NULL-Konflikte, zu kurze Längen, andere Typfamilien).

Verwendung: ``scripts/check_schema_contract.py`` (CI) und ``insight_core/tests/test_schema_contract.py``.
Entscheidung: ``docs/adr/20260909-schema-contract-django-ingestor.md``.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Typfamilien, wie sie in der Datenbank ankommen. "string"/"text" und "integer"/"bigint" sind
# untereinander verträglich (nur Warnung), alles andere ist ein harter Unterschied.
COMPATIBLE_KINDS: dict[str, set[str]] = {
    "string": {"string", "text"},
    "text": {"string", "text"},
    "integer": {"integer", "bigint"},
    "bigint": {"integer", "bigint"},
}

DJANGO_KINDS: dict[str, str] = {
    "UUIDField": "uuid",
    "CharField": "string",
    "SlugField": "string",
    "EmailField": "string",
    "URLField": "string",
    "TextField": "text",
    "IntegerField": "integer",
    "PositiveIntegerField": "integer",
    "PositiveSmallIntegerField": "integer",
    "SmallIntegerField": "integer",
    "AutoField": "integer",
    "BigAutoField": "bigint",
    "BigIntegerField": "bigint",
    "PositiveBigIntegerField": "bigint",
    "BooleanField": "boolean",
    "DateTimeField": "datetime",
    "DateField": "date",
    "TimeField": "time",
    "JSONField": "json",
    "FloatField": "float",
    "DecimalField": "decimal",
    "FileField": "string",
    "ImageField": "string",
    "BinaryField": "binary",
    "DurationField": "duration",
}


@dataclass(frozen=True)
class ColumnSpec:
    """Normalisierte Spaltenbeschreibung, unabhängig vom ORM."""

    name: str
    kind: str
    nullable: bool
    length: int | None = None
    fk_table: str | None = None
    primary_key: bool = False
    has_db_default: bool = False


TableSpec = dict[str, ColumnSpec]
SchemaSpec = dict[str, TableSpec]


@dataclass
class Finding:
    level: str  # "error" | "warning" | "info"
    table: str
    column: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level.upper():7}] {self.table}.{self.column}: {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    compared_tables: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Django-Seite
# ---------------------------------------------------------------------------


def django_schema(app_label: str = "insight_core") -> SchemaSpec:
    """Liest alle konkreten Modelle (inkl. automatischer M2M-Zwischentabellen) einer App."""
    from django.apps import apps
    from django.db.models.fields import NOT_PROVIDED

    schema: SchemaSpec = {}
    for model in apps.get_app_config(app_label).get_models(include_auto_created=True):
        opts = model._meta
        if opts.abstract or opts.proxy:
            continue
        table: TableSpec = {}
        for f in opts.concrete_fields:
            if f.many_to_many or f.column is None:
                continue
            column: str = f.column
            internal = f.get_internal_type()
            fk_table: str | None = None
            if f.is_relation and f.remote_field is not None and hasattr(f, "target_field"):
                internal = f.target_field.get_internal_type()
                fk_table = f.remote_field.model._meta.db_table
            kind = DJANGO_KINDS.get(internal, "unknown")
            db_default = getattr(f, "db_default", NOT_PROVIDED)
            table[column] = ColumnSpec(
                name=column,
                kind=kind,
                nullable=bool(f.null),
                length=getattr(f, "max_length", None) if kind == "string" else None,
                fk_table=fk_table,
                primary_key=bool(f.primary_key),
                has_db_default=db_default is not NOT_PROVIDED,
            )
        schema[opts.db_table] = table
    return schema


# ---------------------------------------------------------------------------
# SQLAlchemy-Seite (Ingestor)
# ---------------------------------------------------------------------------


def _sa_kind(sa_type: Any) -> tuple[str, int | None]:
    import sqlalchemy as sa

    if isinstance(sa_type, sa.Uuid):
        return "uuid", None
    if isinstance(sa_type, sa.Text):
        return "text", None
    if isinstance(sa_type, sa.String):
        return "string", getattr(sa_type, "length", None)
    if isinstance(sa_type, sa.BigInteger):
        return "bigint", None
    if isinstance(sa_type, sa.SmallInteger | sa.Integer):
        return "integer", None
    if isinstance(sa_type, sa.Boolean):
        return "boolean", None
    if isinstance(sa_type, sa.DateTime):
        return "datetime", None
    if isinstance(sa_type, sa.Date):
        return "date", None
    if isinstance(sa_type, sa.Time):
        return "time", None
    if isinstance(sa_type, sa.JSON):
        return "json", None
    if isinstance(sa_type, sa.Float):
        return "float", None
    if isinstance(sa_type, sa.Numeric):
        return "decimal", None
    if isinstance(sa_type, sa.LargeBinary):
        return "binary", None
    if isinstance(sa_type, sa.Interval):
        return "duration", None
    return "unknown", None


def sqlalchemy_schema(ingestor_dir: Path) -> SchemaSpec:
    """Lädt ``src/storage/models.py`` des Ingestors als eigenständiges Modul und liest ``Base.metadata``.

    Die Datei wird direkt geladen (ohne das Paket ``src.storage``), damit nur ``sqlalchemy`` gebraucht
    wird und nicht die komplette Ingestor-Umgebung (asyncpg, mandari_oparl, …).
    """
    models_path = Path(ingestor_dir).resolve() / "src" / "storage" / "models.py"
    module_spec = importlib.util.spec_from_file_location("ingestor_storage_models", models_path)
    if module_spec is None or module_spec.loader is None:
        raise FileNotFoundError(f"Ingestor-Modelle nicht gefunden: {models_path}")
    models = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = models
    module_spec.loader.exec_module(models)
    metadata = models.Base.metadata

    schema: SchemaSpec = {}
    for table in metadata.tables.values():
        spec: TableSpec = {}
        for col in table.columns:
            kind, length = _sa_kind(col.type)
            fk_table = next((fk.column.table.name for fk in col.foreign_keys), None)
            spec[col.name] = ColumnSpec(
                name=col.name,
                kind=kind,
                nullable=bool(col.nullable),
                length=length,
                fk_table=fk_table,
                primary_key=bool(col.primary_key),
                has_db_default=col.server_default is not None,
            )
        schema[table.name] = spec
    return schema


# ---------------------------------------------------------------------------
# Vergleich
# ---------------------------------------------------------------------------


def compare(django: SchemaSpec, ingestor: SchemaSpec) -> Report:
    """Vergleicht alle Tabellen, die der Ingestor kennt, mit der Django-Definition."""
    report = Report()
    for table_name, sa_table in sorted(ingestor.items()):
        dj_table = django.get(table_name)
        if dj_table is None:
            report.findings.append(
                Finding("error", table_name, "*", "Tabelle ist im Ingestor definiert, aber nicht in Django")
            )
            continue
        report.compared_tables.append(table_name)

        for name, sa_col in sorted(sa_table.items()):
            dj_col = dj_table.get(name)
            if dj_col is None:
                report.findings.append(
                    Finding(
                        "error",
                        table_name,
                        name,
                        "Spalte existiert im Ingestor, aber nicht in Django (fehlt in der DB)",
                    )
                )
                continue
            _compare_column(report, table_name, dj_col, sa_col)

        for name, dj_col in sorted(dj_table.items()):
            if name in sa_table:
                continue
            if dj_col.nullable or dj_col.has_db_default or dj_col.primary_key:
                report.findings.append(
                    Finding(
                        "info",
                        table_name,
                        name,
                        "Django-Spalte, die der Ingestor nicht kennt (nullable/Default – unkritisch)",
                    )
                )
            else:
                report.findings.append(
                    Finding(
                        "error",
                        table_name,
                        name,
                        "NOT-NULL-Spalte ohne DB-Default nur in Django – Ingestor-INSERTs würden scheitern",
                    )
                )
    return report


def _compare_column(report: Report, table: str, dj: ColumnSpec, sa: ColumnSpec) -> None:
    name = dj.name
    if dj.kind != sa.kind:
        compatible = sa.kind in COMPATIBLE_KINDS.get(dj.kind, set())
        if dj.kind == "string" and sa.kind == "text":
            # Django legt varchar(n) an, der Ingestor darf beliebig lange Werte schreiben → DB-Fehler
            report.findings.append(
                Finding(
                    "error",
                    table,
                    name,
                    f"Ingestor Text gegen Django varchar({dj.length}) – zu lange Werte scheitern in der DB",
                )
            )
        else:
            level = "warning" if compatible else "error"
            report.findings.append(
                Finding(level, table, name, f"Typfamilie unterschiedlich: Django={dj.kind}, Ingestor={sa.kind}")
            )

    if not dj.nullable and sa.nullable and not dj.primary_key:
        report.findings.append(
            Finding("error", table, name, "Django NOT NULL, Ingestor nullable – Ingestor könnte NULL schreiben")
        )
    elif dj.nullable and not sa.nullable:
        report.findings.append(
            Finding("warning", table, name, "Django nullable, Ingestor NOT NULL (strenger, unkritisch)")
        )

    if dj.length is not None and sa.length is not None and dj.length != sa.length:
        level = "error" if sa.length > dj.length else "warning"
        report.findings.append(
            Finding(level, table, name, f"Länge unterschiedlich: Django={dj.length}, Ingestor={sa.length}")
        )

    if (dj.fk_table or sa.fk_table) and dj.fk_table != sa.fk_table:
        report.findings.append(
            Finding(
                "error",
                table,
                name,
                f"Fremdschlüsselziel unterschiedlich: Django={dj.fk_table}, Ingestor={sa.fk_table}",
            )
        )


def format_report(report: Report) -> str:
    lines = [f"Schema-Contract Django ↔ Ingestor: {len(report.compared_tables)} Tabellen verglichen"]
    for finding in report.findings:
        if finding.level != "info":
            lines.append(str(finding))
    infos = [f for f in report.findings if f.level == "info"]
    if infos:
        lines.append(f"({len(infos)} Django-Spalten, die der Ingestor nicht kennt – unkritisch)")
    lines.append(
        f"Ergebnis: {len(report.errors)} Fehler, {len(report.warnings)} Warnungen"
        + ("" if report.ok else " – Contract verletzt")
    )
    return "\n".join(lines)
