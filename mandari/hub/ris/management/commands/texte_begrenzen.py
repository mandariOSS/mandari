# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``texte_begrenzen``: gespeicherte Texte über der Obergrenze der Texterkennung finden und kürzen.

    python manage.py texte_begrenzen [--ausfuehren] [--stapel 100]

Die Texterkennung speichert je Datei höchstens ``MAX_TEXT_CHARS`` Zeichen (``mandari_dokumente``). Texte aus der Zeit
davor können länger sein. Der Befehl prüft ``oparl_files`` (RIS-Bestand) und ``session_files`` (Anlagen in Session).

- Standard ist der Probelauf: Er zählt die betroffenen Dateien und nennt Kennung und Länge, ändert aber nichts.
- ``--ausfuehren`` kürzt sie stapelweise (``--stapel`` Dateien je Transaktion) auf ``MAX_TEXT_CHARS`` Zeichen. RIS-Dateien
  bekommen denselben Vermerk wie bei der Texterkennung in ``text_extraction_error``; ``updated_at`` zeigt die Änderung.

Vorfilter ist die Länge in Bytes (``octet_length``: PostgreSQL liest sie, ohne die gespeicherten Werte zu entpacken);
die Länge in Zeichen wird danach nur für diese Dateien bestimmt. Idempotent: Gekürzte Texte liegen an der Grenze und
werden nicht erneut angefasst. Der Suchindex folgt nicht von selbst; betroffene Dateien bei Bedarf neu indexieren.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from django.db.models import Case, F, Func, IntegerField, Model, Q, TextField, Value, When
from django.db.models.functions import Concat, Length, Substr
from django.utils import timezone
from mandari_dokumente import MAX_TEXT_CHARS, truncation_note

from insight_core.models import OParlFile

#: So viele Kennungen mit Länge nennt der Probelauf je Tabelle höchstens
ZEIGEN: int = 50


class _Bytes(Func):
    """Länge in Bytes: PostgreSQL ``octet_length`` (ohne Entpacken), SQLite über ``CAST … AS BLOB``."""

    function = "OCTET_LENGTH"
    output_field = IntegerField()

    def as_sqlite(self, compiler: Any, connection: Any, **extra_context: Any) -> Any:
        return self.as_sql(compiler, connection, template="LENGTH(CAST(%(expressions)s AS BLOB))", **extra_context)


def _modelle() -> list[tuple[str, type[Model]]]:
    # Session über die Registry: Die Drehscheibe importiert das Fachmodul nicht
    return [("oparl_files", OParlFile), ("session_files", django_apps.get_model("session", "SessionFile"))]


def _ueber_der_grenze(model: type[Model], kennungen: Sequence[Any] | None = None) -> list[tuple[Any, int]]:
    """(Kennung, Zeichen) der Dateien mit mehr als ``MAX_TEXT_CHARS`` Zeichen Text, längste zuerst."""
    abfrage = model._default_manager.all()
    if kennungen is None:
        # Vorfilter in Bytes: Mehr Zeichen als die Grenze hat nur, wer mindestens so viele Bytes hat
        abfrage = abfrage.alias(laenge_bytes=_Bytes("text_content")).filter(laenge_bytes__gt=MAX_TEXT_CHARS)
        kennungen = list(abfrage.values_list("pk", flat=True))
        if not kennungen:
            return []
        abfrage = model._default_manager.all()
    zeilen = (
        abfrage.filter(pk__in=list(kennungen))
        .annotate(zeichen=Length("text_content"))
        .filter(zeichen__gt=MAX_TEXT_CHARS)
        .order_by("-zeichen")
        .values_list("pk", "zeichen")
    )
    return [(pk, int(zeichen)) for pk, zeichen in zeilen]


def _stapel(werte: Sequence[Any], groesse: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(werte), groesse):
        yield werte[start : start + groesse]


def _kuerzen(model: type[Model], kennungen: Sequence[Any]) -> int:
    """Texte der Dateien auf ``MAX_TEXT_CHARS`` Zeichen kürzen (in der Datenbank); Rückgabe: geänderte Zeilen."""
    werte: dict[str, Any] = {"text_content": Substr("text_content", 1, MAX_TEXT_CHARS), "updated_at": timezone.now()}
    if model is OParlFile:
        vermerk = truncation_note(MAX_TEXT_CHARS)
        werte["text_extraction_error"] = Case(
            When(Q(text_extraction_error__isnull=True) | Q(text_extraction_error=""), then=Value(vermerk)),
            default=Concat(F("text_extraction_error"), Value(f"; {vermerk}")),
            output_field=TextField(),
        )
    with transaction.atomic():
        # Erneut prüfen: Seit der Zählung kann eine Datei neu erkannt worden sein
        noch = [pk for pk, _zeichen in _ueber_der_grenze(model, kennungen)]
        return model._default_manager.filter(pk__in=noch).update(**werte) if noch else 0


class Command(BaseCommand):
    help = "Gespeicherte Texte über der Obergrenze der Texterkennung zählen (Standard) oder kürzen (--ausfuehren)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--ausfuehren", action="store_true", help="Texte kürzen (sonst nur zählen)")
        parser.add_argument("--stapel", type=int, default=100, help="Dateien je Transaktion (Standard 100)")

    def handle(self, *args: Any, **options: Any) -> None:
        ausfuehren = bool(options["ausfuehren"])
        stapel = max(1, int(options["stapel"]))
        grenze = f"{MAX_TEXT_CHARS:,}".replace(",", ".")
        self.stdout.write(f"Obergrenze: {grenze} Zeichen je Datei{'' if ausfuehren else ' (Probelauf)'}")
        for tabelle, model in _modelle():
            betroffen = _ueber_der_grenze(model)
            self.stdout.write(f"{tabelle}: {len(betroffen)} Dateien über der Obergrenze")
            for pk, zeichen in betroffen[:ZEIGEN]:
                self.stdout.write(f"  {pk}: {zeichen:,} Zeichen".replace(",", "."))
            if len(betroffen) > ZEIGEN:
                self.stdout.write(f"  … und {len(betroffen) - ZEIGEN} weitere")
            if not ausfuehren or not betroffen:
                continue
            gekuerzt = sum(_kuerzen(model, teil) for teil in _stapel([pk for pk, _ in betroffen], stapel))
            self.stdout.write(self.style.SUCCESS(f"{tabelle}: {gekuerzt} Texte gekürzt"))
        if not ausfuehren:
            self.stdout.write("Nichts geändert. Kürzen mit --ausfuehren.")
