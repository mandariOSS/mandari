# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Erreichbarkeit aller registrierten Seiten (URL-Smoke).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_url_reachability.py

Drei Prüfungen:
  1. URL-Erreichbarkeit: Alle URL-Namen (work, insight, session, accounts,
     oparl_api, öffentliche Seiten) werden per get_resolver enumeriert und —
     soweit parameterlos bzw. mit Testdaten parametrisierbar — als
     eingeloggtes Org-Admin-Mitglied per Django-Client gerendert.
     Jede Exception/jeder 500er ist ein FAIL. 302/403/404/405 sind ok
     (Redirects, POST-only-Endpunkte, fehlende Detail-Objekte).
  2. Template-URL-Abgleich: Alle literalen {% url '...' %}-Namen in
     templates/ müssen als URL-Name registriert sein — sonst wäre die
     jeweilige Seite ein NoReverseMatch-500er.
  3. Permission-Codenames: Jedes permission_required an einer View muss in
     apps.common.permissions.PERMISSIONS definiert sein (sonst ist die
     Funktion für alle Nicht-Admins unerreichbar).

Gefunden/gefixt mit dieser Suite: Regressionen aus dem views/-Paket- und
Template-Partial-Refactoring (fehlende Re-Exports, kaputte Include-Pfade,
nicht existierende {% url %}-Namen).
"""

import base64
import os
import re
import secrets
import sys
import tempfile
import traceback
import uuid as uuid_mod
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_urls_")) / "smoke.sqlite3"
_media_root = _db_path.parent / "media"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"  # DB-Schreiber-Thread stoert SQLite-Migrationen
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""

import django  # noqa: E402

sys.argv = ["manage.py", "smoke_url_reachability"]
django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
_overrides = override_settings(MEDIA_ROOT=str(_media_root))
_overrides.enable()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.common.encryption import TenantEncryption  # noqa: E402
from apps.common.permissions import PERMISSIONS  # noqa: E402
from apps.session.models import SessionRole, SessionTenant, SessionUser  # noqa: E402
from apps.tenants.models import Membership, Organization, Role  # noqa: E402
from django.urls import get_resolver  # noqa: E402
from django.utils import timezone  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# =============================================================================
# Testdaten
# =============================================================================
print("=== Setup Testdaten ===")

source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
body = OParlBody.objects.create(
    external_id="https://ris.example.org/body/1",
    source=source,
    name="Teststadt",
    slug="teststadt",
)
oparl_org = OParlOrganization.objects.create(
    external_id="https://ris.example.org/organization/1",
    body=body,
    name="Rat der Teststadt",
)
oparl_person = OParlPerson.objects.create(
    external_id="https://ris.example.org/person/1",
    body=body,
    name="Max Muster",
)
oparl_meeting = OParlMeeting.objects.create(
    external_id="https://ris.example.org/meeting/1",
    body=body,
    name="Ratssitzung",
    start=timezone.now(),
)
oparl_paper = OParlPaper.objects.create(
    external_id="https://ris.example.org/paper/1",
    body=body,
    name="Testvorlage",
)

org = Organization.objects.create(name="Fraktion Smoke", slug="fraktion-smoke", body=body)
TenantEncryption(org).key
admin_role = Role.objects.filter(organization=org, is_admin=True).first()
if admin_role is None:
    admin_role = Role.objects.create(organization=org, name="Administrator", is_admin=True)
admin = User.objects.create_user(email="admin@example.org", password="test1234!")
admin_ms = Membership.objects.create(user=admin, organization=org)
admin_ms.roles.add(admin_role)

tenant = SessionTenant.objects.create(name="Verwaltung Teststadt", slug="verwaltung-teststadt")
session_role = SessionRole.objects.filter(tenant=tenant, is_admin=True).first()
if session_role is None:
    session_role = SessionRole.objects.create(tenant=tenant, name="Administrator", is_admin=True)
session_user = SessionUser.objects.create(user=admin, tenant=tenant)
session_user.roles.add(session_role)

client = Client()
client.force_login(admin)

# =============================================================================
# 1. URL-Erreichbarkeit
# =============================================================================
print("=== 1. URL-Erreichbarkeit (GET als Org-Admin) ===")

# Parameterwerte für parametrisierbare URLs. Nicht gemappte UUID/Int-Parameter
# bekommen Zufallswerte (Detail-Views antworten dann 404 — das ist ok, die
# View inklusive Fehlerpfad wird trotzdem ausgeführt).
RANDOM_UUID = str(uuid_mod.uuid4())
PARAM_VALUES = {
    "org_slug": org.slug,
    "tenant_slug": tenant.slug,
    "body_slug": body.slug,
    "body_id": str(body.id),
    "paper_id": str(oparl_paper.id),
    "person_id": str(oparl_person.id),
    "member_id": str(admin_ms.id),
    "membership_id": str(admin_ms.id),
    "role_id": str(admin_role.id),
    "category_slug": "allgemein",
    "article_slug": "artikel",
    "app_label": "tenants",
    "uidb64": "x",
    "token": RANDOM_UUID,
    "slug": body.slug,
}

# Per-URL-Name-Overrides, wo Parameternamen mehrdeutig sind
NAME_PARAM_VALUES = {
    "insight:organization_detail": {"pk": str(oparl_org.id)},
    "insight:person_detail": {"pk": str(oparl_person.id)},
    "insight:meeting_detail": {"pk": str(oparl_meeting.id)},
    "insight:paper_detail": {"pk": str(oparl_paper.id)},
    "insight:paper_summary": {"pk": str(oparl_paper.id)},
}

# Nicht sinnvoll automatisiert erreichbar:
SKIP_PREFIXES = (
    "admin/",  # Django-Admin (eigene Test-Suite von Django)
    "media/",  # Datei-Serving (eigener Test: smoke_org_logo.py)
    "__debug__",
)
# Suffix-Match (Namespace-Präfixe variieren, z.B. insight_core:insight:...)
SKIP_NAME_SUFFIXES = (
    "accounts:logout",  # würde die Client-Session beenden
    ":tile_proxy",  # externe Tile-/Karten-Proxies (Netzwerk nötig)
    ":map_style",
    ":map_sprite",
    ":map_sprite_base",
    ":map_glyphs",
    ":file_proxy",  # lädt externe Datei nach (Netzwerk nötig)
)


def walk(patterns, prefix="", ns=""):
    for p in patterns:
        if hasattr(p, "url_patterns"):
            new_ns = ns + p.namespace + ":" if p.namespace else ns
            yield from walk(p.url_patterns, prefix + str(p.pattern), new_ns)
        else:
            name = (ns + p.name) if p.name else None
            yield name, prefix + str(p.pattern), p


PATH_PARAM_RE = re.compile(r"<(?:(?P<conv>[^:<>]+):)?(?P<name>[^:<>]+)>")


def build_url(route, url_name):
    """Substitute test values into a path()-route. None if not possible."""
    if "(?P" in route or route.startswith("^"):
        return None  # re_path: nicht generisch parametrisierbar
    overrides = NAME_PARAM_VALUES.get(url_name, {})

    def repl(m):
        conv, pname = m.group("conv") or "str", m.group("name")
        if pname in overrides:
            return str(overrides[pname])
        if pname in PARAM_VALUES:
            return str(PARAM_VALUES[pname])
        if conv == "uuid":
            return RANDOM_UUID
        if conv == "int":
            return "1"
        return "x"

    return "/" + PATH_PARAM_RE.sub(repl, route)


tested = skipped = 0
status_counts = {}
resolver = get_resolver()
for url_name, route, pattern in walk(resolver.url_patterns):
    if any(route.startswith(p) for p in SKIP_PREFIXES) or (url_name and url_name.endswith(SKIP_NAME_SUFFIXES)):
        skipped += 1
        continue
    url = build_url(route, url_name)
    if url is None:
        skipped += 1
        continue
    label = url_name or route
    try:
        resp = client.get(url)
    except Exception as exc:  # Exception im View-Code == 500er in Produktion
        check(f"GET {label} ({url})", False, f"EXCEPTION {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
        continue
    tested += 1
    status_counts[resp.status_code] = status_counts.get(resp.status_code, 0) + 1
    check(f"GET {label} ({url})", resp.status_code < 500, f"status={resp.status_code}")
    # Parameterlose Seiten sollten als Admin nicht 404/403 sein — nur Hinweis,
    # kein FAIL (einzelne Endpunkte antworten je nach Testdaten legitim 404).
    if resp.status_code in (403, 404) and "<" not in route:
        print(f"  WARN {label} ({url}) status={resp.status_code}")

print(f"  {tested} URLs getestet, {skipped} übersprungen, Status-Verteilung: {dict(sorted(status_counts.items()))}")

# =============================================================================
# 2. Template-{% url %}-Abgleich
# =============================================================================
print("=== 2. Template-{% url %}-Namen vs. registrierte URL-Namen ===")

# Registrierte Namen inkl. aller Namespace-Suffixe: Templates referenzieren
# verschachtelte Namespaces relativ (z.B. 'insight:paper_detail' für den
# vollen Namen 'insight_core:insight:paper_detail').
registered = set()
for url_name, _route, _p in walk(resolver.url_patterns):
    if not url_name:
        continue
    parts = url_name.split(":")
    for i in range(len(parts)):
        registered.add(":".join(parts[i:]))

URL_TAG_RE = re.compile(r"{%\s*url\s+['\"]([^'\"]+)['\"]")
template_root = PROJECT_DIR / "templates"
checked_names = 0
for html in sorted(template_root.rglob("*.html")):
    content = html.read_text(encoding="utf-8", errors="replace")
    for name in URL_TAG_RE.findall(content):
        checked_names += 1
        check(
            f"{{% url '{name}' %}} in {html.relative_to(template_root)}",
            name in registered,
            "URL-Name nicht registriert (NoReverseMatch)",
        )
print(f"  {checked_names} literale URL-Verweise geprüft")

# =============================================================================
# 3. permission_required-Codenames existieren
# =============================================================================
print("=== 3. permission_required-Codenames ===")

from apps.common.mixins import PermissionRequiredMixin as WorkPermissionMixin  # noqa: E402
from apps.session.permissions import SessionPermissionMixin  # noqa: E402

# Session hat ein eigenes Rechtesystem: SessionRole.can_<codename>-Felder
SESSION_PERMISSIONS = {f.name[len("can_") :] for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}

checked_perms = 0
seen = set()
for url_name, _route, pattern in walk(resolver.url_patterns):
    view_class = getattr(pattern.callback, "view_class", None)
    if view_class is None or view_class in seen:
        continue
    seen.add(view_class)
    required = getattr(view_class, "permission_required", None)
    if not required:
        continue
    if issubclass(view_class, SessionPermissionMixin):
        catalog, catalog_name = SESSION_PERMISSIONS, "SessionRole.can_*"
    elif issubclass(view_class, WorkPermissionMixin):
        catalog, catalog_name = PERMISSIONS, "PERMISSIONS"
    else:
        continue
    perms = [required] if isinstance(required, str) else list(required)
    for perm in perms:
        checked_perms += 1
        check(
            f"{view_class.__name__}.permission_required='{perm}'",
            perm in catalog,
            f"Codename nicht in {catalog_name} definiert",
        )
print(f"  {checked_perms} Codenames geprüft")

print(f"\n=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
