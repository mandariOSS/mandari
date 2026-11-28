# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Textbausteine als Befehle (Issue #1588): das Beispiel der Befehlsschicht in Session.

Beide Wege laufen durch dieselbe Fachfunktion (``apps/session/befehle/textbausteine.py``):

- **Oberfläche:** Die View liest das Formular und ruft ``ausfuehren``; Meldungen kommen aus dem Ergebnis.
- **Schnittstelle:** ``POST /api/mandari/v1/session/<mandant>/befehle/session.textblock.<befehl>/v1`` mit der
  Browser-Sitzung oder einem Access-Token im Namen einer Person (Scope ``sitzung:schreiben``).

Geprüft werden Rechte nach dem Rechtemodell von Session, Mandantentrennung, Prüfprotokoll mit Weg und Anwendung, die
Fehler für das Formular und der Schutz gegen doppelt abgeschickte Formulare.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.oidc import catalog
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import enable_2fa
from apps.common.api.idempotency import FORM_FIELD
from apps.common.tests.factories import UserFactory
from apps.session import befehle
from apps.session.befehle import textbausteine
from apps.session.models import SessionAuditLog, SessionRole, SessionTenant, SessionTextBlock, SessionUser
from hub.commands import get_dispatcher

pytestmark = pytest.mark.django_db

SCOPE = catalog.SITZUNG_SCHREIBEN


@pytest.fixture(autouse=True)
def _cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt")


@pytest.fixture
def fremd() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Fremdstadt", slug="fremdstadt")


def konto(tenant: SessionTenant, **rechte: bool) -> tuple[User, SessionUser]:
    user = cast(User, UserFactory())  # type: ignore[no-untyped-call]
    session_user = SessionUser.objects.create(user=user, tenant=tenant, is_active=True)
    rolle = SessionRole.objects.create(tenant=tenant, name=f"Rolle {uuid.uuid4().hex[:8]}", **rechte)
    session_user.roles.add(rolle)
    return user, session_user


@pytest.fixture
def verwaltung(tenant: SessionTenant) -> tuple[User, SessionUser]:
    return konto(tenant, can_manage_settings=True)


def _oberflaeche(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _formular(user: User, tenant: SessionTenant, **daten: str) -> tuple[Any, list[str]]:
    """Formular abschicken, je Aufruf mit eigener Sitzung: Die Meldungen sind die dieser Anfrage."""
    response = _oberflaeche(user).post(reverse("session:textblock_manage", kwargs={"tenant_slug": tenant.slug}), daten)
    return response, [str(meldung) for meldung in get_messages(response.wsgi_request)]


def _api(client: Client, tenant: SessionTenant, befehl: str, inhalt: dict[str, Any], **kopf: str) -> Any:
    url = f"/api/mandari/v1/session/{tenant.slug}/befehle/{befehl}/v1"
    headers = {"Idempotency-Key": str(uuid.uuid4()), **kopf}
    return client.post(url, data=json.dumps(inhalt), content_type="application/json", headers=headers)


def _baustein(tenant: SessionTenant, **felder: Any) -> SessionTextBlock:
    werte = {"title": "Einstimmig", "content": "Der {gremium} beschließt einstimmig:", **felder}
    return SessionTextBlock.objects.create(tenant=tenant, **werte)


# =============================================================================
# Oberfläche
# =============================================================================


class TestOberflaeche:
    def test_anlegen(self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]) -> None:
        user, session_user = verwaltung
        response, meldungen = _formular(
            user,
            tenant,
            title="  Einstimmig beschlossen ",
            content="Der {gremium} beschließt einstimmig:",
            category="resolution",
            order="7",
        )

        assert response.status_code == 302
        assert meldungen == ["Textbaustein „Einstimmig beschlossen“ angelegt."]
        baustein = SessionTextBlock.objects.get(tenant=tenant)
        assert (baustein.title, baustein.category, baustein.order, baustein.is_active) == (
            "Einstimmig beschlossen",
            "resolution",
            7,
            True,
        )
        eintrag = SessionAuditLog.objects.get(tenant=tenant, object_id=baustein.pk, action="create")
        assert eintrag.user_id == session_user.pk
        assert eintrag.changes == {"titel": "Einstimmig beschlossen", "kategorie": "resolution", "weg": "oberflaeche"}

    def test_aendern_und_loeschen(self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]) -> None:
        baustein = _baustein(tenant)
        user = verwaltung[0]

        _, meldungen = _formular(
            user,
            tenant,
            block_id=str(baustein.pk),
            title="Mehrheitlich",
            content="Der Rat beschließt:",
            is_active="0",
        )
        assert meldungen == ["Textbaustein „Mehrheitlich“ gespeichert."]
        baustein.refresh_from_db()
        assert (baustein.title, baustein.content, baustein.is_active) == ("Mehrheitlich", "Der Rat beschließt:", False)

        _, meldungen = _formular(user, tenant, block_id=str(baustein.pk), action="delete")
        assert meldungen == ["Textbaustein gelöscht."]
        assert not SessionTextBlock.objects.filter(pk=baustein.pk).exists()
        assert SessionAuditLog.objects.filter(tenant=tenant, object_id=baustein.pk, action="delete").exists()

    def test_fehler_fuer_das_formular(self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]) -> None:
        user = verwaltung[0]

        _, meldungen = _formular(user, tenant, title="   ", content="x" * 20001, category="unbekannt")

        assert meldungen == [
            "Bitte die Angaben prüfen: Kategorie hat einen nicht vorgesehenen Wert; Text ist zu lang; "
            "Titel ist leer oder zu kurz."
        ]
        assert not SessionTextBlock.objects.exists()

    def test_doppelt_abgeschickt_wirkt_einmal(
        self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]
    ) -> None:
        user = verwaltung[0]
        daten = {"title": "Einstimmig", "content": "Der Rat beschließt:", FORM_FIELD: str(uuid.uuid4())}

        _, erste = _formular(user, tenant, **daten)
        _, zweite = _formular(user, tenant, **daten)

        assert erste == zweite == ["Textbaustein „Einstimmig“ angelegt."]
        assert SessionTextBlock.objects.filter(tenant=tenant).count() == 1

    def test_baustein_eines_anderen_mandanten(
        self, tenant: SessionTenant, fremd: SessionTenant, verwaltung: tuple[User, SessionUser]
    ) -> None:
        fremder = _baustein(fremd)
        user = verwaltung[0]

        _, aendern = _formular(user, tenant, block_id=str(fremder.pk), title="Gekapert", content="x")
        _, loeschen = _formular(user, tenant, block_id=str(fremder.pk), action="delete")

        assert aendern == loeschen == [textbausteine.UNBEKANNT]
        fremder.refresh_from_db()
        assert fremder.title == "Einstimmig"

    def test_seite_zeigt_den_schluessel_im_formular(
        self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]
    ) -> None:
        response = _oberflaeche(verwaltung[0]).get(
            reverse("session:settings_textblocks", kwargs={"tenant_slug": tenant.slug})
        )
        assert response.status_code == 200
        assert f'name="{FORM_FIELD}"' in response.content.decode()


# =============================================================================
# Schnittstelle
# =============================================================================


class TestSchnittstelle:
    def test_sitzung_der_oberflaeche(self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]) -> None:
        response = _api(
            _oberflaeche(verwaltung[0]), tenant, textbausteine.ANLEGEN, {"title": "Einstimmig", "content": "Text"}
        )

        assert response.status_code == 201, response.content
        quittung = response.json()
        baustein = SessionTextBlock.objects.get(tenant=tenant)
        assert quittung["reference"] == quittung["aggregate_id"] == str(baustein.pk)
        assert quittung["command"] == textbausteine.ANLEGEN

    def test_access_token_braucht_api_zugang(self, tenant: SessionTenant) -> None:
        mit, _ = konto(tenant, can_manage_settings=True, can_access_api=True)
        ohne, _ = konto(tenant, can_manage_settings=True)
        app = h.anwendung(name="Ratsapp", scopes=["openid", SCOPE])[0]
        inhalt = {"title": "Einstimmig", "content": "Text"}

        with h.eingeschaltet():
            for person in (mit, ohne):
                enable_2fa(person)
            erlaubt = _api(Client(), tenant, textbausteine.ANLEGEN, inhalt, Authorization=f"Bearer {_token(app, mit)}")
            verwehrt = _api(
                Client(), tenant, textbausteine.ANLEGEN, inhalt, Authorization=f"Bearer {_token(app, ohne)}"
            )

        assert erlaubt.status_code == 201, erlaubt.content
        assert verwehrt.status_code == 403
        assert verwehrt.json()["detail"] == befehle.KEIN_API_ZUGANG
        eintrag = SessionAuditLog.objects.get(tenant=tenant, action="create", model_name="SessionTextBlock")
        assert eintrag.changes["weg"] == "schnittstelle"
        assert eintrag.changes["anwendung"] == app.client_id

    @pytest.mark.parametrize(
        ("rechte", "detail"),
        [({"can_manage_settings": False, "can_view_meetings": True}, befehle.KEIN_RECHT), (None, befehle.KEIN_ZUGANG)],
    )
    def test_rechte_im_mandanten(
        self, tenant: SessionTenant, fremd: SessionTenant, rechte: dict[str, bool] | None, detail: str
    ) -> None:
        # Ohne Recht im Mandanten bzw. mit Zugang nur zu einem anderen Mandanten
        user, _ = konto(tenant, **rechte) if rechte else konto(fremd, can_manage_settings=True)
        response = _api(_oberflaeche(user), tenant, textbausteine.ANLEGEN, {"title": "T", "content": "Text"})

        assert response.status_code == 403
        assert response.json()["detail"] == detail
        assert not SessionTextBlock.objects.exists()

    def test_nur_leerzeichen_422_am_feld(self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]) -> None:
        response = _api(_oberflaeche(verwaltung[0]), tenant, textbausteine.ANLEGEN, {"title": "   ", "content": "Text"})

        assert response.status_code == 422
        assert response.json()["errors"] == [{"pointer": "/title", "detail": befehle.NUR_LEERZEICHEN}]

    def test_aendern_nur_genannte_felder(self, tenant: SessionTenant, verwaltung: tuple[User, SessionUser]) -> None:
        baustein = _baustein(tenant, category="protocol", order=3)
        response = _api(
            _oberflaeche(verwaltung[0]), tenant, textbausteine.AENDERN, {"textblock": str(baustein.pk), "order": 9}
        )

        assert response.status_code == 201, response.content
        baustein.refresh_from_db()
        assert (baustein.title, baustein.category, baustein.order) == ("Einstimmig", "protocol", 9)

    def test_unbekannter_mandant(self, verwaltung: tuple[User, SessionUser]) -> None:
        url_mandant = SessionTenant(slug="nirgendwo")
        response = _api(
            _oberflaeche(verwaltung[0]), url_mandant, textbausteine.LOESCHEN, {"textblock": str(uuid.uuid4())}
        )
        assert response.status_code == 404
        assert response.json()["type"].endswith("/mandant-unbekannt")


def _token(app: Any, user: User) -> str:
    return h.access_token(app, user, scope=f"openid {SCOPE}")


# =============================================================================
# Ein Weg
# =============================================================================


@pytest.mark.parametrize("name", [textbausteine.ANLEGEN, textbausteine.AENDERN, textbausteine.LOESCHEN])
def test_handler_ist_die_fachfunktion_und_freigegeben(name: str) -> None:
    """Oberfläche und Schnittstelle rufen denselben registrierten Handler: die Fachfunktion hinter ``@befehl``."""
    handler = get_dispatcher().handlers[(name, 1)]
    fachfunktion = {
        textbausteine.ANLEGEN: textbausteine.anlegen,
        textbausteine.AENDERN: textbausteine.aendern,
        textbausteine.LOESCHEN: textbausteine.loeschen,
    }[name]
    assert handler.__wrapped__ is fachfunktion  # type: ignore[attr-defined]
    assert get_dispatcher().exposure(name, 1) == befehle.FREIGABE


def test_beide_wege_ergeben_denselben_baustein(tenant: SessionTenant, verwaltung: tuple[User, SessionUser]) -> None:
    user = verwaltung[0]
    _formular(user, tenant, title="Gleich", content="Der Rat beschließt:", category="general", order="2")
    _api(
        _oberflaeche(user),
        tenant,
        textbausteine.ANLEGEN,
        {"title": "Gleich", "content": "Der Rat beschließt:", "category": "general", "order": 2, "is_active": True},
    )

    felder = ("title", "content", "category", "order", "is_active")
    erster, zweiter = (tuple(getattr(b, f) for f in felder) for b in SessionTextBlock.objects.filter(tenant=tenant))
    assert erster == zweiter
    eintraege = SessionAuditLog.objects.filter(tenant=tenant, action="create", model_name="SessionTextBlock")
    wege = sorted(eintrag.changes["weg"] for eintrag in eintraege)
    assert wege == ["oberflaeche", "oberflaeche"]


def test_eintraege_aus_signalen_nennen_die_person_des_befehls(
    tenant: SessionTenant, fremd: SessionTenant, verwaltung: tuple[User, SessionUser]
) -> None:
    """Über die Schnittstelle trägt die Anfrage keine Session-Person; das Prüfprotokoll nennt trotzdem, wer handelt."""
    from apps.session import audit

    baustein = _baustein(tenant)
    _, fremde_person = konto(fremd, can_manage_settings=True)

    assert audit.build_event("update", baustein).user is None
    with audit.im_befehl(verwaltung[1]):
        assert audit.build_event("update", baustein).user == verwaltung[1]
        # Ausdrücklich genannt geht vor
        assert audit.build_event("update", baustein, user=fremde_person).user == fremde_person
    with audit.im_befehl(fremde_person):
        # Nie eine Person eines anderen Mandanten
        assert audit.build_event("update", baustein).user is None
