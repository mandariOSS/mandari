# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Strikte HTML-Whitelist für Redebeiträge (WYSIWYG-Inhalte).

Es gibt im Repo keine Sanitizing-Bibliothek (kein bleach/nh3), daher eine
eigene, bewusst restriktive Whitelist auf Basis von html.parser:

- Erlaubte Tags: b, i, u, strong, em, ul, ol, li, p, br, h2, h3
- ALLE Attribute werden entfernt (kein href, kein style, keine Events)
- Nicht erlaubte Tags werden komplett entfernt, ihr Textinhalt bleibt
  erhalten (Ausnahme: script/style — dort wird auch der Inhalt verworfen)
- Text wird HTML-escaped wieder ausgegeben
"""

from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = frozenset({"b", "i", "u", "strong", "em", "ul", "ol", "li", "p", "br", "h2", "h3"})

# Tags, deren Inhalt komplett verworfen wird
DROP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "noscript"})

VOID_TAGS = frozenset({"br"})


class _WhitelistParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self._drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth = 1
            return
        if tag in ALLOWED_TAGS:
            if tag in VOID_TAGS:
                self.parts.append(f"<{tag}>")
            else:
                self.parts.append(f"<{tag}>")
                self.open_tags.append(tag)
        # nicht erlaubte Tags: verwerfen, Inhalt bleibt

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            return
        if tag in ALLOWED_TAGS and tag in VOID_TAGS:
            self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self._drop_depth -= 1
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS and tag in self.open_tags:
            # Schließe ggf. dazwischen offen gebliebene Tags (wohlgeformte Ausgabe)
            while self.open_tags:
                open_tag = self.open_tags.pop()
                self.parts.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data):
        if not self._drop_depth and data:
            self.parts.append(escape(data, quote=False))

    def get_html(self) -> str:
        # Am Ende noch offene erlaubte Tags schließen
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)


def sanitize_speech_html(value: str) -> str:
    """
    Bereinigt WYSIWYG-HTML eines Redebeitrags auf die strikte Whitelist.

    Sicher gegen Script-Injection: nur explizit erlaubte Tags ohne
    Attribute bleiben erhalten, alles andere wird entfernt bzw. escaped.
    """
    if not value:
        return ""
    parser = _WhitelistParser()
    parser.feed(value)
    parser.close()
    return parser.get_html()
