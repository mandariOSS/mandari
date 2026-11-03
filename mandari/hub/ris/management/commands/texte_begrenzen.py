# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl ``texte_begrenzen``: gespeicherte Texte über der Obergrenze der Texterkennung finden und kürzen.

    python manage.py texte_begrenzen [--ausfuehren] [--stapel 10]

Die Texterkennung speichert je Datei höchstens ``MAX_TEXT_CHARS`` Zeichen (``mandari_dokumente``). Texte aus der Zeit
davor können länger sein. Der Befehl prüft ``oparl_files`` (RIS-Bestand) und ``session_files`` (Anlagen in Session).

- Standard ist der Probelauf: Er nennt je Tabelle die Zahl und jede betroffene Datei mit Kennung und Länge, ändert
  aber nichts.
- ``--ausfuehren`` kürzt sie stapelweise (``--stapel`` Dateien je Transaktion) in der Datenbank auf ``MAX_TEXT_CHARS``
  Zeichen. Die Bedingung „länger als die Grenze“ steht im ``UPDATE`` selbst: Eine inzwischen neu erkannte Datei
  bleibt unberührt. RIS-Dateien bekommen denselben Vermerk wie bei der Texterkennung in ``text_extraction_error``;
  ``updated_at`` zeigt die Änderung. Danach schreibt der Befehl jede gekürzte RIS-Datei über den üblichen Weg neu in
  den Suchindex (Signal ``post_save`` wie bei der Texterkennung, ``insight_core.signals.index_file``), eine nach der
  anderen. Anlagen in Session stehen nicht im Suchindex.

Vorfilter ist die Länge in Bytes (``octet_length``: PostgreSQL liest sie, ohne die gespeicherten Werte zu entpacken);
die Länge in Zeichen wird danach nur für diese Dateien bestimmt. Idempotent: Gekürzte Texte liegen an der Grenze und
werden nicht erneut angefasst.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandParser
from django.db import router, transaction
from django.db.models import Case, F, Func, IntegerField, Model, Q, TextField, Value, When
from django.db.models.functions import Concat, Length, Substr
from django.db.models.signals import post_save
from django.utils import timezone
from mandari_dokumente import MAX_TEXT_CHARS, truncation_note

from insight_core.models import OParlFile

#: Felder, die das Kürzen ändert (für die Empfänger von ``post_save``, wie bei der Texterkennung)
GEAENDERT = frozenset({"text_content", "text_extraction_error", "updated_at"})


class _Bytes(Func):
    """Länge in Bytes: PostgreSQL ``octet_length`` (ohne Entpacken), SQLite über ``CAST … AS BLOB``."""

    function = "OCTET_LENGTH"
    output_field = IntegerField()

    def as_sqlite(self, compiler: Any, connection: Any, **extra_context: Any) -> Any:
        return self.as_sql(compiler, connection, template="LENGTH(CAST(%(expressions)s AS BLOB))", **extra_context)


def _modelle() -> list[tuple[str, type[Model]]]:
    # Session über die Registry: Die Drehscheibe importiert das Fachmodul nicht
    return [("oparl_files", OParlFile), ("session_files", django_apps.get_model("session", "SessionFile"))]


def _ueber_der_grenze(model: type[Model]) -> list[tuple[Any, int]]:
    """(Kennung, Zeichen) der Dateien mit mehr als ``MAX_TEXT_CHARS`` Zeichen Text, längste zuerst."""
    # Vorfilter in Bytes: Mehr Zeichen als die Grenze hat nur, wer mindestens so viele Bytes hat
    kandidaten = list(
        model._default_manager.alias(laenge_bytes=_Bytes("text_content"))
        .filter(laenge_bytes__gt=MAX_TEXT_CHARS)
        .values_list("pk", flat=True)
    )
    if not kandidaten:
        return []
    zeilen = (
        model._default_manager.filter(pk__in=kandidaten)
        .annotate(zeichen=Length("text_content"))
        .filter(zeichen__gt=MAX_TEXT_CHARS)
        .order_by("-zeichen")
        .values_list("pk", "zeichen")
    )
    return [(pk, int(zeichen)) for pk, zeichen in zeilen]


def _stapel(werte: Sequence[Any], groesse: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(werte), groesse):
        yield werte[start : start + groesse]


def _kuerzen(model: type[Model], kennungen: Sequence[Any]) -> list[Any]:
    """
    Texte der Dateien in der Datenbank auf ``MAX_TEXT_CHARS`` Zeichen kürzen, nur solange sie länger sind (Bedingung
    im ``UPDATE``). Rückgabe: Kennungen der geänderten Dateien.
    """
    werte: dict[str, Any] = {"text_content": Substr("text_content", 1, MAX_TEXT_CHARS), "updated_at": timezone.now()}
    if model is OParlFile:
        vermerk = truncation_note(MAX_TEXT_CHARS)
        werte["text_extraction_error"] = Case(
            When(Q(text_extraction_error__isnull=True) | Q(text_extraction_error=""), then=Value(vermerk)),
            default=Concat(F("text_extraction_error"), Value(f"; {vermerk}")),
            output_field=TextField(),
        )
    zu_lang = (
        model._default_manager.filter(pk__in=list(kennungen))
        .alias(zeichen=Length("text_content"))
        .filter(zeichen__gt=MAX_TEXT_CHARS)
    )
    with transaction.atomic():
        # Zeilen bis zum Ende der Transaktion gesperrt; das UPDATE prüft die Bedingung selbst noch einmal
        geaendert = list(zu_lang.select_for_update().values_list("pk", flat=True))
        if geaendert:
            zu_lang.filter(pk__in=geaendert).update(**werte)
    return geaendert


def _neu_indexieren(kennungen: Sequence[Any]) -> None:
    """Gekürzte RIS-Dateien über das Signal der Texterkennung neu in den Suchindex schreiben, eine nach der anderen."""
    for pk in kennungen:
        datei = OParlFile.objects.filter(pk=pk).first()
        if datei is None:
            continue
        post_save.send(
            sender=OParlFile,
            instance=datei,
            created=False,
            update_fields=GEAENDERT,
            raw=False,
            using=router.db_for_write(OParlFile),
        )


class Command(BaseCommand):
    help = "Gespeicherte Texte über der Obergrenze der Texterkennung zählen (Standard) oder kürzen (--ausfuehren)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--ausfuehren", action="store_true", help="Texte kürzen (sonst nur zählen)")
        parser.add_argument("--stapel", type=int, default=10, help="Dateien je Transaktion (Standard 10)")

    def handle(self, *args: Any, **options: Any) -> None:
        ausfuehren = bool(options["ausfuehren"])
        stapel = max(1, int(options["stapel"]))
        grenze = f"{MAX_TEXT_CHARS:,}".replace(",", ".")
        self.stdout.write(f"Obergrenze: {grenze} Zeichen je Datei{'' if ausfuehren else ' (Probelauf)'}")
        for tabelle, model in _modelle():
            betroffen = _ueber_der_grenze(model)
            self.stdout.write(f"{tabelle}: {len(betroffen)} Dateien über der Obergrenze")
            for pk, zeichen in betroffen:
                self.stdout.write(f"  {pk}: {zeichen:,} Zeichen".replace(",", "."))
            if not ausfuehren or not betroffen:
                continue
            gekuerzt: list[Any] = []
            for teil in _stapel([pk for pk, _ in betroffen], stapel):
                gekuerzt += _kuerzen(model, teil)
            self.stdout.write(self.style.SUCCESS(f"{tabelle}: {len(gekuerzt)} Texte gekürzt"))
            if model is OParlFile and gekuerzt:
                _neu_indexieren(gekuerzt)
                self.stdout.write(f"{tabelle}: {len(gekuerzt)} Dateien im Suchindex neu geschrieben")
        if not ausfuehren:
            self.stdout.write("Nichts geändert. Kürzen mit --ausfuehren.")
