# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gespeicherte KI-Zusammenfassungen der Vorgänge (``OParlPaper.summary``) beim Eigentümer des RIS-Bestands.

Erzeugt werden Zusammenfassungen auf Abruf (``insight_ai.services.summarizer``) und, mit der Claude API, vorab im
Message Batch (``insight_ai.services.zusammenfassung_batch``: Auswahl ``fuer_tagesbatch`` und ``tagesbatch_wartet``,
Übernahme ``speichern``); verworfen werden sie, wenn sich ihre Grundlage ändert: bei Rücknahme einer Anlage
(``SourceDeletionModel._forget_summaries``), im Löschabgleich (``file_reconcile``) und nach neuem Text einer Anlage
(Abonnement ``insight.zusammenfassung``, Issue #919). Abonnement und Batch liegen bei den KI-Funktionen und
schreiben über diese Funktionen, nicht selbst in den Bestand (``scripts/check_ris_access_ratchet.py``).

Mit der Zusammenfassung steht der Beratungsstand, den sie kannte (``OParlPaper.summary_stand``, Prüfsumme aus
``beratungsstand``, Issue #1257). Kommt danach ein Ergebnis hinzu (beschlossen, abgelehnt, vertagt …) oder ändert es
sich, ist sie veraltet (``veraltet``): Die Vorgangsseite sagt das, und der Batch erzeugt sie neu (Auswahl
``veraltete``, Übernahme mit ``speichern(..., ersetzt=...)``). Zusammenfassungen von vor #1257 haben keinen Stand;
sie gelten als veraltet, sobald der Vorgang ein Ergebnis hat.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from ..models import OParlAgendaItem, OParlConsultation, OParlFile, OParlPaper, withdrawn_q

#: Länge des gespeicherten Anbieterhinweises (``OParlPaper.summary_hinweis``)
HINWEIS_LAENGE = 300
#: Länge der Prüfsumme des Beratungsstands (``OParlPaper.summary_stand``)
STAND_LAENGE = 32
#: So viele Vorgänge prüft ``veraltete`` je Abfrage des Beratungsverlaufs
STAND_BLOCK = 500


def discard_for_files(file_ids: Iterable[uuid.UUID], *, dry_run: bool = False) -> int:
    """
    Verwirft die Zusammenfassungen der Vorgänge dieser Dateien; Rückgabe: ihre Zahl (mit ``dry_run`` nur gezählt).

    Dateien ohne Vorgang (Sitzungsdokumente), unbekannte Kennungen und Vorgänge ohne Zusammenfassung zählen nicht.
    Ein ``UPDATE`` je Aufruf; idempotent.
    """
    kennungen = list(file_ids)
    if not kennungen:
        return 0
    vorgaenge = OParlFile.objects.filter(pk__in=kennungen, paper_id__isnull=False).values("paper_id")
    betroffen = OParlPaper.objects.filter(pk__in=vorgaenge, summary__isnull=False)
    if dry_run:
        return betroffen.count()
    return betroffen.update(
        summary=None, summary_hinweis="", summary_einfach=None, summary_stand="", summary_prompt_fassung=0
    )


# =============================================================================
# Beratungsstand der Zusammenfassung (Issue #1257)
# =============================================================================


def _tag(zeitpunkt: datetime | None) -> str:
    if zeitpunkt is None:
        return ""
    return (timezone.localtime(zeitpunkt) if timezone.is_aware(zeitpunkt) else zeitpunkt).date().isoformat()


def beratungsstand(schritte: Iterable[Any], *, now: datetime | None = None) -> str:
    """
    Prüfsumme des öffentlich bekannten Beratungsstands eines Vorgangs; leer, solange kein Ergebnis bekannt ist.

    ``schritte``: Beratungsverlauf nur mit öffentlichen Angaben (``hub.ris.selectors.public_consultation_history``).
    Es zählen die Ergebnisse öffentlicher Tagesordnungspunkte mit Sitzungstag und vergangene nichtöffentliche
    Beratungen (ohne Ergebnis, das ist nicht öffentlich). Termine, Gremiennamen, Reihenfolge und Leerraum zählen
    nicht: Ein neuer Termin macht eine Zusammenfassung nicht falsch, ein Ergebnis schon. Abgesagte Sitzungen zählen
    nicht (wie im Stand-Satz, ``paper_status``).
    """
    jetzt = now or timezone.now()
    zeilen: set[str] = set()
    for schritt in schritte:
        if schritt.cancelled:
            continue
        if schritt.public:
            ergebnis = " ".join(str(schritt.result or "").split()).casefold()
            if ergebnis:
                zeilen.add(f"{_tag(schritt.date)}|{ergebnis}")
        elif schritt.date is not None and schritt.date <= jetzt:
            zeilen.add(f"{_tag(schritt.date)}|nichtöffentlich beraten")
    if not zeilen:
        return ""
    return hashlib.sha256("\n".join(sorted(zeilen)).encode()).hexdigest()[:STAND_LAENGE]


def beratungsstaende(vorgang_ids: Iterable[Any], *, now: datetime | None = None) -> dict[Any, str]:
    """Beratungsstand je Vorgang (``beratungsstand``) in einer Abfrage; Vorgänge ohne Beratung fehlen (Stand leer)."""
    from hub.ris.selectors import public_consultation_history

    return {
        kennung: beratungsstand(schritte, now=now)
        for kennung, schritte in public_consultation_history(vorgang_ids).items()
    }


def veraltet(vorgang: OParlPaper, *, now: datetime | None = None) -> bool:
    """Kennt die gespeicherte Zusammenfassung einen anderen Beratungsstand als den aktuellen? Ohne: ``False``."""
    if not vorgang.summary:
        return False
    return (vorgang.summary_stand or "") != beratungsstaende([vorgang.pk], now=now).get(vorgang.pk, "")


def _mit_moeglichem_stand() -> Q:
    """
    Vorfilter in der Datenbank für ``veraltete``: Vorgänge, die einen Beratungsstand haben können (Beratung mit
    Tagesordnungspunkt mit Ergebnis oder nichtöffentlich) oder einen gespeicherten haben (er kann wegfallen).
    """
    punkte = OParlAgendaItem.objects.filter(
        external_id=OuterRef("agenda_item_external_id"), deleted=False, meeting__deleted=False
    ).filter(Q(public=False) | (Q(result__isnull=False) & ~Q(result="")))
    beratungen = OParlConsultation.objects.filter(paper_id=OuterRef("pk")).exclude(withdrawn_q()).filter(Exists(punkte))
    return Q(Exists(beratungen)) | ~Q(summary_stand="")


def veraltete(
    kommunen: Mapping[Any, date | None] | None = None,
    *,
    limit: int,
    ab: date | None = None,
    now: datetime | None = None,
    ohne: Any = None,
) -> list[OParlPaper]:
    """
    Vorgänge mit veralteter Zusammenfassung (``veraltet``), neueste zuerst, höchstens ``limit``: Tagesbatch und
    ``manage.py zusammenfassungen_nachziehen`` (Issue #1257).

    ``kommunen``: Kennung der Kommune → Stichtag (``None``: ohne Stichtag); ohne Angabe alle Kommunen. ``ab``: nur
    Vorgänge ab diesem Datum. Wie ``fuer_tagesbatch`` nur nicht gelöschte Vorgänge gelisteter Kommunen, deren
    Veröffentlichung weder pausiert noch zurückgenommen ist, und nur mit aktueller Anlage mit Text. Der Stand wird
    blockweise berechnet (``STAND_BLOCK`` Vorgänge je Abfrage des Beratungsverlaufs). ``ohne``: Kennungen von
    Vorgängen, die nicht in Frage kommen (Unterabfrage, etwa mit derselben Prompt-Fassung schon verworfene, #1271).
    """
    from insight_core import publication

    if limit <= 0 or (kommunen is not None and not kommunen):
        return []
    gesperrt = [
        uuid.UUID(kennung) for kennung, stand in publication.states().items() if stand.paused or stand.withdrawn
    ]
    auswahl = OParlPaper.objects.filter(
        summary__isnull=False, deleted=False, body__deleted=False, body__is_listed=True
    ).exclude(body_id__in=gesperrt)
    if kommunen is not None:
        bedingung = Q()
        for kommune, stichtag in kommunen.items():
            bedingung |= Q(body_id=kommune, date__gte=stichtag) if stichtag else Q(body_id=kommune)
        auswahl = auswahl.filter(bedingung)
    if ab is not None:
        auswahl = auswahl.filter(date__gte=ab)
    if ohne is not None:
        auswahl = auswahl.exclude(pk__in=ohne)
    mit_text = _aktuelle_dateien_mit_text().filter(paper_id=OuterRef("pk"))
    auswahl = (
        auswahl.filter(Exists(mit_text))
        .filter(_mit_moeglichem_stand())
        .select_related("body", "body__source")
        .defer("raw_json", "summary_einfach")
        .order_by("-date", "-pk")
    )
    jetzt = now or timezone.now()
    treffer: list[OParlPaper] = []
    block: list[OParlPaper] = []

    def pruefen() -> None:
        staende = beratungsstaende([vorgang.pk for vorgang in block], now=jetzt)
        treffer.extend(vorgang for vorgang in block if (vorgang.summary_stand or "") != staende.get(vorgang.pk, ""))
        block.clear()

    for vorgang in auswahl.iterator(chunk_size=STAND_BLOCK):
        block.append(vorgang)
        if len(block) >= STAND_BLOCK:
            pruefen()
            if len(treffer) >= limit:
                break
    if block and len(treffer) < limit:
        pruefen()
    return treffer[:limit]


def _aktuelle_dateien_mit_text() -> QuerySet[OParlFile]:
    """Anlagen, deren gespeicherter Text in eine Zusammenfassung einfließen darf (wie ``SummaryService``)."""
    return OParlFile.objects.filter(
        deleted=False, source_missing_since__isnull=True, text_content__isnull=False
    ).exclude(text_content="")


def kommune_finden(schluessel: str) -> Any:
    """
    Kommune zu Kennung (UUID) oder eindeutigem Teil des Namens (für ``manage.py tagesbatch``); ``LookupError``,
    wenn keine oder mehrere passen.
    """
    from ..models import OParlBody

    try:
        return OParlBody.objects.get(pk=uuid.UUID(schluessel), deleted=False)
    except (ValueError, OParlBody.DoesNotExist):
        pass
    treffer = list(OParlBody.objects.filter(name__icontains=schluessel, deleted=False)[:5])
    genau = [kommune for kommune in treffer if (kommune.name or "").casefold() == schluessel.casefold()]
    if len(genau) == 1:
        return genau[0]
    if len(treffer) != 1:
        namen = ", ".join(kommune.name or str(kommune.pk) for kommune in treffer) or "keine"
        raise LookupError(f"„{schluessel}“ passt nicht eindeutig: {namen}")
    return treffer[0]


def fuer_tagesbatch(kommunen: Mapping[Any, date], *, limit: int, ohne: Any = None) -> list[OParlPaper]:
    """
    Vorgänge ausgewählter Kommunen für den Tagesbatch (Issue #1220): neueste zuerst, höchstens ``limit``; nur ohne
    Zusammenfassung, nicht gelöscht, aus einer gelisteten Kommune, je Kommune ab ihrem Stichtag (``kommunen``: Kennung
    der Kommune → Datum) und mit mindestens einer aktuellen Anlage mit gespeichertem Text. Kommunen, deren
    Veröffentlichung pausiert oder zurückgenommen ist (``publication``), fallen heraus: Ihre Seiten antworten mit 503
    bzw. 410, also geht auch nichts an einen KI-Anbieter. Ohne Kommunen keine Vorgänge. Ob der Text aller
    Anlagen schon vorliegt, prüft ``tagesbatch_wartet`` je Vorgang. ``ohne``: Kennungen von Vorgängen, die nicht in
    Frage kommen (Unterabfrage, etwa mit derselben Prompt-Fassung schon verworfene, Issue #1271).
    """
    from insight_core import publication

    if not kommunen:
        return []
    gesperrt = [
        uuid.UUID(kennung) for kennung, stand in publication.states().items() if stand.paused or stand.withdrawn
    ]
    auswahl_kommunen = Q()
    for kommune, ab in kommunen.items():
        auswahl_kommunen |= Q(body_id=kommune, date__gte=ab)
    mit_text = _aktuelle_dateien_mit_text().filter(paper_id=OuterRef("pk"))
    auswahl = (
        OParlPaper.objects.filter(auswahl_kommunen)
        .filter(summary__isnull=True, deleted=False, body__deleted=False, body__is_listed=True)
        .filter(Exists(mit_text))
        .exclude(body_id__in=gesperrt)
        .select_related("body", "body__source")
        .order_by("-date", "-pk")
    )
    if ohne is not None:
        auswahl = auswahl.exclude(pk__in=ohne)
    return list(auswahl[: max(0, limit)])


#: Stände der Texterkennung, in denen der Text noch kommt
_TEXT_AUSSTEHEND = ("pending", "processing")
#: Von der Obergrenze des Dokument-Caches verdrängt: der Text kommt nicht von selbst (wie ``SummaryService``)
_VERDRAENGT = "evicted"
#: Fehlertexte eines gescheiterten Abrufs (``hub.ris.erkennung.DOWNLOAD_ERROR_PREFIX``): wird nachgearbeitet
_ABRUFFEHLER = "Download"


def _abruf_wird_nachgearbeitet(paper: OParlPaper) -> bool:
    """Reiht die tägliche Nacharbeit gescheiterte Abrufe dieses Vorgangs noch neu ein (Alter des Vorgangs)?"""
    from datetime import timedelta

    from django.utils import timezone

    from hub.ris.erkennung import NACHARBEIT_TAGE

    tag = paper.date.date() if isinstance(paper.date, datetime) else paper.date
    if tag is None:
        return False
    return tag >= timezone.localdate() - timedelta(days=NACHARBEIT_TAGE)


def tagesbatch_wartet(paper: OParlPaper, *, verortung: bool) -> str:
    """
    Worauf ein Vorgang vor dem Tagesbatch noch wartet; leer: bereit (Texterkennung ist Pflicht, Issue #1220).

    - ``texterkennung``: Die Erkennung einer aktuellen Anlage steht aus oder läuft (verdrängte Anlagen zählen nicht).
    - ``abruf``: Eine Anlage ist am Abruf gescheitert; ``abrufe_nacharbeiten`` reiht sie täglich neu ein, solange der
      Vorgang höchstens ``hub.ris.erkennung.NACHARBEIT_TAGE`` alt ist. Danach hält sie ihn nicht mehr auf.
    - ``verortung``: Mit ``verortung`` muss der automatische Verortungslauf nach dem letzten Text gelaufen sein, damit
      Claude die gefundenen Orte prüft und nicht einen alten Stand. Ist er älter, wird der Vorgang dafür wieder auf
      ``pending`` gesetzt (der Zeitplan ``verortung_automatisch`` holt ihn ab), außer die Orte stammen von der KI
      oder die KI fand keine (``no_locations``): Die ersetzte der automatische Lauf nicht, sie gelten als aktuell.

    Anlagen, die dauerhaft keinen Text liefern (zu groß, nicht unterstützt, unlesbar), halten nichts auf.
    """
    from django.db.models import Max

    dateien = OParlFile.objects.filter(paper=paper, deleted=False, source_missing_since__isnull=True)
    if dateien.filter(text_extraction_status__in=_TEXT_AUSSTEHEND).exclude(local_status=_VERDRAENGT).exists():
        return "texterkennung"
    if _abruf_wird_nachgearbeitet(paper) and (
        dateien.filter(text_extraction_status="failed", text_extraction_error__startswith=_ABRUFFEHLER).exists()
    ):
        return "abruf"
    if not verortung or paper.georef_status == "no_locations" or "ai" in (paper.georef_method or ""):
        return ""
    if paper.georef_status == "pending":
        return "verortung"
    letzter_text = dateien.aggregate(letzter=Max("text_extracted_at"))["letzter"]
    if paper.georef_extracted_at is None or (letzter_text is not None and paper.georef_extracted_at < letzter_text):
        OParlPaper.objects.filter(pk=paper.pk).exclude(georef_status="pending").update(georef_status="pending")
        return "verortung"
    return ""


def abrufe_nacharbeiten(kommunen: Mapping[Any, date]) -> int:
    """
    Am Abruf gescheiterte Anlagen der Kommunen im Tagesbatch zurück in die Dokumentkette
    (``hub.ris.erkennung.nacharbeiten`` mit Auswahl); Rückgabe: Zahl der zurückgesetzten Anlagen. Die KI-Funktionen
    laden selbst nichts (``insight_ai/tests/test_keine_dateien.py``), deshalb liegt der Aufruf hier.
    """
    from hub.ris.erkennung import NACH_ABRUF, NACH_ERKENNUNG, NACH_WARTET, nacharbeiten

    if not kommunen:
        return 0
    gesamt = nacharbeiten(ausfuehren=True, kommunen=kommunen).gesamt()
    return sum(gesamt.get(art, 0) for art in (NACH_ERKENNUNG, NACH_ABRUF, NACH_WARTET))


def vorgaenge(kennungen: Iterable[Any]) -> QuerySet[OParlPaper]:
    """Vorgänge zu diesen Kennungen (etwa aus einem Batch), mit Kommune und Quelle."""
    return OParlPaper.objects.filter(pk__in=list(kennungen)).select_related("body", "body__source")


def stichprobe(kommune_id: Any, *, anzahl: int, startwert: str, bis: date | None = None) -> list[OParlPaper]:
    """
    Feste Stichprobe zur Bewertung einer Prompt-Fassung (Issue #1271, ``manage.py zusammenfassung_stichprobe``):
    ``anzahl`` nicht gelöschte Vorgänge der Kommune mit aktueller Anlage mit Text, mit ``bis`` nur bis zu diesem Datum.

    Gewählt wird nach einer Prüfsumme aus ``startwert`` und Kennung: Dieselben Angaben ergeben dieselbe Stichprobe in
    derselben Reihenfolge. Ein neuer Vorgang kommt nur hinein, wenn seine Prüfsumme kleiner ist; mit ``bis`` nie.
    """
    if anzahl <= 0:
        return []
    mit_text = _aktuelle_dateien_mit_text().filter(paper_id=OuterRef("pk"))
    auswahl = OParlPaper.objects.filter(body_id=kommune_id, deleted=False).filter(Exists(mit_text))
    if bis is not None:
        auswahl = auswahl.filter(date__lte=bis)
    kennungen = sorted(
        auswahl.values_list("pk", flat=True),
        key=lambda kennung: hashlib.sha256(f"{startwert}:{kennung}".encode()).hexdigest(),
    )[:anzahl]
    gefunden = {vorgang.pk: vorgang for vorgang in vorgaenge(kennungen)}
    return [gefunden[kennung] for kennung in kennungen if kennung in gefunden]


def speichern(
    vorgang_id: Any,
    zusammenfassung: str,
    hinweis: str,
    *,
    seit: datetime,
    dateien: Iterable[Any] = (),
    einfach: str = "",
    stand: str = "",
    ersetzt: str | None = None,
    prompt_fassung: int = 0,
) -> bool:
    """
    Speichert eine vorab erstellte Zusammenfassung, wenn sie noch passt; Rückgabe: gespeichert?

    Nicht gespeichert wird, wenn der Vorgang gelöscht ist oder schon eine Zusammenfassung hat, wenn eine der
    verwendeten Anlagen (``dateien``) inzwischen gelöscht oder in der Quelle verschwunden ist oder wenn seit ``seit``
    (Beginn der Erstellung) neuer Text zu einer Anlage vorliegt. Der Vorgang ist dabei gesperrt: Eine gleichzeitige
    Rücknahme wartet und verwirft die Zusammenfassung danach (``_forget_summaries``).

    ``stand``: Beratungsstand, den die neue Zusammenfassung kennt (``beratungsstand``). ``ersetzt``: Sie ersetzt eine
    veraltete Zusammenfassung mit diesem Stand (Issue #1257). Gespeichert wird dann nur, wenn die vorhandene noch
    diesen Stand hat (sonst hat sie inzwischen jemand neu erstellt) oder keine mehr da ist. ``prompt_fassung``:
    Fassung des Prompts, mit der sie entstand (Issue #1271).
    """
    datei_ids = [kennung for kennung in dateien if kennung]
    with transaction.atomic():
        vorgang = (
            OParlPaper.objects.select_for_update()
            .filter(pk=vorgang_id)
            .only("pk", "deleted", "summary", "summary_stand")
            .first()
        )
        if vorgang is None or vorgang.deleted:
            return False
        if vorgang.summary is not None and (ersetzt is None or (vorgang.summary_stand or "") != ersetzt):
            return False
        if (
            datei_ids
            and OParlFile.objects.filter(pk__in=datei_ids)
            .filter(Q(deleted=True) | Q(source_missing_since__isnull=False))
            .exists()
        ):
            return False
        if OParlFile.objects.filter(
            paper_id=vorgang_id, text_extraction_status="completed", text_extracted_at__gt=seit
        ).exists():
            return False
        OParlPaper.objects.filter(pk=vorgang_id).update(
            summary=zusammenfassung,
            summary_hinweis=(hinweis or "")[:HINWEIS_LAENGE],
            summary_einfach=einfach or None,
            summary_stand=(stand or "")[:STAND_LAENGE],
            summary_prompt_fassung=max(0, int(prompt_fassung or 0)),
        )
    return True
