# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Smoke-Test: Ratsfragen-Portal (Abgeordnetenwatch-Stil) + Personenfotos.

Läuft gegen eine frische SQLite-Instanz:
    python scripts/smoke_insight_questions.py

Prüft:
- Mandatsträger:innen-Erkennung (Rat/Hauptorgan + Fraktion, ohne Verwaltungsrollen)
- Portal mit Filtern, Statistik, Fraktions-Ranking; eigener Reiter in der Navigation
- Frage-Workflow: Formular (Honeypot, Mindestlänge, Themenbereich) → Verifizierung →
  Moderations-Hinweis → Freischaltung (Mails an Ratsmitglied + Fragesteller:in) →
  Antwort per Token → Antwort-Moderation → Veröffentlichung
- Detailseite (QAPage-JSON-LD), Personen-Tab, Sitemap, Erinnerungs-Command, Rate-Limit
- Personenfotos: RIS-URL aus Body-Konfiguration/OParl-Feld, lokaler Cache
  (ok/missing/error/manual), Presets, Command, öffentliche Auslieferung
"""

import base64
import io
import os
import secrets
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

# --- Umgebung VOR django.setup() konfigurieren -------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent / "mandari"
sys.path.insert(0, str(PROJECT_DIR))

_tmp = Path(tempfile.mkdtemp(prefix="mandari_smoke_"))
_db_path = _tmp / "smoke.sqlite3"
_media_root = _tmp / "media"
os.environ["DJANGO_SETTINGS_MODULE"] = "mandari.settings"
os.environ["DEBUG"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"
os.environ["ENCRYPTION_MASTER_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode()
os.environ["ELASTICSEARCH_AUTO_INDEX"] = "False"
os.environ["MANDARI_SYNC_WATCHDOG"] = "0"
os.environ["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost"
os.environ["REDIS_URL"] = ""
os.environ["SITE_URL"] = "https://insight.example"

import django  # noqa: E402

django.setup()

from django.conf import settings as _dj_settings  # noqa: E402

_dj_settings.DATABASES["default"].setdefault("OPTIONS", {})["timeout"] = 30

from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client, override_settings  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.utils import timezone  # noqa: E402

setup_test_environment()
_overrides = override_settings(MEDIA_ROOT=str(_media_root))
_overrides.enable()
call_command("migrate", verbosity=0, interactive=False)

from apps.accounts.models import User  # noqa: E402
from insight_core.models import (  # noqa: E402
    OParlBody,
    OParlMembership,
    OParlOrganization,
    OParlPerson,
    OParlSource,
    PublicQuestion,
)
from insight_core.services import person_photos, question_service  # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def html(resp):
    return resp.content.decode("utf-8", errors="replace")


# =============================================================================
# Migrationen vollständig?
# =============================================================================
print("=== Phase 0: Migrationsstand ===")
try:
    call_command("makemigrations", "insight_core", check=True, dry_run=True, verbosity=0)
    check("Keine fehlenden Migrationen für insight_core", True)
except SystemExit:
    check("Keine fehlenden Migrationen für insight_core", False, "makemigrations --check schlägt fehl")

# =============================================================================
# Fixture
# =============================================================================
now = timezone.now()
today = now.date()
source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/oparl/system")
body = OParlBody.objects.create(
    external_id="https://ris.example.org/oparl/body/1",
    source=source,
    name="Fragestadt",
    short_name="Fragestadt",
    slug="fragestadt",
    person_photo_url_template="https://ris.example.org/im/pe{id}.jpg",
    person_photo_id_pattern=r"/people/(\d+)$",
)


def org(name, otype=None, classification=None):
    return OParlOrganization.objects.create(
        external_id=f"https://ris.example.org/oparl/org/{name}",
        body=body,
        name=name,
        organization_type=otype,
        classification=classification,
    )


rat = org("Rat", None, "Rat")
fraktion_a = org("Musterfraktion", "Fraktion", "Fraktionen")
fraktion_b = org("Bündnis-Fraktion im Rat", None, None)
amt = org("Amt für Verwaltung", "Amt", None)


def person(num, given, family, email=None, raw=None):
    return OParlPerson.objects.create(
        external_id=f"https://ris.example.org/oparl/people/{num}",
        body=body,
        given_name=given,
        family_name=family,
        name=f"{given} {family}",
        email=email,
        raw_json=raw or {},
    )


def member(p, o, role, end_date=None):
    return OParlMembership.objects.create(
        external_id=f"https://ris.example.org/oparl/membership/{p.family_name}-{o.name}",
        person=p,
        organization=o,
        role=role,
        end_date=end_date,
    )


anna = person(101, "Anna", "Amberg", email="anna@example.org")
member(anna, rat, "Ratsmitglied")
member(anna, fraktion_a, "Mitglied")
ben = person(102, "Ben", "Berger", email="ben@example.org")
member(ben, fraktion_b, "Fraktionsvorsitzender")
carla = person(103, "Carla", "Cramer", email="carla@example.org")
member(carla, rat, "Dezernent/in")
dirk = person(104, "Dirk", "Dahl")
member(dirk, rat, "Ratsmitglied", end_date=today - timedelta(days=30))
erik = person(105, "Erik", "Ernst")
member(erik, amt, "Sachbearbeiter")

moderator = User.objects.create_superuser(email="moderation@example.org", password="pw-Smoke-1!")

client = Client()
session = client.session
session["active_body_id"] = str(body.id)
session.save()

# =============================================================================
print()
print("=== Phase 1: Mandatsträger:innen-Erkennung ===")
check("Ratsmitglied + Fraktion -> Mandat", question_service.is_mandate_holder(anna))
check("Nur Fraktion (Name enthält Fraktion) -> Mandat", question_service.is_mandate_holder(ben))
check("Dezernent/in im Rat -> kein Mandat", not question_service.is_mandate_holder(carla))
check("Beendete Ratsmitgliedschaft -> kein Mandat", not question_service.is_mandate_holder(dirk))
check("Nur Amt -> kein Mandat", not question_service.is_mandate_holder(erik))
holders = set(question_service.mandate_holders_queryset(body).values_list("id", flat=True))
check("Mandatsträger-Queryset = {Anna, Ben}", holders == {anna.id, ben.id}, str(holders))
check("Fraktion von Anna", question_service.get_faction(anna) == fraktion_a)
check("Ratsrolle von Anna", question_service.get_council_role(anna) == "Ratsmitglied")

# =============================================================================
print()
print("=== Phase 2: Portal, Navigation, Einstieg ===")
resp = client.get("/insight/fragen/")
page = html(resp)
check("Portal -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Empty-State", "Noch keine öffentlichen Fragen" in page)
check("Eigener Reiter in Sidebar", "Ratsfragen" in page and "/insight/fragen/" in page)
check(
    "Navbar-Link Fragen",
    ">\n                    Fragen\n" in page
    or ">Fragen<" in page.replace("\n", "").replace(" ", "")
    or "Fragen" in page,
)

resp = client.get("/insight/fragen/stellen/")
page = html(resp)
check("Einstieg -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Einstieg listet Mandatsträger", "Anna Amberg" in page and "Ben Berger" in page)
check(
    "Einstieg ohne Verwaltung/Ehemalige",
    "Carla Cramer" not in page and "Dirk Dahl" not in page and "Erik Ernst" not in page,
)
check("Fraktion am Personeneintrag", "Musterfraktion" in page)
resp = client.get("/insight/fragen/stellen/?q=Bündnis")
page = html(resp)
check("Einstieg Suche nach Fraktion", "Ben Berger" in page and "Anna Amberg" not in page)

resp = client.get("/insight/")
check("Startseite verlinkt Fragen-Portal", "/insight/fragen/" in html(resp))

# =============================================================================
print()
print("=== Phase 3: Frage stellen -> Verifizierung -> Moderation ===")
resp = client.get(f"/insight/personen/{anna.id}/frage-stellen/")
page = html(resp)
check("Formular für Mandatsträgerin -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Themenbereich-Auswahl im Formular", 'name="topic"' in page and "Verkehr &amp; Mobilität" in page)
check("Spielregeln sichtbar", "Respektvoll" in page)
check("Formular für Dezernentin -> 404", client.get(f"/insight/personen/{carla.id}/frage-stellen/").status_code == 404)
check("Formular für Amtsperson -> 404", client.get(f"/insight/personen/{erik.id}/frage-stellen/").status_code == 404)

base_post = {
    "questioner_name": "Frieda Fragerin",
    "questioner_email": "frieda@example.org",
    "questioner_city": "Fragestadt",
    "topic": "verkehr",
    "subject": "Radweg an der Hauptstraße",
    "question_text": "Wann wird der seit Jahren versprochene Radweg an der Hauptstraße endlich gebaut und wie ist der Zeitplan?",
    "privacy_accepted": "on",
}
resp = client.post(f"/insight/personen/{anna.id}/frage-stellen/", {**base_post, "website": "http://spam"})
check("Honeypot blockt", resp.status_code == 200 and "Ungültige Anfrage" in html(resp))
resp = client.post(f"/insight/personen/{anna.id}/frage-stellen/", {**base_post, "question_text": "Zu kurz."})
check("Mindestlänge erzwungen", resp.status_code == 200 and "mindestens 50 Zeichen" in html(resp))
check("Noch keine Frage gespeichert", PublicQuestion.objects.count() == 0)

mail.outbox.clear()
resp = client.post(f"/insight/personen/{anna.id}/frage-stellen/", base_post)
check("Gültige Frage -> Redirect", resp.status_code == 302 and resp["Location"].endswith("/insight/fragen/gesendet/"))
q1 = PublicQuestion.objects.get()
check("Status unverified + Themenbereich", q1.status == "unverified" and q1.topic == "verkehr")
check("Verifizierungs-Mail an Fragestellerin", len(mail.outbox) == 1 and mail.outbox[0].to == ["frieda@example.org"])
check("Verifizierungslink in Mail", str(q1.verification_token) in mail.outbox[0].body)

check("Unverifizierte Frage nicht im Portal", "Radweg" not in html(client.get("/insight/fragen/")))
check("Unverifizierte Frage: Detail 404", client.get(f"/insight/fragen/{q1.id}/").status_code == 404)

mail.outbox.clear()
resp = client.get(f"/insight/fragen/verifizieren/{q1.verification_token}/")
q1.refresh_from_db()
check("Verifizierung -> 200 + pending", resp.status_code == 200 and q1.status == "pending")
check("Moderations-Hinweis an Superuser", len(mail.outbox) == 1 and mail.outbox[0].to == ["moderation@example.org"])
check(
    "Moderations-Mail nennt Frage + Admin-Link",
    "wartet auf Freigabe" in mail.outbox[0].subject and "/admin/insight_core/publicquestion/" in mail.outbox[0].body,
)
check(
    "Verifizierungslink nur einmal nutzbar",
    client.get(f"/insight/fragen/verifizieren/{q1.verification_token}/").status_code == 404,
)
check("Pending nicht im Portal", "Radweg" not in html(client.get("/insight/fragen/")))

# =============================================================================
print()
print("=== Phase 4: Freischaltung -> Portal, Detail, Personen-Tab ===")
mail.outbox.clear()
check("publish_question", question_service.publish_question(q1, moderator))
q1.refresh_from_db()
check(
    "Status published + published_at",
    q1.status == "published" and q1.published_at is not None and q1.moderated_by == moderator,
)
subjects = sorted(m.subject for m in mail.outbox)
check("Mails: Ratsmitglied + Fragestellerin", len(mail.outbox) == 2, str(subjects))
check(
    "Ratsmitglied erhält Antwort-Link",
    any(str(q1.answer_token) in m.body for m in mail.outbox if m.to == ["anna@example.org"]),
)
check(
    "Fragestellerin erhält Veröffentlichungs-Mail mit Link",
    any(f"/insight/fragen/{q1.id}/" in m.body for m in mail.outbox if m.to == ["frieda@example.org"]),
)
check("Doppelte Freischaltung wirkungslos", not question_service.publish_question(q1, moderator))

resp = client.get("/insight/fragen/")
page = html(resp)
check("Portal listet Frage", "Radweg an der Hauptstraße" in page)
check("Themen-Badge", "Verkehr &amp; Mobilität" in page)
check("Empfängerin + Fraktion an der Karte", "Anna Amberg" in page and "Musterfraktion" in page)
check("Offen-Badge", "Offen seit 0 Tagen" in page)
check("Statistik: 1 Frage, 0 % Quote", "1</p>" in page and ">0<span" in page)
check("Fraktions-Ranking sichtbar", "Antwortquote nach Fraktion" in page)
check("Filter Thema (Treffer)", "Radweg" in html(client.get("/insight/fragen/?thema=verkehr")))
check("Filter Thema (kein Treffer)", "Radweg" not in html(client.get("/insight/fragen/?thema=umwelt")))
check("Filter offen", "Radweg" in html(client.get("/insight/fragen/?status=offen")))
check("Filter beantwortet (leer)", "Radweg" not in html(client.get("/insight/fragen/?status=beantwortet")))
check("Volltext-Suche", "Radweg" in html(client.get("/insight/fragen/?q=Zeitplan")))
check("Filter Fraktion (Treffer)", "Radweg" in html(client.get(f"/insight/fragen/?fraktion={fraktion_a.id}")))
check("Filter Fraktion (kein Treffer)", "Radweg" not in html(client.get(f"/insight/fragen/?fraktion={fraktion_b.id}")))
check("Filter Person", "Radweg" in html(client.get(f"/insight/fragen/?person={anna.id}")))

resp = client.get(f"/insight/fragen/{q1.id}/")
page = html(resp)
check("Detail -> 200", resp.status_code == 200, f"got {resp.status_code}")
check("Detail: Frage + Fragestellerin", "Radweg an der Hauptstraße" in page and "Frieda Fragerin" in page)
check("Detail: noch keine Antwort", "Noch keine Antwort" in page)
check("Detail: QAPage-JSON-LD", "QAPage" in page)
check("Detail: Link kopieren", "Link kopieren" in page)

resp = client.get(f"/insight/personen/{anna.id}/?tab=fragen")
page = html(resp)
check("Personen-Tab zeigt Frage", "Radweg an der Hauptstraße" in page and "Frage stellen" in page)
check("Personen-Hero zeigt Fraktion", "Musterfraktion" in page)
check("Deep-Link öffnet Fragen-Tab", "get('tab') === 'fragen'" in page)
resp = client.get(f"/insight/personen/{ben.id}/")
check("Fraktions-Mitglied ohne Ratsrolle hat Fragen-Tab", "Fragen" in html(resp) and "frage-stellen" in html(resp))
resp = client.get(f"/insight/personen/{carla.id}/")
check("Dezernentin ohne Fragen-Tab", "frage-stellen" not in html(resp))

# =============================================================================
print()
print("=== Phase 5: Antwort per Token -> Moderation -> Veröffentlichung ===")
answer_url = f"/insight/fragen/antworten/{q1.answer_token}/"
check("Antwortformular -> 200", client.get(answer_url).status_code == 200)
resp = client.post(answer_url, {"answer_text": "Kurz."})
check("Antwort Mindestlänge", resp.status_code == 200 and "mindestens 20 Zeichen" in html(resp))
mail.outbox.clear()
resp = client.post(
    answer_url, {"answer_text": "Der Radweg ist für 2027 im Haushalt eingeplant; Baubeginn nach der Sommerpause."}
)
q1.refresh_from_db()
check(
    "Antwort eingereicht -> pending",
    resp.status_code == 200 and q1.answer_status == "pending" and q1.answered_at is not None,
)
check(
    "Moderations-Hinweis zur Antwort", len(mail.outbox) == 1 and "Antwort wartet auf Freigabe" in mail.outbox[0].subject
)
check("Antwortlink danach gesperrt", client.get(answer_url).status_code == 404)
check("Antwort noch nicht öffentlich", "Sommerpause" not in html(client.get(f"/insight/fragen/{q1.id}/")))

mail.outbox.clear()
check("publish_answer", question_service.publish_answer(q1))
q1.refresh_from_db()
check("Antwort veröffentlicht", q1.answer_status == "published" and q1.is_answered)
check(
    "Fragestellerin informiert",
    len(mail.outbox) == 1 and mail.outbox[0].to == ["frieda@example.org"] and "beantwortet" in mail.outbox[0].subject,
)
page = html(client.get(f"/insight/fragen/{q1.id}/"))
check("Detail zeigt Antwort", "Sommerpause" in page and "Antwort von Anna Amberg" in page)
check("Portal: Beantwortet-Badge", "Beantwortet" in html(client.get("/insight/fragen/?status=beantwortet")))

stats = question_service.get_answer_stats(anna)
check(
    "Personen-Statistik 1/1 = 100 %",
    stats["total"] == 1 and stats["answered"] == 1 and stats["rate"] == 100 and stats["avg_response_days"] == 0,
)
ranking = question_service.get_faction_ranking(body)
check(
    "Fraktions-Ranking: Musterfraktion 100 %",
    ranking and ranking[0]["name"] == "Musterfraktion" and ranking[0]["rate"] == 100,
    str(ranking),
)

# =============================================================================
print()
print("=== Phase 6: Erinnerungen, Sortierung, Sitemap, Rate-Limit, Admin ===")
q2 = PublicQuestion.objects.create(
    body=body,
    recipient=ben,
    questioner_name="Gustav",
    questioner_email="gustav@example.org",
    topic="umwelt",
    subject="Baumpflanzungen im Stadtpark",
    question_text="Warum wurden die angekündigten 200 Bäume im Stadtpark bisher nicht gepflanzt?",
    status="published",
    published_at=now - timedelta(days=20),
    privacy_accepted=True,
)
page = html(client.get("/insight/fragen/?sort=offen"))
check("Sortierung 'am längsten offen' zuerst", page.index("Baumpflanzungen") < page.index("Radweg"))
check("Offen-seit-Badge mit 20 Tagen", "Offen seit 20 Tagen" in page)
mail.outbox.clear()
check(
    "Erinnerung nach 14 Tagen gesendet",
    question_service.send_due_reminders(days=14) == 1 and mail.outbox[0].to == ["ben@example.org"],
)
q2.refresh_from_db()
check("reminder_sent_at gesetzt", q2.reminder_sent_at is not None)
check("Keine Doppel-Erinnerung", question_service.send_due_reminders(days=14) == 0)
out = io.StringIO()
call_command("send_question_reminders", dry_run=True, stdout=out)
check("Command send_question_reminders läuft", "Erinnerung" in out.getvalue())

resp = client.get("/sitemap-insight-fragestadt.xml")
check("Sitemap enthält Fragen", resp.status_code == 200 and f"/insight/fragen/{q1.id}/" in html(resp))

for i in range(2):
    PublicQuestion.objects.create(
        body=body,
        recipient=anna,
        questioner_name="Frieda Fragerin",
        questioner_email="frieda@example.org",
        subject=f"Weitere Frage {i}",
        question_text="x" * 60,
        privacy_accepted=True,
    )
check("Rate-Limit greift nach 3 Fragen/Tag", not question_service.check_rate_limit("frieda@example.org"))
resp = client.post(f"/insight/personen/{anna.id}/frage-stellen/", base_post)
check("Formular meldet Tageslimit", resp.status_code == 200 and "zu viele Fragen" in html(resp))

admin = Client()
admin.force_login(moderator)
check("Admin-Liste Fragen -> 200", admin.get("/admin/insight_core/publicquestion/").status_code == 200)
check("Admin-Liste Personen -> 200", admin.get("/admin/insight_core/oparlperson/").status_code == 200)

# =============================================================================
print()
print("=== Phase 7: Personenfotos ===")
check(
    "RIS-Foto-URL aus Body-Konfiguration", anna.photo_url == "https://ris.example.org/im/pe101.jpg", str(anna.photo_url)
)
other = OParlPerson.objects.create(external_id="https://ris.example.org/oparl/other/x", body=body, name="Ohne Muster")
check("Kein Muster-Treffer -> keine URL", other.photo_url is None)
raw_person = OParlPerson.objects.create(
    external_id="https://ris.example.org/oparl/people/999",
    body=body,
    name="Mit OParl-Bild",
    raw_json={"image": "https://cdn.example.org/bild.jpg"},
)
check("OParl-Bildfeld hat Vorrang", raw_person.photo_url == "https://cdn.example.org/bild.jpg")
page = html(client.get(f"/insight/personen/{anna.id}/"))
check("Avatar hotlinkt RIS-URL vor dem Cache", 'src="https://ris.example.org/im/pe101.jpg"' in page)


def make_png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (900, 1200), (120, 80, 200)).save(buf, format="PNG")
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status, content=b"", ctype="image/png"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype}


class FakeClient:
    """Simuliert das RIS: 101-104 liefern Bilder, 105 hat kein Foto, 106 Serverfehler."""

    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def get(self, url):
        FakeClient.calls.append(url)
        if url.endswith("pe105.jpg"):
            return FakeResponse(404, b"", "text/html")
        if url.endswith("pe106.jpg"):
            return FakeResponse(500, b"", "text/html")
        return FakeResponse(200, make_png())


status = person_photos.fetch_person_photo(anna, client=FakeClient())
anna.refresh_from_db()
check("Foto-Abruf ok", status == "ok" and anna.photo_status == "ok" and bool(anna.photo))
check(
    "Datei lokal gespeichert + verkleinert",
    Path(anna.photo.path).exists() and Path(anna.photo.path).stat().st_size < 60_000,
)
from PIL import Image  # noqa: E402

with Image.open(anna.photo.path) as img:
    check("Max. 400 px, JPEG", max(img.size) <= 400 and img.format == "JPEG", f"{img.size} {img.format}")
check("photo_src liefert lokale URL", anna.photo_src.startswith("/media/persons/photos/"))
page = html(client.get(f"/insight/personen/{anna.id}/"))
check("Avatar nutzt lokales Foto", "/media/persons/photos/" in page and "ris.example.org/im/pe101" not in page)
resp = client.get(anna.photo_src)
check("Foto öffentlich abrufbar", resp.status_code == 200, f"got {resp.status_code}")
resp.close()  # FileResponse schließen (Windows-Dateilock)

status = person_photos.fetch_person_photo(erik, client=FakeClient())
erik.refresh_from_db()
check("Kein Foto im RIS -> missing", status == "missing" and erik.photo_status == "missing" and not erik.photo)
check("missing -> keine Bild-URL mehr (Initialen)", erik.photo_src is None)
page = html(client.get(f"/insight/personen/{erik.id}/"))
check("Avatar ohne kaputten Hotlink", "im/pe105.jpg" not in page and "EE" in page)

broken = person(106, "Fehler", "Fall")
status = person_photos.fetch_person_photo(broken, client=FakeClient())
broken.refresh_from_db()
check(
    "Serverfehler -> error + Meldung",
    status == "error" and broken.photo_status == "error" and "HTTP 500" in broken.photo_error,
)

carla.photo_status = "manual"
carla.save(update_fields=["photo_status"])
check(
    "Manuelles Foto wird nicht überschrieben", person_photos.fetch_person_photo(carla, client=FakeClient()) == "skipped"
)

import httpx  # noqa: E402

_real_client = httpx.Client
httpx.Client = FakeClient
try:
    results = person_photos.fetch_photos_for_body(body, force=True, sleep=0)
    check(
        "Body-Lauf: ok/missing/error gezählt, manual übersprungen",
        results["ok"] >= 3
        and results["missing"] == 1
        and results["error"] == 1
        and carla.id not in [p.id for p in OParlPerson.objects.filter(photo_status="ok")],
        str(results),
    )
    out = io.StringIO()
    call_command("fetch_person_photos", body="fragestadt", force=True, sleep=0, stdout=out)
    check(
        "Command fetch_person_photos läuft", "Fragestadt" in out.getvalue() and "ok=" in out.getvalue(), out.getvalue()
    )
finally:
    httpx.Client = _real_client

muenster_src = OParlSource.objects.create(name="Münster", url="https://oparl.stadt-muenster.de/system")
muenster = OParlBody.objects.create(
    external_id="https://oparl.stadt-muenster.de/bodies/0001",
    source=muenster_src,
    name="Stadt Münster",
    slug="muenster",
)
check(
    "Preset setzt Münster-Konfiguration",
    person_photos.apply_photo_presets(muenster) and "sessionnetbi/im/pe{id}.jpg" in muenster.person_photo_url_template,
)
check("Preset für unbekanntes RIS greift nicht", not person_photos.apply_photo_presets(body))

# =============================================================================
print()
print(f"=== Ergebnis: {PASS} OK, {FAIL} FAIL ===")
sys.exit(1 if FAIL else 0)
