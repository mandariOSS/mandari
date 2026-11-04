# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abrechnungslauf des Sitzungsgelds speichert gesammelt (Issue #715).

Die Zahl der Abfragen wächst nicht mehr mit der Zahl der Positionen; jede Position behält ihren eigenen,
sprechenden Eintrag im Mandantenprotokoll, mit demselben Inhalt wie beim Speichern einzeln, und die Hash-Kette
bleibt lückenlos und prüfbar.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.common import audit_chain
from apps.common.models import AuditChainHead
from apps.common.tests.factories import UserFactory
from apps.session import audit
from apps.session.models import (
    SessionAllowance,
    SessionAllowanceRate,
    SessionAttendance,
    SessionAuditLog,
    SessionMeeting,
    SessionOrganization,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import allowance_service, audit_log_service

pytestmark = pytest.mark.django_db

BERLIN = ZoneInfo("Europe/Berlin")

#: Felder eines Protokolleintrags, die beim gesammelten und beim einzelnen Schreiben übereinstimmen müssen
INHALT = ("action", "model_name", "object_repr", "changes", "user_ref", "on_behalf_of_ref", "ip_address", "user_agent")


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    kasse: SessionUser
    tag: date
    anwesenheiten: list[SessionAttendance]

    def lauf(self, **extra: Any) -> dict[str, Any]:
        return allowance_service.generate_allowances(self.tenant, self.tag, self.tag, created_by=self.kasse, **extra)


def _welt(slug: str, anzahl: int) -> Welt:
    """Mandant mit einer Ratssitzung und ``anzahl`` anrechenbaren Anwesenheiten (jede fünfte im Vorsitz)."""
    tenant = SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug)
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", allowance_amount=Decimal("35.00"))
    SessionAllowanceRate.objects.create(organization=rat, role="chair", amount=Decimal("52.50"))
    role = SessionRole.objects.create(tenant=tenant, name=f"kasse-{slug}", can_manage_allowances=True)
    user = cast(Any, UserFactory)(email=f"kasse@{slug}.example.org")
    kasse = SessionUser.objects.create(user=user, tenant=tenant)
    kasse.roles.add(role)
    tag = timezone.localdate() - timedelta(days=10)
    sitzung = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=rat, start=datetime.combine(tag, time(18), tzinfo=BERLIN)
    )
    anwesenheiten = []
    for i in range(anzahl):
        person = SessionPerson.objects.create(tenant=tenant, given_name=f"Vorname{i}", family_name=f"Mitglied{i:03d}")
        anwesenheiten.append(
            SessionAttendance.objects.create(
                meeting=sitzung, person=person, status="present", role="chair" if i % 5 == 0 else "member"
            )
        )
    return Welt(tenant=tenant, rat=rat, kasse=kasse, tag=tag, anwesenheiten=anwesenheiten)


def _positionseintraege(tenant: SessionTenant) -> list[SessionAuditLog]:
    """Protokolleinträge zu Sitzungsgeld-Positionen in der Reihenfolge der Kette."""
    return list(SessionAuditLog.objects.filter(tenant=tenant, model_name="SessionAllowance").order_by("seq"))


def _konto(welt: Welt, name: str) -> SessionUser:
    user = cast(Any, UserFactory)(email=f"{name}@{welt.tenant.slug}.example.org")
    return SessionUser.objects.create(user=user, tenant=welt.tenant)


def _inhalt(eintrag: SessionAuditLog, konten: dict[str, str]) -> dict[str, Any]:
    """Vergleichbarer Inhalt eines Eintrags: Kennungen der Konten (je Mandant verschieden) durch Namen ersetzt."""
    werte = {name: getattr(eintrag, name) for name in INHALT}
    werte["changes"] = {
        feld: {seite: konten.get(wert, wert) for seite, wert in aenderung.items()}
        for feld, aenderung in werte["changes"].items()
    }
    return werte


def _pruefen(tenant: SessionTenant) -> None:
    ergebnis = audit_log_service.verify_tenant(tenant)
    assert ergebnis.ok, ergebnis.errors
    assert ergebnis.initialized and not ergebnis.warnings
    call_command("verify_audit_chain", tenant=tenant.slug, stdout=io.StringIO())


def _haelfte_wieder_aufnehmen(welt: Welt) -> int:
    """
    Erste Hälfte festgesetzt und automatisch storniert, zweite Hälfte noch ohne Position; danach sind alle
    Anwesenheiten wieder anrechenbar. Der nächste Lauf nimmt die erste Hälfte wieder auf und setzt die zweite fest.
    """
    haelfte = len(welt.anwesenheiten) // 2
    erste = [a.pk for a in welt.anwesenheiten[:haelfte]]
    zweite = [a.pk for a in welt.anwesenheiten[haelfte:]]
    SessionAttendance.objects.filter(pk__in=zweite).update(status="excused")
    assert welt.lauf()["created"] == haelfte
    SessionAttendance.objects.filter(pk__in=erste).update(status="absent")
    assert welt.lauf()["cancelled"] == haelfte
    SessionAttendance.objects.filter(pk__in=erste + zweite).update(status="present")
    return haelfte


@pytest.mark.parametrize("wiederaufnahme", [False, True], ids=["neu", "mit-wiederaufnahme"])
def test_abfragen_wachsen_nicht_mit_der_zahl_der_positionen(wiederaufnahme: bool) -> None:
    """10 und 100 Positionen kosten gleich viele Abfragen (vorher rund acht je Position), auch mit Wiederaufnahmen."""
    klein, gross = _welt("zehn", 10), _welt("hundert", 100)
    wieder = [_haelfte_wieder_aufnehmen(welt) if wiederaufnahme else 0 for welt in (klein, gross)]

    with CaptureQueriesContext(connection) as zehn:
        ergebnis_zehn = klein.lauf()
    with CaptureQueriesContext(connection) as hundert:
        ergebnis_hundert = gross.lauf()

    assert (ergebnis_zehn["reactivated"], ergebnis_hundert["reactivated"]) == (wieder[0], wieder[1])
    assert (ergebnis_zehn["created"], ergebnis_hundert["created"]) == (10 - wieder[0], 100 - wieder[1])
    assert len(hundert.captured_queries) == len(zehn.captured_queries), (
        len(zehn.captured_queries),
        len(hundert.captured_queries),
    )
    assert len(hundert.captured_queries) <= 20
    assert SessionAllowance.objects.filter(attendance__meeting__tenant=gross.tenant, status="pending").count() == 100
    _pruefen(klein.tenant)
    _pruefen(gross.tenant)


def test_fehler_beim_protokollieren_rollt_den_lauf_zurueck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheitert das Anhängen an die Kette, bleibt keine neue oder geänderte Position zurück, die Kette unverändert."""
    welt = _welt("rueckrollen", 4)
    _haelfte_wieder_aufnehmen(welt)
    positionen = SessionAllowance.objects.filter(attendance__meeting__tenant=welt.tenant)
    felder = ("pk", "status", "notes", "amount", "created_by_id", "approved_by_id", "updated_at")
    vorher = list(positionen.order_by("pk").values_list(*felder))
    kopf = AuditChainHead.objects.get(scope=audit_chain.SESSION.scope_key(welt.tenant.pk))
    eintraege = SessionAuditLog.objects.filter(tenant=welt.tenant).count()

    def kaputt(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Datenbank weg")

    monkeypatch.setattr(audit_chain, "append_many", kaputt)
    with pytest.raises(RuntimeError, match="Datenbank weg"):
        welt.lauf()

    assert list(positionen.order_by("pk").values_list(*felder)) == vorher
    assert len(vorher) == 2 and all(status == "cancelled" for _, status, *_ in vorher)
    nachher = AuditChainHead.objects.get(pk=kopf.pk)
    assert (nachher.last_seq, nachher.last_hash) == (kopf.last_seq, kopf.last_hash)
    assert SessionAuditLog.objects.filter(tenant=welt.tenant).count() == eintraege
    _pruefen(welt.tenant)


def test_wiederaufnahme_hebt_eine_fruehere_genehmigung_auf() -> None:
    """Genehmigt, dann storniert, dann wieder aufgenommen: Genehmigung leer, Änderung im Protokoll (Vier Augen)."""
    welt = _welt("genehmigt", 2)
    welt.lauf()
    pruefer = _konto(welt, "pruefer")
    anwesenheit = welt.anwesenheiten[0]
    position = SessionAllowance.objects.get(attendance=anwesenheit)
    assert allowance_service.approve_allowances([position], pruefer)["approved"] == 1

    anwesenheit.status = "absent"
    anwesenheit.save()
    assert welt.lauf()["cancelled"] == 1
    position.refresh_from_db()
    assert position.status == "cancelled" and position.approved_by_id == pruefer.pk
    assert position.approved_at is not None

    anwesenheit.status = "present"
    anwesenheit.save()
    assert welt.lauf()["reactivated"] == 1

    position.refresh_from_db()
    assert position.status == "pending"
    assert position.approved_by_id is None and position.approved_at is None
    eintrag = _positionseintraege(welt.tenant)[-1]
    assert (eintrag.action, eintrag.object_id) == ("update", position.pk)
    assert eintrag.changes["status"] == {"alt": "cancelled", "neu": "pending"}
    assert eintrag.changes["approved_by"] == {"alt": str(pruefer.pk), "neu": None}
    assert eintrag.changes["approved_at"]["alt"] and eintrag.changes["approved_at"]["neu"] is None
    _pruefen(welt.tenant)


def test_jede_position_mit_eigenem_eintrag_ueber_die_oberflaeche() -> None:
    """Abrechnungslauf aus der Ansicht: Beträge je Funktion, je Position ein Eintrag mit Konto, Kette intakt."""
    welt = _welt("oberflaeche", 12)
    client = Client()
    client.force_login(welt.kasse.user)
    zeitraum = {"from": welt.tag.isoformat(), "to": welt.tag.isoformat()}

    antwort = client.post(f"/session/{welt.tenant.slug}/allowances/generate/", zeitraum)

    assert antwort.status_code == 302
    positionen = {p.attendance_id: p for p in SessionAllowance.objects.filter(attendance__meeting__tenant=welt.tenant)}
    assert len(positionen) == 12
    for i, anwesenheit in enumerate(welt.anwesenheiten):
        position = positionen[anwesenheit.pk]
        assert position.amount == (Decimal("52.50") if i % 5 == 0 else Decimal("35.00"))
        assert (position.currency, position.status, position.created_by_id) == ("EUR", "pending", welt.kasse.pk)

    # Je Position genau ein Eintrag, in der Reihenfolge der Anwesenheiten, vor der Zusammenfassung des Laufs
    eintraege = _positionseintraege(welt.tenant)
    assert [e.object_id for e in eintraege] == [positionen[a.pk].pk for a in welt.anwesenheiten]
    for eintrag in eintraege:
        position = SessionAllowance.objects.select_related("attendance__person").get(pk=eintrag.object_id)
        assert eintrag.action == "allowance_created"
        assert eintrag.object_repr == str(position)
        assert eintrag.changes == {}
        assert eintrag.user_id == welt.kasse.pk
    seqs = [e.seq for e in eintraege if e.seq is not None]
    assert len(seqs) == 12 and seqs == list(range(seqs[0], seqs[0] + 12)), "lückenlos hintereinander"
    lauf = SessionAuditLog.objects.get(tenant=welt.tenant, model_name="SessionTenant", action="create")
    assert lauf.seq == seqs[-1] + 1 and lauf.changes["abrechnungslauf"]["erzeugt"] == 12
    _pruefen(welt.tenant)


def test_eintraege_wie_beim_speichern_einzeln() -> None:
    """Festsetzen und Wiederaufnahme: Einträge mit demselben Inhalt wie auf dem bisherigen Weg (Signale)."""
    gesammelt, einzeln = _welt("gesammelt", 3), _welt("einzeln", 3)

    # Festsetzen: gesammelt im Lauf, zum Vergleich je Position einzeln gespeichert wie bisher
    assert gesammelt.lauf()["created"] == 3
    for anwesenheit in einzeln.anwesenheiten:
        SessionAllowance.objects.create(
            attendance=anwesenheit,
            amount=allowance_service.rate_for(einzeln.rat, anwesenheit.role),
            currency="EUR",
            status="pending",
            created_by=einzeln.kasse,
        )

    # Grundlage entfällt und kehrt zurück: der Lauf storniert und nimmt wieder auf
    konten: dict[str, dict[str, str]] = {}
    for welt in (gesammelt, einzeln):
        welt.anwesenheiten[1].status = "absent"
        welt.anwesenheiten[1].save()
        assert welt.lauf()["cancelled"] == 1
        welt.anwesenheiten[1].status = "present"
        welt.anwesenheiten[1].save()
        zweites = _konto(welt, "zweites")  # Wiederaufnahme durch ein anderes Konto
        konten[welt.tenant.slug] = {str(welt.kasse.pk): "<erstes>", str(zweites.pk): "<zweites>"}
        welt.kasse = zweites
    ergebnis = gesammelt.lauf()
    assert (ergebnis["reactivated"], ergebnis["created"], ergebnis["skipped_existing"]) == (1, 0, 2)
    wieder = SessionAllowance.objects.get(attendance=einzeln.anwesenheiten[1])
    wieder.status, wieder.created_by, wieder.notes = "pending", einzeln.kasse, ""
    wieder.save(update_fields=["status", "created_by", "notes", "updated_at"])

    erwartet = [_inhalt(e, konten["einzeln"]) for e in _positionseintraege(einzeln.tenant)]
    tatsaechlich = [_inhalt(e, konten["gesammelt"]) for e in _positionseintraege(gesammelt.tenant)]
    assert [e["action"] for e in tatsaechlich] == ["allowance_created"] * 3 + ["allowance_cancelled", "update"]
    assert tatsaechlich == erwartet
    assert tatsaechlich[-1]["changes"] == {
        "status": {"alt": "cancelled", "neu": "pending"},
        "created_by": {"alt": "<erstes>", "neu": "<zweites>"},
        "notes": {"alt": allowance_service.AUTO_CANCEL_NOTE, "neu": ""},
    }

    position = SessionAllowance.objects.get(attendance=gesammelt.anwesenheiten[1])
    assert (position.status, position.notes, position.created_by_id) == ("pending", "", gesammelt.kasse.pk)
    assert position.approved_by_id is None and position.approved_at is None
    assert position.updated_at > position.created_at
    _pruefen(gesammelt.tenant)


def test_altbestand_ohne_scharfe_kette_bleibt_unverkettet() -> None:
    """Wie beim einzelnen Schreiben: Solange der Altbestand nicht verkettet ist, kommen Einträge ohne Nummer hinzu."""
    welt = _welt("altbestand", 3)
    scope = audit_chain.SESSION.scope_key(welt.tenant.pk)
    AuditChainHead.objects.filter(scope=scope).update(initialized=False)
    kopf = AuditChainHead.objects.get(scope=scope)

    welt.lauf()

    eintraege = _positionseintraege(welt.tenant)
    assert len(eintraege) == 3
    assert all(e.seq is None and e.entry_hash is None for e in eintraege)
    nachher = AuditChainHead.objects.get(scope=scope)
    assert (nachher.last_seq, nachher.last_hash) == (kopf.last_seq, kopf.last_hash)


def test_gesammelt_nur_eintraege_eines_mandanten() -> None:
    erster, zweiter = _welt("erster", 0), _welt("zweiter", 0)
    eintraege = [audit.build_event("update", welt.tenant, tenant=welt.tenant) for welt in (erster, zweiter)]

    with pytest.raises(audit_chain.ChainError):
        audit.log_events(eintraege)

    assert not SessionAuditLog.objects.filter(pk__in=[e.pk for e in eintraege]).exists()
    with CaptureQueriesContext(connection) as ctx:
        assert audit.log_events([None]) == []
    assert not ctx.captured_queries


def test_gespeicherte_eintraege_nicht_noch_einmal() -> None:
    welt = _welt("gespeichert", 0)
    eintrag = audit.log_event("update", welt.tenant, tenant=welt.tenant)
    kopf = AuditChainHead.objects.get(scope=audit_chain.SESSION.scope_key(welt.tenant.pk))

    with pytest.raises(audit_chain.ChainError, match="Bereits gespeicherte"):
        audit.log_events([eintrag])

    nachher = AuditChainHead.objects.get(pk=kopf.pk)
    assert (nachher.last_seq, nachher.last_hash) == (kopf.last_seq, kopf.last_hash)
    _pruefen(welt.tenant)
