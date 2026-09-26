# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Positivliste für HTML aus dem Editor (Dokumente, Redebeiträge, Vorlagen).

Der Editor (TipTap, ``frontend/editor/index.ts``) erzeugt einen festen Satz an Elementen und
Attributen. Gespeichertes HTML stammt aber vom Client und aus Importen; es wird deshalb beim
Speichern und bei jeder Ausgabe als HTML auf genau diesen Satz reduziert. Im Projekt gibt es
keine Bibliothek dafür (kein bleach/nh3), daher wie in ``apps/work/meetings/sanitize.py`` eine
eigene, bewusst enge Positivliste auf Basis von ``html.parser``:

- nur Elemente und Attribute des Editor-Schemas; Ereignis-Attribute gibt es nie
- Links nur mit http(s), mailto, tel oder relativ; Bilder nur http(s), relativ oder als
  eingebettetes Rasterbild (``data:image/png|jpeg|gif|webp;base64``)
- ``style`` nur mit Ausrichtung, Einzug, Farben und Breiten; Klassen nur die des Editors
- Inhalt von ``script``, ``style``, ``iframe`` & Co. entfällt ganz, andere unbekannte
  Elemente fallen weg, ihr Text bleibt
- Text und Attributwerte werden neu maskiert; die Ausgabe ist wohlgeformt und idempotent
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

from django.utils.safestring import SafeString, mark_safe

ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "strike",
        "del",
        "ins",
        "sub",
        "sup",
        "code",
        "pre",
        "blockquote",
        "hr",
        "ul",
        "ol",
        "li",
        "a",
        "span",
        "mark",
        "div",
        "label",
        "input",
        "img",
        "table",
        "caption",
        "colgroup",
        "col",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
    }
)

VOID_TAGS = frozenset({"br", "hr", "img", "input", "col"})

#: Elemente, deren Inhalt komplett entfällt (Skript, Styles, eingebettete Fremddokumente)
DROP_CONTENT_TAGS = frozenset(
    {
        "script",
        "style",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "noscript",
        "noembed",
        "noframes",
        "template",
        "textarea",
        "select",
        "svg",
        "math",
        "title",
        "head",
        "xmp",
        "plaintext",
    }
)

GLOBAL_ATTRS = frozenset(
    {
        "class",
        "style",
        "data-type",
        "data-checked",
        "data-comment-id",
        "data-resolved",
        "data-color",
        "data-page-break",
        "contenteditable",
    }
)

TAG_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "target", "rel"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "colwidth"}),
    "th": frozenset({"colspan", "rowspan", "colwidth"}),
    "col": frozenset({"span"}),
    "ol": frozenset({"start", "type"}),
    "input": frozenset({"type", "checked", "disabled"}),
}

BOOLEAN_ATTRS = frozenset({"checked", "disabled"})

#: Klassen, die der Editor selbst setzt (Links, Tabellen, Bilder, Kommentare, Seitenumbruch)
ALLOWED_CLASSES = frozenset(
    {
        "text-primary-600",
        "underline",
        "hover:text-primary-700",
        "editor-table",
        "editor-image",
        "comment-mark",
        "comment-mark--resolved",
        "page-break",
    }
)
_CLASS_PATTERN_RE = re.compile(r"^language-[A-Za-z0-9_+-]{1,40}$")

_COLOR = r"(?:#[0-9a-fA-F]{3,8}|rgba?\(\s*[\d.,%\s]+\)|hsla?\(\s*[\d.,%\sdeg]+\)|[a-zA-Z]{3,20})"
_LENGTH = r"(?:0|\d{1,5}(?:\.\d{1,3})?(?:px|em|rem|%|pt|mm|cm))"
STYLE_RULES: dict[str, re.Pattern[str]] = {
    "text-align": re.compile(r"^(?:left|right|center|justify|start|end)$"),
    "margin-left": re.compile(rf"^{_LENGTH}$"),
    "color": re.compile(rf"^{_COLOR}$"),
    "background-color": re.compile(rf"^{_COLOR}$"),
    "width": re.compile(rf"^{_LENGTH}$"),
    "min-width": re.compile(rf"^{_LENGTH}$"),
}

_NUMBER_LIST_RE = re.compile(r"^\d{1,5}(?:,\d{1,5})*$")
_NUMBER_RE = re.compile(r"^\d{1,5}$")
_COMMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_COLOR_RE = re.compile(rf"^{_COLOR}$")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]+")
_DATA_IMAGE_RE = re.compile(r"^data:image/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+$")
LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
IMAGE_SCHEMES = frozenset({"http", "https"})
REL_TOKENS = frozenset({"noopener", "noreferrer", "nofollow", "ugc"})


def _safe_url(value: str, schemes: frozenset[str]) -> str | None:
    """URL mit erlaubtem Schema oder relativ; sonst ``None``. Steuerzeichen zählen nicht als Trennung."""
    compact = _CONTROL_RE.sub("", value)
    if not compact:
        return None
    match = _SCHEME_RE.match(compact)
    if match is None:
        # Ohne Schema: nur relative Pfade und Anker, keine "\\"-Tricks
        return value.strip() if "\\" not in compact else None
    return value.strip() if match.group(1).lower() in schemes else None


def _clean_style(value: str) -> str | None:
    kept = []
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        prop, _, raw = declaration.partition(":")
        prop = prop.strip().lower()
        raw = raw.strip()
        rule = STYLE_RULES.get(prop)
        if rule is not None and rule.match(raw):
            kept.append(f"{prop}: {raw}")
    return "; ".join(kept) or None


def _clean_class(value: str) -> str | None:
    kept = [c for c in value.split() if c in ALLOWED_CLASSES or _CLASS_PATTERN_RE.match(c)]
    return " ".join(kept) or None


def _clean_attr(tag: str, name: str, value: str | None) -> str | None:
    """Bereinigter Attributwert oder ``None`` (Attribut entfällt)."""
    if name in BOOLEAN_ATTRS:
        return name
    if value is None:
        return None
    if name == "class":
        return _clean_class(value)
    if name == "style":
        return _clean_style(value)
    if name == "href":
        return _safe_url(value, LINK_SCHEMES)
    if name == "src":
        compact = _CONTROL_RE.sub("", value)
        if _DATA_IMAGE_RE.match(compact):
            return compact
        return None if compact.lower().startswith("data:") else _safe_url(value, IMAGE_SCHEMES)
    if name == "target":
        return "_blank" if value == "_blank" else None
    if name == "rel":
        tokens = [t for t in value.lower().split() if t in REL_TOKENS]
        return " ".join(tokens) or None
    if name in ("colspan", "rowspan", "span", "start", "width", "height"):
        return value if _NUMBER_RE.match(value) else None
    if name == "colwidth":
        return value if _NUMBER_LIST_RE.match(value) else None
    if name == "type":
        if tag == "input":
            return "checkbox" if value.lower() == "checkbox" else None
        return value if value in ("1", "a", "A", "i", "I") else None
    if name == "data-type":
        return value if value in ("taskList", "taskItem") else None
    if name in ("data-checked", "data-resolved", "data-page-break"):
        return value if value in ("true", "false") else None
    if name == "data-comment-id":
        return value if _COMMENT_ID_RE.match(value) else None
    if name == "data-color":
        return value if _COLOR_RE.match(value) else None
    if name == "contenteditable":
        return "false" if value == "false" else None
    if name in ("alt", "title"):
        return value
    return None


class _EditorHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self._drop_depth = 0

    def _render_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, frozenset())
        rendered: list[str] = []
        seen: set[str] = set()
        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            if name in seen or name not in allowed:
                continue
            value = _clean_attr(tag, name, raw_value)
            if value is None:
                continue
            seen.add(name)
            rendered.append(f' {name}="{escape(value, quote=True)}"')
        if tag == "img" and "src" not in seen:
            return None  # Bild ohne zulässige Quelle ergibt nichts
        if tag == "input" and "type" not in seen:
            return None  # nur Kontrollkästchen der Aufgabenlisten
        if tag == "a" and "target" in seen and "rel" not in seen:
            rendered.append(' rel="noopener noreferrer nofollow"')
        return f"<{tag}{''.join(rendered)}>"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self._drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        rendered = self._render_start(tag, attrs)
        if rendered is None:
            return
        self.parts.append(rendered)
        if tag not in VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._drop_depth or tag not in ALLOWED_TAGS:
            return
        if tag in VOID_TAGS:
            rendered = self._render_start(tag, attrs)
            if rendered is not None:
                self.parts.append(rendered)
        else:
            # <p/> u. ä.: als leeres Element ausgeben
            rendered = self._render_start(tag, attrs)
            if rendered is not None:
                self.parts.append(f"{rendered}</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self._drop_depth -= 1
            return
        if tag in VOID_TAGS or tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self._drop_depth and data:
            self.parts.append(escape(data, quote=False))

    def get_html(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)


def sanitize_editor_html(value: str | None) -> str:
    """HTML aus dem Editor auf die Positivliste reduzieren (Speichern und Ausgabe)."""
    if not value:
        return ""
    parser = _EditorHtmlParser()
    parser.feed(value)
    parser.close()
    return parser.get_html()


def safe_editor_html(value: str | None) -> SafeString:
    """Bereinigtes Editor-HTML, im Template ohne weiteres Maskieren ausgebbar."""
    return mark_safe(sanitize_editor_html(value))
