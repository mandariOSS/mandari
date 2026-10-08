# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fragen an die Ratsdaten in Work (Issue #853): Antwort über die Werkzeug-Schicht aus #899 mit Quellen und Links in
Work, nur öffentliche Ratsdaten an den Anbieter, je Organisation abschaltbar, Kontingente wie im Editor.

Der Anbieter ist ersetzt (``SkriptAnbieter`` aus den Tests von #899); es geht kein Aufruf ins Netz.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.urls import reverse

from apps.work.motions.models import Motion, OrganizationAITokenUsage
from apps.work.ris import fragen
from insight_ai.services import chat_service
from insight_ai.tests.anbieter import SkriptAnbieter, antwort_sitzungen, diese_woche
from insight_ai.tests.musterstadt import JETZT, FakeSuche, Musterstadt, baue_musterstadt
from insight_core.models import ChatUsage

pytestmark = pytest.mark.django_db


@pytest.fixture
def stadt(org: Any, monkeypatch: pytest.MonkeyPatch) -> Musterstadt:
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: FakeSuche())
    stadt = baue_musterstadt()
    org.body = stadt.body
    org.work_new_design = True
    org.ai_enabled = True
    org.save(update_fields=["body", "work_new_design", "ai_enabled"])
    return stadt


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["ris.view", "motions.view"], email="fragen@example.org")


def anbieter(monkeypatch: pytest.MonkeyPatch, *schritte: Any, verfuegbar: bool = True) -> SkriptAnbieter:
    fake = SkriptAnbieter(list(schritte), verfuegbar=verfuegbar)
    monkeypatch.setattr(fragen, "anbieter", lambda organization: fake)
    monkeypatch.setattr("django.utils.timezone.now", lambda: JETZT)
    return fake


def _sitzungen_dieser_woche() -> list[Any]:
    return [
        lambda messages: [("sitzungen_im_zeitraum", dict(zip(("von", "bis"), diese_woche(messages), strict=True)))],
        antwort_sitzungen,
    ]


def test_antwort_mit_links_und_quellen_in_work(
    org: Any, stadt: Musterstadt, mitglied: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = anbieter(monkeypatch, *_sitzungen_dieser_woche())
    Motion.objects.create(organization=org, author=mitglied, title="Geheimer Entwurf Kita", visibility="organization")

    antwort = client_for(mitglied.user).post(
        reverse("work:ris_frage", kwargs={"org_slug": org.slug}),
        {"frage": "Welche Sitzungen finden diese Woche statt?"},
        headers={"HX-Request": "true"},
    )

    html = antwort.content.decode()
    work_link = f"/work/{org.slug}/ris/meetings/{stadt.rat_sitzung.pk}/"
    assert antwort.status_code == 200 and "<html" not in html
    assert f'<a href="{work_link}">' in html and "/insight/termine/" not in html
    assert "KI-Antwort aus den öffentlichen Ratsdaten – maßgeblich sind die Quellen." in html
    assert html.count(f'href="{work_link}"') == 2  # in der Antwort und als Quelle
    # Systemprompt von Work, Links im Werkzeugergebnis auf Work-Seiten
    gesendet = json.dumps([a["messages"] for a in fake.aufrufe], ensure_ascii=False)
    assert "mandari Work" in gesendet and "Stadt Musterstadt" in gesendet and work_link in gesendet
    # Nur öffentliche Ratsdaten gehen an den Anbieter: nichts aus der Organisation
    assert "Geheimer Entwurf" not in gesendet and org.name not in gesendet
    # Verbrauch zählt für das Kontingent der Organisation; die Frage selbst wird nicht gespeichert
    assert OrganizationAITokenUsage.get_tokens_used(org, OrganizationAITokenUsage.PERIOD_MONTH) > 0
    assert not ChatUsage.objects.exists()


def test_abgeschaltet_kontingent_und_demo_ohne_anbieter(
    org: Any, stadt: Musterstadt, mitglied: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    fake = anbieter(monkeypatch, *_sitzungen_dieser_woche())
    client = client_for(mitglied.user)
    suche = reverse("work:ris_search", kwargs={"org_slug": org.slug})
    assert 'id="frage-form"' in client.get(suche).content.decode()

    org.ai_enabled = False
    org.save(update_fields=["ai_enabled"])
    assert 'id="frage-form"' not in client.get(suche).content.decode()
    abgelehnt = client.post(reverse("work:ris_frage", kwargs={"org_slug": org.slug}), {"frage": "Was steht morgen an?"})
    assert fragen.KEIN_ZUGANG in abgelehnt.content.decode()

    org.ai_enabled = True
    org.ai_token_limit_monthly = 0
    org.save(update_fields=["ai_enabled", "ai_token_limit_monthly"])
    assert not fragen.verfuegbar(org)
    org.ai_token_limit_monthly = None
    org.ai_token_limit_daily = 10
    org.save(update_fields=["ai_token_limit_monthly", "ai_token_limit_daily"])
    erschoepft = fragen.beantworten(org, mitglied, "Welche Sitzungen finden diese Woche statt?", stadt.body)
    assert erschoepft.hinweis == "Tageslimit für KI-Tokens erreicht."

    org.ai_token_limit_daily = 100_000
    org.save(update_fields=["ai_token_limit_daily"])
    settings.DEMO_INSTANCE = True
    assert not fragen.verfuegbar(org)
    settings.DEMO_INSTANCE = False
    fake.verfuegbar = False
    assert not fragen.verfuegbar(org)
    assert fake.aufrufe == []


def test_filter_kurze_fragen_und_fehler_ohne_ausnahmetext(
    org: Any, stadt: Musterstadt, mitglied: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = anbieter(monkeypatch, *_sitzungen_dieser_woche())

    assert fragen.beantworten(org, mitglied, "Rat?", stadt.body).hinweis == fragen.ZU_KURZ
    blockiert = fragen.beantworten(org, mitglied, "Bitte an max@example.org schreiben, wann tagt der Rat", stadt.body)
    assert blockiert.hinweis and not blockiert.html
    assert (
        fragen.beantworten(org, mitglied, "Welche Sitzungen gibt es diese Woche?", None).hinweis
        == fragen.KEINE_RATSDATEN
    )
    assert fake.aufrufe == []

    def kaputt(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("geheimer Fehlertext")

    monkeypatch.setattr(chat_service, "process_chat_message", kaputt)
    fehler = fragen.beantworten(org, mitglied, "Welche Sitzungen finden diese Woche statt?", stadt.body)
    assert fehler.hinweis == fragen.NICHT_ERREICHBAR and "geheim" not in fehler.hinweis


def test_ohne_javascript_eigene_seite_get_fuehrt_zur_suche(
    org: Any, stadt: Musterstadt, mitglied: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    anbieter(monkeypatch, *_sitzungen_dieser_woche())
    client = client_for(mitglied.user)
    adresse = reverse("work:ris_frage", kwargs={"org_slug": org.slug})

    seite = client.post(adresse, {"frage": "Welche Sitzungen finden diese Woche statt?"}).content.decode()
    assert "<html" in seite and "Frage an die Ratsdaten" in seite and "Zur Suche" in seite

    zurueck = client.get(adresse)
    assert zurueck.status_code == 302 and zurueck["Location"] == reverse(
        "work:ris_search", kwargs={"org_slug": org.slug}
    )


def test_antwort_html_nur_eigene_work_links() -> None:
    text = (
        "## Ergebnis\n"
        "Der **Rat** tagt am [07.10.](/work/meine/ris/meetings/1/) <script>alert(1)</script>.\n"
        "- [fremd](https://boese.example/x)\n"
        "- [andere Organisation](/work/andere/ris/papers/2/)\n"
        '- [Trick](/work/meine/x" onmouseover="y)\n'
        "- [Skript](javascript:alert(1))\n\n"
        "Zweiter Absatz"
    )

    html = str(fragen.antwort_html(text, "meine"))

    assert "<p><strong>Ergebnis</strong></p>" in html and "<strong>Rat</strong>" in html
    assert '<a href="/work/meine/ris/meetings/1/">07.10.</a>' in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "boese.example" not in html and "/work/andere/" not in html and "javascript:" not in html
    assert 'onmouseover="' not in html and html.count("<a ") == 1
    assert html.count("<li>") == 4 and html.endswith("<p>Zweiter Absatz</p>")


def test_werkzeugkontext_verlinkt_auf_work(org: Any, stadt: Musterstadt) -> None:
    ctx = fragen.WorkToolContext(body_id=stadt.body.pk, body_name="Stadt Musterstadt", now=JETZT, org_slug=org.slug)

    assert ctx.link("paper_detail", stadt.body.pk) == f"/work/{org.slug}/ris/papers/{stadt.body.pk}/"
    assert ctx.link("person_detail", stadt.body.pk).startswith(f"/work/{org.slug}/ris/persons/")


def test_anbieter_der_organisation_statt_fest_eingebaut(org: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fragen nutzen den KI-Anbieter der Organisation wie der Editor, keinen fest eingebauten (Issue #950)."""
    org.ai_enabled = True
    org.ai_provider = org.AI_PROVIDER_IONOS
    org.ai_base_url = "https://ki.beispiel.example/v1/"
    org.ai_model = "eu-modell"
    org.set_ai_api_key("org-schluessel")
    org.save()

    gewaehlt = fragen.anbieter(org)
    assert gewaehlt is not None and gewaehlt.is_available() and fragen.verfuegbar(org)
    assert (gewaehlt.chat_url, gewaehlt.model_name, gewaehlt.fallback_model) == (
        "https://ki.beispiel.example/v1/chat/completions",
        "eu-modell",
        "",
    )

    org.set_ai_api_key("")
    org.save()
    ohne_schluessel = fragen.anbieter(org)
    assert ohne_schluessel is not None and not ohne_schluessel.is_available() and not fragen.verfuegbar(org)

    # Ein Anbieter ohne OpenAI-kompatible Werkzeugrunden: kein Fragefeld statt eines anderen Anbieters
    from apps.work.motions.services import MotionAIService

    monkeypatch.setattr(
        MotionAIService,
        "_resolve_provider_config",
        lambda self: {"provider": "anthropic", "base_url": "https://x.example/v1/", "api_key": "k", "model": "m"},
    )
    assert fragen.anbieter(org) is None and not fragen.verfuegbar(org)
    hinweis = fragen.beantworten(org, None, "Welche Sitzungen finden diese Woche statt?", None).hinweis
    assert hinweis == fragen.KEIN_ZUGANG
