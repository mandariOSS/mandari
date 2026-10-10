# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fragen an die Ratsdaten in Work (Issue #853): Antwort über die Werkzeug-Schicht aus #899 mit Quellen und Links in
Work, nur öffentliche Ratsdaten an den Anbieter, je Organisation abschaltbar, Kontingente wie im Editor.

Der Anbieter ist ersetzt (``SkriptAnbieter`` aus den Tests von #899) oder antwortet über ``httpx.MockTransport``; es
geht kein Aufruf ins Netz. Der Endpunkt kommt nur aus ``endpunkt_fuer_work`` (Issue #950, Positivliste
``KI_ERLAUBTE_HOSTS``).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.common.ki_anbieter import KiEndpunkt
from apps.common.models import AISettings
from apps.tenants.models import Organization
from apps.work.motions.ai_security import AIRateLimiter
from apps.work.motions.models import Motion, OrganizationAITokenUsage
from apps.work.ris import fragen
from insight_ai.services import chat_service
from insight_ai.services.chat_service import ChatAbbruchError, Usage
from insight_ai.tests.anbieter import SkriptAnbieter, antwort_sitzungen, diese_woche, werkzeuge
from insight_ai.tests.musterstadt import JETZT, FakeSuche, Musterstadt, baue_musterstadt
from insight_core.models import ChatUsage

pytestmark = pytest.mark.django_db

STACKIT = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1"
#: Endpunkt der Organisation in den Tests mit Drehbuch-Anbieter (statt endpunkt_fuer_work)
ENDPUNKT = KiEndpunkt(
    anbieter="stackit",
    anzeigename="STACKIT AI Model Serving",
    verarbeitungsort="Rechenzentren in Deutschland (EU)",
    base_url=STACKIT,
    api_key="org-testschluessel-geheim",
    modell="eu-modell",
)


@pytest.fixture(autouse=True)
def _freigabe(settings: Any) -> None:
    """Die Positivliste hat keinen Standard: Die Tests geben den Host der Vorlage ausdrücklich frei."""
    settings.KI_ERLAUBTE_HOSTS = [urlsplit(STACKIT).hostname]


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
    """Endpunkt der Organisation fest, der Anbieter für ihn folgt dem Drehbuch (``fake.endpunkte``: gefragte Endpunkte)."""
    fake = SkriptAnbieter(list(schritte), verfuegbar=verfuegbar)
    monkeypatch.setattr(fragen, "endpunkt", lambda organization: ENDPUNKT)

    def fuer_endpunkt(endpunkt: KiEndpunkt) -> SkriptAnbieter:
        fake.endpunkte.append(endpunkt)
        return fake

    monkeypatch.setattr(chat_service, "OpenAIKompatiblerProvider", fuer_endpunkt)
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
    # Gefragt wird genau der einmal aufgelöste Endpunkt der Organisation
    assert fake.endpunkte == [ENDPUNKT]
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
    assert fragen.verfuegbar(org)
    monkeypatch.setattr(fragen, "endpunkt", lambda organization: None)
    assert not fragen.verfuegbar(org)
    assert fragen.beantworten(org, mitglied, "Welche Sitzungen finden diese Woche statt?", stadt.body).hinweis == (
        fragen.KEIN_ZUGANG
    )
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
    vorher = AIRateLimiter.get_remaining(mitglied.user_id, "per_day")
    fehler = fragen.beantworten(org, mitglied, "Welche Sitzungen finden diese Woche statt?", stadt.body)
    assert fehler.hinweis == fragen.NICHT_ERREICHBAR and "geheim" not in fehler.hinweis
    # Auch der Fehlschlag zählt für die Grenze je Person
    assert AIRateLimiter.get_remaining(mitglied.user_id, "per_day") == vorher - 1


def test_fehlschlag_bucht_grenze_und_verbrauch(
    org: Any, stadt: Musterstadt, mitglied: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scheitert die Antwort nach einer Werkzeugrunde, zählen Anfrage und Token der Runde davor (Kontingente)."""
    fake = anbieter(
        monkeypatch,
        werkzeuge(("gremien", {})),
        ValueError("Der KI-Anbieter hat nicht geantwortet."),
        ValueError("Der KI-Anbieter hat nicht geantwortet."),
    )
    vorher = AIRateLimiter.get_remaining(mitglied.user_id, "per_day")

    fehler = fragen.beantworten(org, mitglied, "Welche Gremien gibt es in der Stadt?", stadt.body)

    assert fehler.hinweis == fragen.NICHT_ERREICHBAR and not fehler.html
    assert len(fake.aufrufe) == 3 and fake.endpunkte == [ENDPUNKT]
    assert AIRateLimiter.get_remaining(mitglied.user_id, "per_day") == vorher - 1
    assert OrganizationAITokenUsage.get_tokens_used(org, OrganizationAITokenUsage.PERIOD_MONTH) > 0


def test_fehlschlag_mit_verbrauch_aus_dem_chat_dienst(
    org: Any, stadt: Musterstadt, mitglied: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    anbieter(monkeypatch)

    def abbruch(*args: Any, **kwargs: Any) -> Any:
        raise ChatAbbruchError("weg", Usage(prompt_tokens=700, completion_tokens=50, rounds=1))

    monkeypatch.setattr(chat_service, "process_chat_message", abbruch)
    fragen.beantworten(org, mitglied, "Welche Gremien gibt es in der Stadt?", stadt.body)
    assert OrganizationAITokenUsage.get_tokens_used(org, OrganizationAITokenUsage.PERIOD_MONTH) == 750


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


@pytest.fixture
def anfragen(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Jede HTTP-Anfrage an einen KI-Anbieter landet hier statt im Netz (Antwort ohne Werkzeugaufrufe)."""
    gesendet: list[httpx.Request] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        gesendet.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Antwort."}}], "usage": {}})

    echter_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(antwort)
        return echter_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    cache.delete(AISettings.CACHE_KEY)
    return gesendet


def test_endpunkt_der_organisation_aus_der_ki_konfiguration(
    org: Any, stadt: Musterstadt, mitglied: Any, anfragen: list[httpx.Request]
) -> None:
    """Fragen nutzen den Endpunkt der Organisation wie Schreibhilfe und Co-Editor, keinen fest eingebauten (#950)."""
    org.set_ai_api_key("org-testschluessel-geheim")
    org.ai_provider = "stackit"
    org.ai_model = "eu-modell"
    org.save()

    gewaehlt = fragen.endpunkt(org)
    assert gewaehlt is not None and fragen.verfuegbar(org)
    assert (gewaehlt.chat_url, gewaehlt.modell) == (f"{STACKIT}/chat/completions", "eu-modell")

    antwort = fragen.beantworten(org, mitglied, "Welche Sitzungen finden diese Woche statt?", stadt.body)
    assert not antwort.hinweis and len(anfragen) >= 1
    assert {str(a.url) for a in anfragen} == {f"{STACKIT}/chat/completions"}
    assert {a.headers["Authorization"] for a in anfragen} == {"Bearer org-testschluessel-geheim"}

    # Ohne Schlüssel der Organisation und ohne KI-Einstellungen für Work: kein Endpunkt, kein Fragefeld
    org.set_ai_api_key("")
    org.save()
    assert fragen.endpunkt(org) is None and not fragen.verfuegbar(org)


def test_adresse_ausserhalb_der_positivliste_ohne_fragefeld_und_ohne_aufruf(
    org: Any, stadt: Musterstadt, mitglied: Any, client_for: Any, anfragen: list[httpx.Request]
) -> None:
    # Plattform eingerichtet (freigegeben): trotzdem kein Rückfall, wenn die Organisation eine gesperrte Adresse hat
    ki = AISettings.get_settings()
    ki.provider, ki.enabled, ki.model_name = "stackit", True, "modell-work"
    ki.set_api_key("plattform-testschluessel-geheim")
    ki.save()
    org.set_ai_api_key("org-testschluessel-geheim")
    org.save()
    Organization.objects.filter(pk=org.pk).update(
        ai_provider="eigener",
        ai_base_url="https://ki.nicht-freigegeben.example/v1",
        ai_anzeigename="Fremd",
        ai_verarbeitungsort="unbekannt",
        ai_model="modell",
    )
    org.refresh_from_db()
    client = client_for(mitglied.user)

    assert fragen.endpunkt(org) is None and not fragen.verfuegbar(org)
    seite = client.get(reverse("work:ris_search", kwargs={"org_slug": org.slug})).content.decode()
    assert 'id="frage-form"' not in seite and 'id="ki-antwort"' not in seite
    antwort = client.post(
        reverse("work:ris_frage", kwargs={"org_slug": org.slug}),
        {"frage": "Welche Sitzungen finden diese Woche statt?"},
        headers={"HX-Request": "true"},
    )
    assert fragen.KEIN_ZUGANG in antwort.content.decode()
    assert anfragen == []


def test_live_suche_prueft_das_fragefeld_nicht(
    org: Any, stadt: Musterstadt, mitglied: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    aufrufe: list[Any] = []

    def verfuegbar(organization: Any) -> bool:
        aufrufe.append(organization)
        return True

    monkeypatch.setattr(fragen, "verfuegbar", verfuegbar)
    client = client_for(mitglied.user)
    suche = reverse("work:ris_search", kwargs={"org_slug": org.slug})

    client.get(suche, {"q": "Radweg"}, headers={"HX-Request": "true"})
    client.get(suche, {"q": "R"}, headers={"HX-Request": "true"})
    assert aufrufe == []
    assert 'id="frage-form"' in client.get(suche, {"q": "Radweg"}).content.decode() and len(aufrufe) == 1


def test_im_bisherigen_rahmen_fuehrt_die_frage_zur_suche(
    org: Any, stadt: Musterstadt, mitglied: Any, client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = anbieter(monkeypatch, *_sitzungen_dieser_woche())
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])

    antwort = client_for(mitglied.user).post(
        reverse("work:ris_frage", kwargs={"org_slug": org.slug}),
        {"frage": "Welche Sitzungen finden diese Woche statt?"},
    )

    assert antwort.status_code == 302
    assert antwort["Location"] == reverse("work:ris_search", kwargs={"org_slug": org.slug})
    assert fake.aufrufe == []
