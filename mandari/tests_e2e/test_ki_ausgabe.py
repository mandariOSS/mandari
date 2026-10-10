# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Antworten werden nur bereinigt als HTML dargestellt (frontend/js/ki-ausgabe.ts).

- KI-Assistent im Bürgerportal: Markdown der Antwort ohne Bilder, Ereignis-Attribute oder Skript-Links; Links nur
  auf die eigene Seite oder https, externe mit ``rel="noopener noreferrer"``.
- KI-Hilfe im Work-Editor: Antworttext und Hinweise erscheinen nur bereinigt bzw. als Text.
- Die Bereinigung selbst gegen eine Reihe typischer Muster, ausgeführt im echten Browser.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

import pytest

from apps.work.motions.models import Motion
from apps.work.motions.services import MotionAIService
from insight_core.models import OParlBody, OParlSource
from tests_e2e.conftest import BrowserProblems, wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

RIS = "https://ris.ki-ausgabe.e2e.example/oparl"
PASSWORD = "E2e-Passwort-123456"

ANTWORT = (
    "Hallo **fett**\n\n"
    '<img src="x" onerror="window.__ki_xss = 1">\n\n'
    "[Skript](javascript:window.__ki_xss=2) und [Daten](data:text/html,<script>window.__ki_xss=5</script>)\n\n"
    "[Vorgang](/insight/vorgaenge/1/) [Extern](https://example.org/seite) [Unverschlüsselt](http://example.org/)\n\n"
    '<a href="javascript:window.__ki_xss=3">roh</a> <svg><script>window.__ki_xss=4</script></svg>'
    '<iframe srcdoc="<script>parent.__ki_xss=6</script>"></iframe>'
)

#: Muster, die nach der Bereinigung weder ausführen noch als Element/Attribut übrig bleiben dürfen
MUSTER = [
    '<img src=x onerror="window.__ki_xss=11">',
    '<a href="  jav&#x09;ascript:window.__ki_xss=12">a</a>',
    '<a href="JaVaScRiPt:window.__ki_xss=13">b</a>',
    '<a href="//example.org/protokollrelativ">c</a>',
    '<a href="https://nutzer:pw@example.org/">d</a>',
    "<math><mtext><table><mglyph><style><img src=x onerror=window.__ki_xss=14>",
    "<svg><a><animate attributeName=href values=javascript:window.__ki_xss=15 /><text>x</text></a></svg>",
    "<form><button formaction=javascript:window.__ki_xss=16>f</button></form>",
    '<p style="background:url(javascript:window.__ki_xss=17)" onclick="window.__ki_xss=18">p</p>',
    "<details open ontoggle=window.__ki_xss=19>",
    '<noscript><p title="</noscript><img src=x onerror=window.__ki_xss=20>">',
    "<!--<img src=x onerror=window.__ki_xss=21>-->",
]

PRUEFUNG = """(html) => {
  const box = document.createElement('div');
  box.innerHTML = html;
  document.body.appendChild(box);
  const befunde = [];
  box.querySelectorAll('*').forEach((el) => {
    const tag = el.localName;
    if (['img', 'svg', 'math', 'script', 'style', 'iframe', 'form', 'button', 'details', 'noscript'].includes(tag)) {
      befunde.push('Element ' + tag);
    }
    for (const attr of el.attributes) {
      if (attr.name.startsWith('on') || attr.name === 'style' || attr.name === 'formaction') {
        befunde.push('Attribut ' + attr.name);
      }
    }
    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      const url = new URL(href, location.href);
      const eigen = url.origin === location.origin;
      if (!eigen && url.protocol !== 'https:') befunde.push('Link ' + href);
      if (url.username || url.password) befunde.push('Link mit Zugangsdaten ' + href);
      if (!eigen && el.getAttribute('rel') !== 'noopener noreferrer') befunde.push('rel fehlt ' + href);
    }
  });
  box.remove();
  return befunde;
}"""


def _kommune() -> OParlBody:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "E2E-KI-Ausgabe"})
    return OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Musterstadt KI", slug="e2e-ki-ausgabe", is_listed=True
    )


def _ki_ausgefuehrt(page: Any) -> Any:
    page.wait_for_timeout(300)  # Bilder/Fehlerereignisse hätten jetzt gefeuert
    return page.evaluate("() => window.__ki_xss")


class TestKiAssistent:
    def test_antwort_ohne_bilder_skripte_und_fremde_links(
        self, page: Any, goto: Any, problems: BrowserProblems
    ) -> None:
        _kommune()

        def antworten(route: Any) -> None:
            daten = json.loads(route.request.post_data or "{}")
            if daten.get("consent"):
                route.fulfill(json={"status": "consent_granted"})
                return
            route.fulfill(
                json={
                    "response": ANTWORT,
                    "sources": [{"title": "Vorgang 1", "url": "/insight/vorgaenge/1/", "type": "paper"}],
                    "remaining_today": 9,
                    "remaining_week": 49,
                }
            )

        page.route("**/insight/chat/api/message/", antworten)
        goto("/insight/chat/")
        page.get_by_role("checkbox", name=re.compile("Datenschutzerklärung gelesen")).check()
        page.get_by_role("button", name="Zustimmen").click()
        page.locator("textarea").fill("Was beschließt der Rat?")
        page.locator("form:has(textarea) button[type=submit]").click()

        antwort = page.locator(".prose").last
        expect(antwort.locator("strong")).to_have_text("fett")
        assert _ki_ausgefuehrt(page) is None
        assert antwort.locator("img, svg, script, iframe").count() == 0
        assert antwort.locator("[onerror]").count() == 0
        assert antwort.locator('a[href^="javascript:"], a[href^="data:"], a[href^="http:"]').count() == 0
        expect(antwort).to_contain_text("Skript")  # Text des entfernten Links bleibt
        expect(antwort).to_contain_text("roh")

        intern = antwort.get_by_role("link", name="Vorgang")
        assert (intern.get_attribute("href") or "").endswith("/insight/vorgaenge/1/")
        assert intern.get_attribute("target") is None
        extern = antwort.get_by_role("link", name="Extern")
        assert extern.get_attribute("href") == "https://example.org/seite"
        assert extern.get_attribute("rel") == "noopener noreferrer"
        assert antwort.get_by_role("link", name="Unverschlüsselt").count() == 0
        problems.assert_clean("KI-Assistent")

    def test_bereinigung_gegen_muster(self, page: Any, goto: Any) -> None:
        _kommune()
        goto("/insight/chat/")
        for muster in MUSTER:
            html = page.evaluate("(m) => window.bereinigeKiHtml(m)", muster)
            assert page.evaluate(PRUEFUNG, html) == [], f"{muster!r} → {html!r}"
        markdown = page.evaluate("(t) => window.kiMarkdownHtml(t)", ANTWORT)
        assert page.evaluate(PRUEFUNG, markdown) == [], markdown
        assert _ki_ausgefuehrt(page) is None


class TestWorkEditorKi:
    def test_ki_antwort_im_editor_bereinigt(
        self, page: Any, live_server: Any, login: Any, org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(MotionAIService, "is_available", lambda self: True)
        membership = make_member(org, ["dashboard.view", "motions.view", "motions.edit"], email="ki@example.org")
        membership.user.set_password(PASSWORD)
        membership.user.save(update_fields=["password"])
        motion = Motion.objects.create(organization=org, author=membership, title="KI-Test", status="draft")
        cast(Any, motion).set_content_encrypted("<p>Text</p>")
        motion.save()
        login(membership.user.email, PASSWORD)

        page.route(
            "**/documents/ai/",
            lambda route: route.fulfill(
                json={
                    "success": True,
                    "content": 'Antwort <img src=x onerror="window.__ki_xss=31"> <a href="javascript:window.__ki_xss=32">x</a>',
                    "suggestions": ['<img src=x onerror="window.__ki_xss=33">Hinweis'],
                }
            ),
        )
        page.goto(f"{live_server.url}/work/{org.slug}/documents/{motion.id}/")
        wait_for_bundle(page)
        page.wait_for_selector("#editor-container .ProseMirror", timeout=15000)
        page.evaluate(
            "() => { const d = Alpine.$data(document.querySelector('.editor-wrapper')); d.sidebarTab = 'ai';"
            " return d.aiAction('chat', 'Frage'); }"
        )
        verlauf = page.locator(".chat-messages")
        expect(verlauf).to_contain_text("Hinweis")
        assert _ki_ausgefuehrt(page) is None
        assert verlauf.locator("img, [onerror], a[href^='javascript:']").count() == 0
