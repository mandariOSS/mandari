# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Support ticket models for the Work module.

Provides support ticket system for organizations to contact Mandari support.
"""

import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField, EncryptionMixin


class SupportTicket(EncryptionMixin, models.Model):
    """
    Support ticket from an organization.
    """

    CATEGORY_CHOICES = [
        ("bug", "Fehler melden"),
        ("feature", "Feature-Wunsch"),
        ("question", "Frage"),
        ("account", "Konto/Zugang"),
        ("other", "Sonstiges"),
    ]

    PRIORITY_CHOICES = [
        ("low", "Niedrig"),
        ("normal", "Normal"),
        ("high", "Hoch"),
        ("urgent", "Dringend"),
    ]

    STATUS_CHOICES = [
        ("open", "Offen"),
        ("in_progress", "In Bearbeitung"),
        ("waiting", "Wartet auf Antwort"),
        ("escalated", "Eskaliert"),
        ("on_hold", "Zurückgestellt"),
        ("resolved", "Gelöst"),
        ("closed", "Geschlossen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="support_tickets",
        verbose_name="Organisation",
    )

    # Ticket info
    subject = models.CharField(max_length=500, verbose_name="Betreff")
    description_encrypted = EncryptedTextField(verbose_name="Beschreibung")

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="question", verbose_name="Kategorie")
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default="normal", verbose_name="Priorität")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open", verbose_name="Status")

    # Creator
    created_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="created_tickets",
        verbose_name="Erstellt von",
    )

    # Assignment (for support staff)
    assigned_to = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
        verbose_name="Zugewiesen an",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(blank=True, null=True, verbose_name="Gelöst am")
    closed_at = models.DateTimeField(blank=True, null=True, verbose_name="Geschlossen am")
    escalated_at = models.DateTimeField(blank=True, null=True, verbose_name="Eskaliert am")
    on_hold_at = models.DateTimeField(blank=True, null=True, verbose_name="Zurückgestellt am")
    last_customer_reply_at = models.DateTimeField(blank=True, null=True, verbose_name="Letzte Kundenantwort")

    # On hold reason
    on_hold_reason = models.CharField(max_length=500, blank=True, verbose_name="Grund für Zurückstellung")

    class Meta:
        verbose_name = "Support-Ticket"
        verbose_name_plural = "Support-Tickets"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["assigned_to", "status"]),
        ]

    def __str__(self):
        return f"#{self.id.hex[:8]}: {self.subject}"

    def get_encryption_organization(self):
        return self.organization

    @property
    def description(self) -> str:
        """
        Returns decrypted description for template use.

        This property provides safe access to the encrypted description,
        returning an error message if decryption fails.
        """
        try:
            return self.get_description_decrypted()
        except Exception:
            return "[Inhalt konnte nicht entschlüsselt werden]"


class SupportTicketMessage(EncryptionMixin, models.Model):
    """
    Message in a support ticket thread.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages", verbose_name="Ticket")

    # Content (encrypted)
    content_encrypted = EncryptedTextField(verbose_name="Nachricht")

    # Author - either membership (customer) or user (support staff)
    author_membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ticket_messages",
        verbose_name="Autor (Mitglied)",
    )
    author_staff = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="support_messages",
        verbose_name="Autor (Support)",
    )

    # Internal note (not visible to customer)
    is_internal = models.BooleanField(default=False, verbose_name="Interne Notiz")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ticket-Nachricht"
        verbose_name_plural = "Ticket-Nachrichten"
        ordering = ["created_at"]

    def __str__(self):
        author = self.author_membership or self.author_staff
        return f"{author}: {self.created_at}"

    def get_encryption_organization(self):
        return self.ticket.organization

    @property
    def is_from_support(self) -> bool:
        return self.author_staff is not None

    @property
    def content(self) -> str:
        """
        Returns decrypted content for template use.

        This property provides safe access to the encrypted content,
        returning an error message if decryption fails.
        """
        try:
            return self.get_content_decrypted()
        except Exception:
            return "[Inhalt konnte nicht entschlüsselt werden]"


class SupportTicketAttachment(models.Model):
    """
    File attachment on a support ticket or message.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    ticket = models.ForeignKey(
        SupportTicket, on_delete=models.CASCADE, related_name="attachments", verbose_name="Ticket"
    )
    message = models.ForeignKey(
        SupportTicketMessage,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
        verbose_name="Nachricht",
    )

    file = models.FileField(upload_to="support/attachments/%Y/%m/", verbose_name="Datei")
    filename = models.CharField(max_length=255, verbose_name="Dateiname")
    mime_type = models.CharField(max_length=100, verbose_name="MIME-Typ")
    file_size = models.PositiveIntegerField(default=0, verbose_name="Dateigröße (Bytes)")

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ticket-Anhang"
        verbose_name_plural = "Ticket-Anhänge"
        ordering = ["uploaded_at"]

    def __str__(self):
        return self.filename


class KnowledgeBaseCategory(models.Model):
    """
    Category for knowledge base articles.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=100, verbose_name="Name")
    slug = models.SlugField(max_length=100, unique=True, verbose_name="Slug")
    description = models.TextField(blank=True, verbose_name="Beschreibung")
    icon = models.CharField(max_length=50, default="help-circle", verbose_name="Icon (Lucide)")
    color = models.CharField(max_length=20, default="blue", verbose_name="Farbe")
    sort_order = models.IntegerField(default=0, verbose_name="Sortierung")

    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "KB-Kategorie"
        verbose_name_plural = "KB-Kategorien"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def article_count(self):
        return self.articles.filter(is_published=True).count()


class KnowledgeBaseArticle(models.Model):
    """
    Knowledge base article for self-service support.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    category = models.ForeignKey(
        KnowledgeBaseCategory,
        on_delete=models.CASCADE,
        related_name="articles",
        verbose_name="Kategorie",
    )

    title = models.CharField(max_length=200, verbose_name="Titel")
    slug = models.SlugField(max_length=200, verbose_name="Slug")
    excerpt = models.TextField(blank=True, verbose_name="Kurzbeschreibung")
    content = models.TextField(verbose_name="Inhalt (Markdown)")

    # Publishing
    is_published = models.BooleanField(default=False, verbose_name="Veröffentlicht")
    is_featured = models.BooleanField(default=False, verbose_name="Hervorgehoben")

    # Statistics
    views_count = models.PositiveIntegerField(default=0, verbose_name="Aufrufe")
    helpful_yes = models.PositiveIntegerField(default=0, verbose_name="Hilfreich: Ja")
    helpful_no = models.PositiveIntegerField(default=0, verbose_name="Hilfreich: Nein")

    # SEO/Search
    tags = models.CharField(max_length=500, blank=True, verbose_name="Tags (kommagetrennt)")

    # Author
    author = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="kb_articles",
        verbose_name="Autor",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True, verbose_name="Veröffentlicht am")

    class Meta:
        verbose_name = "KB-Artikel"
        verbose_name_plural = "KB-Artikel"
        ordering = ["-is_featured", "-published_at"]
        unique_together = [["category", "slug"]]
        indexes = [
            models.Index(fields=["is_published", "category"]),
            models.Index(fields=["tags"]),
        ]

    def __str__(self):
        return self.title

    @property
    def helpful_percentage(self):
        total = self.helpful_yes + self.helpful_no
        if total == 0:
            return None
        return int((self.helpful_yes / total) * 100)

    def get_tags_list(self):
        if not self.tags:
            return []
        return [tag.strip() for tag in self.tags.split(",") if tag.strip()]


class ArticleFeedback(models.Model):
    """
    Feedback on knowledge base articles.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    article = models.ForeignKey(
        KnowledgeBaseArticle,
        on_delete=models.CASCADE,
        related_name="feedback",
        verbose_name="Artikel",
    )

    is_helpful = models.BooleanField(verbose_name="War hilfreich")
    comment = models.TextField(blank=True, verbose_name="Kommentar")

    # Optional user association
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="article_feedback",
        verbose_name="Benutzer",
    )

    session_key = models.CharField(max_length=40, blank=True, verbose_name="Session-Key")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Artikel-Feedback"
        verbose_name_plural = "Artikel-Feedback"
        ordering = ["-created_at"]

    def __str__(self):
        status = "Hilfreich" if self.is_helpful else "Nicht hilfreich"
        return f"{self.article.title}: {status}"
