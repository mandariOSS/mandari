"""
Mandari Content Models - Blog Posts und Releases
"""

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


class BlogPost(models.Model):
    """Blog-Artikel für Mandari"""

    class Status(models.TextChoices):
        DRAFT = "draft", "Entwurf"
        PUBLISHED = "published", "Veröffentlicht"

    class Category(models.TextChoices):
        NEWS = "news", "Neuigkeiten"
        TUTORIAL = "tutorial", "Tutorial"
        UPDATE = "update", "Update"
        COMMUNITY = "community", "Community"
        TECH = "tech", "Technik"

    # Basis-Felder
    title = models.CharField("Titel", max_length=200)
    slug = models.SlugField("URL-Slug", max_length=200, unique=True, blank=True)
    excerpt = models.TextField(
        "Kurzbeschreibung",
        max_length=500,
        help_text="Kurze Zusammenfassung für die Übersicht (max. 500 Zeichen)"
    )
    content = models.TextField("Inhalt", help_text="Unterstützt Markdown-Formatierung")

    # Meta-Daten
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        verbose_name="Autor"
    )
    category = models.CharField(
        "Kategorie",
        max_length=20,
        choices=Category.choices,
        default=Category.NEWS
    )
    status = models.CharField(
        "Status",
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )

    # Bild
    featured_image = models.ImageField(
        "Beitragsbild",
        upload_to="blog/images/",
        blank=True,
        null=True
    )
    image_alt = models.CharField(
        "Bild-Alternativtext",
        max_length=200,
        blank=True
    )

    # Zeitstempel
    created_at = models.DateTimeField("Erstellt am", auto_now_add=True)
    updated_at = models.DateTimeField("Aktualisiert am", auto_now=True)
    published_at = models.DateTimeField("Veröffentlicht am", null=True, blank=True)

    # SEO
    meta_description = models.CharField(
        "Meta-Beschreibung",
        max_length=160,
        blank=True,
        help_text="Für Suchmaschinen (max. 160 Zeichen)"
    )

    class Meta:
        verbose_name = "Blog-Artikel"
        verbose_name_plural = "Blog-Artikel"
        ordering = ["-published_at", "-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Auto-generate slug
        if not self.slug:
            self.slug = slugify(self.title)
            # Ensure uniqueness
            original_slug = self.slug
            counter = 1
            while BlogPost.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
                self.slug = f"{original_slug}-{counter}"
                counter += 1

        # Set published_at when status changes to published
        if self.status == self.Status.PUBLISHED and not self.published_at:
            self.published_at = timezone.now()

        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("insight_core:blog_detail", kwargs={"slug": self.slug})

    @property
    def is_published(self):
        return self.status == self.Status.PUBLISHED

    @property
    def reading_time(self):
        """Geschätzte Lesezeit in Minuten"""
        word_count = len(self.content.split())
        return max(1, round(word_count / 200))


class Release(models.Model):
    """Changelog/Releases für Mandari"""

    class ReleaseType(models.TextChoices):
        MAJOR = "major", "Major Release"
        MINOR = "minor", "Minor Release"
        PATCH = "patch", "Patch/Bugfix"

    # Basis-Felder
    version = models.CharField(
        "Version",
        max_length=20,
        unique=True,
        help_text="Semantic Versioning, z.B. 1.0.0"
    )
    title = models.CharField("Titel", max_length=200)
    content = models.TextField(
        "Changelog",
        help_text="Unterstützt Markdown-Formatierung. Nutze Listen für Änderungen."
    )

    # Meta-Daten
    release_type = models.CharField(
        "Release-Typ",
        max_length=20,
        choices=ReleaseType.choices,
        default=ReleaseType.MINOR
    )
    release_date = models.DateField("Release-Datum", default=timezone.now)
    is_published = models.BooleanField("Veröffentlicht", default=False)

    # Zusätzliche Infos
    github_url = models.URLField(
        "GitHub Release URL",
        blank=True,
        help_text="Link zum GitHub Release"
    )
    breaking_changes = models.BooleanField(
        "Breaking Changes",
        default=False,
        help_text="Enthält inkompatible Änderungen"
    )

    # Zeitstempel
    created_at = models.DateTimeField("Erstellt am", auto_now_add=True)
    updated_at = models.DateTimeField("Aktualisiert am", auto_now=True)

    class Meta:
        verbose_name = "Release"
        verbose_name_plural = "Releases"
        ordering = ["-release_date", "-version"]

    def __str__(self):
        return f"v{self.version} - {self.title}"

    def get_absolute_url(self):
        return reverse("insight_core:releases") + f"#v{self.version}"

    @property
    def version_parts(self):
        """Gibt Version als Tuple zurück (major, minor, patch)"""
        parts = self.version.split(".")
        return tuple(int(p) for p in parts[:3])

    @property
    def badge_color(self):
        """CSS-Klasse für Release-Typ-Badge"""
        colors = {
            self.ReleaseType.MAJOR: "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300",
            self.ReleaseType.MINOR: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300",
            self.ReleaseType.PATCH: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
        }
        return colors.get(self.release_type, colors[self.ReleaseType.PATCH])
