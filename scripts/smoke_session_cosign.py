# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Ämterstruktur und mehrstufige Mitzeichnung (Issue #81).

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_session_cosign.py

Prüft:
- Einstellungen: Mitzeichnungsregeln + Amts-Zuordnungen (inkl. Rechte)
- Pflichtangabe „Finanzielle Auswirkungen" vor der Vorlage zur Freigabe
- Kettenaufbau aus Regeln (Vorlagenart, „nur bei finanziellen Auswirkungen")
- Reihenfolge: Stationen zeichnen nacheinander; nur zugeordnete Ämter/Admin
- Freigabe erst nach vollständiger Mitzeichnung
- Zurückweisung wirft die Vorlage zurück in den Entwurf; erneutes Vorlegen
  baut die Kette neu auf
- Arbeitsvorrat „Meine Mitzeichnungen" + Badge
- Ämter erscheinen nicht in der Gremien-Auswahl für Sitzungen
- Tenant-Isolation und Audit
"""

import base64
import os
import secrets
import sys
import tempfile
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_db_path = Path(tempfile.mkdtemp(prefix="mandari_smoke_")) / "smoke.sqlite3"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from apps.session.models import (  # noqa: E402
    SessionAuditLog,
    SessionCosignature,
    SessionCosignatureRule,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionUser,
)

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


def make_user(name, tenant, role):
    user = User.objects.create_user(email=f"{name}@example.org", password="pw-Smoke-1!")
    su = SessionUser.objects.create(user=user, tenant=tenant)
    su.roles.add(role)
    c = Client()
    c.force_login(user)
    return c, su


# =============================================================================
# Setup
# =============================================================================
tenant = SessionTenant.objects.create(name="Mitzeichnungsstadt", slug="mitzeichnungsstadt")
tenant_b = SessionTenant.objects.create(name="Fremdstadt", slug="fremdstadt-cs")

committee = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
dep_recht = SessionOrganization.objects.create(
    tenant=tenant, name="AMT-RECHTSAMT", organization_type="department"
)
dep_kaemmerei = SessionOrganization.objects.create(
    tenant=tenant, name="AMT-KAEMMEREI", organization_type="department"
)

admin_role = SessionRole.objects.create(tenant=tenant, name="Admin", is_admin=True)
admin, su_admin = make_user("admin-cs", tenant, admin_role)

editor_role = SessionRole.objects.create(
    tenant=tenant, name="Sachbearbeitung",
    can_view_papers=True, can_create_papers=True, can_edit_papers=True, can_view_meetings=True,
)
editor, su_editor = make_user("sachbearbeitung-cs", tenant, editor_role)

approver_role = SessionRole.objects.create(
    tenant=tenant, name="Freigabe", can_view_papers=True, can_approve_papers=True
)
approver, su_approver = make_user("freigabe-cs", tenant, approver_role)

cosigner_role = SessionRole.objects.create(tenant=tenant, name="Amt", can_view_papers=True)
recht_client, su_recht = make_user("rechtsamt-cs", tenant, cosigner_role)
kaem_client, su_kaem = make_user("kaemmerei-cs", tenant, cosigner_role)

base = f"/session/{tenant.slug}"

# =============================================================================
print("=== Phase A: Einstellungen (Regeln + Zuordnungen) ===")
resp = admin.get(f"{base}/settings/cosign/")
check("Einstellungsseite -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Ämter gelistet", "AMT-RECHTSAMT" in resp.content.decode("utf-8"))

resp = admin.post(
    f"{base}/settings/cosign/rule/",
    {"department": str(dep_recht.id), "paper_type": "", "order": "1"},
)
resp = admin.post(
    f"{base}/settings/cosign/rule/",
    {"department": str(dep_kaemmerei.id), "paper_type": "", "order": "2", "only_financial": "1"},
)
check("2 Regeln angelegt", SessionCosignatureRule.objects.filter(tenant=tenant).count() == 2)

resp = admin.post(
    f"{base}/settings/cosign/assignment/",
    {"session_user": str(su_recht.id), "department": str(dep_recht.id)},
)
resp = admin.post(
    f"{base}/settings/cosign/assignment/",
    {"session_user": str(su_kaem.id), "department": str(dep_kaemmerei.id)},
)
check("Amts-Zuordnungen gesetzt",
      su_recht.departments.filter(pk=dep_recht.id).exists() and su_kaem.departments.filter(pk=dep_kaemmerei.id).exists())

resp = editor.get(f"{base}/settings/cosign/")
check("Einstellungen ohne manage_settings -> 403", resp.status_code == 403, f"got {resp.status_code}")
resp = editor.post(f"{base}/settings/cosign/rule/", {"department": str(dep_recht.id)})
check("Regelanlage ohne Recht -> 403", resp.status_code == 403, f"got {resp.status_code}")

resp = admin.post(f"{base}/settings/cosign/rule/", {"department": ""})
check("Regel ohne Amt abgelehnt", SessionCosignatureRule.objects.filter(tenant=tenant).count() == 2)

# =============================================================================
print()
print("=== Phase B: Pflichtangabe + Kettenaufbau ===")
paper_fin = SessionPaper.objects.create(
    tenant=tenant, reference="V-FIN", name="VORLAGE-MIT-FINANZ",
    status="draft", created_by=su_editor,
)
resp = editor.post(f"{base}/papers/{paper_fin.id}/workflow/submit/")
paper_fin.refresh_from_db()
check("Submit ohne Finanzangabe blockiert", paper_fin.status == "draft")

paper_fin.has_financial_impact = True
paper_fin.financial_impact_note = "50.000 € aus HH-Stelle 1234"
paper_fin.save()
resp = editor.post(f"{base}/papers/{paper_fin.id}/workflow/submit/")
paper_fin.refresh_from_db()
check("Submit mit Finanzangabe -> In Prüfung", paper_fin.status == "review")
chain = list(paper_fin.cosignatures.order_by("order"))
check("Kette: 2 Stationen (Recht, Kämmerei)", len(chain) == 2
      and chain[0].department_id == dep_recht.id and chain[1].department_id == dep_kaemmerei.id,
      str([(c.department.name, c.order) for c in chain]))

paper_nofin = SessionPaper.objects.create(
    tenant=tenant, reference="V-NOFIN", name="VORLAGE-OHNE-FINANZ",
    status="draft", has_financial_impact=False, created_by=su_editor,
)
resp = editor.post(f"{base}/papers/{paper_nofin.id}/workflow/submit/")
paper_nofin.refresh_from_db()
check("Ohne Finanzauswirkungen: nur Rechtsamt in der Kette",
      paper_nofin.cosignatures.count() == 1
      and paper_nofin.cosignatures.first().department_id == dep_recht.id)

# =============================================================================
print()
print("=== Phase C: Reihenfolge, Zuständigkeit, Freigabe-Sperre ===")
cos_recht = paper_fin.cosignatures.get(department=dep_recht)
cos_kaem = paper_fin.cosignatures.get(department=dep_kaemmerei)

# Kämmerei ist noch nicht dran
resp = kaem_client.post(f"{base}/cosignatures/{cos_kaem.id}/sign/")
cos_kaem.refresh_from_db()
check("Station 2 vor Station 1 blockiert", cos_kaem.status == "pending")

# Falsches Amt darf nicht entscheiden
resp = kaem_client.post(f"{base}/cosignatures/{cos_recht.id}/sign/")
cos_recht.refresh_from_db()
check("Fremdes Amt darf nicht zeichnen", cos_recht.status == "pending")

# Freigabe vor Mitzeichnung blockiert
resp = approver.post(f"{base}/papers/{paper_fin.id}/workflow/approve/")
paper_fin.refresh_from_db()
check("Freigabe vor Mitzeichnung blockiert", paper_fin.status == "review")

# Rechtsamt zeichnet
resp = recht_client.post(f"{base}/cosignatures/{cos_recht.id}/sign/", {"comment": "Rechtlich unbedenklich."})
cos_recht.refresh_from_db()
check("Rechtsamt zeichnet mit", cos_recht.status == "signed" and cos_recht.decided_by_id == su_recht.id)

# Freigabe weiterhin blockiert (Kämmerei offen)
resp = approver.post(f"{base}/papers/{paper_fin.id}/workflow/approve/")
paper_fin.refresh_from_db()
check("Freigabe bei offener Kämmerei blockiert", paper_fin.status == "review")

# Kämmerei zeichnet, dann Freigabe
resp = kaem_client.post(f"{base}/cosignatures/{cos_kaem.id}/sign/")
cos_kaem.refresh_from_db()
check("Kämmerei zeichnet mit", cos_kaem.status == "signed")
resp = approver.post(f"{base}/papers/{paper_fin.id}/workflow/approve/")
paper_fin.refresh_from_db()
check("Freigabe nach vollständiger Mitzeichnung", paper_fin.status == "approved")

check("Audit: Mitzeichnungs-Einträge",
      SessionAuditLog.objects.filter(tenant=tenant, changes__mitzeichnung="AMT-RECHTSAMT").exists())

# =============================================================================
print()
print("=== Phase D: Zurückweisung ===")
cos_nofin = paper_nofin.cosignatures.get(department=dep_recht)
resp = recht_client.post(f"{base}/cosignatures/{cos_nofin.id}/reject/", {"comment": ""})
cos_nofin.refresh_from_db()
check("Zurückweisung ohne Kommentar abgelehnt", cos_nofin.status == "pending")

resp = recht_client.post(f"{base}/cosignatures/{cos_nofin.id}/reject/", {"comment": "Bitte § 12 prüfen."})
paper_nofin.refresh_from_db()
cos_nofin.refresh_from_db()
check("Zurückweisung -> Vorlage im Entwurf", paper_nofin.status == "draft")
check("Station zurückgewiesen mit Kommentar", cos_nofin.status == "rejected" and "§ 12" in cos_nofin.comment)

# Erneut vorlegen -> Kette neu (pending)
resp = editor.post(f"{base}/papers/{paper_nofin.id}/workflow/submit/")
paper_nofin.refresh_from_db()
check("Erneutes Vorlegen baut Kette neu",
      paper_nofin.status == "review"
      and paper_nofin.cosignatures.count() == 1
      and paper_nofin.cosignatures.first().status == "pending")

# =============================================================================
print()
print("=== Phase E: Arbeitsvorrat + Badge ===")
resp = recht_client.get(f"{base}/cosignatures/")
html = resp.content.decode("utf-8")
check("Arbeitsvorrat -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Offene Station gelistet", "V-NOFIN" in html)
import re as _re  # noqa: E402

check(
    "Badge in Navigation",
    "Mitzeichnungen" in html and _re.search(r"nav-badge[^>]*>\s*1\s*<", html) is not None,
)

resp = kaem_client.get(f"{base}/cosignatures/")
check("Kämmerei: keine offene Station", "V-NOFIN" not in resp.content.decode("utf-8"))

# =============================================================================
print()
print("=== Phase F: Detail-Ansicht + Sitzungs-Auswahl ===")
resp = editor.get(f"{base}/papers/{paper_fin.id}/")
html = resp.content.decode("utf-8")
check("Detail: Mitzeichnungslauf sichtbar", "Mitzeichnung & finanzielle Auswirkungen" in html)
check("Detail: Finanzvermerk sichtbar", "50.000" in html)
check("Detail: beide Stationen", "AMT-RECHTSAMT" in html and "AMT-KAEMMEREI" in html)

resp = admin.get(f"{base}/meetings/create/")
html = resp.content.decode("utf-8")
check("Sitzungsformular ohne Ämter", "AMT-RECHTSAMT" not in html and "Hauptausschuss" in html)

# =============================================================================
print()
print("=== Phase G: Tenant-Isolation ===")
org_b = SessionOrganization.objects.create(tenant=tenant_b, name="Fremdamt", organization_type="department")
paper_b = SessionPaper.objects.create(tenant=tenant_b, reference="F-1", name="Fremdvorlage", status="review")
cos_b = SessionCosignature.objects.create(paper=paper_b, department=org_b, order=1)
resp = admin.post(f"{base}/cosignatures/{cos_b.id}/sign/")
cos_b.refresh_from_db()
check("Fremde Mitzeichnung -> 404", resp.status_code == 404 and cos_b.status == "pending")
resp = admin.post(
    f"{base}/settings/cosign/assignment/",
    {"session_user": str(su_recht.id), "department": str(org_b.id)},
)
check("Fremdes Amt nicht zuordenbar", not su_recht.departments.filter(pk=org_b.id).exists())

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
