# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorschläge des Protokolldienstes für die Niederschrift: annehmen (Befehl), übernehmen, verwerfen.

**Befehl** ``session.minutes_draft.propose`` (Vertrag ``hub/contracts/schemas/session.minutes_draft.propose``): Der
Dienst übergibt nach der Freigabe je TOP Entwurf des Protokolltexts, Beschlusstext und Abstimmungsergebnis. Geprüft
wird: Mandant aktiv und angebunden, Sitzung des Mandanten, Niederschrift nicht genehmigt (sonst 409, unter der Sperre
der Sitzung wie bei der Genehmigung), jeder TOP zur Sitzung und höchstens einmal, Stimmenzahlen nur vollständig (sonst
422). Gespeichert wird je TOP ein Vorschlag (Texte verschlüsselt); offene Vorschläge desselben TOP gelten als ersetzt.
Liegt vom selben Dokument schon eine neuere Fassung vor, ersetzt eine ältere nichts und wird nicht angelegt (Zustellung
außer der Reihe). Die Niederschrift selbst ändert sich nicht.

**Übernehmen** (Recht ``edit_protocols``, für nichtöffentliche TOPs dazu das NÖ-Recht): Session schreibt in den TOP

- den Entwurf als Protokolltext, beim nichtöffentlichen TOP in den nichtöffentlichen Protokolltext,
- den Beschlusstext, beim nichtöffentlichen TOP in den nichtöffentlichen Beschlusstext,
- das Abstimmungsergebnis nur, solange noch keins festgestellt ist, keine Abstimmung geöffnet ist und nicht einzeln
  abgestimmt wird (dann stellt die Abstimmungserfassung Ergebnis und Summen fest); dieselben Prüfungen wie beim
  Bearbeiten der Niederschrift (Zahl der Stimmberechtigten, Ergebnisregel des Landesprofils, Sperre bei
  Zugeschalteten).

Leere Felder des Vorschlags lassen den TOP unverändert. Übernehmen und Verwerfen stehen im Prüfprotokoll (ohne
Inhalte); danach, und wenn eine neuere Fassung ihn ersetzt, löscht Session die Texte des Vorschlags. Wie beim
Bearbeiten nur bei einer Niederschrift im Entwurf oder in der Prüfung, unter der Sperre von Sitzung und Niederschrift.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

from django.db import transaction
from django.utils import timezone

from apps.session import audit
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionNiederschriftVorschlag,
    SessionTenant,
    SessionUser,
)
from hub.commands import Command, CommandError, HandlerResult, Problem, command_handler

from . import einstellungen

Vorschlag = SessionNiederschriftVorschlag

PROPOSE: Final = "session.minutes_draft.propose"
#: Auslöser des Befehls über die Schnittstelle
ACTOR: Final = "system:session.protokolldienst"
#: Ergebnis beim Protokolldienst → Ergebnis in Session; „verwiesen“ hat keine Entsprechung (nur Summen)
ERGEBNISSE: Final[dict[str, str]] = {
    "adopted": "approved",
    "adopted_amended": "approved",
    "rejected": "rejected",
    "postponed": "deferred",
    "noted": "noted",
    "withdrawn": "withdrawn",
}
EDITABLE: Final = ("draft", "review")
#: Nach der Entscheidung (übernommen, verworfen, ersetzt) braucht es die Texte des Vorschlags nicht mehr
TEXTE_LEER: Final[dict[str, None]] = {"draft_text_encrypted": None, "resolution_text_encrypted": None}

TENANT_UNKNOWN: Final = "Die Verwaltung ist nicht bekannt oder deaktiviert."
NOT_CONNECTED: Final = "Die Anbindung an den Protokolldienst ist ausgeschaltet."
MEETING_UNKNOWN: Final = "Die Sitzung ist nicht bekannt."
LOCKED: Final = "Die Niederschrift dieser Sitzung ist genehmigt; Vorschläge sind nicht mehr möglich."
ITEM_UNKNOWN: Final = "Der Tagesordnungspunkt gehört nicht zu dieser Sitzung."
ITEM_TWICE: Final = "Der Tagesordnungspunkt steht mehrfach in der Übergabe."
PARTIAL_COUNTS: Final = "Stimmenzahlen nur vollständig: Ja, Nein und Enthaltungen zusammen oder keine."
#: Stimmenzahlen eines Vorschlags (Vertrag) → Felder
SUMMEN: Final = (("yes", "votes_yes"), ("no", "votes_no"), ("abstain", "votes_abstain"))


def _tenant(ref: str) -> SessionTenant:
    kind, _, raw_id = ref.partition(":")
    tenant = SessionTenant.objects.filter(pk=raw_id, is_active=True).first() if kind == "session" else None
    if tenant is None:
        raise CommandError.of(404, "mandant-unbekannt", TENANT_UNKNOWN)
    return tenant


@command_handler(PROPOSE)
def vorschlagen(command: Command) -> HandlerResult:
    """Vorschläge je TOP speichern (siehe Moduldokumentation)."""
    from apps.session.services import cockpit_service, protocol_lock

    body = command.body
    tenant = _tenant(command.tenant_ref)
    if einstellungen.aktiv(tenant) is None:
        raise CommandError.of(403, "nicht-angebunden", NOT_CONNECTED)
    meeting = SessionMeeting.objects.filter(pk=body["meeting"], tenant=tenant).first()
    if meeting is None:
        raise CommandError.of(404, "nicht-gefunden", MEETING_UNKNOWN)
    # Dieselbe Sperre wie die Genehmigung (protocol_service.lock_protocol), bis zum Ende der Transaktion: Eine
    # Genehmigung zwischen Prüfung und Speichern ist damit ausgeschlossen
    cockpit_service.lock_meeting(meeting.pk)
    if protocol_lock.is_locked(meeting.pk):
        raise CommandError.of(409, "niederschrift-genehmigt", LOCKED)
    eintraege = list(body["items"])
    tops = {str(item.pk): item for item in meeting.agenda_items.filter(pk__in=[e["agenda_item"] for e in eintraege])}
    _eintraege_pruefen(eintraege, tops)
    revision = int(body["revision"])
    angelegt = []
    for eintrag in eintraege:
        item = tops[str(eintrag["agenda_item"])]
        neuere = Vorschlag.objects.filter(
            meeting=meeting, agenda_item=item, source_document=body["source_document"], revision__gt=revision
        )
        if neuere.exists():
            # Außer der Reihe zugestellt: Die neuere Fassung bleibt, diese ersetzt nichts
            continue
        Vorschlag.objects.filter(meeting=meeting, agenda_item=item, status=Vorschlag.OFFEN).update(
            status=Vorschlag.ERSETZT, decided_at=timezone.now(), **TEXTE_LEER
        )
        vorschlag = Vorschlag(
            meeting=meeting,
            agenda_item=item,
            source_document=body["source_document"],
            revision=revision,
            released_at=body.get("released_at"),
        )
        _texte_setzen(vorschlag, str(eintrag.get("draft_text") or ""), str(eintrag.get("resolution_text") or ""))
        _abstimmung_setzen(vorschlag, eintrag.get("vote") or {})
        vorschlag.save()
        angelegt.append(vorschlag)
    return HandlerResult(
        reference=f"niederschrift:{meeting.pk}:{int(body['revision'])}",
        aggregate_id=meeting.pk,
        data={"vorschlaege": len(angelegt)},
    )


def _eintraege_pruefen(eintraege: list[dict[str, Any]], tops: Mapping[str, SessionAgendaItem]) -> None:
    """Vor dem ersten Schreiben: jeder TOP zur Sitzung und nur einmal, Stimmenzahlen vollständig; sonst 422."""
    fehler: list[dict[str, str]] = []
    gesehen: set[str] = set()
    for index, eintrag in enumerate(eintraege):
        kennung = str(eintrag["agenda_item"])
        if kennung not in tops:
            fehler.append({"pointer": f"/items/{index}/agenda_item", "detail": ITEM_UNKNOWN})
        elif kennung in gesehen:
            fehler.append({"pointer": f"/items/{index}/agenda_item", "detail": ITEM_TWICE})
        gesehen.add(kennung)
        stimmen = eintrag.get("vote") or {}
        if len({stimmen.get(name) is None for name, _ in SUMMEN}) > 1:
            fehler.append({"pointer": f"/items/{index}/vote", "detail": PARTIAL_COUNTS})
    if fehler:
        raise CommandError(Problem(status=422, kind="validierung", detail=fehler[0]["detail"], errors=tuple(fehler)))


def _texte_setzen(vorschlag: Vorschlag, entwurf: str, beschluss: str) -> None:
    zugriff = cast(Any, vorschlag)
    zugriff.set_draft_text_encrypted(entwurf.strip())
    zugriff.set_resolution_text_encrypted(beschluss.strip())


def _abstimmung_setzen(vorschlag: Vorschlag, vote: dict[str, Any]) -> None:
    if not vote:
        return
    vorschlag.vote_result = ERGEBNISSE.get(str(vote.get("outcome") or ""), "")
    for name, feld in SUMMEN:
        if vote.get(name) is not None:
            setattr(vorschlag, feld, int(vote[name]))
    vorschlag.unanimous = bool(vote.get("unanimous"))


# --- Anzeige ---------------------------------------------------------------------------------------------------------


@dataclass
class Anzeige:
    """Ein offener Vorschlag für die Niederschrift-Seite, entschlüsselt nur für Berechtigte."""

    vorschlag: Vorschlag
    item: SessionAgendaItem
    entwurf: str
    beschluss: str
    nichtoeffentlich: bool
    #: Ergebnis und Stimmen als eine Zeile, etwa „Angenommen: Ja 21, Nein 3, Enthaltungen 1“; leer ohne Abstimmung
    abstimmung: str = ""
    hinweise: list[str] = field(default_factory=list)


def _abstimmung_text(vorschlag: Vorschlag) -> str:
    ergebnisse = dict(SessionAgendaItem._meta.get_field("vote_result").choices or [])
    teile = []
    if vorschlag.vote_result:
        teile.append(str(ergebnisse.get(vorschlag.vote_result, vorschlag.vote_result)))
    if vorschlag.unanimous:
        teile.append("einstimmig")
    elif None not in (vorschlag.votes_yes, vorschlag.votes_no, vorschlag.votes_abstain):
        # Nur vollständige Summen; eine fehlende Zahl erschiene sonst als 0
        teile.append(f"Ja {vorschlag.votes_yes}, Nein {vorschlag.votes_no}, Enthaltungen {vorschlag.votes_abstain}")
    return ": ".join(teile)


def _oeffentlich(item: SessionAgendaItem) -> bool:
    return bool(item.is_public and item.meeting.is_public)


def offene(meeting: SessionMeeting, *, can_view_np: bool) -> list[Anzeige]:
    """Offene Vorschläge der Sitzung, nichtöffentliche TOPs nur mit dem NÖ-Recht."""
    zeilen = (
        Vorschlag.objects.filter(meeting=meeting, status=Vorschlag.OFFEN)
        .select_related("agenda_item__meeting__tenant", "meeting__tenant")
        .order_by("agenda_item__order", "agenda_item__number")
    )
    ergebnis = []
    for vorschlag in zeilen:
        item = vorschlag.agenda_item
        if not _oeffentlich(item) and not can_view_np:
            continue
        zugriff = cast(Any, vorschlag)
        anzeige = Anzeige(
            vorschlag=vorschlag,
            item=item,
            entwurf=str(zugriff.get_draft_text_decrypted() or ""),
            beschluss=str(zugriff.get_resolution_text_decrypted() or ""),
            nichtoeffentlich=not _oeffentlich(item),
            abstimmung=_abstimmung_text(vorschlag),
        )
        if vorschlag.has_vote and item.vote_result != "pending":
            anzeige.hinweise.append("Das Ergebnis ist schon festgestellt und bleibt.")
        ergebnis.append(anzeige)
    return ergebnis


# --- Übernehmen und Verwerfen ------------------------------------------------------------------------------------------


class VorschlagError(ValueError):
    """Übernehmen oder Verwerfen nicht möglich; die Meldung ist für die Oberfläche formuliert."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def _laden(meeting: SessionMeeting, vorschlag_id: Any, permissions: Collection[str]) -> Vorschlag:
    vorschlag = (
        Vorschlag.objects.select_for_update(of=("self",))
        .select_related("agenda_item")
        .filter(pk=vorschlag_id, meeting=meeting)
        .first()
    )
    if vorschlag is None or vorschlag.status != Vorschlag.OFFEN:
        raise VorschlagError("Den Vorschlag gibt es nicht mehr oder er ist schon entschieden.")
    if not _oeffentlich(vorschlag.agenda_item) and "view_non_public_meetings" not in permissions:
        raise VorschlagError("Den Vorschlag gibt es nicht mehr oder er ist schon entschieden.")
    return vorschlag


def _protokoll_sperren(meeting: SessionMeeting, session_user: SessionUser | None, *, anlegen: bool = True) -> None:
    """Erst die Sitzung, dann die Niederschrift sperren und ihren Status prüfen.

    ``anlegen=False`` (Verwerfen): eine fehlende Niederschrift nicht anlegen; dann entfällt nur die Statusprüfung.
    """
    from apps.session.models import SessionProtocol
    from apps.session.services import cockpit_service, protocol_service

    cockpit_service.lock_meeting(meeting.pk)
    if anlegen:
        protocol, _ = protocol_service.get_or_create_protocol(meeting, created_by=session_user)
    else:
        vorhanden = SessionProtocol.objects.filter(meeting_id=meeting.pk).first()
        if vorhanden is None:
            return
        protocol = vorhanden
    protocol_service.lock_protocol(protocol)
    protocol.refresh_from_db(fields=["status"])
    if protocol.status not in EDITABLE:
        raise VorschlagError(
            "Genehmigte oder veröffentlichte Niederschriften sind nicht mehr bearbeitbar. "
            "Korrekturen laufen über eine Berichtigung."
        )


def uebernehmen(
    meeting: SessionMeeting,
    vorschlag_id: Any,
    *,
    permissions: Collection[str],
    session_user: SessionUser | None,
    request: Any = None,
) -> list[str]:
    """Vorschlag in den TOP übernehmen; liefert Hinweise (etwa: Ergebnis nicht übernommen). Wirft ``VorschlagError``."""
    from apps.session import hub_events

    hinweise: list[str] = []
    with transaction.atomic(), hub_events.track(meeting.tenant) as tracked:
        _protokoll_sperren(meeting, session_user)
        vorschlag = _laden(meeting, vorschlag_id, permissions)
        item = SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=vorschlag.agenda_item_id)
        tracked.agenda(meeting)
        felder = _texte_uebernehmen(vorschlag, item)
        felder += _ergebnis_uebernehmen(vorschlag, item, hinweise)
        if felder:
            item.save(update_fields=[*felder, "updated_at"])
        _entscheiden(vorschlag, Vorschlag.UEBERNOMMEN, session_user)
        audit.log_event(
            "update",
            item,
            tenant=meeting.tenant,
            user=session_user,
            request=request,
            changes={"vorschlag_protokolldienst": "übernommen", "fassung": vorschlag.revision, "felder": felder},
        )
    return hinweise


def _texte_uebernehmen(vorschlag: Vorschlag, item: SessionAgendaItem) -> list[str]:
    zugriff, ziel = cast(Any, vorschlag), cast(Any, item)
    entwurf = str(zugriff.get_draft_text_decrypted() or "")
    beschluss = str(zugriff.get_resolution_text_decrypted() or "")
    felder: list[str] = []
    oeffentlich = _oeffentlich(item)
    if entwurf:
        if oeffentlich:
            item.protocol_note = entwurf
            felder.append("protocol_note")
        else:
            ziel.set_protocol_note_encrypted(entwurf)
            felder.append("protocol_note_encrypted")
    if beschluss:
        if oeffentlich:
            item.resolution_text = beschluss
            felder.append("resolution_text")
        else:
            ziel.set_resolution_text_encrypted(beschluss)
            felder.append("resolution_text_encrypted")
    return felder


def _ergebnis_uebernehmen(vorschlag: Vorschlag, item: SessionAgendaItem, hinweise: list[str]) -> list[str]:
    from apps.session.services import participation_service, voting_service

    if not vorschlag.has_vote:
        return []
    if item.vote_result != "pending":
        hinweise.append(f"TOP {item.number}: Das Ergebnis war schon festgestellt und bleibt.")
        return []
    if item.vote_open or item.voting_method in voting_service.INDIVIDUAL_METHODS:
        # Ergebnis und Summen stellt die Abstimmung fest, nicht der Vorschlag
        hinweise.append(
            f"TOP {item.number}: Die Abstimmung ist geöffnet oder wird einzeln erfasst; Ergebnis und Stimmenzahlen "
            "stellt die Abstimmung fest."
        )
        return []
    sperre = participation_service.remote_vote_rule(item.meeting, item)
    if sperre is not None and sperre.barred and vorschlag.vote_result != "deferred":
        hinweise.append(f"TOP {item.number}: {sperre.message}")
        return []
    vorher = {name: getattr(item, name) for name in ("vote_result", *voting_service.COUNT_FIELDS)}
    felder: list[str] = []
    if vorschlag.vote_result:
        # „Verwiesen“ hat kein Ergebnis in Session: dann nur die Summen
        item.vote_result = vorschlag.vote_result
        felder.append("vote_result")
    summen = (vorschlag.votes_yes, vorschlag.votes_no, vorschlag.votes_abstain)
    if all(wert is not None for wert in summen):
        ja, nein, enthaltung = (int(wert or 0) for wert in summen)
        pruefung = voting_service.check_counts(item, ja, nein, enthaltung)
        if pruefung.message:
            hinweise.append(pruefung.message)
        if not pruefung.hard:
            item.votes_yes, item.votes_no, item.votes_abstain = ja, nein, enthaltung
            felder += list(voting_service.COUNT_FIELDS)
    problem = voting_service.result_rule_problem(item)
    if problem:
        for name, wert in vorher.items():
            setattr(item, name, wert)
        hinweise.append(f"TOP {item.number}: {problem} Ergebnis und Stimmenzahlen wurden nicht übernommen.")
        return []
    return felder


def _entscheiden(vorschlag: Vorschlag, status: str, session_user: SessionUser | None) -> None:
    """Entscheidung vermerken und die Texte löschen; es bleiben Fassung, Zeitpunkt und wer entschieden hat."""
    vorschlag.status = status
    vorschlag.decided_at = timezone.now()
    vorschlag.decided_by = session_user
    for name, wert in TEXTE_LEER.items():
        setattr(vorschlag, name, wert)
    vorschlag.save(update_fields=["status", "decided_at", "decided_by", *TEXTE_LEER])


def verwerfen(
    meeting: SessionMeeting,
    vorschlag_id: Any,
    *,
    permissions: Collection[str],
    session_user: SessionUser | None,
    request: Any = None,
) -> None:
    """Vorschlag verwerfen; die Niederschrift bleibt unverändert. Wirft ``VorschlagError``."""
    with transaction.atomic():
        # Wie beim Übernehmen: nur im Entwurf oder in der Prüfung, unter denselben Sperren; legt aber keine
        # Niederschrift an
        _protokoll_sperren(meeting, session_user, anlegen=False)
        vorschlag = _laden(meeting, vorschlag_id, permissions)
        _entscheiden(vorschlag, Vorschlag.VERWORFEN, session_user)
        audit.log_event(
            "update",
            vorschlag.agenda_item,
            tenant=meeting.tenant,
            user=session_user,
            request=request,
            changes={"vorschlag_protokolldienst": "verworfen", "fassung": vorschlag.revision},
        )
