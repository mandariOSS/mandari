# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hash-Kette der Protokolle (Issue #221): Verkettung, Prüfung, Manipulationserkennung, Altbestand.

Manipulationen werden wie ein Angreifer mit Datenbankzugriff simuliert – per Raw-SQL an der
Anwendung vorbei. Das Prüfwerkzeug muss jede Änderung, jede Löschung und jeden nachträglich
eingefügten Eintrag erkennen und mit Exit-Code ungleich 0 melden.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from io import StringIO
from typing import Any, cast
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.accounts.models import SecurityAuditLog, User
from apps.common import audit_chain
from apps.common.models import AuditChainHead
from apps.common.tests.factories import UserFactory
from apps.session import audit
from apps.session.models import SessionAuditLog, SessionOrganization, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db


def _tenant(slug: str) -> SessionTenant:
    return SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug)


def _entries(tenant: SessionTenant, count: int) -> list[SessionAuditLog]:
    org = SessionOrganization.objects.create(tenant=tenant, name=f"Ausschuss {tenant.slug}")
    result = []
    for number in range(count):
        entry = audit.log_event("update", org, tenant=tenant, changes={"nr": number})
        result.append(cast(SessionAuditLog, entry))
    return result


def _raw(sql: str, *params: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, list(params))


def _db_id(entry: SessionAuditLog) -> Any:
    """Primärschlüssel in der Darstellung der Datenbank (SQLite speichert UUIDs ohne Bindestriche)."""
    return SessionAuditLog._meta.pk.get_db_prep_value(entry.pk, connection)


def _verify(tenant: SessionTenant) -> audit_chain.VerifyResult:
    return audit_chain.verify(audit_chain.SESSION, tenant.pk)


class TestVerkettung:
    def test_eintraege_sind_lueckenlos_verkettet(self) -> None:
        tenant = _tenant("kette")
        eintraege = SessionAuditLog.objects.filter(tenant=tenant).order_by("seq")
        _entries(tenant, 3)
        seqs = list(eintraege.values_list("seq", flat=True))
        assert seqs == list(range(1, len(seqs) + 1))
        erster = eintraege.first()
        assert erster is not None and erster.prev_hash == audit_chain.GENESIS_HASH
        vorher = audit_chain.GENESIS_HASH
        for eintrag in eintraege:
            assert eintrag.prev_hash == vorher
            assert eintrag.entry_hash == audit_chain.compute_hash(audit_chain.SESSION, eintrag)
            vorher = eintrag.entry_hash
        kopf = AuditChainHead.objects.get(scope=f"session:{tenant.pk}")
        assert kopf.initialized and kopf.last_seq == len(seqs) and kopf.last_hash == vorher
        assert _verify(tenant).ok

    def test_jeder_mandant_hat_eine_eigene_kette(self) -> None:
        a, b = _tenant("a-stadt"), _tenant("b-stadt")
        _entries(a, 2)
        _entries(b, 2)
        assert SessionAuditLog.objects.filter(tenant=b, seq=1).count() == 1
        assert _verify(a).ok and _verify(b).ok

    def test_eintraege_bleiben_unveraenderbar(self) -> None:
        tenant = _tenant("fest")
        eintrag = _entries(tenant, 1)[0]
        eintrag.object_repr = "anders"
        with pytest.raises(ValueError, match="unveränderbar"):
            eintrag.save()
        with pytest.raises(ValueError, match="unveränderbar"):
            eintrag.delete()

    def test_doppelte_nummer_verhindert_die_datenbank(self) -> None:
        tenant = _tenant("doppelt")
        eintrag = _entries(tenant, 1)[0]
        kopie = SessionAuditLog(
            tenant=tenant, action="update", model_name="X", object_id=uuid.uuid4(), seq=eintrag.seq, entry_hash="x"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionAuditLog.objects.bulk_create([kopie])

    def test_werte_ueberstehen_die_rundreise_durch_die_datenbank(self) -> None:
        """Dezimalzahlen, große Gleitkommazahlen, IPv6 und Uhrzeiten dürfen den Hash nicht brechen."""
        tenant = _tenant("rundreise")
        org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        request = mock.Mock(session={}, META={"HTTP_X_FORWARDED_FOR": "::ffff:10.1.2.3", "HTTP_USER_AGENT": "UA\x00"})
        request.session_user = None
        audit.log_event(
            "update",
            org,
            tenant=tenant,
            request=request,
            changes={"betrag": Decimal("12.50"), "gross": 1e20, "anteil": 0.1, "am": dt.date(2026, 9, 1)},
        )
        eintrag = SessionAuditLog.objects.filter(tenant=tenant).order_by("-seq").first()
        assert eintrag is not None
        assert eintrag.ip_address == "10.1.2.3"
        assert eintrag.user_agent == "UA"
        assert eintrag.changes["betrag"] == "12.50"
        assert _verify(tenant).ok

    def test_ungueltige_ip_wird_verworfen_statt_zu_scheitern(self) -> None:
        tenant = _tenant("ip")
        org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        request = mock.Mock(session={}, META={"HTTP_X_FORWARDED_FOR": "unbekannt", "HTTP_USER_AGENT": ""})
        request.session_user = None
        eintrag = audit.log_event("update", org, tenant=tenant, request=request)
        assert eintrag is not None and eintrag.ip_address is None
        assert _verify(tenant).ok

    def test_geloeschter_nutzer_bricht_die_kette_nicht(self) -> None:
        tenant = _tenant("konto")
        user = cast(User, UserFactory(email="weg@example.org"))  # type: ignore[no-untyped-call]
        session_user = SessionUser.objects.create(user=user, tenant=tenant)
        org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
        audit.log_event("update", org, tenant=tenant, user=session_user)
        eintrag = SessionAuditLog.objects.get(tenant=tenant, user=session_user, model_name="SessionOrganization")
        assert eintrag.user_ref == session_user.pk
        user.delete()  # kaskadiert den Session-Nutzer, das Protokoll setzt user auf NULL
        eintrag.refresh_from_db()
        assert eintrag.user_id is None and eintrag.user_ref is not None
        assert _verify(tenant).ok


class TestManipulationErkennen:
    def test_raw_sql_aenderung_wird_erkannt(self) -> None:
        tenant = _tenant("aenderung")
        eintraege = _entries(tenant, 3)
        _raw("UPDATE session_audit_logs SET object_repr = %s WHERE id = %s", "gefälscht", _db_id(eintraege[1]))
        ergebnis = _verify(tenant)
        assert not ergebnis.ok
        assert any("nachträglich verändert" in fehler for fehler in ergebnis.errors)

    def test_geaenderte_aenderungsdaten_werden_erkannt(self) -> None:
        tenant = _tenant("diff")
        eintrag = _entries(tenant, 2)[0]
        cast_jsonb = "::jsonb" if connection.vendor == "postgresql" else ""
        _raw(f"UPDATE session_audit_logs SET changes = %s{cast_jsonb} WHERE id = %s", '{"nr": 99}', _db_id(eintrag))
        assert not _verify(tenant).ok

    def test_geloeschter_eintrag_in_der_mitte_wird_erkannt(self) -> None:
        tenant = _tenant("mitte")
        eintraege = _entries(tenant, 4)
        _raw("DELETE FROM session_audit_logs WHERE id = %s", _db_id(eintraege[1]))
        ergebnis = _verify(tenant)
        assert not ergebnis.ok
        assert any("Lücke" in fehler for fehler in ergebnis.errors)

    def test_geloeschter_letzter_eintrag_wird_erkannt(self) -> None:
        tenant = _tenant("ende")
        _entries(tenant, 3)
        letzter = SessionAuditLog.objects.filter(tenant=tenant).order_by("-seq").first()
        assert letzter is not None
        _raw("DELETE FROM session_audit_logs WHERE id = %s", _db_id(letzter))
        ergebnis = _verify(tenant)
        assert not ergebnis.ok
        assert any("Kettenkopf" in fehler for fehler in ergebnis.errors)

    def test_nachtraeglich_eingefuegter_eintrag_wird_erkannt(self) -> None:
        tenant = _tenant("eingefuegt")
        _entries(tenant, 2)
        SessionAuditLog.objects.bulk_create(
            [SessionAuditLog(tenant=tenant, action="view", model_name="X", object_id=uuid.uuid4())]
        )
        ergebnis = _verify(tenant)
        assert not ergebnis.ok
        assert any("außerhalb der Kette" in fehler for fehler in ergebnis.errors)

    def test_pruefbefehl_meldet_manipulation_mit_exit_code(self) -> None:
        tenant = _tenant("befehl")
        eintraege = _entries(tenant, 2)
        out = StringIO()
        call_command("verify_audit_chain", tenant=tenant.slug, stdout=out)
        assert "intakt" in out.getvalue()

        _raw("UPDATE session_audit_logs SET action = %s WHERE id = %s", "delete", _db_id(eintraege[0]))
        with pytest.raises(CommandError) as fehler:
            call_command("verify_audit_chain", tenant=tenant.slug, stdout=StringIO(), stderr=StringIO())
        assert fehler.value.returncode == 1

    def test_pruefbefehl_prueft_alle_ketten(self) -> None:
        _entries(_tenant("alle"), 1)
        out = StringIO()
        call_command("verify_audit_chain", stdout=out)
        assert "Session alle: intakt" in out.getvalue()
        assert "Sicherheitsprotokoll" in out.getvalue()


class TestAltbestand:
    def _altbestand(self, tenant: SessionTenant, anzahl: int) -> list[SessionAuditLog]:
        """Einträge wie vor der Kette: ohne Nummer, mit alten Zeitpunkten (am Modell vorbei)."""
        user = cast(User, UserFactory(email=f"alt-{tenant.slug}@example.org"))  # type: ignore[no-untyped-call]
        # Nutzer in einem Hilfsmandanten: sein Anlegen schreibt dort, nicht in die Kette von tenant
        session_user = SessionUser.objects.create(user=user, tenant=_tenant(f"hilf-{tenant.slug}"))
        start = timezone.now() - dt.timedelta(days=400)
        alte = [
            SessionAuditLog(
                tenant=tenant,
                user=session_user,
                action="update",
                model_name="SessionMeeting",
                object_id=uuid.uuid4(),
                object_repr=f"alt {n}",
                changes={"n": n},
                created_at=start + dt.timedelta(minutes=n),
            )
            for n in range(anzahl)
        ]
        return SessionAuditLog.objects.bulk_create(alte)

    def test_neue_eintraege_warten_bis_zur_verkettung_des_altbestands(self) -> None:
        tenant = _tenant("altstadt")
        alte = self._altbestand(tenant, 5)
        neu = _entries(tenant, 2)
        assert all(e.seq is None for e in neu), "Ohne verketteten Altbestand keine Nummern vergeben"
        vorher = _verify(tenant)
        assert vorher.ok and vorher.warnings

        verkettet = audit_chain.backfill(audit_chain.SESSION, tenant.pk, batch_size=3)
        assert verkettet >= len(alte) + len(neu)
        nachher = _verify(tenant)
        assert nachher.ok and nachher.initialized and not nachher.warnings
        # Altbestand steht in Zeitreihenfolge vorn und hat die Nutzer-Referenz bekommen
        erste = SessionAuditLog.objects.get(tenant=tenant, seq=1)
        assert erste.object_repr == "alt 0" and erste.user_ref is not None

        # Danach wird wieder sofort verkettet; ein zweiter Lauf tut nichts
        _entries(tenant, 1)
        assert not SessionAuditLog.objects.filter(tenant=tenant, seq__isnull=True).exists()
        assert audit_chain.backfill(audit_chain.SESSION, tenant.pk) == 0

    def test_backfill_befehl(self) -> None:
        tenant = _tenant("befehlstadt")
        self._altbestand(tenant, 3)
        out = StringIO()
        call_command("audit_chain_backfill", tenant=tenant.slug, stdout=out)
        assert "3 Einträge verkettet" in out.getvalue()
        assert _verify(tenant).ok


class TestSicherheitsprotokoll:
    def test_globale_kette(self) -> None:
        for event in ("login", "logout"):
            SecurityAuditLog.objects.create(event=event, user_ref=uuid.uuid4(), ip_address="192.0.2.1")
        ergebnis = audit_chain.verify(audit_chain.SECURITY, None)
        assert ergebnis.ok and ergebnis.checked >= 2
        eintrag = SecurityAuditLog.objects.order_by("-seq").first()
        assert eintrag is not None
        _raw(
            "UPDATE accounts_securityauditlog SET event = %s WHERE id = %s",
            "login_failed",
            SecurityAuditLog._meta.pk.get_db_prep_value(eintrag.pk, connection),
        )
        assert not audit_chain.verify(audit_chain.SECURITY, None).ok
