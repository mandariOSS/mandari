# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Veröffentlichung im Bürgerportal beenden: drei Möglichkeiten, umkehrbar, mit Bestätigung und Audit
(Issue #618). Die Wirkung auf Seiten, Suche, Sitemaps und OParl prüft
``insight_core/tests/test_veroeffentlichung_beendet.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.core.cache import cache
from django.core.management import call_command

from apps.session.models import SessionAuditLog, SessionTenant
from apps.session.services import insight_service, portal_publication, tenant_provisioning
from apps.session.tests._niederschrift import client as angemeldet
from apps.session.tests._niederschrift import nutzer
from insight_core import publication
from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

PAUSED = SessionTenant.PORTAL_END_PAUSED
ARCHIVED = SessionTenant.PORTAL_END_ARCHIVED
WITHDRAWN = SessionTenant.PORTAL_END_WITHDRAWN


@pytest.fixture(autouse=True)
def _leerer_cache() -> Any:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def welt() -> dict[str, Any]:
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord", insight_publish=True)
    source = OParlSource.objects.get(sync_config__session_tenant="nord")
    basis = source.url
    body = OParlBody.objects.create(external_id=f"{basis}body/", source=source, name="Bezirk Nord", slug="bezirk-nord")
    eintraege = [
        OParlMeeting.objects.create(external_id=f"{basis}meeting/{uuid.uuid4()}/", body=body, name="Sitzung"),
        OParlPaper.objects.create(external_id=f"{basis}paper/{uuid.uuid4()}/", body=body, name="Radweg"),
    ]
    return {"tenant": tenant, "source": source, "body": body, "eintraege": eintraege}


def _frisch(welt: dict[str, Any]) -> SessionTenant:
    return SessionTenant.objects.get(pk=welt["tenant"].pk)


def _stand(welt: dict[str, Any]) -> tuple[bool, bool, str | None, list[bool]]:
    """(Quelle aktiv, Kommune gelistet, Stand im Bürgerportal, gelöscht je Eintrag)"""
    welt["source"].refresh_from_db()
    welt["body"].refresh_from_db()
    state = publication.source_state(welt["source"])
    geloescht = [type(obj).objects.get(pk=obj.pk).deleted for obj in welt["eintraege"]]
    return welt["source"].is_active, welt["body"].is_listed, state.mode if state else None, geloescht


class TestService:
    def test_voruebergehend(self, welt: dict[str, Any]) -> None:
        change = portal_publication.end_publication(_frisch(welt), PAUSED)

        assert change is not None and change.entries == 0
        assert _stand(welt) == (False, True, publication.PAUSED, [False, False])
        tenant = _frisch(welt)
        assert (tenant.insight_publish, tenant.insight_end_mode) == (False, PAUSED)

    def test_archiv(self, welt: dict[str, Any]) -> None:
        portal_publication.end_publication(_frisch(welt), ARCHIVED)
        assert _stand(welt) == (False, True, publication.ARCHIVED, [False, False])

    def test_dauerhaft(self, welt: dict[str, Any]) -> None:
        change = portal_publication.end_publication(_frisch(welt), WITHDRAWN)

        assert change is not None and change.entries == 2
        assert _stand(welt) == (False, False, publication.WITHDRAWN, [True, True])

    def test_von_dauerhaft_zum_archiv_kommt_bestand_zurueck(self, welt: dict[str, Any]) -> None:
        portal_publication.end_publication(_frisch(welt), WITHDRAWN)

        change = portal_publication.end_publication(_frisch(welt), ARCHIVED)

        assert change is not None and change.entries == 2
        assert _stand(welt) == (False, True, publication.ARCHIVED, [False, False])
        assert insight_service.RETRACTION_KEY not in welt["source"].sync_config

    @pytest.mark.parametrize("mode", [PAUSED, ARCHIVED, WITHDRAWN])
    def test_wieder_veroeffentlichen(self, welt: dict[str, Any], mode: str) -> None:
        portal_publication.end_publication(_frisch(welt), mode)

        portal_publication.resume_publication(_frisch(welt))

        assert _stand(welt) == (True, True, None, [False, False])
        tenant = _frisch(welt)
        assert (tenant.insight_publish, tenant.insight_end_mode) == (True, "")

    def test_audit_mit_zusammenfassung(self, welt: dict[str, Any]) -> None:
        portal_publication.end_publication(_frisch(welt), WITHDRAWN)
        portal_publication.resume_publication(_frisch(welt))

        eintraege = list(
            SessionAuditLog.objects.filter(tenant=welt["tenant"], object_repr="Veröffentlichung im Bürgerportal")
            .order_by("seq")
            .values_list("action", "changes")
        )
        assert [aktion for aktion, _ in eintraege] == ["unpublish", "publish"]
        assert eintraege[0][1]["buergerportal"] == {"alt": "veröffentlicht", "neu": "Dauerhaft zurücknehmen"}
        assert eintraege[0][1]["wirkung"]["eintraege"] == 2

    def test_gleiche_moeglichkeit_nochmals_ohne_wirkung(self, welt: dict[str, Any]) -> None:
        portal_publication.end_publication(_frisch(welt), ARCHIVED)
        assert portal_publication.end_publication(_frisch(welt), ARCHIVED) is None
        with pytest.raises(ValueError):
            portal_publication.end_publication(_frisch(welt), "irgendwas")

    def test_deaktivieren_und_reaktivieren_behaelt_archiv(self, welt: dict[str, Any]) -> None:
        portal_publication.end_publication(_frisch(welt), ARCHIVED)

        tenant_provisioning.set_tenant_active(_frisch(welt), False)
        assert _stand(welt) == (False, False, None, [True, True])

        tenant_provisioning.set_tenant_active(_frisch(welt), True)
        assert _stand(welt) == (False, True, publication.ARCHIVED, [False, False])

    def test_alter_weg_ohne_moeglichkeit_bleibt_wie_bisher(self, welt: dict[str, Any]) -> None:
        call_command("session_insight_source", "--tenant", "nord", "--deactivate")
        assert _stand(welt) == (False, True, None, [False, False])

        call_command("session_insight_source", "--tenant", "nord", "--deactivate", "--mode", "paused")
        assert _stand(welt)[2] == publication.PAUSED

        call_command("session_insight_source", "--tenant", "nord")
        assert _stand(welt) == (True, True, None, [False, False])
        assert _frisch(welt).insight_end_mode == ""


class TestOberflaeche:
    URL = "/session/nord/settings/buergerportal-beenden/"

    @pytest.fixture
    def admin(self, welt: dict[str, Any]) -> Any:
        return angemeldet(nutzer(welt["tenant"], "verwaltung", "manage_settings"))

    def test_auswahl_mit_hilfetext(self, admin: Any) -> None:
        seite = admin.get(self.URL)

        assert seite.status_code == 200
        inhalt = seite.content.decode()
        for option in portal_publication.OPTIONS:
            assert option.title in inhalt
            assert f'value="{option.key}"' in inhalt

    def test_einstellungen_verweisen_auf_die_auswahl(self, admin: Any) -> None:
        inhalt = admin.get("/session/nord/settings/").content.decode()
        assert self.URL in inhalt
        assert "Veröffentlichung beenden" in inhalt

    def test_bestaetigung_zeigt_folgen_und_aendert_nichts(self, admin: Any, welt: dict[str, Any]) -> None:
        seite = admin.post(self.URL, {"mode": WITHDRAWN})

        assert seite.status_code == 200
        inhalt = seite.content.decode()
        assert 'data-testid="bestaetigung"' in inhalt
        assert "1 Sitzungen, 1 Vorlagen" in inhalt
        assert "HTTP 410" in inhalt
        assert _frisch(welt).insight_publish is True

    def test_bestaetigt_beendet_und_protokolliert(self, admin: Any, welt: dict[str, Any]) -> None:
        antwort = admin.post(self.URL, {"mode": ARCHIVED, "confirm": "1"})

        assert antwort.status_code == 302
        tenant = _frisch(welt)
        assert (tenant.insight_publish, tenant.insight_end_mode) == (False, ARCHIVED)
        assert SessionAuditLog.objects.filter(tenant=tenant, action="unpublish").exists()
        inhalt = admin.get("/session/nord/settings/").content.decode()
        assert "Als Archiv behalten" in inhalt and "Wieder veröffentlichen" in inhalt

    def test_ohne_auswahl_kein_beenden(self, admin: Any, welt: dict[str, Any]) -> None:
        assert admin.post(self.URL, {"confirm": "1"}).status_code == 302
        # Altes Formular ohne Auswahl führt zur Auswahl statt still abzuschalten
        antwort = admin.post("/session/nord/settings/insight-publish/", {"publish": "0"})
        assert antwort.status_code == 302 and antwort["Location"].endswith(self.URL)
        assert _frisch(welt).insight_publish is True

    def test_wieder_veroeffentlichen(self, admin: Any, welt: dict[str, Any]) -> None:
        admin.post(self.URL, {"mode": PAUSED, "confirm": "1"})

        admin.post("/session/nord/settings/insight-publish/", {"publish": "1"})

        assert _stand(welt) == (True, True, None, [False, False])

    def test_nur_mit_einstellungsrecht(self, welt: dict[str, Any]) -> None:
        leser = angemeldet(nutzer(welt["tenant"], "leser", "view_meetings"))
        assert leser.get(self.URL).status_code == 403
        assert leser.post(self.URL, {"mode": WITHDRAWN, "confirm": "1"}).status_code == 403
        assert _frisch(welt).insight_publish is True


class TestVollstaendigOderGarNicht:
    """Beenden wirkt ganz oder gar nicht; eine unvollständige Umsetzung zieht derselbe Aufruf nach."""

    def test_abbruch_laesst_alles_beim_alten(self, welt: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        def _abbruch(tenant: Any) -> int:
            raise RuntimeError("Abbruch mitten in der Rücknahme")

        monkeypatch.setattr(insight_service, "apply_portal_state", _abbruch)

        with pytest.raises(RuntimeError):
            portal_publication.end_publication(_frisch(welt), WITHDRAWN)

        tenant = _frisch(welt)
        assert (tenant.insight_publish, tenant.insight_end_mode) == (True, "")
        assert _stand(welt) == (True, True, None, [False, False])
        assert not SessionAuditLog.objects.filter(tenant=tenant, action="unpublish").exists()

    def test_unvollstaendige_ruecknahme_wird_nachgezogen(self, welt: dict[str, Any]) -> None:
        # Stand wie nach einem Abbruch ohne Transaktion: Möglichkeit gespeichert, Wirkung fehlt
        SessionTenant.objects.filter(pk=welt["tenant"].pk).update(insight_publish=False, insight_end_mode=WITHDRAWN)

        change = portal_publication.end_publication(_frisch(welt), WITHDRAWN)

        assert change is not None and change.entries == 2
        assert _stand(welt) == (False, False, publication.WITHDRAWN, [True, True])
        assert SessionAuditLog.objects.filter(tenant=welt["tenant"], action="unpublish").exists()
        # Danach ist wirklich nichts mehr zu tun
        assert portal_publication.end_publication(_frisch(welt), WITHDRAWN) is None

    def test_unvollstaendiges_wiederveroeffentlichen_wird_nachgezogen(self, welt: dict[str, Any]) -> None:
        portal_publication.end_publication(_frisch(welt), PAUSED)
        SessionTenant.objects.filter(pk=welt["tenant"].pk).update(insight_publish=True, insight_end_mode="")

        change = portal_publication.resume_publication(_frisch(welt))

        assert change is not None
        assert _stand(welt) == (True, True, None, [False, False])
        assert portal_publication.resume_publication(_frisch(welt)) is None

    def test_stand_nach_dem_commit_frisch(self, welt: dict[str, Any], django_capture_on_commit_callbacks: Any) -> None:
        """Liest eine parallele Anfrage vor dem Commit den alten Stand in den Cache, gilt danach trotzdem der neue."""
        with django_capture_on_commit_callbacks(execute=True):
            portal_publication.end_publication(_frisch(welt), PAUSED)
            cache.set(publication.CACHE_KEY, {}, publication.CACHE_SECONDS)

        state = publication.body_state(welt["body"].pk)
        assert state is not None and state.paused


class TestAltbestand:
    """Mandanten, die vor der Auswahl beendet haben: Bestand weiter öffentlich, noch keine Möglichkeit gewählt."""

    URL = "/session/nord/settings/buergerportal-beenden/"

    def test_einstellungen_bieten_die_auswahl_an(self, welt: dict[str, Any]) -> None:
        call_command("session_insight_source", "--tenant", "nord", "--deactivate")
        admin = angemeldet(nutzer(welt["tenant"], "verwaltung", "manage_settings"))

        inhalt = admin.get("/session/nord/settings/").content.decode()

        assert self.URL in inhalt
        assert "Umgang mit dem bisherigen Bestand festlegen" in inhalt
        assert 'data-testid="altbestand-hinweis"' in inhalt

    def test_auswahl_wirkt_auf_den_altbestand(self, welt: dict[str, Any]) -> None:
        call_command("session_insight_source", "--tenant", "nord", "--deactivate")
        admin = angemeldet(nutzer(welt["tenant"], "verwaltung", "manage_settings"))

        admin.post(self.URL, {"mode": ARCHIVED, "confirm": "1"})

        assert _stand(welt) == (False, True, publication.ARCHIVED, [False, False])
        assert 'data-testid="altbestand-hinweis"' not in admin.get("/session/nord/settings/").content.decode()

    def test_ohne_bestand_kein_verweis(self) -> None:
        tenant = SessionTenant.objects.create(name="Neu", slug="neu")
        admin = angemeldet(nutzer(tenant, "verwaltung", "manage_settings"))

        inhalt = admin.get("/session/neu/settings/").content.decode()

        assert "/session/neu/settings/buergerportal-beenden/" not in inhalt
        assert 'data-testid="altbestand-hinweis"' not in inhalt
