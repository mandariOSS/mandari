# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aktionen im Django-Admin: Rechte und Schutz vor fremd ausgelösten Aufrufen.

- Jede Sammel-, Zeilen- und Detailaktion nennt das Recht, das sie verlangt. Ohne Angabe ließe Django eine
  Sammelaktion schon mit dem Ansichtsrecht am Modell zu.
- Ändernde Aktionen hinter einem Link zeigen beim Aufruf (GET) nur eine Bestätigung; ausgeführt wird erst mit dem
  abgeschickten Formular (POST mit CSRF-Token).
- Der Assistent „Mandant anlegen“ vervollständigt einen bestehenden Mandanten nur mit dem Änderungsrecht an ihm.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.common.tests.factories import UserFactory
from apps.session.admin_provisioning import FEHLER_KUERZEL_VERGEBEN
from apps.session.models import SessionTenant, SessionUser
from insight_core.models import OParlSource

pytestmark = pytest.mark.django_db

#: Listen, in denen Unfold Aktionen einer Admin-Klasse führt
UNFOLD_LISTEN = ("actions_list", "actions_row", "actions_detail", "actions_submit_line")


def _staff(*rechte: str, email: str = "teilrechte@example.org") -> Any:
    """Staff-Konto mit einzelnen Modellrechten (``app.codename``), kein Superuser."""
    user = cast(Any, UserFactory)(email=email, is_staff=True, is_superuser=False)
    for recht in rechte:
        app, codename = recht.split(".")
        user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=codename))
    return user


def _client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def superuser() -> Any:
    return cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)


# --- Jede Aktion nennt ihr Recht ----------------------------------------------------------------------------


def _aktionen_ohne_recht() -> list[str]:
    ohne: list[str] = []
    for model, model_admin in admin.site._registry.items():
        namen: set[str] = set()
        for eintrag in model_admin.actions or ():
            namen.add(eintrag if isinstance(eintrag, str) else eintrag.__name__)
        for liste in UNFOLD_LISTEN:
            for eintrag in getattr(model_admin, liste, None) or ():
                if isinstance(eintrag, str):
                    namen.add(eintrag)
                elif isinstance(eintrag, dict):
                    namen.update(str(name) for name in eintrag.get("items", ()))
        for name in sorted(namen - {"delete_selected"}):
            funktion = getattr(model_admin, name, None)
            if funktion is None or getattr(funktion, "allowed_permissions", None):
                continue
            ohne.append(f"{model._meta.label}.{name}")
    return ohne


def test_jede_admin_aktion_nennt_ihr_recht() -> None:
    assert _aktionen_ohne_recht() == [], "Aktionen ohne permissions=[…] laufen schon mit dem Ansichtsrecht"


def test_sammelaktion_nicht_mit_reinem_ansichtsrecht() -> None:
    mandant = SessionTenant.objects.create(name="Stadt Nord", slug="nord")
    liste = reverse("admin:session_sessiontenant_changelist")
    daten = {"action": "deactivate_tenants", "_selected_action": [str(mandant.pk)]}

    _client(_staff("session.view_sessiontenant")).post(liste, daten)
    mandant.refresh_from_db()
    assert mandant.is_active

    berechtigt = _staff("session.view_sessiontenant", "session.change_sessiontenant", email="aendern@example.org")
    _client(berechtigt).post(liste, daten)
    mandant.refresh_from_db()
    assert not mandant.is_active


# --- Ändernde Aktionen hinter Links ---------------------------------------------------------------------------


def test_sync_hinter_link_erst_nach_bestaetigung(superuser: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    quelle = OParlSource.objects.create(name="Beispielquelle", url="https://ris.beispielstadt.example/oparl/system")
    gestartet: list[tuple[Any, bool]] = []

    def vormerken(source: Any, full: bool = False) -> str:
        gestartet.append((source.pk, full))
        return "requested"

    monkeypatch.setattr("insight_core.admin.request_source_sync", vormerken)
    monkeypatch.setattr("insight_core.admin._report_source_sync", lambda *args, **kwargs: None)
    url = reverse("admin:insight_core_oparlsource_change", args=[quelle.pk]).replace("change/", "sync-full/")
    client = _client(superuser)

    seite = client.get(url)
    assert seite.status_code == 200 and gestartet == []
    assert "csrfmiddlewaretoken" in seite.content.decode()

    # Ohne Token (fremde Seite) lehnt der CSRF-Schutz das Formular ab
    fremd = Client(enforce_csrf_checks=True)
    fremd.force_login(superuser)
    assert fremd.post(url).status_code == 403 and gestartet == []

    assert client.post(url).status_code == 302
    assert gestartet == [(quelle.pk, True)]


def test_sync_hinter_link_nur_mit_aenderungsrecht(monkeypatch: pytest.MonkeyPatch) -> None:
    quelle = OParlSource.objects.create(name="Beispielquelle", url="https://ris.beispielstadt.example/oparl/system")
    gestartet: list[Any] = []
    monkeypatch.setattr("insight_core.admin.request_source_sync", lambda source, full=False: gestartet.append(source))
    url = reverse("admin:insight_core_oparlsource_change", args=[quelle.pk]).replace("change/", "sync-full/")

    antwort = _client(_staff("insight_core.view_oparlsource")).post(url)

    assert antwort.status_code == 403 and gestartet == []


# --- Assistent „Mandant anlegen“ --------------------------------------------------------------------------------


def _assistent(slug: str, mail: str) -> dict[str, str]:
    return {
        "name": "Stadt Nord",
        "slug": slug,
        "numbering": "standard",
        "term_name": "Wahlperiode 2025–2030",
        "term_start": "2025-11-01",
        "term_end": "2030-10-31",
        "admin_email": mail,
    }


def test_assistent_vervollstaendigt_bestehenden_mandanten_nicht_mit_anlegerecht() -> None:
    mandant = SessionTenant.objects.create(name="Stadt Nord", slug="nord")
    url = reverse("admin:session_sessiontenant_provision")
    anlegen = _staff("session.add_sessiontenant", "session.view_sessiontenant")

    antwort = _client(anlegen).post(url, _assistent("nord", "fremd@example.org"))

    assert antwort.status_code == 200
    assert FEHLER_KUERZEL_VERGEBEN in antwort.content.decode()
    assert not SessionUser.objects.filter(tenant=mandant).exists()


def test_assistent_vervollstaendigt_mit_aenderungsrecht(superuser: Any) -> None:
    SessionTenant.objects.create(name="Stadt Nord", slug="nord")
    url = reverse("admin:session_sessiontenant_provision")

    antwort = _client(superuser).post(url, _assistent("nord", "verwaltung@example.org"))

    assert FEHLER_KUERZEL_VERGEBEN not in antwort.content.decode()
