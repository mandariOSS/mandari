# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Summary service for OParl documents.

Generates AI-powered multi-perspective summaries of municipal documents.

Der Dienst liest nur den gespeicherten Text der Anlagen; er lädt keine Dateien und erkennt keinen Text
(Issue #919, ADR Dokumentkette, Abschnitt 9; Prüfung ``insight_ai/tests/test_keine_dateien.py``). Fehlt der
Text, weil die Erkennung der Anlagen noch aussteht, meldet er das (``TextPendingError``, die Ansicht zeigt einen
Hinweis) und plant die Erkennung dieser Anlagen mit Vorrang ein – aber nur, wenn Aufträge im Worker laufen
(``TASKS_BACKEND=journal``) und der Worker auch den Text erkennt (``TEXT_EXTRACTION_RUNNER=worker``). Mit dem
sofort ausführenden Backend liefe die Erkennung sonst in der Webanfrage; mit ``ingestor`` erkennt der OCR-Worker
des Ingestors den Text, ein Auftrag wäre ein zweiter Weg zur Quelle.

Neuer Text verwirft eine gespeicherte Zusammenfassung (Abonnement ``insight.zusammenfassung``,
``insight_ai/abonnement.py``); erzeugt wird sie auf Abruf und, mit der Claude API, vorab im Message Batch
(``insight_ai.services.zusammenfassung_batch``, gleicher Prompt über ``vorbereiten``). Kommt während der Erstellung
neuer Text hinzu, wird das Ergebnis ausgegeben, aber nicht gespeichert. Der Prompt enthält den Beratungsverlauf;
dessen Stand wird mitgespeichert, damit ein späteres Ergebnis die Zusammenfassung als veraltet erkennen lässt
(``insight_core.services.summary_store.veraltet``, Issue #1257).

Vor dem Speichern prüft ein Programm jede Zusammenfassung gegen das Material des Prompts (Issue #1271,
``zusammenfassung_pruefung``): Zahlen, Daten, Beträge und Fraktionen müssen dort stehen, längere wörtliche Übernahmen
und Hausnummern führen zum Verwerfen. Eine verworfene Zusammenfassung wird protokolliert und nicht angezeigt
(``SummaryRejectedError``); fällt nur die Fassung in Einfacher Sprache durch, entfällt nur sie. Gespeichert wird mit
der Prompt-Fassung (``prompts.PROMPT_FASSUNG``).

Den Anbieter liefert ``get_summary_provider()``: nur für die Zusammenfassungen ggf. die Claude API, sonst der
Endpunkt des Bürgerportals. Mit der Zusammenfassung wird gespeichert, wer sie erstellt hat
(``OParlPaper.summary_hinweis``: Anbieter, Modell, Verarbeitungsort), damit der Hinweis darunter stimmt.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from django.conf import settings
from django.db.models.functions import Substr
from django.utils import timezone

from apps.common.db_connections import release_idle_thread_connections
from insight_ai.providers import get_summary_provider
from insight_ai.providers.base import STANDARD_MAX_AUSGABE, ChatMessage
from insight_ai.providers.claude import BudgetErschoepftError

from . import zusammenfassung_pruefung
from .prompts import (
    EINFACH_SYSTEM_ZUSATZ,
    PAPER_SUMMARY_SYSTEM_PROMPT,
    PROMPT_FASSUNG,
    TAGESBATCH_SCHEMA,
    VERORTUNG_SYSTEM_ZUSATZ,
    ZUSAMMENFASSUNG_SCHEMA,
    anlage_block,
    beratungsverlauf_block,
    build_paper_summary_user_prompt,
    orte_block,
)

if TYPE_CHECKING:
    from insight_core.models import OParlPaper

logger = logging.getLogger(__name__)

#: Höchstens so viele Anlagen je Anfrage werden mit Vorrang zur Texterkennung eingeplant (Last der Warteschlange ocr)
MAX_PRIORITY_EXTRACTIONS: Final = 3
#: Priorität dieser Aufträge: vor denen aus Zeitplan und Ereignissen (Standard 0), damit der Text bald vorliegt
EXTRACTION_PRIORITY: Final = 80
#: Stände der Texterkennung, in denen der Text noch kommen kann
TEXT_PENDING_STATUSES: Final = ("pending", "processing")
#: Schalter der Quelle für den Dateiabruf (gleich ``file_cache.FILE_DOWNLOADS_KEY``): ``false`` = nie abrufen
FILE_DOWNLOADS_KEY: Final = "file_downloads"
#: Von der Obergrenze des Dokument-Caches verdrängt (#961, gleich ``file_cache_limit.EVICTED``): niemand holt die
#: Anlage von selbst, ihr Text kommt erst nach einem ausdrücklichen Abruf (Vorschau, ``cache_files --verdraengte``)
EVICTED: Final = "evicted"
#: Text aller Anlagen zusammen höchstens so viele Zeichen (rund 60.000 Token); jede Anlage bekommt denselben Anteil
TEXT_BUDGET_CHARS: Final = 200_000
#: Zusätzliche Ausgabe-Token für die Fassung in Einfacher Sprache
ZUSATZ_AUSGABE_EINFACH: Final = 800


def zerlegen(antwort: str, *, json_pflicht: bool = False) -> tuple[str, str]:
    """
    Strukturierte Antwort (``ZUSAMMENFASSUNG_SCHEMA``, ``TAGESBATCH_SCHEMA``) in Zusammenfassung und Fassung in
    Einfacher Sprache; reiner Text (ältere Anfragen, andere Anbieter) bleibt die Zusammenfassung, außer die Anfrage
    verlangte JSON (``json_pflicht``). Unlesbar: leer.
    """
    import json

    text = (antwort or "").strip()
    if not text.startswith("{"):
        return ("", "") if json_pflicht else (text, "")
    try:
        daten = json.loads(text)
    except ValueError:
        return "", ""
    if not isinstance(daten, dict):
        return "", ""
    return str(daten.get("zusammenfassung") or "").strip(), str(daten.get("einfache_sprache") or "").strip()


class SummaryError(Exception):
    """Base exception for summary generation errors."""

    pass


class NoTextContentError(SummaryError):
    """Raised when no text content is available for summarization."""

    pass


class APINotConfiguredError(SummaryError):
    """Raised when the AI API is not properly configured."""

    pass


class SummaryRevokedError(SummaryError):
    """Raised when the paper or one of its files was withdrawn while the summary was generated."""

    pass


class SummaryRejectedError(SummaryError):
    """Die Zusammenfassung hat die mechanische Prüfung nicht bestanden (``zusammenfassung_pruefung``)."""

    def __init__(self, befund: zusammenfassung_pruefung.Befund) -> None:
        super().__init__(befund.grund)
        self.befund = befund


class TextPendingError(SummaryError):
    """Noch kein Text, aber die Texterkennung mindestens einer Anlage steht aus: später erneut versuchen."""


class BudgetExhaustedError(SummaryError):
    """Das Monatsbudget der Zusammenfassungen (Claude API) ist ausgeschöpft."""


@dataclass(frozen=True)
class Vorbereitung:
    """Prompt einer Zusammenfassung aus dem gespeicherten Text, mit den verwendeten Anlagen."""

    nachrichten: list[ChatMessage]
    datei_ids: list[Any]
    #: Zeichen aller Nachrichten zusammen (Schätzung der Kosten vor einem Batch)
    zeichen: int
    #: Mit Prüfung der Verortung (Tagesbatch, Issue #1222): Kennungen der Orte in der Reihenfolge des Prompts
    orte_ids: list[str] | None = None
    #: Antwortformat (JSON-Schema) bei Prüfung der Verortung; ``None``: Text
    schema: dict[str, Any] | None = None
    #: Beratungsstand, den der Prompt kennt (``summary_store.beratungsstand``, Issue #1257); wird mitgespeichert
    stand: str = ""
    #: Material der Prüfung (Issue #1271): Nutzerprompt mit Metadaten, Beratungsverlauf und Dokumenten
    quelle: str = ""
    #: Angaben, die wörtlich in der Zusammenfassung stehen dürfen (Titel, Gremien, Kommune)
    erlaubt: tuple[str, ...] = ()

    def pruefquelle(self) -> zusammenfassung_pruefung.Quelle:
        """Quelle der mechanischen Prüfung (``zusammenfassung_pruefung.pruefen``)."""
        return zusammenfassung_pruefung.quelle_aus(self.quelle, self.erlaubt)


@dataclass(frozen=True)
class Entwurf:
    """Antwort der KI auf eine ``Vorbereitung``, geprüft, aber noch nicht gespeichert."""

    zusammenfassung: str
    einfach: str
    pruefung: zusammenfassung_pruefung.Pruefung
    input_tokens: int = 0
    output_tokens: int = 0


class SummaryService:
    """
    Service for generating AI summaries of OParl documents.

    Nutzt den Anbieter der Zusammenfassungen aus der zentralen KI-Konfiguration (``get_summary_provider``).
    Liest nur gespeicherten Text; lädt und erkennt nichts selbst (siehe Moduldokumentation).
    """

    def __init__(self, provider: Any = None) -> None:
        """
        Initialize the summary service.

        Args:
            provider: Optional AI provider. Standard: ``get_summary_provider()`` (KI-Einstellungen).
        """
        self.provider = provider or get_summary_provider()

    def generate_summary(self, paper: "OParlPaper", save: bool = True) -> str:
        """
        Generate a summary for an OParl paper.

        Uses only the stored text of the files; never downloads or recognizes text itself.

        Args:
            paper: OParlPaper instance to summarize
            save: Whether to save the summary to the paper

        Returns:
            Generated summary text

        Raises:
            TextPendingError: No text yet, but text recognition of a file is pending (planned with priority)
            NoTextContentError: If no text content is available
            APINotConfiguredError: If the API is not configured
            BudgetExhaustedError: Monthly budget of the summaries (Claude API) used up
            SummaryRejectedError: Die Zusammenfassung hat die mechanische Prüfung nicht bestanden (Issue #1271)
            SummaryError: For other generation errors
        """
        # Check if API is available
        if not self.provider.is_available():
            raise APINotConfiguredError(
                "KI im Bürgerportal nicht eingerichtet (Admin → KI-Einstellungen, nur freigegebene Endpunkte)."
            )

        # Stand zu Beginn: Wird der Vorgang oder eine dieser Anlagen während der Erstellung
        # zurückgenommen, darf das Ergebnis weder gespeichert noch ausgegeben werden
        started = (bool(paper.deleted), list(self._current_files(paper).values_list("pk", flat=True)))
        # Neuer Text ab hier verwirft die Zusammenfassung (Abonnement); ein Ergebnis aus dem älteren Text wird
        # deshalb nicht gespeichert
        started_at = timezone.now()

        # Nur gespeicherter Text; der Dienst lädt und erkennt nichts selbst. Anbieter mit strukturierter Antwort
        # (Claude) schreiben zusätzlich die Fassung in Einfacher Sprache
        strukturiert = bool(getattr(self.provider, "strukturiert", False))
        vorbereitung = self.vorbereiten(paper, einfach=strukturiert)

        if vorbereitung is None:
            if self._text_pending(paper):
                raise TextPendingError("Die Texterkennung der Anlagen steht noch aus.")
            raise NoTextContentError(
                "Keine Textinhalte verfügbar. Das Dokument enthält keine "
                "Dateien oder die Textextraktion ist fehlgeschlagen."
            )

        try:
            logger.info(f"Generating summary for paper {paper.id} ({paper.reference})")

            # Der KI-Aufruf dauert Minuten: Datenbankverbindung solange an den Pool zurückgeben
            release_idle_thread_connections()

            entwurf = self.entwurf_erstellen(vorbereitung)
            summary, einfach = entwurf.zusammenfassung, entwurf.einfach

            logger.info(
                f"Summary generated for {paper.id}: {entwurf.input_tokens} input, {entwurf.output_tokens} output tokens"
            )

            # Während der Erstellung zurückgenommen (Vorgang oder Anlage)? Dann nichts ausgeben
            if self._withdrawn_since(paper, started):
                raise SummaryRevokedError("Vorgang oder Anlage wurde während der Erstellung zurückgenommen.")

            # Wer sie erstellt hat (Hinweis unter dem Text, auch wenn sie unten nicht gespeichert wird);
            # Ersatz-Anbieter in Tests haben keinen
            hinweis = getattr(self.provider, "zusammenfassung_hinweis", "")
            hinweis = (hinweis if isinstance(hinweis, str) else "")[:300]

            # Mechanische Prüfung (Issue #1271): Verworfenes wird protokolliert und nicht ausgegeben
            pruefung = entwurf.pruefung
            if pruefung.befund is not None:
                zusammenfassung_pruefung.protokollieren(
                    paper.pk, paper.body_id, summary, pruefung.befund, weg="klick", hinweis=hinweis
                )
                raise SummaryRejectedError(pruefung.befund)
            if pruefung.befund_einfach is not None:
                zusammenfassung_pruefung.protokollieren(
                    paper.pk,
                    paper.body_id,
                    einfach,
                    pruefung.befund_einfach,
                    weg="klick",
                    teil="einfache_sprache",
                    hinweis=hinweis,
                )
                einfach = ""
            paper.summary_hinweis = hinweis

            # Save to paper if requested – nicht, wenn inzwischen neuer Text vorliegt: Das Abonnement hätte die
            # Zusammenfassung verworfen, sie stammt aus dem älteren Text
            if save:
                if self._text_changed_since(paper, started_at):
                    logger.info(f"Summary for paper {paper.id} not saved: new text since the start")
                else:
                    paper.summary = summary
                    paper.summary_einfach = einfach or None
                    paper.summary_stand = vorbereitung.stand
                    paper.summary_prompt_fassung = PROMPT_FASSUNG
                    paper.save(
                        update_fields=[
                            "summary",
                            "summary_hinweis",
                            "summary_einfach",
                            "summary_stand",
                            "summary_prompt_fassung",
                        ]
                    )
                    logger.info(f"Summary saved to paper {paper.id}")

            return summary

        except (NoTextContentError, APINotConfiguredError, SummaryRevokedError, SummaryRejectedError):
            raise
        except BudgetErschoepftError as e:
            logger.warning("Summary for paper %s not generated: monthly budget of the summaries used up", paper.id)
            raise BudgetExhaustedError("Monatsbudget der Zusammenfassungen ausgeschöpft.") from e
        except Exception as e:
            logger.exception(f"Summary generation failed for paper {paper.id}: {e}")
            # Feste Meldung: Anbieter-Ausnahmen können Adressen, Schlüssel oder Interna enthalten
            raise SummaryError(
                "Die Zusammenfassung konnte gerade nicht erstellt werden. Bitte später erneut versuchen."
            ) from e

    def entwurf_erstellen(self, vorbereitung: Vorbereitung) -> Entwurf:
        """
        Fragt den Anbieter mit dem vorbereiteten Prompt und prüft die Antwort mechanisch; speichert nichts.

        Einzelaufruf (``generate_summary``) und Stichprobe (``manage.py zusammenfassung_stichprobe``) nutzen ihn.
        Fehler des Anbieters gehen unverändert weiter; eine strukturierte Antwort ohne Zusammenfassung ist ein
        ``SummaryError``.
        """
        # Modelle mit Reasoning brauchen viel Platz vor der eigentlichen Antwort: Obergrenze aus der
        # KI-Konfiguration (AISettings.insight_max_output_tokens)
        max_tokens = getattr(self.provider, "max_output_tokens", 0) or STANDARD_MAX_AUSGABE
        if vorbereitung.schema is not None:
            response = self.provider.chat_completion(
                messages=vorbereitung.nachrichten,
                max_tokens=max_tokens + ZUSATZ_AUSGABE_EINFACH,
                temperature=0.3,
                schema=vorbereitung.schema,
            )
            summary, einfach = zerlegen(response.content, json_pflicht=True)
            if not summary:
                raise SummaryError("Antwort ohne Zusammenfassung")
        else:
            response = self.provider.chat_completion(
                messages=vorbereitung.nachrichten,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            summary, einfach = response.content, ""
        pruefung = zusammenfassung_pruefung.pruefen_beide(summary, einfach, vorbereitung.pruefquelle())
        return Entwurf(
            zusammenfassung=summary,
            einfach=einfach,
            pruefung=pruefung,
            input_tokens=getattr(response, "input_tokens", 0) or 0,
            output_tokens=getattr(response, "output_tokens", 0) or 0,
        )

    def vorbereiten(
        self, paper: "OParlPaper", orte: list[Any] | None = None, einfach: bool = False
    ) -> Vorbereitung | None:
        """
        Prompt der Zusammenfassung aus dem gespeicherten Text, den Metadaten und dem Beratungsverlauf; ``None`` ohne
        Text.

        Einzelaufruf (``generate_summary``) und Batch nutzen denselben Prompt. Lädt und erkennt nichts.

        Der Beratungsverlauf (nur öffentliche Angaben, mit dem Stand-Satz der Vorgangsseite) steht im Prompt, damit
        die Zusammenfassung dem Stand nicht widerspricht; sein Stand (``summary_store.beratungsstand``) wird mit der
        Zusammenfassung gespeichert, ein neues Ergebnis macht sie damit erkennbar veraltet (Issue #1257).

        Mit ``orte`` (``verortung_pruefung.orte_fuer_pruefung``, auch leer) prüft Claude in derselben Anfrage die
        Verortung (Tagesbatch, Issue #1222): Zusatz im Systemprompt, Orte im Nutzerprompt, Antwort als JSON nach
        ``TAGESBATCH_SCHEMA``. Mit ``einfach`` (oder ``orte``) schreibt Claude dieselbe Zusammenfassung zusätzlich in
        Einfacher Sprache (``EINFACH_SYSTEM_ZUSATZ``, ohne Orte nach ``ZUSAMMENFASSUNG_SCHEMA``).
        """
        datei_ids = list(self._current_files(paper).values_list("pk", flat=True))
        text_content = self._collect_text_content(paper)
        if not text_content:
            return None

        # Collect metadata
        body_name = None
        if paper.body:
            body_name = paper.body.name or paper.body.short_name

        # Get organizations from consultations via meetings
        organizations = set()
        try:
            from hub.ris.selectors import consultation_meeting_in_source
            from insight_core.models import OParlMeeting, withdrawn_q

            # Get meeting external IDs from consultations (ohne von Session zurückgenommene, nur Sitzungen aus der
            # Quelle der Beratung)
            meeting_ext_ids = (
                paper.consultations.exclude(withdrawn_q())
                .filter(consultation_meeting_in_source())
                .values_list("meeting_external_id", flat=True)
                .distinct()[:10]
            )

            # Find meetings and their organizations
            meetings = (
                OParlMeeting.objects.filter(external_id__in=[m for m in meeting_ext_ids if m])
                .exclude(withdrawn_q())
                .prefetch_related("organizations")
            )

            for meeting in meetings:
                for org in meeting.organizations.all():
                    if org.name:
                        organizations.add(org.name)
        except Exception as e:
            logger.debug(f"Could not get organizations: {e}")

        # Beratungsverlauf mit Stand-Satz (wie auf der Vorgangsseite) und sein Stand (Issue #1257)
        from hub.ris.selectors import public_consultation_history
        from insight_core.services import summary_store
        from insight_core.services.paper_status import paper_status, status_eintraege

        jetzt = timezone.now()
        schritte = public_consultation_history([paper.pk]).get(paper.pk, [])
        verlauf = beratungsverlauf_block(schritte, paper_status(status_eintraege(schritte), jetzt).text, now=jetzt)

        # Build prompts
        einfach = einfach or orte is not None
        system_prompt = PAPER_SUMMARY_SYSTEM_PROMPT
        if einfach:
            system_prompt = f"{system_prompt}\n\n{EINFACH_SYSTEM_ZUSATZ}"
        if orte is not None:
            system_prompt = f"{system_prompt}\n\n{VERORTUNG_SYSTEM_ZUSATZ}"
        user_prompt = build_paper_summary_user_prompt(
            paper_name=paper.name or "Unbekannt",
            paper_type=paper.paper_type,
            reference=paper.reference,
            date=str(paper.date) if paper.date else None,
            text_content=text_content,
            body_name=body_name,
            organizations=list(organizations) if organizations else None,
            orte=orte_block(orte) if orte is not None else None,
            einfach=einfach,
            verlauf=verlauf,
        )

        # Create messages
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]
        # Wörtlich erlaubt (Prüfung, Issue #1271): Titel, Kommune und Gremien aus Metadaten und Beratungsverlauf
        gremien = {str(schritt.organization_name) for schritt in schritte if schritt.organization_name}
        erlaubt = (paper.name or "", body_name or "", *sorted(organizations | gremien))
        return Vorbereitung(
            nachrichten=messages,
            datei_ids=datei_ids,
            zeichen=len(system_prompt) + len(user_prompt),
            orte_ids=[ort.pk for ort in orte] if orte is not None else None,
            schema=TAGESBATCH_SCHEMA if orte is not None else (ZUSAMMENFASSUNG_SCHEMA if einfach else None),
            stand=summary_store.beratungsstand(schritte, now=jetzt),
            quelle=user_prompt,
            erlaubt=tuple(angabe for angabe in erlaubt if angabe),
        )

    def _collect_text_content(self, paper: "OParlPaper") -> str:
        """
        Gespeicherten Text aller aktuellen Anlagen zusammenführen (ohne Abruf, ohne Erkennung): je Anlage der Anfang,
        zusammen höchstens ``TEXT_BUDGET_CHARS`` Zeichen, gekürzte Anlagen mit Vermerk.

        Args:
            paper: OParlPaper instance

        Returns:
            Combined text content from all files
        """
        # Nur der Anfang je Anlage, zusammen höchstens TEXT_BUDGET_CHARS Zeichen; gekürzt wird in der Datenbank, die
        # Volltexte (je Datei bis zu Millionen Zeichen) kommen nicht in den Speicher
        mit_text = self._current_files(paper).filter(text_content__isnull=False).exclude(text_content="")
        anzahl = mit_text.count()
        if not anzahl:
            return ""
        je_datei = max(1, TEXT_BUDGET_CHARS // anzahl)
        texts = []
        for file in mit_text.only("id", "name", "file_name").annotate(anfang=Substr("text_content", 1, je_datei + 1)):
            text = (file.anfang or "")[:je_datei].strip()
            if text:
                file_name = file.name or file.file_name or "Dokument"
                gekuerzt = "\n[… gekürzt]" if len(file.anfang) > je_datei else ""
                texts.append(anlage_block(file_name, f"{text}{gekuerzt}"))
        return "\n\n".join(texts)

    def _text_pending(self, paper: "OParlPaper") -> bool:
        """
        Steht die Texterkennung einer aktuellen Anlage noch aus? Dann ihre Erkennung mit Vorrang einplanen.

        Eingeplant werden höchstens ``MAX_PRIORITY_EXTRACTIONS`` wartende Anlagen (``pending``) mit abgelegtem
        Inhalt (``local_status = ok``) und nur unter den Bedingungen aus der Moduldokumentation; sonst bleibt es beim
        Hinweis. Der Auftrag liest nur aus der Ablage (``hub/ris/erkennung.py``); Anlagen ohne abgelegten Inhalt holt
        zuerst der Abruf, danach plant sie der Zeitplan ein. Ein Fehler beim Einplanen ändert den Hinweis nicht.
        Anlagen in Bearbeitung (``processing``) sind schon beansprucht. Hat die Quelle den Dateiabruf abgeschaltet
        (Dokumente nur hinter einer Prüfung für Menschen), kommt kein Text: dann ``False``. Ebenso zählen von der
        Obergrenze verdrängte Anlagen (``evicted``, #961) nicht: Ihr Text kommt nicht von selbst, ein Dauerhinweis
        „Texterkennung steht aus“ wäre falsch.
        """
        if self._downloads_disabled(paper):
            return False
        offen = [
            file
            for file in self._current_files(paper)
            .filter(text_extraction_status__in=TEXT_PENDING_STATUSES)
            .exclude(local_status=EVICTED)
            .only("id", "download_url", "access_url", "text_extraction_status", "local_status")
            .order_by("created_at", "id")
            if file.download_url or file.access_url
        ]
        if not offen:
            return False
        wartend = [file.pk for file in offen if file.text_extraction_status == "pending" and file.local_status == "ok"]
        if wartend and priority_extraction_enabled():
            try:
                plan_priority_extraction(wartend[:MAX_PRIORITY_EXTRACTIONS])
            except Exception:
                logger.exception("Texterkennung für Vorgang %s nicht mit Vorrang eingeplant", paper.pk)
        return True

    @staticmethod
    def _downloads_disabled(paper: "OParlPaper") -> bool:
        """Hat die Quelle des Vorgangs den Dateiabruf abgeschaltet (``sync_config["file_downloads"] = false``)?"""
        source = getattr(paper.body, "source", None) if paper.body_id else None
        config = getattr(source, "sync_config", None)
        return isinstance(config, dict) and config.get(FILE_DOWNLOADS_KEY) is False

    @staticmethod
    def _text_changed_since(paper: "OParlPaper", started_at: datetime) -> bool:
        """Liegt für eine Anlage des Vorgangs seit ``started_at`` neuer Text vor?"""
        return paper.files.filter(text_extraction_status="completed", text_extracted_at__gt=started_at).exists()

    @staticmethod
    def _withdrawn_since(paper: "OParlPaper", started: tuple[bool, list]) -> bool:
        """Vorgang oder eine der verwendeten Anlagen seit Beginn gelöscht, zurückgenommen oder gesperrt?"""
        from django.db.models import Q

        from insight_core.models import OParlFile, OParlPaper

        was_deleted, file_ids = started
        return (
            not was_deleted and OParlPaper.objects.filter(pk=paper.pk, deleted=True).exists()
        ) or OParlFile.objects.filter(pk__in=file_ids).filter(
            Q(deleted=True) | Q(source_missing_since__isnull=False)
        ).exists()

    @staticmethod
    def _current_files(paper: "OParlPaper"):
        """
        Anlagen, die in die Zusammenfassung einfließen dürfen: nur nicht gelöschte und nicht gesperrte.

        Zurückgenommene Anlagen (in Session nicht-öffentlich gestellt oder gelöscht), in der
        Quelle gelöschte und dort nicht mehr abrufbare Dateien (Löschabgleich, #787) gehören nicht mehr
        zum Vorgang; ihr Text darf nicht über eine öffentlich abrufbare Zusammenfassung weiterleben.
        """
        return paper.files.filter(deleted=False, source_missing_since__isnull=True)

    def is_available(self) -> bool:
        """
        Check if the summary service is available.

        Returns:
            True if the AI provider is properly configured
        """
        return self.provider.is_available()


def priority_extraction_enabled() -> bool:
    """
    Darf die Zusammenfassung die Texterkennung mit Vorrang einplanen?

    Nur, wenn der Auftrag ``file.extract_text`` im Journal landet (``TASKS_BACKEND=journal``; das sofort ausführende
    Backend erkennte den Text in der Webanfrage) und der Worker den Text erkennt (``TEXT_EXTRACTION_RUNNER=worker``;
    mit ``ingestor`` macht das der OCR-Worker des Ingestors).
    """
    if str(getattr(settings, "TEXT_EXTRACTION_RUNNER", "ingestor")).strip().lower() != "worker":
        return False
    from django.tasks import task_backends

    from apps.events.tasks_backend import JournalBackend
    from insight_core.background_tasks import file_extract_text

    return isinstance(task_backends[file_extract_text.backend], JournalBackend)


def plan_priority_extraction(file_ids: list[Any]) -> int:
    """
    Reiht ``file.extract_text`` je Datei mit Vorrang ein (``EXTRACTION_PRIORITY``); Rückgabe: Zahl der Aufträge.

    Nur aufrufen, wenn ``priority_extraction_enabled()``. Wartet für eine Datei schon ein Auftrag mit Vorrang (ein
    früherer Klick), entsteht kein zweiter. Einen gewöhnlichen wartenden Auftrag überholt der neue; welcher zuerst
    beansprucht, erkennt, der andere endet ohne Wirkung.
    """
    from apps.events.models import Task, TaskStatus
    from insight_core.background_tasks import file_extract_text

    if not file_ids:
        return 0
    schon_vorrang: set[str] = set()
    for args in Task.objects.filter(
        task_path=file_extract_text.module_path, status=TaskStatus.WARTEND, priority__gte=EXTRACTION_PRIORITY
    ).values_list("args", flat=True):
        werte = args.get("args") if isinstance(args, dict) else None
        if werte:
            schon_vorrang.add(str(werte[0]))
    vorrang = file_extract_text.using(priority=EXTRACTION_PRIORITY)
    eingereiht = 0
    for file_id in file_ids:
        if str(file_id) in schon_vorrang:
            continue
        vorrang.enqueue(str(file_id))
        eingereiht += 1
    if eingereiht:
        logger.info("Texterkennung: %d Anlagen für eine Zusammenfassung mit Vorrang eingereiht", eingereiht)
    return eingereiht
