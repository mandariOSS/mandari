# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bürgerportal je Körperschaft (Issue #317, Teil A): eigener Einstieg ``/insight/k/<slug>/`` mit
eigenem Namen, Logo und Akzentfarbe, festgelegter Kommunenauswahl und Links, die im Kontext
bleiben; optional ein eigener Host (``PORTAL_HOSTS``) nur für Hosts aus ``ALLOWED_HOSTS``.
Einstiege zeigen nur gelistete Kommunen, Weiterleitungen nur auf geprüfte Portalpfade.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext

from apps.session.models import SessionTenant
from insight_core import portal
from insight_core.models import OParlBody, OParlMeeting, OParlSource

pytestmark = pytest.mark.django_db
HOST = "rat.bezirk-nord.example"


@pytest.fixture(autouse=True)
def _cache_leeren() -> None:
    cache.clear()


@pytest.fixture
def kommunen(db: Any) -> dict[str, OParlBody]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    nord = OParlBody.objects.create(
        external_id="https://ris.example.org/body/nord",
        source=source,
        name="Bezirk Hamburg-Nord",
        display_name="Hamburg-Nord",
        slug="nord",
        accent_color="#0f766e",
    )
    nord.logo.name = "bodies/logos/nord.svg"
    nord.save(update_fields=["logo"])
    sued = OParlBody.objects.create(
        external_id="https://ris.example.org/body/sued", source=source, name="Bezirk Süd", slug="sued"
    )
    sued.logo.name = "bodies/logos/sued.png"
    sued.save(update_fields=["logo"])
    versteckt = OParlBody.objects.create(
        external_id="https://ris.example.org/body/demo", source=source, name="Demo", slug="demo", is_listed=False
    )
    OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/n1", body=nord, name="Sitzung Nord")
    OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/s1", body=sued, name="Sitzung Süd")
    return {"nord": nord, "sued": sued, "versteckt": versteckt}


class TestEinstieg:
    def test_eigener_name_logo_und_akzentfarbe(self, kommunen: dict[str, OParlBody]) -> None:
        antwort = Client().get("/insight/k/nord/")

        assert antwort.status_code == 200
        seite = antwort.content.decode()
        kontext = antwort.context["insight_portal"]
        assert kontext.name == "Hamburg-Nord" and kontext.accent_color == "#0f766e"
        assert "/media/bodies/logos/nord.svg" in seite
        assert "--portal-accent: #0f766e" in seite
        assert "| Hamburg-Nord</title>" in seite
        assert antwort.context["available_bodies"] == [kommunen["nord"]]
        assert antwort.context["active_body"] == kommunen["nord"]

    def test_zwei_koerperschaften_getrennte_einstiege(self, kommunen: dict[str, OParlBody]) -> None:
        nord = Client().get("/insight/k/nord/").content.decode()
        sued = Client().get("/insight/k/sued/").content.decode()

        assert "Hamburg-Nord" in nord and "bodies/logos/nord.svg" in nord and "bodies/logos/sued.png" not in nord
        assert "Bezirk Süd" in sued and "bodies/logos/sued.png" in sued and "Hamburg-Nord" not in sued
        assert "--portal-accent" not in sued, "ohne Akzentfarbe die mandari-Farben"

    def test_links_bleiben_im_kontext(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client()
        client.get("/insight/k/nord/")

        termine = client.get("/insight/termine/?period=all")

        assert termine.context["active_body"] == kommunen["nord"]
        assert termine.context["insight_portal"].body == kommunen["nord"]
        seite = termine.content.decode()
        assert "Sitzung Nord" in seite and "Sitzung Süd" not in seite
        assert 'href="/insight/k/nord/"' in seite, "Übersicht führt zurück zum Einstieg"

    def test_kommunenauswahl_ist_festgelegt(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client()
        client.get("/insight/k/nord/")

        start = client.get("/insight/")

        assert start.context["all_bodies_mode"] is False
        assert start.context["available_bodies"] == [kommunen["nord"]]
        seite = start.content.decode()
        assert "Kommune wechseln" not in seite and "Liste der Kommunen" not in seite
        # Auswahl einer Kommune per Abfrage-Parameter ändert den Kontext nicht
        termine = client.get(f"/insight/termine/?kommune={kommunen['sued'].id}")
        assert termine.context["active_body"] == kommunen["nord"]

    def test_alle_kommunen_verlaesst_den_einstieg(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client()
        client.get("/insight/k/nord/")

        client.get("/insight/kommune/alle/")
        start = client.get("/insight/")

        assert start.context["insight_portal"] is None
        assert start.context["all_bodies_mode"] is True

    def test_andere_kommune_waehlen_verlaesst_den_einstieg(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client()
        client.get("/insight/k/nord/")

        client.get(f"/insight/kommune/{kommunen['sued'].id}/")

        assert client.get("/insight/").context["active_body"] == kommunen["sued"]

    @pytest.mark.parametrize("slug", ["demo", "gibt-es-nicht"])
    def test_nur_gelistete_kommunen(self, kommunen: dict[str, OParlBody], slug: str) -> None:
        assert Client().get(f"/insight/k/{slug}/").status_code == 404

    def test_geloeschte_oder_ausgeblendete_kommune_beendet_kontext(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client()
        client.get("/insight/k/nord/")
        kommunen["nord"].is_listed = False
        kommunen["nord"].save(update_fields=["is_listed"])

        start = client.get("/insight/")

        assert start.context["insight_portal"] is None
        assert portal.PORTAL_SESSION_KEY not in client.session

    def test_mandanten_slug_fuehrt_zur_gespiegelten_kommune(self, db: Any) -> None:
        tenant = SessionTenant.objects.create(
            name="Bezirksversammlung Ost", slug="ost", insight_publish=True, primary_color="#9d174d"
        )
        tenant.logo.name = "session/tenants/logos/ost.png"
        cast(Any, tenant).save(update_fields=["logo"])
        source = OParlSource.objects.get(sync_config__session_tenant="ost")
        OParlBody.objects.create(external_id=f"{source.url}body/", source=source, name="Bezirk Ost", slug="bezirk-ost")

        antwort = Client().get("/insight/k/ost/")

        assert antwort.status_code == 200
        kontext = antwort.context["insight_portal"]
        assert kontext.name == "Bezirk Ost"
        assert kontext.logo_url == "/media/session/tenants/logos/ost.png"
        assert kontext.accent_color == "#9d174d"
        assert kontext.home_url == "/insight/k/ost/"

    def test_ungueltige_primaerfarbe_wird_nicht_ausgegeben(self, db: Any) -> None:
        SessionTenant.objects.create(name="Bezirk West", slug="west", insight_publish=True, primary_color="red;}x")
        source = OParlSource.objects.get(sync_config__session_tenant="west")
        OParlBody.objects.create(external_id=f"{source.url}body/", source=source, name="Bezirk West", slug="west-b")

        antwort = Client().get("/insight/k/west/")

        assert antwort.context["insight_portal"].accent_color is None
        assert "red;}x" not in antwort.content.decode()


class TestWeiterleitung:
    def test_tiefer_link_fuehrt_auf_portalseite(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client()
        antwort = client.get("/insight/k/nord/termine/")

        assert antwort.status_code == 302 and antwort["Location"] == "/insight/termine/"
        assert client.get("/insight/termine/").context["active_body"] == kommunen["nord"]

    @pytest.mark.parametrize(
        "pfad",
        [
            "kommune/alle/",  # keine Zustandsänderungen über den Einstieg
            "merkliste/api/toggle/",
            "gibt/es/nicht/",
            "..%2F..%2Fadmin/",
            "/evil.example/",
            "Termine/",
        ],
    )
    def test_keine_fremden_ziele(self, kommunen: dict[str, OParlBody], pfad: str) -> None:
        antwort = Client().get(f"/insight/k/nord/{pfad}")

        assert antwort.status_code == 404

    def test_parameter_leiten_nie_weiter(self, kommunen: dict[str, OParlBody]) -> None:
        antwort = Client().get("/insight/k/nord/?next=https://evil.example/&redirect=//evil.example")

        assert antwort.status_code == 200
        antwort = Client().get("/insight/k/nord/termine/?next=https://evil.example/")
        assert antwort["Location"] == "/insight/termine/"


class TestEigenerHost:
    @pytest.fixture(autouse=True)
    def _portal_host(self, settings: Any) -> None:
        settings.ALLOWED_HOSTS = ["testserver", HOST, "rat.bezirk-sued.example"]
        settings.PORTAL_HOSTS = {HOST: "nord"}

    def test_host_legt_kommune_fest(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client(HTTP_HOST=HOST)

        start = client.get("/insight/")

        assert start.status_code == 200
        kontext = start.context["insight_portal"]
        assert kontext.body == kommunen["nord"] and kontext.via_host and not kontext.can_leave
        assert kontext.home_url == "/insight/"
        assert "Alle Kommunen im Bürgerportal" not in start.content.decode()

    def test_host_laesst_sich_nicht_verlassen(self, kommunen: dict[str, OParlBody]) -> None:
        client = Client(HTTP_HOST=HOST)

        assert client.get("/insight/kommune/alle/")["Location"] == "/insight/"
        assert client.get(f"/insight/kommune/{kommunen['sued'].id}/")["Location"] == "/insight/"
        assert client.get("/insight/").context["active_body"] == kommunen["nord"]
        assert client.get("/insight/k/sued/").status_code == 404
        assert client.get("/insight/k/nord/").status_code == 200

    def test_wurzel_leitet_ins_portal(self, kommunen: dict[str, OParlBody]) -> None:
        antwort = Client(HTTP_HOST=HOST).get("/")

        assert antwort.status_code == 302 and antwort["Location"] == "/insight/"

    def test_host_ohne_gelistete_kommune_faellt_nicht_auf_gemeinsames_portal(
        self, kommunen: dict[str, OParlBody]
    ) -> None:
        kommunen["nord"].is_listed = False
        kommunen["nord"].save(update_fields=["is_listed"])

        antwort = Client(HTTP_HOST=HOST).get("/insight/")

        assert antwort.status_code == 404

    def test_andere_hosts_bleiben_gemeinsames_portal(self, kommunen: dict[str, OParlBody]) -> None:
        start = Client(HTTP_HOST="rat.bezirk-sued.example").get("/insight/")

        assert start.context["insight_portal"] is None

    def test_host_ausserhalb_allowed_hosts_wird_abgewiesen(self, kommunen: dict[str, OParlBody], settings: Any) -> None:
        settings.ALLOWED_HOSTS = ["testserver"]
        antwort = Client(HTTP_HOST=HOST).get("/insight/")

        assert antwort.status_code == 400
        warnungen = portal.check_portal_hosts()
        assert [w.id for w in warnungen] == ["insight_core.W001"]

    def test_subdomain_weiterleitung_greift_nicht(self, kommunen: dict[str, OParlBody]) -> None:
        from apps.tenants.models import Organization

        Organization.objects.create(name="Rat", slug="rat")
        with override_settings(MAIN_DOMAIN="bezirk-nord.example"):
            antwort = Client(HTTP_HOST=HOST).get("/insight/")

        assert antwort.status_code == 200


def test_gemeinsames_portal_ohne_zusaetzliche_abfragen(kommunen: dict[str, OParlBody]) -> None:
    """Ohne Einstieg kostet der Portal-Kontext keine Abfrage (Performance-Budget der Startseite)."""
    request = RequestFactory().get("/insight/")
    request.session = SessionStore()
    request.session["active_body_id"] = str(kommunen["nord"].id)

    with CaptureQueriesContext(connection) as erfasst:
        assert portal.get_portal(request) is None

    assert len(erfasst) == 0
