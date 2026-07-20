# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Editor-Feinschliff Richtung Word/Google Docs (Etappe 3).

Läuft gegen eine frische SQLite-Instanz mit django.test.Client:
    python scripts/smoke_editor_polish.py

Prüft:
- Editor-Seite 200 + neue Toolbar (Formatvorlagen, S/Highlight, Ausrichtung,
  Einfügen-Gruppe mit Seitenumbruch, Suchen&Ersetzen, Überlauf-Menü)
- Statusleiste (Wörter/Zeichen/Seite/Speicherstatus)
- Suchen&Ersetzen-Panel, Tabellen-Kontextleiste, Link-Dialog/-Popover
- WYSIWYG: Briefkopf-Ränder + Schrift im Template-CSS (inkl. Fallback)
- letterheads_json enthält Schrift-Felder
- Editor-Bundle enthält die neuen Funktionen
- Manueller Seitenumbruch im PDF-Export (2 Seiten) und DOCX-Export (w:br page)
- Bestehende Hooks unverändert (Kommentare, Share, Status, KI, Versionen,
  Etappe-1/2-Sidebar-Panels)
"""

import base64
import io
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp_dir = Path(tempfile.mkdtemp(prefix="mandari_smoke_editor_"))
_db_path = _tmp_dir / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

# Sync-Watchdog (insight_sync.apps) nicht starten: er greift parallel auf die
# SQLite zu und blockiert die Migrationen ("database is locked"). Die App
# erkennt Management-Commands an sys.argv — entsprechend tarnen.
sys.argv = ["manage.py", "smoke_editor_polish"]

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

settings.MEDIA_ROOT = str(_tmp_dir / "media")

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from apps.work.motions.models import Motion, OrganizationLetterhead  # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


print("=== Setup ===")
org = Organization.objects.create(name="Fraktion A", slug="fraktion-a")
role = Role.objects.filter(organization=org, is_admin=True).first()
if role is None:
    role = Role.objects.create(organization=org, name="Administrator", is_admin=True)
user = User.objects.create_user(email="autor@example.org", password="test1234!")
membership = Membership.objects.create(user=user, organization=org)
membership.roles.add(role)

client = Client()
client.force_login(user)

DOCS = f"/work/{org.slug}/documents"

motion = Motion.objects.create(
    organization=org,
    author=membership,
    title="Editor-Feinschliff",
    responsible=membership,
)
motion.set_content_encrypted("<p>Erster Absatz</p>")
motion.save()

# --- 1. Editor-Seite: neue Toolbar ---------------------------------------------
print("=== 1. Toolbar-Neuordnung ===")
resp = client.get(f"{DOCS}/{motion.id}/")
check("Editor-Seite 200", resp.status_code == 200, f"got {resp.status_code}")
html = resp.content.decode()

check("Formatvorlagen-Dropdown (styleLabel)", 'x-text="styleLabel"' in html)
check("Formatvorlage: Standard", 'data-editor-cmd="paragraph"' in html)
check("Durchgestrichen-Button", 'data-editor-cmd="strike"' in html)
check("Hervorheben-Button", 'data-editor-cmd="highlight"' in html)
check(
    "Ausrichtungs-Buttons (4x)",
    all(f'data-editor-value="{v}"' in html for v in ("left", "center", "right", "justify")),
)
check("Seitenumbruch-Button", 'data-editor-cmd="pageBreak"' in html)
check("Link-Button (Dialog)", "openLinkDialog()" in html)
check("Suchen&Ersetzen-Button", "toggleSearch()" in html)
check("Überlauf-Menü (…)", 'data-lucide="more-horizontal"' in html)
check("Tooltips mit Kürzeln (Strg+B)", "Fett (Strg+B)" in html)
check("Tooltip Seitenumbruch (Strg+Enter)", "Seitenumbruch (Strg+Enter)" in html)

# --- 2. Suchen & Ersetzen Panel --------------------------------------------------
print("=== 2. Suchen & Ersetzen ===")
check("Suchfeld", 'placeholder="Suchen..."' in html)
check("Ersetzen-Feld", 'placeholder="Ersetzen durch..."' in html)
check("Alle ersetzen", "Alle ersetzen" in html)
check("Groß-/Kleinschreibung-Toggle", "searchMatchCase" in html)
check("Treffer x/y", "searchCurrent + '/' + searchTotal" in html)
check("Strg+F-Handler", "openSearch()" in html and "_globalKeydownHandler" in html)

# --- 3. Statusleiste --------------------------------------------------------------
print("=== 3. Statusleiste ===")
check("Statusleiste vorhanden", 'id="editor-statusbar"' in html)
check("Wörter in Statusleiste", 'x-text="wordCount"' in html)
check("Zeichen in Statusleiste", 'x-text="charCount"' in html)
check("Seite x/y in Statusleiste", 'x-text="currentPage"' in html and 'x-text="pageCount"' in html)
check("Speicherstatus (Speichert…)", "Speichert…" in html)
check("Speicherstatus (Gespeichert)", "'Gespeichert ' + lastSaved" in html)
check("Online-Teilnehmer angebunden", "collabUsers.length + ' online'" in html)

# --- 4. Tabellen-Kontextleiste + Link-Popover -------------------------------------
print("=== 4. Tabellen-Kontextleiste + Link-Popover ===")
check("Tabellen-Kontextleiste (x-show formats.table)", 'x-show="formats.table"' in html)
check("Kontextleiste: Zeile davor/danach", "Zeile davor" in html and "Zeile danach" in html)
check("Kontextleiste: Spalte löschen", "Spalte löschen" in html)
check("Link-Popover", "linkPopupVisible" in html and "linkPopupHref" in html)
check("Link-Popover: bearbeiten/entfernen", "editLinkFromPopup()" in html and "removeLinkFromPopup()" in html)
check("Link-Dialog", "showLinkModal" in html and "insertLink()" in html)

# --- 5. Editor-Init + Strg+S -------------------------------------------------------
print("=== 5. Editor-Init ===")
check("Editor-Init vorhanden", "MandariEditor.createEditor" in html)
check("onSearchUpdate angebunden", "onSearchUpdate" in html)
check("Strg+S-Handler", "e.key.toLowerCase() === 's'" in html)

# --- 6. WYSIWYG: Ränder/Schrift aus dem Briefkopf ----------------------------------
print("=== 6. WYSIWYG-Ränder ===")
# Ohne Briefkopf: Fallback-Werte
check("Fallback-Ränder (30/25/25/25)", "padding: 30mm 25mm 25mm 25mm" in html)
check("Fallback-Schrift (11pt)", "font-size: 11pt" in html)

lh = OrganizationLetterhead.objects.create(
    organization=org,
    name="CD-Briefkopf",
    kind="generated",
    content_margin_top=45,
    content_margin_right=20,
    content_margin_bottom=30,
    content_margin_left=25,
    font_family="Georgia",
    font_size=12,
)
motion.letterhead = lh
motion.save(update_fields=["letterhead"])

resp = client.get(f"{DOCS}/{motion.id}/")
check("Editor mit Briefkopf 200", resp.status_code == 200, f"got {resp.status_code}")
html_lh = resp.content.decode()
check("Briefkopf-Ränder im CSS", "padding: 45mm 20mm 30mm 25mm" in html_lh)
check("Briefkopf-Schriftgröße im CSS", "font-size: 12pt" in html_lh)
check("Briefkopf-Schriftart im CSS", '"Georgia", Arial, Helvetica, sans-serif' in html_lh)
check("letterheads_json: font_family", '"font_family": "Georgia"' in html_lh)
check("letterheads_json: font_size", '"font_size": 12' in html_lh)

# --- 7. Bundle enthält neue Funktionen ----------------------------------------------
print("=== 7. Editor-Bundle ===")
bundle = (PROJECT_DIR / "static" / "js" / "editor.bundle.js").read_text(encoding="utf-8", errors="ignore")
for symbol in ("setSearchTerm", "replaceAll", "findReplace", "cleanPastedHtml", "setPageBreak", "data-page-break"):
    check(f"Bundle enthält {symbol}", symbol in bundle)

# --- 8. Manueller Seitenumbruch im Export --------------------------------------------
print("=== 8. Seitenumbruch im Export ===")
motion.set_content_encrypted('<p>Seite eins</p><div data-page-break="true" class="page-break"></div><p>Seite zwei</p>')
motion.save()

resp = client.get(f"{DOCS}/{motion.id}/export/", {"format": "pdf"})
check("PDF-Export 200", resp.status_code == 200, f"got {resp.status_code}")
check("PDF-Magic", resp.content.startswith(b"%PDF"))

from pypdf import PdfReader  # noqa: E402

reader = PdfReader(io.BytesIO(resp.content))
check("PDF hat 2 Seiten (manueller Umbruch)", len(reader.pages) == 2, f"got {len(reader.pages)}")
page1 = reader.pages[0].extract_text() or ""
page2 = reader.pages[1].extract_text() or ""
check("Seite 1: Inhalt vor Umbruch", "Seite eins" in page1, page1[:80])
check("Seite 2: Inhalt nach Umbruch", "Seite zwei" in page2, page2[:80])

resp = client.get(f"{DOCS}/{motion.id}/export/", {"format": "docx"})
check("DOCX-Export 200", resp.status_code == 200, f"got {resp.status_code}")

from docx import Document  # noqa: E402

doc = Document(io.BytesIO(resp.content))
body_xml = doc.element.body.xml
check("DOCX enthält Seitenumbruch (w:br page)", 'w:type="page"' in body_xml)
body_text = "\n".join(p.text for p in doc.paragraphs)
check("DOCX enthält beide Absätze", "Seite eins" in body_text and "Seite zwei" in body_text)

# --- 9. Bestehende Hooks unverändert --------------------------------------------------
print("=== 9. Bestehende Hooks ===")
check("Kommentar-Tab", "sidebarTab === 'comments'" in html_lh)
check("Details-Panel (Etappe 1)", "sidebarTab === 'details'" in html_lh)
check("Aufgaben-Panel (Etappe 1)", "sidebarTab === 'tasks'" in html_lh)
check("KI-Tab", "sidebarTab === 'ai'" in html_lh)
check("Versionen-Tab", "sidebarTab === 'history'" in html_lh)
check("Share-Modal", "showShareModal" in html_lh)
check("Statuswechsel", "changeStatus(" in html_lh)
check("KI-Aktionen", "aiAction(" in html_lh)
check("Versionen laden", "loadRevisions()" in html_lh)
check("Kommentar-Marks-Handler", "_handleMarkClick" in html_lh)
check("Bild-Modal", "showImageModal" in html_lh)
check("Tabellen-Dropdown (Toolbar)", "editor-table-cmd" in html_lh)

# Toolbar-Kommandos, die es vorher schon gab, sind weiterhin verdrahtet
for cmd in ("bold", "italic", "underline", "bulletList", "orderedList", "taskList", "undo", "redo", "horizontalRule"):
    check(f"Toolbar-Kommando {cmd}", f'data-editor-cmd="{cmd}"' in html_lh)

# --- Ergebnis ---------------------------------------------------------------------------
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
