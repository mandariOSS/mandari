# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Data migration to create initial Knowledge Base content.

Creates:
- Basic categories
- FAQ articles
- Getting started guides
"""

from django.db import migrations
from django.utils import timezone


def create_initial_content(apps, schema_editor):
    """Create initial KB categories and articles."""
    KnowledgeBaseCategory = apps.get_model("work", "KnowledgeBaseCategory")
    KnowledgeBaseArticle = apps.get_model("work", "KnowledgeBaseArticle")

    # ==========================================================================
    # Categories
    # ==========================================================================

    categories = {
        "getting-started": KnowledgeBaseCategory.objects.create(
            name="Erste Schritte",
            slug="erste-schritte",
            description="Anleitungen für den Einstieg in Mandari Work",
            icon="rocket",
            color="blue",
            sort_order=1,
            is_active=True,
        ),
        "meetings": KnowledgeBaseCategory.objects.create(
            name="Sitzungen & Vorbereitung",
            slug="sitzungen",
            description="Alles rund um Ratssitzungen, Tagesordnungen und Sitzungsvorbereitung",
            icon="calendar",
            color="purple",
            sort_order=2,
            is_active=True,
        ),
        "motions": KnowledgeBaseCategory.objects.create(
            name="Anträge",
            slug="antraege",
            description="Anträge erstellen, bearbeiten und einreichen",
            icon="file-text",
            color="green",
            sort_order=3,
            is_active=True,
        ),
        "faction": KnowledgeBaseCategory.objects.create(
            name="Fraktionsarbeit",
            slug="fraktion",
            description="Fraktionssitzungen, Protokolle und interne Abstimmung",
            icon="users",
            color="amber",
            sort_order=4,
            is_active=True,
        ),
        "tasks": KnowledgeBaseCategory.objects.create(
            name="Aufgaben",
            slug="aufgaben",
            description="Aufgabenverwaltung und Kanban-Board",
            icon="check-square",
            color="teal",
            sort_order=5,
            is_active=True,
        ),
        "account": KnowledgeBaseCategory.objects.create(
            name="Konto & Sicherheit",
            slug="konto-sicherheit",
            description="Profil, Passwort, 2FA und Sicherheitseinstellungen",
            icon="shield",
            color="red",
            sort_order=6,
            is_active=True,
        ),
        "faq": KnowledgeBaseCategory.objects.create(
            name="Häufige Fragen",
            slug="faq",
            description="Antworten auf die am häufigsten gestellten Fragen",
            icon="help-circle",
            color="indigo",
            sort_order=7,
            is_active=True,
        ),
    }

    # ==========================================================================
    # Articles - Getting Started
    # ==========================================================================

    KnowledgeBaseArticle.objects.create(
        category=categories["getting-started"],
        title="Willkommen bei Mandari Work",
        slug="willkommen",
        excerpt="Ein Überblick über die wichtigsten Funktionen von Mandari Work für Ratsmitglieder.",
        content="""# Willkommen bei Mandari Work

Mandari Work ist Ihre zentrale Plattform für die kommunalpolitische Arbeit. Hier finden Sie alle Werkzeuge, die Sie für Ihre Tätigkeit als Ratsmitglied benötigen.

## Was kann Mandari Work?

- **Sitzungen vorbereiten**: Alle Ratssitzungen und Tagesordnungen im Überblick, mit persönlichen Notizen und Positionsmarkierungen
- **Anträge erstellen**: Professionelle Anträge mit KI-Unterstützung verfassen
- **Fraktionsarbeit koordinieren**: Interne Sitzungen planen, Protokolle führen und abstimmen
- **Aufgaben verwalten**: Übersichtliches Kanban-Board für alle Todos
- **RIS durchsuchen**: Zugriff auf alle Ratsinformationen Ihrer Kommune

## Erste Schritte

1. **Profil vervollständigen**: Gehen Sie zu *Profil* und ergänzen Sie Ihre Daten
2. **Sicherheit einrichten**: Aktivieren Sie die Zwei-Faktor-Authentifizierung unter *Sicherheit*
3. **Dashboard erkunden**: Das Dashboard zeigt Ihnen anstehende Termine und aktuelle Aufgaben
4. **Sitzung vorbereiten**: Wählen Sie eine bevorstehende Sitzung und beginnen Sie mit der Vorbereitung

## Hilfe & Support

Bei Fragen stehen wir Ihnen gerne zur Verfügung:
- Durchsuchen Sie diese Wissensdatenbank
- Erstellen Sie ein Support-Ticket

Viel Erfolg bei Ihrer Arbeit!
""",
        is_published=True,
        is_featured=True,
        published_at=timezone.now(),
        tags="start, einführung, übersicht, willkommen",
    )

    KnowledgeBaseArticle.objects.create(
        category=categories["getting-started"],
        title="Das Dashboard verstehen",
        slug="dashboard",
        excerpt="Eine Einführung in das Mandari Work Dashboard und seine Funktionen.",
        content="""# Das Dashboard verstehen

Das Dashboard ist Ihre Startseite in Mandari Work. Es gibt Ihnen einen schnellen Überblick über alles Wichtige.

## Bereiche des Dashboards

### Anstehende Termine
Hier sehen Sie Ihre nächsten Sitzungen auf einen Blick:
- Ratssitzungen
- Ausschusssitzungen
- Fraktionssitzungen

Klicken Sie auf einen Termin, um direkt zur Vorbereitung zu gelangen.

### Aktuelle Aufgaben
Ihre wichtigsten offenen Aufgaben werden hier angezeigt. So behalten Sie den Überblick über:
- Fällige Aufgaben
- Aufgaben mit hoher Priorität
- Kürzlich zugewiesene Aufgaben

### Neuigkeiten
Bleiben Sie informiert über:
- Neue Vorlagen im RIS
- Fraktionsmitteilungen
- Systembenachrichtigungen

## Tipps

- Nutzen Sie die **Schnellnavigation** oben rechts für häufige Aktionen
- Das Dashboard wird automatisch aktualisiert
- Passen Sie Ihr Dashboard nach Ihren Bedürfnissen an (kommt bald)
""",
        is_published=True,
        is_featured=False,
        published_at=timezone.now(),
        tags="dashboard, startseite, übersicht, navigation",
    )

    # ==========================================================================
    # Articles - Meetings
    # ==========================================================================

    KnowledgeBaseArticle.objects.create(
        category=categories["meetings"],
        title="Sitzungen vorbereiten",
        slug="sitzungen-vorbereiten",
        excerpt="So bereiten Sie sich effektiv auf Ratssitzungen vor.",
        content="""# Sitzungen vorbereiten

Eine gute Vorbereitung ist der Schlüssel für effektive Sitzungsteilnahme. Mandari Work unterstützt Sie dabei.

## Zur Sitzungsvorbereitung

1. Gehen Sie zu **Sitzungen** in der Navigation
2. Wählen Sie die gewünschte Sitzung aus
3. Klicken Sie auf **Vorbereiten**

## Funktionen der Vorbereitung

### Positionsmarkierung
Für jeden Tagesordnungspunkt können Sie Ihre Position festlegen:
- **Dafür** (grün)
- **Dagegen** (rot)
- **Enthaltung** (grau)
- **Diskussionsbedarf** (gelb)
- **Vertagen** (lila)

### Persönliche Notizen
Schreiben Sie sich Notizen zu jedem TOP:
- Argumente pro/contra
- Fragen, die Sie stellen möchten
- Wichtige Informationen

### Redenotizen
Wenn Sie zu einem TOP sprechen möchten:
1. Markieren Sie "Möchte sprechen"
2. Bereiten Sie Ihre Rede in den Redenotizen vor
3. Schätzen Sie die Redezeit ein

### Zusammenfassung
Die Zusammenfassung zeigt Ihnen:
- Alle Ihre Positionen
- Geplante Redebeiträge
- Offene Fragen

## Tipps

- Beginnen Sie früh mit der Vorbereitung
- Lesen Sie die Vorlagen zu jedem TOP
- Stimmen Sie sich mit Ihrer Fraktion ab
""",
        is_published=True,
        is_featured=True,
        published_at=timezone.now(),
        tags="sitzung, vorbereitung, tagesordnung, position, notizen",
    )

    # ==========================================================================
    # Articles - Motions
    # ==========================================================================

    KnowledgeBaseArticle.objects.create(
        category=categories["motions"],
        title="Anträge erstellen mit KI-Unterstützung",
        slug="antraege-erstellen",
        excerpt="So erstellen Sie professionelle Anträge mit Hilfe der KI-Assistenz.",
        content="""# Anträge erstellen mit KI-Unterstützung

Mandari Work bietet Ihnen einen intelligenten Assistenten für die Antragserstellung.

## Neuen Antrag erstellen

1. Gehen Sie zu **Anträge** → **Neuer Antrag**
2. Wählen Sie den Antragstyp
3. Geben Sie einen Arbeitstitel ein
4. Beginnen Sie mit dem Antrag

## Die KI-Assistenz

Unser KI-Assistent hilft Ihnen bei:

### Text verbessern
Markieren Sie Text und klicken Sie auf "Verbessern". Die KI:
- Korrigiert Rechtschreibung und Grammatik
- Verbessert den Stil
- Macht den Text klarer

### Formale Prüfung
Die KI prüft Ihren Antrag auf:
- Vollständigkeit (Betreff, Begründung, etc.)
- Formale Anforderungen
- Typische Fehler

### Stichpunkte ausformulieren
Schreiben Sie Ihre Ideen als Stichpunkte und lassen Sie die KI:
- Vollständige Sätze daraus machen
- Eine formale Antragsbegründung erstellen

### Titel generieren
Die KI schlägt passende Titel für Ihren Antrag vor.

## Antrag einreichen

1. Prüfen Sie den Antrag mit "Formale Prüfung"
2. Lassen Sie ggf. von Fraktionskollegen gegenlesen
3. Exportieren Sie als PDF oder Word
4. Reichen Sie über das offizielle Verfahren ein

## Tipps

- Speichern Sie regelmäßig (Autosave ist aktiv)
- Nutzen Sie die Versionierung für größere Änderungen
- Diskutieren Sie kontroverse Anträge in der Fraktion
""",
        is_published=True,
        is_featured=True,
        published_at=timezone.now(),
        tags="antrag, ki, assistent, erstellen, einreichen",
    )

    # ==========================================================================
    # Articles - Account & Security
    # ==========================================================================

    KnowledgeBaseArticle.objects.create(
        category=categories["account"],
        title="Zwei-Faktor-Authentifizierung einrichten",
        slug="2fa-einrichten",
        excerpt="Schützen Sie Ihr Konto mit der Zwei-Faktor-Authentifizierung (2FA).",
        content="""# Zwei-Faktor-Authentifizierung einrichten

Die Zwei-Faktor-Authentifizierung (2FA) bietet zusätzlichen Schutz für Ihr Konto.

## Was ist 2FA?

Bei der 2FA benötigen Sie zum Login:
1. Ihr Passwort (etwas, das Sie wissen)
2. Einen Code aus Ihrer Authenticator-App (etwas, das Sie haben)

Selbst wenn jemand Ihr Passwort kennt, kann er sich ohne Ihr Handy nicht einloggen.

## 2FA aktivieren

1. Gehen Sie zu **Profil** → **Sicherheit**
2. Klicken Sie auf **2FA aktivieren**
3. Scannen Sie den QR-Code mit Ihrer Authenticator-App
4. Geben Sie den 6-stelligen Code ein
5. Speichern Sie die Backup-Codes sicher ab

## Authenticator-Apps

Wir empfehlen diese kostenlosen Apps:
- **Microsoft Authenticator** (iOS/Android)
- **Google Authenticator** (iOS/Android)
- **Authy** (iOS/Android/Desktop)

## Backup-Codes

Nach der Einrichtung erhalten Sie 10 Backup-Codes:
- Jeder Code kann nur einmal verwendet werden
- Nutzen Sie diese, wenn Sie keinen Zugriff auf Ihre App haben
- Bewahren Sie die Codes sicher auf (ausdrucken oder in Passwort-Manager)

## 2FA deaktivieren

Falls nötig, können Sie 2FA wieder deaktivieren:
1. Gehen Sie zu **Profil** → **Sicherheit**
2. Klicken Sie auf **2FA deaktivieren**
3. Bestätigen Sie mit Ihrem Passwort

**Hinweis**: Wir empfehlen dringend, 2FA aktiviert zu lassen.
""",
        is_published=True,
        is_featured=False,
        published_at=timezone.now(),
        tags="2fa, sicherheit, authenticator, login, schutz",
    )

    KnowledgeBaseArticle.objects.create(
        category=categories["account"],
        title="Passwort ändern",
        slug="passwort-aendern",
        excerpt="So ändern Sie Ihr Passwort sicher.",
        content="""# Passwort ändern

Ein sicheres Passwort ist wichtig für den Schutz Ihres Kontos.

## Passwort ändern

1. Gehen Sie zu **Profil** → **Sicherheit**
2. Geben Sie Ihr aktuelles Passwort ein
3. Geben Sie das neue Passwort zweimal ein
4. Klicken Sie auf **Passwort ändern**

## Passwort-Anforderungen

Ihr Passwort muss:
- Mindestens 8 Zeichen lang sein
- Mindestens einen Großbuchstaben enthalten
- Mindestens einen Kleinbuchstaben enthalten
- Mindestens eine Zahl enthalten

## Tipps für sichere Passwörter

- Verwenden Sie einen Passwort-Manager
- Nutzen Sie unterschiedliche Passwörter für verschiedene Dienste
- Ändern Sie Ihr Passwort regelmäßig
- Teilen Sie Ihr Passwort niemals mit anderen

## Passwort vergessen?

Falls Sie Ihr Passwort vergessen haben:
1. Klicken Sie auf der Login-Seite auf "Passwort vergessen"
2. Geben Sie Ihre E-Mail-Adresse ein
3. Sie erhalten einen Link zum Zurücksetzen per E-Mail
""",
        is_published=True,
        is_featured=False,
        published_at=timezone.now(),
        tags="passwort, sicherheit, ändern, zurücksetzen",
    )

    # ==========================================================================
    # Articles - FAQ
    # ==========================================================================

    KnowledgeBaseArticle.objects.create(
        category=categories["faq"],
        title="Wie kann ich mein Profil bearbeiten?",
        slug="profil-bearbeiten",
        excerpt="Anleitung zum Bearbeiten Ihrer Profildaten.",
        content="""# Wie kann ich mein Profil bearbeiten?

Sie können Ihr Profil jederzeit anpassen.

## Profil bearbeiten

1. Klicken Sie oben rechts auf Ihren Namen
2. Wählen Sie **Profil**
3. Bearbeiten Sie Ihre Daten
4. Klicken Sie auf **Speichern**

## Welche Daten kann ich ändern?

- Vorname und Nachname
- Profilbild (kommt bald)

Ihre E-Mail-Adresse ist Ihr Login-Name und kann nicht geändert werden. Kontaktieren Sie bei Bedarf den Support.
""",
        is_published=True,
        is_featured=False,
        published_at=timezone.now(),
        tags="profil, bearbeiten, name, daten",
    )

    KnowledgeBaseArticle.objects.create(
        category=categories["faq"],
        title="Warum sehe ich bestimmte Sitzungen nicht?",
        slug="sitzungen-nicht-sichtbar",
        excerpt="Mögliche Gründe, warum Sitzungen nicht angezeigt werden.",
        content="""# Warum sehe ich bestimmte Sitzungen nicht?

Es gibt verschiedene Gründe, warum eine Sitzung nicht angezeigt werden könnte.

## Mögliche Ursachen

### 1. Sie sind kein Mitglied des Gremiums
Mandari Work zeigt nur Sitzungen von Gremien an, denen Sie angehören. Prüfen Sie:
- Sind Sie dem richtigen Gremium zugeordnet?
- Wurde Ihre Mitgliedschaft korrekt eingerichtet?

### 2. Die Sitzung liegt zu weit in der Vergangenheit
Standardmäßig werden nur aktuelle und zukünftige Sitzungen angezeigt. Nutzen Sie den Filter:
- Wählen Sie "Vergangene Sitzungen anzeigen"
- Passen Sie den Zeitraum an

### 3. Die Sitzung wurde noch nicht veröffentlicht
Sitzungen erscheinen erst, wenn sie im Ratsinformationssystem veröffentlicht wurden.

### 4. Technisches Problem
Falls die Sitzung veröffentlicht ist und Sie Mitglied sind:
- Laden Sie die Seite neu
- Kontaktieren Sie den Support

## Support kontaktieren

Wenn das Problem weiterhin besteht, erstellen Sie ein Support-Ticket mit:
- Name der fehlenden Sitzung
- Datum der Sitzung
- Gremium
""",
        is_published=True,
        is_featured=False,
        published_at=timezone.now(),
        tags="sitzung, nicht sichtbar, fehlt, gremium, filter",
    )

    KnowledgeBaseArticle.objects.create(
        category=categories["faq"],
        title="Kann ich Mandari Work auf dem Handy nutzen?",
        slug="mobile-nutzung",
        excerpt="Informationen zur mobilen Nutzung von Mandari Work.",
        content="""# Kann ich Mandari Work auf dem Handy nutzen?

Ja! Mandari Work ist für die mobile Nutzung optimiert.

## Mobile Website

Mandari Work passt sich automatisch an Ihr Gerät an:
- Smartphone
- Tablet
- Desktop

Öffnen Sie einfach die gleiche URL in Ihrem mobilen Browser.

## Tipps für die mobile Nutzung

### Zum Homescreen hinzufügen
Sie können Mandari Work wie eine App auf Ihrem Homescreen speichern:

**iPhone/iPad:**
1. Öffnen Sie Mandari Work in Safari
2. Tippen Sie auf das Teilen-Symbol
3. Wählen Sie "Zum Home-Bildschirm"

**Android:**
1. Öffnen Sie Mandari Work in Chrome
2. Tippen Sie auf die drei Punkte
3. Wählen Sie "Zum Startbildschirm hinzufügen"

### Offline-Nutzung
Bestimmte Funktionen sind auch offline verfügbar (kommt bald).

## Native App

Eine native App für iOS und Android ist in Planung.
""",
        is_published=True,
        is_featured=False,
        published_at=timezone.now(),
        tags="mobil, smartphone, tablet, app, ios, android",
    )


def remove_initial_content(apps, schema_editor):
    """Remove initial content (for reverse migration)."""
    KnowledgeBaseCategory = apps.get_model("work", "KnowledgeBaseCategory")
    KnowledgeBaseCategory.objects.filter(
        slug__in=[
            "erste-schritte", "sitzungen", "antraege", "fraktion",
            "aufgaben", "konto-sicherheit", "faq"
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("work", "0004_add_support_knowledge_base_models"),
    ]

    operations = [
        migrations.RunPython(create_initial_content, remove_initial_content),
    ]
