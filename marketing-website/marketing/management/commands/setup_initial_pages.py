"""
Management Command: Erstellt den initialen Wagtail Page-Tree.

Idempotent — kann beliebig oft ausgeführt werden.
Erstellt nur Seiten, die noch nicht existieren.

Usage:
    python manage.py setup_initial_pages
"""

from django.core.management.base import BaseCommand
from wagtail.models import Page, Site


class Command(BaseCommand):
    help = "Erstellt die initiale Seitenstruktur für die Marketing-Website"

    def handle(self, *args, **options):
        from blog.models import BlogIndexPage, ReleaseIndexPage
        from marketing.models import ContactPage, HomePage, LegalPage, MarketingPage

        # Wagtail Root Page holen
        root = Page.objects.filter(depth=1).first()
        if not root:
            self.stderr.write(self.style.ERROR("Keine Wagtail Root Page gefunden. Wurden Migrationen ausgeführt?"))
            return

        # Default "Welcome to your new Wagtail site!" entfernen
        welcome = Page.objects.filter(depth=2, title="Welcome to your new Wagtail site!").first()

        # HomePage erstellen (oder finden)
        home = HomePage.objects.first()
        if not home:
            home = HomePage(
                title="Mandari",
                slug="mandari",
                subtitle="Kommunalpolitische Transparenz für Deutschland",
                seo_title="Mandari – Kommunalpolitische Transparenz für Deutschland",
                search_description=(
                    "Mandari macht kommunalpolitische Entscheidungen transparent und zugänglich. "
                    "Open Source unter AGPL-3.0."
                ),
                show_in_menus=True,
            )
            root.add_child(instance=home)
            home.save_revision().publish()
            self.stdout.write(self.style.SUCCESS("  Startseite erstellt"))
        else:
            self.stdout.write("  Startseite existiert bereits")

        # Site-Konfiguration: HomePage als Root setzen
        site = Site.objects.filter(is_default_site=True).first()
        if site:
            if site.root_page_id != home.pk:
                site.root_page = home
                site.save()
                self.stdout.write(self.style.SUCCESS("  Site Root → Startseite"))
        else:
            Site.objects.create(
                hostname="localhost",
                root_page=home,
                is_default_site=True,
                site_name="Mandari",
            )
            self.stdout.write(self.style.SUCCESS("  Site erstellt"))

        # Welcome-Page entfernen (nach Site-Umstellung)
        if welcome:
            welcome.delete()
            self.stdout.write(self.style.SUCCESS("  Welcome-Page entfernt"))

        # ── Marketing-Seiten ──────────────────────────────────────────────

        marketing_pages = [
            {
                "title": "Produkt",
                "slug": "produkt",
                "custom_template": "marketing/produkt.html",
                "seo_title": "Produkt – Mandari",
                "search_description": "Drei Module für kommunalpolitische Transparenz: Insight, Work und Session.",
            },
            {
                "title": "Lösungen",
                "slug": "loesungen",
                "custom_template": "marketing/loesungen.html",
                "seo_title": "Lösungen – Mandari",
                "search_description": "Mandari-Lösungen für Bürger:innen, Fraktionen und Verwaltungen.",
            },
            {
                "title": "Preise",
                "slug": "preise",
                "custom_template": "marketing/preise.html",
                "seo_title": "Preise – Mandari",
                "search_description": "Transparente Preisgestaltung. Insight ist kostenlos, Work ab 39,90€/Monat.",
            },
            {
                "title": "Sicherheit",
                "slug": "sicherheit",
                "custom_template": "marketing/sicherheit.html",
                "seo_title": "Sicherheit & Datenschutz – Mandari",
                "search_description": "AES-256-Verschlüsselung, DSGVO-Konformität, Hosting in Deutschland.",
            },
            {
                "title": "Open Source",
                "slug": "open-source",
                "custom_template": "marketing/open-source.html",
                "seo_title": "Open Source – Mandari",
                "search_description": "Mandari ist Open Source unter AGPL-3.0. Selbst-Hosting möglich.",
            },
            {
                "title": "Über uns",
                "slug": "ueber-uns",
                "custom_template": "marketing/ueber-uns.html",
                "seo_title": "Über uns – Mandari",
                "search_description": "Unsere Mission: Kommunalpolitik transparent und zugänglich machen.",
            },
            {
                "title": "Team",
                "slug": "team",
                "custom_template": "marketing/team.html",
                "seo_title": "Team – Mandari",
                "search_description": "Das Team hinter Mandari.",
            },
            {
                "title": "Partner",
                "slug": "partner",
                "custom_template": "marketing/partner.html",
                "seo_title": "Partner – Mandari",
                "search_description": "Partnerschaften mit Kommunen, Fraktionen und zivilgesellschaftlichen Organisationen.",
            },
            {
                "title": "Mitmachen",
                "slug": "mitmachen",
                "custom_template": "marketing/mitmachen.html",
                "seo_title": "Mitmachen – Mandari",
                "search_description": "Werde Teil der Mandari-Community. Entwicklung, Dokumentation, Übersetzung.",
            },
            {
                "title": "Roadmap",
                "slug": "roadmap",
                "custom_template": "marketing/roadmap.html",
                "seo_title": "Roadmap – Mandari",
                "search_description": "Öffentliche Roadmap und geplante Features.",
            },
            {
                "title": "FAQ",
                "slug": "faq",
                "custom_template": "marketing/faq.html",
                "seo_title": "Häufige Fragen – Mandari",
                "search_description": "Antworten auf häufig gestellte Fragen zu Mandari.",
            },
            {
                "title": "Kommunen",
                "slug": "kommunen",
                "custom_template": "marketing/kommunen.html",
                "seo_title": "Verfügbare Kommunen – Mandari",
                "search_description": "Kommunen, deren Ratsinformationen über Mandari verfügbar sind.",
            },
            {
                "title": "Presse",
                "slug": "presse",
                "custom_template": "marketing/presse.html",
                "seo_title": "Presse – Mandari",
                "search_description": "Pressematerial und Kontakt für Journalist:innen.",
            },
            {
                "title": "Danksagungen",
                "slug": "danksagungen",
                "custom_template": "marketing/danksagungen.html",
                "seo_title": "Danksagungen – Mandari",
                "search_description": "Dank an alle Unterstützer:innen und Open-Source-Projekte.",
            },
        ]

        for page_data in marketing_pages:
            slug = page_data.pop("slug")
            custom_template = page_data.pop("custom_template")

            if MarketingPage.objects.filter(slug=slug).exists():
                self.stdout.write(f"  {page_data['title']} existiert bereits")
                continue

            page = MarketingPage(
                slug=slug,
                custom_template=custom_template,
                show_in_menus=True,
                **page_data,
            )
            home.add_child(instance=page)
            page.save_revision().publish()
            self.stdout.write(self.style.SUCCESS(f"  {page_data['title']} erstellt"))

        # ── Kontaktseite ──────────────────────────────────────────────────

        if not ContactPage.objects.exists():
            contact = ContactPage(
                title="Kontakt",
                slug="kontakt",
                seo_title="Kontakt – Mandari",
                search_description="Kontaktformular und Ansprechpartner.",
                intro="<p>Wir freuen uns über Ihre Nachricht.</p>",
                thank_you_text="<p>Vielen Dank für Ihre Nachricht! Wir melden uns so schnell wie möglich.</p>",
                show_in_menus=True,
            )
            home.add_child(instance=contact)
            contact.save_revision().publish()
            self.stdout.write(self.style.SUCCESS("  Kontakt erstellt"))
        else:
            self.stdout.write("  Kontakt existiert bereits")

        # ── Rechtliche Seiten ─────────────────────────────────────────────

        legal_pages = [
            {
                "title": "Impressum",
                "slug": "impressum",
                "custom_template": "marketing/impressum.html",
                "body": "<p>Angaben gemäß § 5 TMG — bitte im Wagtail-Admin vervollständigen.</p>",
            },
            {
                "title": "Datenschutz",
                "slug": "datenschutz",
                "custom_template": "marketing/datenschutz.html",
                "body": "<p>Datenschutzerklärung — bitte im Wagtail-Admin vervollständigen.</p>",
            },
            {
                "title": "AGB",
                "slug": "agb",
                "custom_template": "marketing/agb.html",
                "body": "<p>Allgemeine Geschäftsbedingungen — bitte im Wagtail-Admin vervollständigen.</p>",
            },
        ]

        for page_data in legal_pages:
            slug = page_data.pop("slug")
            custom_template = page_data.pop("custom_template")

            if LegalPage.objects.filter(slug=slug).exists():
                self.stdout.write(f"  {page_data['title']} existiert bereits")
                continue

            page = LegalPage(
                slug=slug,
                custom_template=custom_template,
                show_in_menus=False,
                **page_data,
            )
            home.add_child(instance=page)
            page.save_revision().publish()
            self.stdout.write(self.style.SUCCESS(f"  {page_data['title']} erstellt"))

        # ── Blog ──────────────────────────────────────────────────────────

        if not BlogIndexPage.objects.exists():
            blog = BlogIndexPage(
                title="Blog",
                slug="blog",
                seo_title="Blog – Mandari",
                search_description="Neuigkeiten, Tutorials und Community-Beiträge rund um Mandari.",
                intro="<p>Neuigkeiten und Einblicke aus der Entwicklung von Mandari.</p>",
                show_in_menus=True,
            )
            home.add_child(instance=blog)
            blog.save_revision().publish()
            self.stdout.write(self.style.SUCCESS("  Blog erstellt"))
        else:
            self.stdout.write("  Blog existiert bereits")

        # ── Releases ──────────────────────────────────────────────────────

        if not ReleaseIndexPage.objects.exists():
            releases = ReleaseIndexPage(
                title="Releases",
                slug="releases",
                seo_title="Releases – Mandari",
                search_description="Versionshistorie und Changelogs.",
                intro="<p>Alle Mandari-Releases mit Changelogs und Release-Notes.</p>",
                show_in_menus=True,
            )
            home.add_child(instance=releases)
            releases.save_revision().publish()
            self.stdout.write(self.style.SUCCESS("  Releases erstellt"))
        else:
            self.stdout.write("  Releases existiert bereits")

        # ── Zusammenfassung ───────────────────────────────────────────────

        total = Page.objects.descendant_of(home).live().count()
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Seitenstruktur fertig: {total} Seiten unter /{home.slug}/"))
