# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OParl Database Models (Django ORM)

Migriert von SQLAlchemy zu Django ORM.
"""

import uuid
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower, Now
from django.utils import timezone
from mandari_oparl.extensions import (
    APPROVAL_MODE_LABELS,
    IMPLEMENTATION_LABELS,
    RESULT_LABELS,
    VOTING_METHOD_LABELS,
)
from mandari_oparl.ids import IdBases, canonical_id

from apps.common.formatting import human_size
from apps.common.tokens import HashedTokenMixin, unusable_token_hash

#: Kennung gespiegelter Objekte aus der OParl-API von mandari Session: ``…/session/<slug>/api/oparl/…``
SESSION_OPARL_MARKERS = ("/session/", "/api/oparl/")

#: Gründe einer Löschmarkierung im RIS-Bestand (Issue #524, ADR ``docs/adr/20260929-kanonisches-modell.md``), wie im
#: Vertrag ``ris.object.depublished``: in der Quelle gelöscht (``deleted``) oder vom Herausgeber zurückgenommen
#: (``depublished``: zurückgezogen, nicht mehr öffentlich, aus Datenschutzgründen entfernt).
REASON_DELETED_AT_SOURCE = "quelle_geloescht"
REASON_WITHDRAWN = "zurueckgenommen"
REASON_NOT_PUBLIC = "nichtoeffentlich"
REASON_PRIVACY = "datenschutz"
DELETION_REASON_CHOICES = [
    (REASON_DELETED_AT_SOURCE, "In der Quelle gelöscht"),
    (REASON_WITHDRAWN, "Zurückgezogen"),
    (REASON_NOT_PUBLIC, "Nicht mehr öffentlich"),
    (REASON_PRIVACY, "Aus Datenschutzgründen entfernt"),
]
#: Gründe, aus denen ein Objekt nicht gelöscht, sondern zurückgenommen ist (``depublished``)
DEPUBLISHED_REASONS = frozenset({REASON_WITHDRAWN, REASON_NOT_PUBLIC, REASON_PRIVACY})

#: Slugs, die mit festen Adressen kollidieren (``/sitemap-insight-index.xml`` ist der Sitemap-Index)
RESERVED_BODY_SLUGS = frozenset({"index"})
BODY_SLUG_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def validate_body_slug(value: str) -> None:
    """Slug einer Kommune: klein, ohne Umlaute, Bindestriche nur zwischen Wörtern, nicht reserviert."""
    RegexValidator(
        BODY_SLUG_RE, "Nur Kleinbuchstaben (ohne Umlaute), Ziffern und einzelne Bindestriche zwischen Wörtern."
    )(value)
    if value in RESERVED_BODY_SLUGS:
        raise ValidationError(f"„{value}“ ist reserviert.")


def withdrawn_q(prefix: str = "") -> Q:
    """
    Queryset-Gegenstück zu ``withdrawn_by_publisher``: von mandari Session zurückgenommen.

    ``prefix`` gilt für Beziehungen, z. B. ``OParlConsultation.objects.exclude(withdrawn_q("paper"))``.
    Ein leerer Fremdschlüssel zählt nicht als zurückgenommen.
    """
    feld = f"{prefix}__" if prefix else ""
    bedingung = Q(**{f"{feld}deleted": True})
    for marker in SESSION_OPARL_MARKERS:
        bedingung &= Q(**{f"{feld}external_id__contains": marker})
    return bedingung


class CanonicalIdModel(models.Model):
    """Abstrakte Basis: Neue RIS-Objekte erhalten die kanonische Kennung aus ``external_id``.

    ``id = uuid5(NS_MANDARI_RIS, external_id)`` wie im Ingestor (``shared/mandari_oparl/ids.py``,
    ADR ``docs/adr/20260929-kanonisches-modell.md``). Das gilt für jeden Schreibweg in Django
    (``create``, ``update_or_create``, ``bulk_create``, Formulare), aber nur für neu angelegte Objekte
    ohne ausdrücklich gesetzte ``id``. Aus der Datenbank geladene Objekte behalten ihre Kennung; der
    Bestand ändert sich dadurch nie (Links, Lesezeichen, Suchindex). Abweichungen im Bestand zählt
    ``manage.py check_ris_ids``.
    """

    class Meta:
        abstract = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Positionale Werte kommen aus der Datenbank (Model.from_db); eine übergebene id gilt unverändert.
        self._default_id = None if args or "id" in kwargs or "pk" in kwargs else self.pk
        self._assign_canonical_id()

    def _assign_canonical_id(self) -> None:
        """Kanonische Kennung setzen, solange das Objekt neu ist und seine Kennung nicht gesetzt wurde."""
        if not self._state.adding or self._default_id is None or self.pk != self._default_id:
            return
        external_id = getattr(self, "external_id", "")
        if external_id:
            self.pk = self._default_id = canonical_id(external_id)

    def save(self, *args: Any, **kwargs: Any) -> None:
        # external_id kann nach dem Erzeugen gesetzt worden sein (Formulare, Verwaltung)
        self._assign_canonical_id()
        super().save(*args, **kwargs)


class SourceDeletionModel(CanonicalIdModel):
    """Abstrakte Basis: Lösch-Markierung für OParl-Entitäten (Tombstones).

    Objekte, die im Quellsystem gelöscht wurden (OParl ``deleted: true``)
    oder aus der Quelle verschwinden, werden bei uns NIE physisch gelöscht,
    sondern nur markiert. Physische Löschung erfolgt ausschließlich manuell
    per ``manage.py purge_deleted`` auf explizite Aufforderung der Kommune.
    """

    deleted = models.BooleanField(
        default=False,
        # DB-seitiger Default: Der Python-Ingestor schreibt per Raw-SQL/SQLAlchemy
        # und kennt die Spalte nicht zwingend — Inserts ohne die Spalte müssen
        # weiterhin funktionieren.
        db_default=False,
        db_index=True,
        verbose_name="In Quelle gelöscht",
        help_text="Wurde vom Quellsystem als gelöscht markiert (OParl-Tombstone).",
    )
    deleted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Gelöscht am",
        help_text="Zeitpunkt, zu dem die Löschung in der Quelle erkannt wurde.",
    )
    # Grund der Markierung (Issue #524); nullable, damit Schreiber ohne die Spalte weiter markieren können.
    # Leer bei Markierungen vor diesem Stand.
    deletion_reason = models.CharField(
        "Grund der Löschmarkierung",
        max_length=20,
        blank=True,
        null=True,
        choices=DELETION_REASON_CHOICES,
    )

    class Meta:
        abstract = True

    def mark_deleted(self, when: Any = None, reason: str = REASON_DELETED_AT_SOURCE) -> None:
        """Markiert das Objekt als gelöscht bzw. zurückgenommen, mit Grund (idempotent).

        ``oparl_modified`` wird auf den Löschzeitpunkt gesetzt, damit der
        Tombstone in inkrementellen ``modified_since``-Abfragen unserer
        OParl-API auftaucht (Spec: ``modified`` = Zeitpunkt der Löschung).
        """
        from django.utils import timezone

        if self.deleted:
            return
        if reason not in dict(DELETION_REASON_CHOICES):
            raise ValueError("Unbekannter Grund einer Löschmarkierung.")
        self.deleted = True
        self.deleted_at = when or timezone.now()
        self.deletion_reason = reason
        self.oparl_modified = self.deleted_at
        self.save(update_fields=["deleted", "deleted_at", "deletion_reason", "oparl_modified", "updated_at"])
        if self.withdrawn_by_publisher:
            self._forget_summaries()

    def _forget_summaries(self) -> None:
        """KI-Zusammenfassungen, die zurückgenommene Inhalte enthalten können, verwerfen.

        Eine Zusammenfassung fasst alle Anlagen eines Vorgangs zusammen. Wird der Vorgang oder
        eine seiner Anlagen zurückgenommen, entsteht sie bei Bedarf neu – ohne diese Inhalte.
        """
        paper_id = self.pk if isinstance(self, OParlPaper) else getattr(self, "paper_id", None)
        if isinstance(self, OParlPaper | OParlFile) and paper_id:
            OParlPaper.objects.filter(pk=paper_id, summary__isnull=False).update(summary=None)
            if isinstance(self, OParlPaper):
                self.summary = None

    @property
    def depublished(self) -> bool:
        """
        Vom Herausgeber zurückgenommen (``depublished``) statt in der Quelle gelöscht (``deleted``).

        Markierungen ohne Grund (vor Issue #524) gelten als zurückgenommen, wenn mandari Session sie gesetzt hat.
        """
        if not self.deleted:
            return False
        if self.deletion_reason:
            return self.deletion_reason in DEPUBLISHED_REASONS
        return self.withdrawn_by_publisher

    @property
    def deletion_label(self) -> str:
        """Hinweis für Oberflächen: „Zurückgezogen“ bzw. „In der Quelle gelöscht“; leer, solange es das Objekt gibt."""
        if not self.deleted:
            return ""
        return "Zurückgezogen" if self.depublished else "In der Quelle gelöscht"

    @property
    def withdrawn_by_publisher(self) -> bool:
        """
        Von mandari Session selbst zurückgenommen (nicht-öffentlich gestellt oder gelöscht).

        Fremde Ratsinformationssysteme: Gelöschtes bleibt aus Transparenzgründen mit Hinweis
        sichtbar. Eigene Session-Daten dagegen verlassen die Öffentlichkeit vollständig – eine
        Ö→NÖ-Umstellung darf im Bürgerportal keinen Inhalt mehr zeigen.
        """
        ext = getattr(self, "external_id", "") or ""
        return bool(self.deleted) and all(marker in ext for marker in SESSION_OPARL_MARKERS)


class OParlSource(models.Model):
    """Eine registrierte OParl-Datenquelle (z.B. RIS-API einer Stadt)."""

    # Fehlerklassen des Ingestors (ingestor/src/client/oparl_client.py, Issue #123)
    ERROR_KIND_UA_BLOCKED = "ua_blocked"
    ERROR_KIND_SERVER_ERROR_SERIES = "server_error_series"
    ERROR_KIND_ROBOTS_BLOCKED = "robots_blocked"  # Scraper-Quellen: robots.txt verbietet den Crawl (Issue #116)
    ERROR_KIND_CHOICES = [
        (ERROR_KIND_UA_BLOCKED, "User-Agent gesperrt"),
        (ERROR_KIND_SERVER_ERROR_SERIES, "5xx-Serie"),
        (ERROR_KIND_ROBOTS_BLOCKED, "robots.txt sperrt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    url = models.TextField(unique=True)
    contact_email = models.EmailField(max_length=255, blank=True, null=True)
    contact_name = models.CharField(max_length=255, blank=True, null=True)
    website = models.URLField(max_length=1000, blank=True, null=True)

    # Sync-Konfiguration
    is_active = models.BooleanField(default=True)
    last_sync = models.DateTimeField(blank=True, null=True)
    last_full_sync = models.DateTimeField(blank=True, null=True)
    # Aktualität (Issue #556): letzter Abgleich, der alles gelesen hat (jede Kommune, jede Liste ganz, keine
    # Sperr- oder Teilantwort). last_sync/last_full_sync zählen auch Läufe mit Lücken. Setzt der Ingestor.
    last_successful_sync = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Letzter vollständiger Abgleich",
        help_text="Letzter Abgleich ohne Lücke: jede Kommune und jede Liste ganz gelesen.",
    )
    last_successful_full_sync = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Letzter vollständiger Vollabgleich",
        help_text="Letzter Vollabgleich ohne Lücke. Nur danach darf ein Scraper-Abgleich auf Löschungen schließen.",
    )
    sync_config = models.JSONField(default=dict, blank=True)
    # User-Agent je Quelle (Issue #123): leer = Standard des Ingestors
    user_agent = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name="User-Agent",
        help_text=(
            "Leer = Standard des Ingestors (mandari-ingestor/<Version> mit Kontaktadresse). "
            "Nur setzen, wenn mit dem Betreiber der Quelle ein bestimmter Wert vereinbart ist."
        ),
    )

    # Betriebsmonitor: vom Ingestor bei jedem Fehlversuch gesetzt, bei Erfolg zurückgesetzt
    last_error = models.TextField(blank=True, null=True, verbose_name="Letzter Fehler")
    last_error_at = models.DateTimeField(blank=True, null=True, verbose_name="Letzter Fehler am")
    # Sperre oder Störung als Klasse (Issue #123): steuert Bewertung, Empfehlung und Schonung
    last_error_kind = models.CharField(
        max_length=40, blank=True, null=True, choices=ERROR_KIND_CHOICES, verbose_name="Fehlerklasse"
    )
    consecutive_failures = models.PositiveIntegerField(default=0, verbose_name="Fehlversuche in Folge")
    health_alert_sent_at = models.DateTimeField(blank=True, null=True, verbose_name="Alarm gesendet am")

    # Vom Ingestor bei der Autodiscovery erkannte OParl-Version ("1.0"/"1.1");
    # leer = noch nicht erkannt. Siehe docs/SCRAPER_SOURCES.md, Abschnitt OParl 1.0.
    oparl_version = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name="OParl-Version",
        help_text="Vom Ingestor erkannt (z. B. 1.0 bei more! rubin auf gremien.info).",
    )

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Bekannte source_type-Werte (sync_config["source_type"], additiv —
    # bewusst KEINE eigene Spalte, um Schema-Drift zwischen Django und
    # Ingestor-SQLAlchemy zu vermeiden). Siehe docs/SCRAPER_SOURCES.md.
    SOURCE_TYPE_OPARL = "oparl"
    SOURCE_TYPE_BRIDGE_ALLRIS = "bridge:allris"
    SOURCE_TYPE_SCRAPER_SESSIONNET = "scraper:sessionnet"

    class Meta:
        db_table = "oparl_sources"
        verbose_name = "OParl-Quelle"
        verbose_name_plural = "OParl-Quellen"

    def __str__(self):
        return self.name

    @property
    def source_type(self) -> str:
        """
        Art der Quelle: "oparl" (Default), "bridge:<vendor>" (externer
        OParl-Proxy, z. B. Aeroid/oparl-bridge vor ALLRIS) oder
        "scraper:<vendor>" (Ingestor-Scraper-Adapter, z. B. SessionNet).
        Gespeichert in sync_config["source_type"].
        """
        if isinstance(self.sync_config, dict):
            return str(self.sync_config.get("source_type") or self.SOURCE_TYPE_OPARL)
        return self.SOURCE_TYPE_OPARL

    @property
    def is_scraper_source(self) -> bool:
        return self.source_type.startswith("scraper:")

    def id_bases(self) -> IdBases:
        """
        Kanonische Kennungen der Objekte dieser Quelle (Issue #733).

        Steht in ``sync_config["id_base"]`` eine festgeschriebene Basis, bilden sich die Kennungen aller
        Adressen unter ``url`` auf dieser Basis – nach einem Umzug der Quelle bleiben sie so erhalten. Ohne
        Eintrag sind die Adressen kanonisch. Hat sich beim Umzug auch die Form der Adressen geändert, gelten
        ``id_address`` und die Abbildungsregeln ``id_rules``. Ingestor und Spiegel rechnen ebenso
        (``mandari_oparl.ids``).

        Raises:
            ValueError: Die Abbildungsregeln der Quelle sind ungültig.
        """
        return IdBases.for_source(self.url, self.sync_config)


class OParlBodyQuerySet(models.QuerySet):
    def listed(self) -> "OParlBodyQuerySet":
        """Kommunen für Kommunenauswahl, Übersichten, Sitemaps und öffentliche Listen.

        Nicht gelistete Kommunen (z. B. die Demo-Kommune) bleiben per direkter URL erreichbar.
        """
        return self.filter(deleted=False, is_listed=True)


class OParlBody(SourceDeletionModel):
    """Eine Körperschaft/Kommune."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    source = models.ForeignKey(OParlSource, on_delete=models.CASCADE, related_name="bodies")

    name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=100, blank=True, null=True)
    # URL-freundlicher Slug für Sitemaps und SEO
    slug = models.SlugField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        validators=[validate_body_slug],
        help_text=(
            "URL-freundlicher Identifikator (z.B. 'muenster' für Münster): Bürgerportal unter /insight/k/<slug>/ "
            "und Sitemap. Kleinbuchstaben, Ziffern und Bindestriche. Ändern macht bisherige Links ungültig."
        ),
    )
    # Verzeichnis der Kommune im Dokument-Cache (Issue #373), einmal festgeschrieben aus dem damaligen Slug
    # bzw. Kurznamen. Ein neuer Slug oder ein im RIS geänderter Kurzname legt so keine zweite Ablage an, und
    # prune_file_cache rechnet mit demselben Namen wie die gespeicherten Pfade (OParlFile.local_path).
    # Gesetzt nur über services.file_cache.pin_body_dir; nullable, weil der Ingestor die Spalte nicht kennt.
    file_cache_dir = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        editable=False,
        verbose_name="Verzeichnis im Dokument-Cache",
        help_text="Wird beim ersten Zwischenspeichern festgelegt und danach nicht mehr geändert.",
    )
    # Anzeigename für das Frontend (manuell anpassbar)
    display_name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Kurzer Anzeigename für das Frontend (z.B. 'Köln' statt 'Stadt Köln, kreisfreie Stadt')",
    )
    description = models.TextField(
        blank=True,
        null=True,
        help_text="Kurze Beschreibung der Kommune für die Portalseite (2-3 Sätze, z.B. 'Willkommen im Ratsinformationssystem der Stadt Münster...')",
    )
    hero_image = models.ImageField(
        upload_to="bodies/heroes/",
        blank=True,
        null=True,
        help_text="Bild der Kommune für die Portalseite (z.B. Stadtansicht, Rathaus). Empfohlen: 1920×1080px, Querformat.",
    )
    hero_image_credit = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Bildnachweis (z.B. 'Foto: Max Mustermann, CC BY-SA 4.0')",
    )
    website = models.URLField(max_length=1000, blank=True, null=True)
    license = models.TextField(blank=True, null=True)
    license_valid_since = models.DateTimeField(blank=True, null=True)
    classification = models.CharField(max_length=100, blank=True, null=True)

    # OParl List URLs (für Sync)
    organization_list_url = models.TextField(blank=True, null=True)
    person_list_url = models.TextField(blank=True, null=True)
    meeting_list_url = models.TextField(blank=True, null=True)
    paper_list_url = models.TextField(blank=True, null=True)
    membership_list_url = models.TextField(blank=True, null=True)
    agenda_item_list_url = models.TextField(blank=True, null=True)
    file_list_url = models.TextField(blank=True, null=True)

    # Sync-Tracking
    last_sync = models.DateTimeField(blank=True, null=True)

    # Logo für die Kommune (SVG, PNG, JPG, WebP erlaubt)
    logo = models.FileField(
        upload_to="bodies/logos/",
        blank=True,
        null=True,
        validators=[FileExtensionValidator(allowed_extensions=["svg", "png", "jpg", "jpeg", "webp", "gif"])],
        help_text="Logo der Kommune (SVG, PNG, JPG, WebP). Format wird automatisch angepasst.",
    )
    # WebP-Fassungen des Logos in Anzeigegröße (insight_core/logo_vorschau.py); NULL = noch keine.
    # Nullbar ohne Default: Der Ingestor legt Kommunen per SQLAlchemy an und kennt die Spalte nicht.
    logo_thumbnails = models.JSONField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="Logo-Vorschaubilder",
        help_text="Wird beim Speichern eines Logos erzeugt (Befehl build_logo_thumbnails für den Bestand).",
    )

    # Geografische Daten (für Karten)
    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        blank=True,
        null=True,
        help_text="Breitengrad des Zentrums (z.B. 51.9606649 für Münster)",
    )
    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        blank=True,
        null=True,
        help_text="Längengrad des Zentrums (z.B. 7.6261347 für Münster)",
    )
    # Bounding Box für die Kartenansicht
    bbox_north = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    bbox_south = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    bbox_east = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    bbox_west = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    # OSM Relation ID für automatisches Abrufen der Grenzen
    osm_relation_id = models.BigIntegerField(
        blank=True, null=True, help_text="OpenStreetMap Relation ID (z.B. 62591 für Münster)"
    )
    # Amtlicher Gemeindeschlüssel (AGS), z.B. "05515000" für Münster
    ags = models.CharField(
        max_length=8,
        blank=True,
        null=True,
        verbose_name="AGS",
        help_text="Amtlicher Gemeindeschlüssel (8-stellig, z.B. 05515000 für Münster)",
    )
    # Amtlicher Regionalschlüssel (Issue #351). Verbandsgemeinden, Ämter und Samtgemeinden haben
    # keinen Gemeindeschlüssel, nur diesen (9 Stellen); ihre Gemeinden beginnen mit ihm.
    rgs = models.CharField(
        max_length=12,
        blank=True,
        null=True,
        verbose_name="Regionalschlüssel",
        help_text=(
            "Amtlicher Regionalschlüssel (12-stellig, bei Verbandsgemeinden, Ämtern und Samtgemeinden "
            "9-stellig). Wird von resolve_body_geodata aus OParl oder OSM übernommen."
        ),
    )
    # Körperschaften ohne eigenes Gebiet (Zweckverband, GmbH, Waldgemarkung …, Issue #351).
    # DB-seitige Defaults: Der Ingestor legt Kommunen per SQLAlchemy an und kennt die Spalten nicht.
    is_non_territorial = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="Keine Gebietskörperschaft",
        help_text=(
            "Zweckverband, Gesellschaft, Anstalt, Waldgemarkung o. Ä. ohne eigenes Gebiet: keine OSM-Grenze, "
            "kein Straßenverzeichnis, erscheint nicht als Lücke. Die Karte zeigt das Gebiet der "
            "übergeordneten Körperschaft. Verbandsgemeinden sind Gebietskörperschaften."
        ),
    )
    territory_parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="non_territorial_members",
        verbose_name="Gebiet von",
        help_text="Übergeordnete Körperschaft, deren Gebiet für die Karte gilt (nur ohne eigenes Gebiet).",
    )
    territory_set_manually = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="Gebietsangabe von Hand gesetzt",
        help_text=(
            "Wird beim Ändern von „Keine Gebietskörperschaft“ oder „Gebiet von“ im Admin gesetzt. "
            "resolve_body_geodata ändert die beiden Angaben dann nicht mehr."
        ),
    )

    # Sichtbarkeit im Portal
    is_listed = models.BooleanField(
        default=True,
        # DB-seitiger Default: Der Ingestor legt Kommunen per SQLAlchemy an und kennt die Spalte nicht.
        db_default=True,
        verbose_name="In Listen anzeigen",
        help_text=(
            "Kommune erscheint in Kommunenauswahl, Übersichten, Sitemaps und öffentlichen Listen. "
            "Ausgeschaltet bleibt sie per direkter URL erreichbar (z. B. Demo-Kommune)."
        ),
    )
    # Akzentfarbe im Bürgerportal der Körperschaft (/insight/k/<slug>/, Issue #317). Nur Django kennt
    # die Spalte; sie ist nullable, damit Inserts des Ingestors weiter funktionieren.
    accent_color = models.CharField(
        max_length=7,
        blank=True,
        null=True,
        validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", "Bitte eine Farbe im Format #1a2b3c angeben.")],
        verbose_name="Akzentfarbe im eigenen Portal",
        help_text="Hexadezimal, z. B. #1e40af. Leer: Primärfarbe des Session-Mandanten, sonst mandari-Farben.",
    )
    # Hinweis im Bürgerportal (Issue #734), z. B. wenn die Kommune ihre Daten nicht mehr bereitstellt. Nur
    # Django kennt die Spalte; db_default, damit Inserts des Ingestors und älterer Images weiter funktionieren.
    portal_notice = models.TextField(
        max_length=1000,
        blank=True,
        default="",
        db_default="",
        verbose_name="Hinweis im Bürgerportal",
        help_text=(
            "Wird auf Einstieg und Listenseiten der Kommune angezeigt, z. B. „Die Stadt hat die Bereitstellung "
            "ihrer Daten über die OParl-Schnittstelle zum … beendet.“ Leer: kein Hinweis."
        ),
    )

    # Personenfoto-Konfiguration
    person_photo_url_template = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text=(
            "URL-Template für Personenfotos. Verwende {id} als Platzhalter für die Person-ID. "
            "Beispiel SessionNet: https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg"
        ),
    )
    person_photo_id_pattern = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=(
            "Regex-Pattern zum Extrahieren der ID aus der external_id der Person. "
            "Muss eine Capture-Group enthalten. "
            r"Beispiel: /people/(\d+)$"
        ),
    )

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OParlBodyQuerySet.as_manager()

    class Meta:
        db_table = "oparl_bodies"
        verbose_name = "OParl Kommune"
        verbose_name_plural = "OParl Kommunen"
        ordering = ["name"]

    def __str__(self):
        return self.get_display_name()

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Vor jeder Änderung einer schon gespeicherten Kommune das Cache-Verzeichnis festschreiben – aus dem
        # Stand in der Datenbank, also vor einem neuen Slug oder Kurznamen. Ein vorher geladenes Objekt
        # übernimmt einen inzwischen festgeschriebenen Namen, statt ihn mit NULL zu überschreiben (Issue #373).
        if not self.file_cache_dir and not self._state.adding:
            from .services.file_cache import pin_body_dir

            pin_body_dir(self)
        super().save(*args, **kwargs)
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "logo" in update_fields:
            self.update_logo_thumbnails()

    def update_logo_thumbnails(self, *, force: bool = False) -> bool:
        """Vorschaubilder des Logos erzeugen oder abräumen; ``True``, wenn sich etwas geändert hat."""
        from .logo_vorschau import abgleichen

        neu = abgleichen(
            self.logo, self.logo_thumbnails, erzwingen=force, anderswo_genutzt=self._logo_vorschauen_anderer
        )
        if neu == self.logo_thumbnails:
            return False
        self.logo_thumbnails = neu
        type(self).objects.filter(pk=self.pk).update(logo_thumbnails=neu)
        return True

    def _logo_vorschauen_anderer(self, inhalt: str) -> set[str]:
        """Vorschaubilder mit diesem Hash, die andere Kommunen zeigen (dieselbe Logodatei, z. B. per Shell gesetzt)."""
        andere = type(self).objects.exclude(pk=self.pk).filter(logo_thumbnails__hash=inhalt)
        return {
            str(groesse["name"])
            for daten in andere.values_list("logo_thumbnails", flat=True)
            if isinstance(daten, dict)
            for groesse in daten.get("sizes", [])
            if groesse.get("name")
        }

    @property
    def logo_img_attrs(self) -> str:
        """``src``/``srcset``/``width``/``height`` für ``<img {{ body.logo_img_attrs }} …>`` (Vorschau oder Original)."""
        from .logo_vorschau import img_attrs

        return img_attrs(self.logo, self.logo_thumbnails)

    def get_display_name(self) -> str:
        """Gibt den Anzeigenamen zurück (display_name > short_name > name)."""
        return self.display_name or self.short_name or self.name

    @property
    def is_regional_level(self) -> bool:
        """Gebiet oberhalb der Gemeinde, etwa ein Regierungsbezirk oder Kreis (Issue #54).

        Der amtliche Schlüssel hat bei Gemeinden acht Stellen, höhere Ebenen tragen ihn gekürzt:
        Land zwei, Regierungsbezirk drei, Kreis fünf Stellen (``053`` = Regierungsbezirk Köln).
        OpenStreetMap führt ihn ebenso. Für solche Gebiete gibt es kein Straßen- und
        Adressverzeichnis – für einen ganzen Bezirk wäre das unverhältnismäßig, die Karte braucht
        nur Grenze und Ausschnitt.
        """
        ags = (self.ags or "").strip()
        return bool(ags) and len(ags) < 8

    def get_initials(self):
        """Gibt die Initialen für Fallback-Anzeige zurück."""
        name = self.get_display_name()
        return name[:2].upper() if name else "??"


class OParlOrganization(SourceDeletionModel):
    """Ein Gremium/eine Fraktion."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="organizations")

    name = models.CharField(max_length=500, blank=True, null=True)
    short_name = models.CharField(max_length=100, blank=True, null=True)
    organization_type = models.CharField(max_length=100, blank=True, null=True)
    classification = models.CharField(max_length=100, blank=True, null=True)
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)
    website = models.URLField(max_length=1000, blank=True, null=True)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_organizations"
        verbose_name = "Gremium"
        verbose_name_plural = "Gremien"
        ordering = ["name"]

    def __str__(self):
        return self.name or f"Gremium {self.id}"

    def get_display_name(self) -> str:
        """Anzeigename (voller Name; Kurzname nur als Fallback)."""
        return self.name or self.short_name or str(self.id)

    @property
    def is_active(self):
        """Prüft ob das Gremium noch aktiv ist."""
        from datetime import datetime

        from django.utils import timezone

        if self.end_date is None:
            return True
        end = self.end_date.date() if isinstance(self.end_date, datetime) else self.end_date
        return end >= timezone.now().date()


class OParlPerson(SourceDeletionModel):
    """Eine Person (Ratsmitglied)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="persons")

    name = models.CharField(max_length=255, blank=True, null=True)
    family_name = models.CharField(max_length=255, blank=True, null=True)
    given_name = models.CharField(max_length=255, blank=True, null=True)
    title = models.CharField(max_length=100, blank=True, null=True)
    gender = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=100, blank=True, null=True)

    # Lokal zwischengespeichertes Foto (Django-managed, vom Ingestor nie
    # überschrieben — siehe ENRICHMENT_FIELDS im Ingestor). Wird per
    # `fetch_person_photos` aus dem RIS geladen oder im Admin hochgeladen.
    PHOTO_STATUS_CHOICES = [
        ("unknown", "Noch nicht geprüft"),
        ("ok", "Foto vorhanden"),
        ("missing", "Kein Foto im RIS"),
        ("error", "Fehler beim Abruf"),
        ("manual", "Manuell hochgeladen"),
    ]
    photo = models.FileField(upload_to="persons/photos/", blank=True, null=True, verbose_name="Foto")
    # db_default: siehe OParlFile.local_status (Ingestor-INSERTs kennen diese Spalten nicht)
    photo_status = models.CharField(
        max_length=20, choices=PHOTO_STATUS_CHOICES, default="unknown", db_default="unknown"
    )
    photo_fetched_at = models.DateTimeField(blank=True, null=True)
    photo_error = models.CharField(max_length=255, blank=True, default="", db_default="")

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_persons"
        verbose_name = "Person"
        verbose_name_plural = "Personen"
        ordering = ["family_name", "given_name"]

    def __str__(self):
        if self.name:
            return self.name
        parts = []
        if self.title:
            parts.append(self.title)
        if self.given_name:
            parts.append(self.given_name)
        if self.family_name:
            parts.append(self.family_name)
        return " ".join(parts) if parts else f"Person {self.id}"

    @property
    def display_name(self):
        """Formatierter Anzeigename."""
        if self.name:
            return self.name
        parts = []
        if self.given_name:
            parts.append(self.given_name)
        if self.family_name:
            parts.append(self.family_name)
        return " ".join(parts) if parts else str(self.id)

    @property
    def photo_url(self):
        """Entfernte Bild-URL im RIS (Body-Konfiguration oder OParl-Feld), oder None."""
        import re

        # Manche RIS liefern ein (nicht standardisiertes) Bildfeld direkt mit
        raw = self.raw_json if isinstance(self.raw_json, dict) else {}
        for key in ("image", "photo", "picture"):
            value = raw.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value

        template = self.body.person_photo_url_template
        pattern = self.body.person_photo_id_pattern
        if not template or not pattern:
            return None
        try:
            match = re.search(pattern, self.external_id)
        except re.error:
            return None
        if not match or not match.groups():
            return None
        return template.replace("{id}", match.group(1))

    @property
    def photo_src(self):
        """
        Anzeige-URL für Templates: lokal gecachtes Foto zuerst, sonst die
        RIS-URL (Hotlink). Ist im RIS nachweislich kein Foto vorhanden,
        wird gar nichts geliefert (Initialen-Fallback statt kaputtem Bild).
        """
        if self.photo:
            return self.photo.url
        if self.photo_status == "missing":
            return None
        return self.photo_url

    @property
    def initials(self):
        """Initialen für Fallback-Avatar."""
        parts = []
        if self.given_name:
            parts.append(self.given_name[0])
        if self.family_name:
            parts.append(self.family_name[0])
        return "".join(parts).upper() if parts else "?"


class OParlMeeting(SourceDeletionModel):
    """Eine Sitzung."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="meetings")

    name = models.CharField(max_length=500, blank=True, null=True)
    meeting_state = models.CharField(max_length=100, blank=True, null=True)
    cancelled = models.BooleanField(default=False)

    start = models.DateTimeField(blank=True, null=True, db_index=True)
    end = models.DateTimeField(blank=True, null=True)

    location_name = models.CharField(max_length=500, blank=True, null=True)
    location_address = models.TextField(blank=True, null=True)

    # Genehmigung der veröffentlichten Niederschrift (Erweiterung ``mandari:protocolApproval``, Issue #525)
    protocol_approval_mode = models.CharField(
        "Genehmigungsweg der Niederschrift",
        max_length=20,
        blank=True,
        null=True,
        choices=list(APPROVAL_MODE_LABELS.items()),
    )
    protocol_approved_on = models.DateField("Niederschrift genehmigt am", blank=True, null=True)
    protocol_approved_in_external_id = models.TextField(
        "Genehmigt in der Sitzung", blank=True, null=True, help_text="OParl-Kennung (URL) der genehmigenden Sitzung."
    )

    # Gremien, die an dieser Sitzung beteiligt sind
    organizations = models.ManyToManyField(OParlOrganization, related_name="meetings", blank=True)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_meetings"
        verbose_name = "Sitzung"
        verbose_name_plural = "Sitzungen"
        ordering = ["-start"]
        indexes = [
            # Sitzungslisten und "nächste Sitzungen" filtern je Kommune und
            # sortieren nach Beginn — ohne diesen Index sortiert Postgres die
            # ganze Tabelle.
            models.Index(fields=["body", "start"], name="oparl_meeting_body_start"),
        ]

    def __str__(self):
        return self.get_display_name()

    def get_display_name(self) -> str:
        """Gibt den Gremiennamen zurück statt des generischen 'Sitzung'."""
        # 1. M2M-Beziehung (Django-Signals, manuell gepflegt)
        orgs = self.organizations.all()[:2]
        if orgs:
            org_names = [org.name for org in orgs if org.name]
            if org_names:
                return ", ".join(org_names)

        # 2. Auflösung über raw_json.organization URLs → DB-Lookup
        org_urls = self.raw_json.get("organization", []) if self.raw_json else []
        if org_urls:
            from .models import OParlOrganization as OrgModel

            resolved = OrgModel.objects.filter(external_id__in=org_urls[:2]).values_list("name", flat=True)
            names = [n for n in resolved if n]
            if names:
                return ", ".join(names)

        # 3. Fallback
        if self.name and self.name.lower() != "sitzung":
            return self.name
        return "Sitzung"

    def get_organization_names(self):
        """Gibt die Namen der beteiligten Gremien zurück."""
        orgs = self.organizations.all()
        if orgs:
            return [org.short_name or org.name for org in orgs]
        # Fallback: raw_json
        org_urls = self.raw_json.get("organization", []) if self.raw_json else []
        if org_urls:
            from .models import OParlOrganization as OrgModel

            return list(OrgModel.objects.filter(external_id__in=org_urls).values_list("name", flat=True))
        return []


class OParlPaper(SourceDeletionModel):
    """Ein Vorgang/eine Vorlage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="papers")

    name = models.CharField(max_length=500, blank=True, null=True)
    reference = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    paper_type = models.CharField(max_length=100, blank=True, null=True)

    date = models.DateField(blank=True, null=True, db_index=True)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # KI-generierte Felder
    summary = models.TextField(blank=True, null=True)
    locations = models.JSONField(blank=True, null=True)

    # Offizielle OParl-Locations (Feld `paper.location`, vom Ingestor verknüpft)
    oparl_locations = models.ManyToManyField(
        "OParlLocation",
        related_name="papers",
        blank=True,
        db_table="oparl_papers_locations",
        verbose_name="OParl-Orte",
    )

    # Georeferenzierung
    GEOREF_STATUS_CHOICES = [
        ("pending", "Ausstehend"),
        ("processing", "In Bearbeitung"),
        ("completed", "Abgeschlossen"),
        ("ai_needed", "KI-Extraktion benötigt"),
        ("no_locations", "Keine Ortsbezüge"),
        ("failed", "Fehlgeschlagen"),
        ("skipped", "Übersprungen"),
    ]

    georef_status = models.CharField(
        max_length=20,
        choices=GEOREF_STATUS_CHOICES,
        default="pending",
        db_index=True,
        verbose_name="Georef-Status",
    )
    georef_method = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name="Georef-Methode",
        help_text="regex, ai, regex+ai, manual",
    )
    georef_error = models.TextField(
        blank=True,
        null=True,
        verbose_name="Georef-Fehler",
    )
    georef_extracted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Georef-Zeitpunkt",
    )

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_papers"
        verbose_name = "Vorgang"
        verbose_name_plural = "Vorgänge"
        ordering = ["-date", "-oparl_created"]
        indexes = [
            # Vorgangslisten und die Startseite lesen die neuesten Vorgänge einer
            # Kommune in der Standardsortierung. Der Index liefert sie direkt,
            # statt zehntausende Zeilen zu sortieren.
            models.Index(fields=["body", "-date", "-oparl_created"], name="oparl_paper_body_datum"),
        ]

    def __str__(self):
        if self.reference:
            return f"{self.reference}: {self.name or ''}"
        return self.name or f"Vorgang {self.id}"


class OParlAgendaItem(SourceDeletionModel):
    """Ein Tagesordnungspunkt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    meeting = models.ForeignKey(OParlMeeting, on_delete=models.CASCADE, related_name="agenda_items")

    number = models.CharField(max_length=50, blank=True, null=True)
    order = models.IntegerField(blank=True, null=True)
    name = models.TextField(blank=True, null=True)
    public = models.BooleanField(default=True)
    result = models.TextField(blank=True, null=True)
    resolution_text = models.TextField(blank=True, null=True)

    # Beschlussfassung (Erweiterungen des kanonischen Modells, Issue #525): Beschlussnummer, Abstimmung (eine je
    # TOP), Einzelstimmen nur bei namentlicher Abstimmung und der veröffentlichte Umsetzungsstand. Quelle sind die
    # Erweiterungen ``mandari:*`` des AgendaItem (``mandari_oparl.extensions``); nullable, damit Schreiber ohne
    # diese Spalten (älterer Ingestor) weiter einfügen können.
    resolution_number = models.CharField("Beschlussnummer", max_length=100, blank=True, null=True)
    vote_method = models.CharField(
        "Abstimmungsart", max_length=20, blank=True, null=True, choices=list(VOTING_METHOD_LABELS.items())
    )
    vote_result = models.CharField(
        "Ergebnis der Abstimmung", max_length=20, blank=True, null=True, choices=list(RESULT_LABELS.items())
    )
    votes_yes = models.PositiveIntegerField("Ja-Stimmen", blank=True, null=True)
    votes_no = models.PositiveIntegerField("Nein-Stimmen", blank=True, null=True)
    votes_abstain = models.PositiveIntegerField("Enthaltungen", blank=True, null=True)
    roll_call = models.JSONField(
        "Einzelstimmen", blank=True, null=True, help_text="Nur bei namentlicher Abstimmung: Liste aus name und vote."
    )
    implementation_status = models.CharField(
        "Umsetzungsstand", max_length=20, blank=True, null=True, choices=list(IMPLEMENTATION_LABELS.items())
    )
    implementation_deadline = models.DateField("Erledigungsfrist", blank=True, null=True)
    implementation_public_note = models.TextField("Öffentliche Statusmeldung zur Umsetzung", blank=True, null=True)
    implementation_modified = models.DateTimeField("Umsetzungsstand geändert am", blank=True, null=True)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_agenda_items"
        verbose_name = "Tagesordnungspunkt"
        verbose_name_plural = "Tagesordnungspunkte"
        ordering = ["order", "number"]

    def __str__(self):
        if self.number:
            return f"TOP {self.number}: {self.name or ''}"
        return self.name or f"TOP {self.id}"

    def get_papers(self):
        """Liefert alle Papers/Vorgänge, die mit diesem TOP verknüpft sind."""
        # Nutze prefetched Daten wenn vorhanden (von MeetingDetailView)
        if hasattr(self, "_prefetched_papers"):
            return self._prefetched_papers
        return OParlPaper.objects.filter(
            id__in=self.get_consultations().filter(paper__isnull=False).values("paper_id")
        ).distinct()

    def get_consultations(self):
        """Liefert die Consultations für diesen TOP – ohne die von mandari Session zurückgenommenen."""
        return (
            OParlConsultation.objects.filter(agenda_item_external_id=self.external_id)
            .exclude(withdrawn_q())
            .exclude(withdrawn_q("paper"))
            .select_related("paper")
        )


class OParlFileBlob(models.Model):
    """
    Inhalt einer Datei in der Ablage, abgelegt unter seinem SHA-256 (Issue #788, services/file_store.py).

    Gleiche Dateien (dieselbe Anlage an mehreren Vorgängen) liegen nur einmal in der Ablage; jede Datei
    (``OParlFile.blob``) zählt als Referenz. Fällt die letzte Referenz weg, wird der Inhalt verwaist
    markiert und vom Aufräumen gelöscht – lokal und im Objektspeicher. Die Originale bleiben unverändert.
    Ingestor und Django schreiben beide (gleiches Vorgehen: Zeile sperren, Datei ablegen, zählen).
    """

    sha256 = models.CharField(max_length=64, primary_key=True)
    size = models.BigIntegerField(verbose_name="Größe (Bytes)")
    ref_count = models.IntegerField(default=0, db_default=0, verbose_name="Referenzen")
    created_at = models.DateTimeField(default=timezone.now, db_default=Now(), verbose_name="Abgelegt am")
    orphaned_at = models.DateTimeField(blank=True, null=True, verbose_name="Ohne Referenz seit")
    remote_at = models.DateTimeField(blank=True, null=True, verbose_name="Im Objektspeicher seit")

    class Meta:
        db_table = "oparl_file_blobs"
        verbose_name = "Dateiinhalt"
        verbose_name_plural = "Dateiinhalte"

    def __str__(self) -> str:
        return f"{self.sha256[:12]} ({self.ref_count} Referenzen)"


class OParlFile(SourceDeletionModel):
    """Eine Datei/Anlage."""

    # Text extraction status choices
    EXTRACTION_STATUS_CHOICES = [
        ("pending", "Ausstehend"),
        ("processing", "In Bearbeitung"),
        ("completed", "Abgeschlossen"),
        ("failed", "Fehlgeschlagen"),
        ("ocr_needed", "KI-OCR benötigt"),
        ("skipped", "Übersprungen"),
    ]

    # Text extraction method choices
    EXTRACTION_METHOD_CHOICES = [
        ("pypdf", "pypdf (Text-PDF)"),
        ("mistral", "Mistral OCR (API)"),
        ("tesseract", "Tesseract OCR (lokal)"),
        ("none", "Keine Extraktion"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)

    # OParl relationships (must match ingestor schema)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="files", blank=True, null=True)
    paper = models.ForeignKey(OParlPaper, on_delete=models.CASCADE, related_name="files", blank=True, null=True)
    meeting = models.ForeignKey(OParlMeeting, on_delete=models.CASCADE, related_name="files", blank=True, null=True)

    name = models.CharField(max_length=500, blank=True, null=True)
    file_name = models.CharField(max_length=255, blank=True, null=True)
    mime_type = models.CharField(max_length=100, blank=True, null=True)
    size = models.BigIntegerField(blank=True, null=True)
    access_url = models.URLField(max_length=1000, blank=True, null=True)
    download_url = models.URLField(max_length=1000, blank=True, null=True)
    file_date = models.DateTimeField(blank=True, null=True)

    # Lokale Speicherung
    local_path = models.TextField(blank=True, null=True)
    text_content = models.TextField(blank=True, null=True)
    sha256_hash = models.CharField(max_length=64, blank=True, null=True)

    # Lokaler Dokument-Cache (Django-managed, siehe services/file_cache.py)
    LOCAL_STATUS_CHOICES = [
        ("none", "Nicht zwischengespeichert"),
        ("ok", "Lokal vorhanden"),
        ("missing", "Quelle liefert 404"),
        ("error", "Fehler beim Abruf"),
        ("too_large", "Zu groß für den Cache"),
    ]
    # db_default: Der Ingestor legt Zeilen per SQLAlchemy an, ohne diese Django-Spalten zu kennen;
    # ohne DB-Default würde jeder INSERT an NOT NULL scheitern (Schema-Contract, Issue #161).
    local_status = models.CharField(
        max_length=20,
        choices=LOCAL_STATUS_CHOICES,
        default="none",
        db_default="none",
        db_index=True,
        verbose_name="Lokale Kopie",
    )
    local_cached_at = models.DateTimeField(blank=True, null=True, verbose_name="Lokal gespeichert am")
    local_error = models.CharField(max_length=500, blank=True, default="", db_default="", verbose_name="Cache-Fehler")
    # Gemessene Größe unserer Kopie in Bytes (Issue #786). ``size`` ist die Angabe der Quelle aus OParl und
    # fehlt bei manchen Quellen; als Speichermaß taugt nur diese Spalte.
    local_size = models.BigIntegerField(blank=True, null=True, verbose_name="Größe der Kopie (Bytes)")

    # Löschabgleich mit der Quelle (Issue #787, services/file_reconcile.py). Gesperrt ist ein Dokument,
    # wenn es in der Quelle gelöscht ist (``deleted``) oder seine Download-Adresse 404/410 liefert.
    source_checked_at = models.DateTimeField(blank=True, null=True, verbose_name="Mit der Quelle abgeglichen am")
    source_missing_since = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="In der Quelle nicht mehr abrufbar seit",
        help_text="Download-Adresse liefert 404/410: gesperrt, Kopie und Text werden nach der Frist gelöscht.",
    )
    content_purged_at = models.DateTimeField(
        blank=True, null=True, verbose_name="Kopie und Text gelöscht am", help_text="Nach dem Löschabgleich."
    )
    # Inhalt in der Ablage nach SHA-256 (Issue #788); leer bei Kopien im bisherigen Layout je Kommune
    blob = models.ForeignKey(
        OParlFileBlob,
        on_delete=models.PROTECT,
        related_name="files",
        blank=True,
        null=True,
        verbose_name="Inhalt in der Ablage",
    )

    # Text extraction tracking
    text_extraction_status = models.CharField(
        max_length=20,
        choices=EXTRACTION_STATUS_CHOICES,
        default="pending",
        db_index=True,
        verbose_name="Extraktionsstatus",
        help_text="Status der Textextraktion",
    )
    text_extraction_method = models.CharField(
        max_length=20,
        choices=EXTRACTION_METHOD_CHOICES,
        blank=True,
        null=True,
        verbose_name="Extraktionsmethode",
        help_text="Methode die für die Textextraktion verwendet wurde",
    )
    text_extraction_error = models.TextField(
        blank=True,
        null=True,
        verbose_name="Extraktionsfehler",
        help_text="Fehlermeldung falls Extraktion fehlschlug",
    )
    text_extracted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Extrahiert am",
        help_text="Zeitpunkt der letzten Textextraktion",
    )
    page_count = models.PositiveIntegerField(
        blank=True, null=True, verbose_name="Seitenanzahl", help_text="Anzahl der Seiten (bei PDFs)"
    )
    # Abbrüche der Texterkennung (Issue #817), geschrieben vom OCR-Worker des Ingestors: begonnene, nie beendete
    # Bearbeitungen; nach TEXT_EXTRACTION_MAX_ATTEMPTS gilt die Datei als gescheitert („Speichergrenze“).
    # db_default: Ingestor-INSERTs und ältere Images kennen die Spalten nicht.
    text_extraction_attempts = models.IntegerField(
        default=0,
        db_default=0,
        verbose_name="Abgebrochene Versuche",
        help_text="Begonnene, nie beendete Bearbeitungen der Texterkennung (Worker beendet)",
    )
    text_extraction_started_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name="Bearbeitung seit",
        help_text="Beginn der laufenden Bearbeitung der Texterkennung",
    )

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_files"
        verbose_name = "Datei"
        verbose_name_plural = "Dateien"

    def __str__(self):
        return self.name or self.file_name or f"Datei {self.id}"

    @property
    def size_human(self):
        """Menschenlesbare Dateigröße."""
        return human_size(self.size) if self.size else ""


class OParlFileAccessDay(models.Model):
    """
    Zugriffsprotokoll der Dokumentablage je Tag (Issue #786), ohne Personenbezug.

    Ein Zähler je Tag, Kommune, Ergebnis und Altersklasse des Dokuments. Daraus ergeben sich die
    Trefferquote der lokalen Kopien, die Abrufe bei den Quellen und welche Dokumente überhaupt noch
    gelesen werden – Grundlage für die Größe eines Zwischenspeichers. Geschrieben von
    ``services/file_access.py``; für Dateien ohne Kommune gibt es Zeilen ohne ``body``.
    """

    OUTCOME_HIT = "hit"
    OUTCOME_MISS = "miss"
    OUTCOME_FAILED = "failed"
    OUTCOME_BLOCKED = "blocked"
    OUTCOME_CHOICES = [
        (OUTCOME_HIT, "Treffer (lokale Kopie)"),
        (OUTCOME_MISS, "Fehlzugriff (von der Quelle geholt)"),
        (OUTCOME_FAILED, "Nicht ausgeliefert (Quelle nicht erreichbar, zu groß, gedrosselt)"),
        (OUTCOME_BLOCKED, "Gesperrt"),
    ]
    AGE_CHOICES = [
        ("d30", "jünger als 30 Tage"),
        ("d365", "30 Tage bis 1 Jahr"),
        ("y3", "1 bis 3 Jahre"),
        ("older", "älter als 3 Jahre"),
        ("unknown", "unbekannt"),
    ]

    id = models.BigAutoField(primary_key=True)
    day = models.DateField(verbose_name="Tag")
    body = models.ForeignKey(
        OParlBody, on_delete=models.CASCADE, related_name="file_access_days", blank=True, null=True
    )
    outcome = models.CharField(max_length=16, choices=OUTCOME_CHOICES, verbose_name="Ergebnis")
    age_class = models.CharField(max_length=16, choices=AGE_CHOICES, verbose_name="Alter des Dokuments")
    count = models.PositiveIntegerField(default=0, verbose_name="Abrufe")
    bytes = models.BigIntegerField(default=0, verbose_name="Bytes (Dateigröße je Abruf)")

    class Meta:
        db_table = "oparl_file_access_days"
        verbose_name = "Dokumentabrufe je Tag"
        verbose_name_plural = "Dokumentabrufe je Tag"
        constraints = [
            models.UniqueConstraint(fields=["day", "body", "outcome", "age_class"], name="oparl_file_access_day_key"),
        ]

    def __str__(self) -> str:
        return f"{self.day} {self.outcome} {self.age_class}: {self.count}"


class OParlMembership(SourceDeletionModel):
    """Eine Mitgliedschaft (Person in Gremium)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)

    person = models.ForeignKey(OParlPerson, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(OParlOrganization, on_delete=models.CASCADE, related_name="memberships")

    role = models.CharField(max_length=255, blank=True, null=True)
    voting_right = models.BooleanField(default=True)
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_memberships"
        verbose_name = "Mitgliedschaft"
        verbose_name_plural = "Mitgliedschaften"
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.person} in {self.organization}"

    @property
    def is_active(self):
        """Prüft ob die Mitgliedschaft noch aktiv ist."""
        from datetime import datetime

        from django.utils import timezone

        if self.end_date is None:
            return True
        end = self.end_date.date() if isinstance(self.end_date, datetime) else self.end_date
        return end >= timezone.now().date()


class OParlLocation(SourceDeletionModel):
    """Ein Ort/Standort."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="locations", blank=True, null=True)

    description = models.TextField(blank=True, null=True)
    street_address = models.CharField(max_length=500, blank=True, null=True)
    room = models.CharField(max_length=255, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    locality = models.CharField(max_length=255, blank=True, null=True)
    geojson = models.JSONField(blank=True, null=True)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_locations"
        verbose_name = "Ort"
        verbose_name_plural = "Orte"

    def __str__(self):
        if self.room:
            return f"{self.room}, {self.street_address or ''}"
        return self.description or self.street_address or f"Ort {self.id}"


class OParlConsultation(SourceDeletionModel):
    """Eine Beratung (Verknüpfung Paper-Meeting-AgendaItem)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="consultations", blank=True, null=True)
    paper = models.ForeignKey(OParlPaper, on_delete=models.CASCADE, related_name="consultations", blank=True, null=True)

    paper_external_id = models.TextField(blank=True, null=True)
    meeting_external_id = models.TextField(blank=True, null=True)
    agenda_item_external_id = models.TextField(blank=True, null=True)
    role = models.CharField(max_length=255, blank=True, null=True)
    authoritative = models.BooleanField(default=False)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_consultations"
        verbose_name = "Beratung"
        verbose_name_plural = "Beratungen"
        # Abhängigkeiten des Suchindex-Abonnements: Beratungen zu einer Sitzung bzw. einem Punkt (#821)
        indexes = [
            models.Index(fields=["meeting_external_id"], name="oparl_cons_meeting_ext"),
            models.Index(fields=["agenda_item_external_id"], name="oparl_cons_agenda_ext"),
        ]

    def __str__(self):
        return f"Beratung {self.role or ''} - {self.paper or self.external_id}"


class OParlLegislativeTerm(SourceDeletionModel):
    """Eine Wahlperiode."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody, on_delete=models.CASCADE, related_name="legislative_terms", blank=True, null=True
    )

    name = models.CharField(max_length=255, blank=True, null=True)
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)

    # OParl-Zeitstempel
    oparl_created = models.DateTimeField(blank=True, null=True)
    oparl_modified = models.DateTimeField(blank=True, null=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_legislative_terms"
        verbose_name = "Wahlperiode"
        verbose_name_plural = "Wahlperioden"
        ordering = ["-start_date"]

    def __str__(self):
        return self.name or f"Wahlperiode {self.id}"

    @property
    def is_current(self):
        """Prüft ob dies die aktuelle Wahlperiode ist."""
        from datetime import datetime

        from django.utils import timezone

        today = timezone.now().date()
        start = self.start_date
        end = self.end_date
        if start and isinstance(start, datetime):
            start = start.date()
        if end and isinstance(end, datetime):
            end = end.date()
        if start and end:
            return start <= today <= end
        if start and not end:
            return start <= today
        return False


# =============================================================================
# Location Mapping (für Koordinaten-Zuordnung)
# =============================================================================


class LocationMapping(models.Model):
    """Mapping von Ortsnamen zu Koordinaten, pro Kommune."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="location_mappings",
        verbose_name="Kommune",
    )

    # Der Name des Ortes (z.B. "Hauptausschusszimmer", "Rathaus Festsaal")
    location_name = models.CharField(
        max_length=500,
        verbose_name="Ortsbezeichnung",
        help_text="Der Name wie er in Sitzungen verwendet wird (z.B. 'Hauptausschusszimmer')",
    )

    # Koordinaten
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, verbose_name="Breitengrad", help_text="z.B. 51.9617867"
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, verbose_name="Längengrad", help_text="z.B. 7.6281645"
    )

    # Optionale Zusatzinfos
    address = models.TextField(
        blank=True, null=True, verbose_name="Adresse", help_text="Vollständige Adresse (optional)"
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name="Beschreibung",
        help_text="Zusätzliche Informationen zum Ort",
    )

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "location_mappings"
        verbose_name = "Orts-Zuordnung"
        verbose_name_plural = "Orts-Zuordnungen"
        ordering = ["body", "location_name"]
        unique_together = [["body", "location_name"]]

    def __str__(self):
        return f"{self.location_name} ({self.body.name})"

    @classmethod
    def get_coordinates_for_location(cls, body, location_name):
        """Sucht Koordinaten für einen Ortsnamen in einer Kommune.

        Versucht verschiedene Matching-Strategien:
        1. Exakter Match
        2. Case-insensitive Match
        3. Partial Match (location_name ist Teil des Suchstrings oder umgekehrt)
        """
        if not location_name or not body:
            return None

        # 1. Exakter Match
        mapping = cls.objects.filter(body=body, location_name=location_name).first()
        if mapping:
            return {"lat": float(mapping.latitude), "lng": float(mapping.longitude)}

        # 2. Case-insensitive Match
        mapping = cls.objects.filter(body=body, location_name__iexact=location_name).first()
        if mapping:
            return {"lat": float(mapping.latitude), "lng": float(mapping.longitude)}

        # 3. Partial Match - der gespeicherte Name ist Teil des Suchstrings
        for m in cls.objects.filter(body=body):
            if m.location_name.lower() in location_name.lower():
                return {"lat": float(m.latitude), "lng": float(m.longitude)}
            if location_name.lower() in m.location_name.lower():
                return {"lat": float(m.latitude), "lng": float(m.longitude)}

        return None


class Street(models.Model):
    """Straßenverzeichnis (Gazetteer) pro Kommune, importiert aus OpenStreetMap.

    Dient als Wahrheitsquelle für die Georeferenzierung: Nur Kandidaten,
    die im Straßenverzeichnis der Kommune vorkommen, werden als Ortsbezug
    gewertet. Die Geometrie stammt direkt aus OSM (kein API-Geocoding nötig).
    """

    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="streets",
        verbose_name="Kommune",
    )
    osm_id = models.BigIntegerField(help_text="OpenStreetMap Way-ID")
    name = models.CharField(max_length=255, verbose_name="Straßenname")
    # Kanonisch normalisierter Name (lowercase, ß→ss, str.→strasse, Bindestriche→Leerzeichen)
    normalized_name = models.CharField(max_length=255, db_index=True)

    # Zentroid des Ways (aus Overpass `out center`)
    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)

    # Optionale GeoJSON-Geometrie (LineString), falls beim Import angefordert
    geometry = models.JSONField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insight_streets"
        verbose_name = "Straße"
        verbose_name_plural = "Straßen"
        constraints = [
            models.UniqueConstraint(fields=["body", "osm_id"], name="uniq_street_body_osm"),
        ]
        indexes = [
            models.Index(fields=["body", "normalized_name"], name="idx_street_body_norm"),
        ]

    def __str__(self):
        return f"{self.name} ({self.body.get_display_name()})"


class Address(models.Model):
    """Hausnummern-Punkte pro Kommune, importiert aus OpenStreetMap (addr:*-Tags).

    Ergänzt das Straßenverzeichnis um Adresspunkte: Erkennt die Georeferenzierung
    im Text „Straße Hausnummer“ und liegt die Adresse hier vor, wird ihr Punkt
    statt des Straßen-Zentroids verwendet (Issue #54).
    """

    OSM_TYPE_CHOICES = [
        ("node", "Node"),
        ("way", "Way"),
        ("relation", "Relation"),
    ]

    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="addresses",
        verbose_name="Kommune",
    )
    osm_type = models.CharField(max_length=8, choices=OSM_TYPE_CHOICES, default="node")
    osm_id = models.BigIntegerField(help_text="OpenStreetMap-ID des Objekts mit addr:*-Tags")
    street = models.CharField(max_length=255, verbose_name="Straße")
    normalized_street = models.CharField(max_length=255, db_index=True)
    house_number = models.CharField(max_length=20, verbose_name="Hausnummer")
    # Kleinschreibung ohne Leerzeichen ("12 a" → "12a"), für den Abgleich mit Textfunden
    normalized_house_number = models.CharField(max_length=20)
    postal_code = models.CharField(max_length=20, blank=True, default="")

    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insight_addresses"
        verbose_name = "Adresse"
        verbose_name_plural = "Adressen"
        constraints = [
            models.UniqueConstraint(fields=["body", "osm_type", "osm_id"], name="uniq_address_body_osm"),
        ]
        indexes = [
            models.Index(
                fields=["body", "normalized_street", "normalized_house_number"],
                name="idx_address_body_street_hn",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.street} {self.house_number} ({self.body.get_display_name()})"


class OParlBodyGeoSuggestion(models.Model):
    """Vorschlag für die OSM-Grenze einer Kommune, wenn die automatische Suche nicht eindeutig war.

    ``resolve_body_geodata`` rät nicht: Findet die Namenssuche mehrere oder nur ungefähre Treffer,
    legt sie je Kandidat einen Vorschlag an. Im Admin übernimmt „Vorschlag übernehmen“ Relation und
    Schlüssel an die Kommune und verwirft die übrigen Vorschläge (Issue #351).
    """

    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="geo_suggestions",
        verbose_name="Kommune",
    )
    osm_relation_id = models.BigIntegerField(verbose_name="OSM-Relation")
    name = models.CharField(max_length=255, verbose_name="Name in OSM")
    admin_level = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        verbose_name="Verwaltungsebene",
        help_text="OSM admin_level: 6 Kreis/kreisfreie Stadt, 7 Verbandsgemeinde/Amt, 8 Gemeinde, 9–10 Ortsteil",
    )
    ags = models.CharField(max_length=8, blank=True, default="", verbose_name="AGS")
    rgs = models.CharField(max_length=12, blank=True, default="", verbose_name="Regionalschlüssel")
    reason = models.CharField(max_length=255, blank=True, default="", verbose_name="Herkunft")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "insight_body_geo_suggestions"
        verbose_name = "Geo-Vorschlag"
        verbose_name_plural = "Geo-Vorschläge"
        ordering = ["body__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["body", "osm_relation_id"], name="uniq_geo_suggestion_body_rel"),
        ]

    def __str__(self) -> str:
        return f"{self.name} (Relation {self.osm_relation_id}) für {self.body.get_display_name()}"


class Municipality(models.Model):
    """Eintrag im Kommunenverzeichnis für den Kommunenwechsel im Bürgerportal (Issue #783, Stufe 2).

    Enthält alle Gemeinden, Gemeindeverbände und kreisfreien Städte, nicht nur die mit Daten. Wählbar ist
    ein Eintrag, wenn eine gelistete Kommune denselben Regionalschlüssel oder AGS trägt; die Zuordnung
    entsteht bei jeder Abfrage neu (``services/kommunenverzeichnis.py``). Befüllt per
    ``manage.py kommunenverzeichnis_importieren`` (docs/INSIGHT_KOMMUNENWECHSEL.md).
    """

    key = models.CharField(
        max_length=12,
        unique=True,
        verbose_name="Schlüssel",
        help_text="Regionalschlüssel (12 Stellen) oder, wenn nicht bekannt, Amtlicher Gemeindeschlüssel (8 Stellen)",
    )
    ags = models.CharField(max_length=8, blank=True, default="", db_index=True, verbose_name="AGS")
    name = models.CharField(max_length=200, verbose_name="Name")
    kind = models.CharField(
        max_length=60, blank=True, default="", verbose_name="Art", help_text="z. B. Stadt, Gemeinde, Samtgemeinde"
    )
    is_association = models.BooleanField(
        default=False,
        verbose_name="Gemeindeverband",
        help_text="Samtgemeinde, Verbandsgemeinde, Amt o. Ä.: im Stöbern eine Stufe zwischen Kreis und Gemeinde",
    )
    district_key = models.CharField(max_length=5, db_index=True, verbose_name="Kreisschlüssel")
    district = models.CharField(max_length=200, blank=True, default="", verbose_name="Kreis")
    state_key = models.CharField(max_length=2, db_index=True, verbose_name="Land")
    latitude = models.FloatField(blank=True, null=True, db_index=True, verbose_name="Breite")
    longitude = models.FloatField(blank=True, null=True, verbose_name="Länge")
    imported = models.BooleanField(
        default=False,
        verbose_name="Aus Datei importiert",
        help_text="Aus der CSV-Datei (Quellen mit Namensnennung); nicht gesetzt bei Einträgen aus den gelisteten Kommunen",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insight_municipality"
        verbose_name = "Kommune im Verzeichnis"
        verbose_name_plural = "Kommunenverzeichnis"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"


class MunicipalityTerm(models.Model):
    """Suchbegriff eines Verzeichniseintrags: Name, Ortsteil oder Postleitzahl.

    ``normalized`` ist klein geschrieben, ohne Satzzeichen, Umlaute als ``ae`` usw. Für Umlaute ohne Punkte
    („Ubungsheim“ für „Übungsheim“) steht eine zweite Zeile mit ``a``, ``o``, ``u``. In PostgreSQL trägt die Spalte einen
    Trigramm-Index (``pg_trgm``) für die unscharfe Suche.
    """

    class Kind(models.TextChoices):
        NAME = "name", "Name"
        DISTRICT_PART = "ortsteil", "Ortsteil"
        POSTCODE = "plz", "Postleitzahl"

    municipality = models.ForeignKey(Municipality, on_delete=models.CASCADE, related_name="terms")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.NAME)
    label = models.CharField(max_length=200, verbose_name="Anzeige")
    normalized = models.CharField(max_length=200, db_index=True)

    class Meta:
        db_table = "insight_municipality_term"
        verbose_name = "Suchbegriff im Kommunenverzeichnis"
        verbose_name_plural = "Suchbegriffe im Kommunenverzeichnis"
        constraints = [
            models.UniqueConstraint(fields=["municipality", "kind", "normalized"], name="uniq_municipality_term"),
        ]

    def __str__(self) -> str:
        return f"{self.label} → {self.municipality_id}"


class PaperLocation(models.Model):
    """Eine Verortung eines Vorgangs als eigene, indexierbare Zeile.

    Spiegelt die Einträge aus ``OParlPaper.locations`` (JSON, Anzeigeformat für
    Karte und Detailseite) in eine Tabelle mit Index auf (Kommune, Breite, Länge).
    Die Umkreissuche filtert damit per Bounding-Box statt per JSONB-Vollscan.
    Zusätzlich trägt jede Zeile Herkunft und Prüfstatus für den Korrektur-Workflow
    im Admin: Entfernte Verortungen legt der automatische Lauf nicht wieder an.
    """

    SOURCE_CHOICES = [
        ("oparl", "OParl-Ort"),
        ("address_match", "Adresse (Straßenverzeichnis)"),
        ("street_match", "Straße (Straßenverzeichnis)"),
        ("ai", "KI-Extraktion"),
        ("manual", "Manuell"),
        ("plan_boundary", "Amtlicher Umring (Bebauungsplan)"),
    ]
    STATUS_AUTO = "auto"
    STATUS_CONFIRMED = "confirmed"
    STATUS_REMOVED = "removed"
    STATUS_CHOICES = [
        (STATUS_AUTO, "Automatisch"),
        (STATUS_CONFIRMED, "Bestätigt"),
        (STATUS_REMOVED, "Entfernt"),
    ]

    paper = models.ForeignKey(
        OParlPaper,
        on_delete=models.CASCADE,
        related_name="paper_locations",
        verbose_name="Vorgang",
    )
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="paper_locations",
        verbose_name="Kommune",
    )
    name = models.CharField(max_length=500, blank=True, default="", verbose_name="Ortsbezeichnung")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="street_match", verbose_name="Herkunft")
    confidence = models.FloatField(blank=True, null=True, verbose_name="Konfidenz")
    latitude = models.FloatField(verbose_name="Breite")
    longitude = models.FloatField(verbose_name="Länge")
    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_AUTO,
        db_index=True,
        verbose_name="Prüfstatus",
    )
    reviewed_at = models.DateTimeField(blank=True, null=True, verbose_name="Geprüft am")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insight_paper_locations"
        verbose_name = "Verortung"
        verbose_name_plural = "Verortungen"
        constraints = [
            models.UniqueConstraint(
                fields=["paper", "latitude", "longitude"],
                name="uniq_paperloc_paper_point",
            ),
        ]
        indexes = [
            # Bounding-Box-Vorfilter der Umkreissuche: Kommune, dann Breite/Länge
            models.Index(fields=["body", "latitude", "longitude"], name="idx_paperloc_body_lat_lon"),
        ]

    def __str__(self) -> str:
        return f"{self.name or 'Verortung'} ({self.get_source_display()})"


# =============================================================================
# Amtliche Umringe von Bebauungsplänen (Issue #598)
# =============================================================================


class PlanBoundarySource(models.Model):
    """Geodienst, der die amtlichen Umringe (Geltungsbereiche) der Bebauungspläne einer Kommune liefert.

    Umringe werden verlinkt statt aus PDFs rekonstruiert: Viele Kommunen und das Land NRW veröffentlichen
    sie als WFS bzw. OGC API – Features. ``sync_plan_boundaries`` lädt die Umringe einmal täglich in
    ``PlanBoundary`` (kein Abruf je Seitenaufruf) und ordnet sie den Vorlagen zu, deren Titel die
    Plannummer nennt. Mehrere Quellen je Kommune sind möglich, etwa „rechtskräftig“ und „im Verfahren“.
    """

    KIND_OGC_API = "ogc_api_features"
    KIND_WFS = "wfs"
    KIND_CHOICES = [
        (KIND_OGC_API, "OGC API – Features (z. B. Land NRW)"),
        (KIND_WFS, "WFS 2.0 mit GeoJSON-Ausgabe"),
    ]
    PLAN_STATUS_IN_FORCE = "in_force"
    PLAN_STATUS_IN_PROCEDURE = "in_procedure"
    PLAN_STATUS_CHOICES = [
        (PLAN_STATUS_IN_FORCE, "Rechtskräftig"),
        (PLAN_STATUS_IN_PROCEDURE, "Im Verfahren"),
    ]

    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="plan_boundary_sources",
        verbose_name="Kommune",
    )
    name = models.CharField(
        max_length=200,
        verbose_name="Bezeichnung",
        help_text="z. B. „Land NRW – rechtskräftige Bebauungspläne“",
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_OGC_API, verbose_name="Art des Dienstes")
    url = models.URLField(
        max_length=1000,
        verbose_name="Adresse",
        help_text=(
            "OGC API: Adresse der Objektliste einer Sammlung (…/collections/<id>/items). "
            "WFS: Adresse des Dienstes ohne Parameter."
        ),
    )
    layer = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="Ebene (nur WFS)",
        help_text="Wert für TYPENAMES, z. B. ms:bplan2",
    )
    query_params = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Zusätzliche Abfrageparameter",
        help_text='z. B. {"gkz": "05515000"} (NRW) oder {"OUTPUTFORMAT": "GEOJSON"} (MapServer-WFS)',
    )
    property_filter = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Filter auf Eigenschaften",
        help_text='Nur Objekte mit einem dieser Werte übernehmen, z. B. {"planTypeName.code": [1000]}',
    )
    number_property = models.CharField(
        max_length=100,
        default="plannr",
        verbose_name="Eigenschaft: Plannummer",
        help_text="Land NRW: nr (mit Änderungsnummer), MapServer-WFS der Stadt Münster: plannr",
    )
    title_property = models.CharField(
        max_length=100, blank=True, default="name", verbose_name="Eigenschaft: Bezeichnung des Plans"
    )
    link_property = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="Eigenschaft: Link zur Planseite",
        help_text="Land NRW: officialDocument, Stadt Münster: scanurl",
    )
    plan_status = models.CharField(
        max_length=20, choices=PLAN_STATUS_CHOICES, default=PLAN_STATUS_IN_FORCE, verbose_name="Planstand"
    )
    attribution = models.CharField(
        max_length=300,
        verbose_name="Quellenangabe",
        help_text="Steht im Portal an Karte und Umring, z. B. „Land NRW, Datenlizenz Deutschland – Namensnennung – 2.0“",
    )
    license_url = models.URLField(max_length=500, blank=True, default="", verbose_name="Lizenz (Link)")
    priority = models.PositiveSmallIntegerField(
        default=100,
        verbose_name="Rang",
        help_text="Liefern mehrere Quellen denselben Plan, gewinnt die kleinere Zahl.",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    # Betriebszustand, gesetzt von sync_plan_boundaries
    last_attempt_at = models.DateTimeField(blank=True, null=True, verbose_name="Letzter Abruf")
    last_success_at = models.DateTimeField(blank=True, null=True, verbose_name="Letzter erfolgreicher Abruf")
    last_error = models.CharField(max_length=300, blank=True, default="", verbose_name="Letzter Fehler")
    feature_count = models.PositiveIntegerField(default=0, verbose_name="Umringe")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insight_plan_boundary_sources"
        verbose_name = "Umring-Quelle (Bebauungspläne)"
        verbose_name_plural = "Umring-Quellen (Bebauungspläne)"
        ordering = ["body__name", "priority", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.body.get_display_name()})"


class PlanBoundary(models.Model):
    """Zwischengespeicherter amtlicher Umring eines Bebauungsplans (ein Objekt des Geodienstes).

    Die Geometrie liegt als GeoJSON in WGS84 (Länge, Breite) vor. Bounding-Box und ein Punkt im Umring
    stehen in eigenen Spalten: Die Umkreissuche filtert über die Box vor und prüft danach den Abstand
    zum Umring; der Punkt dient als Verortung des Vorgangs (Karte, Abos).
    """

    source = models.ForeignKey(
        PlanBoundarySource,
        on_delete=models.CASCADE,
        related_name="boundaries",
        verbose_name="Quelle",
    )
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="plan_boundaries",
        verbose_name="Kommune",
    )
    feature_key = models.CharField(max_length=300, verbose_name="Kennung in der Quelle")
    plan_number = models.CharField(max_length=100, verbose_name="Plannummer (Quelle)")
    number_key = models.CharField(max_length=100, verbose_name="Plannummer (normalisiert)")
    change_number = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name="Änderung",
        help_text="Nummer der Änderung, sofern die Quelle Änderungen getrennt führt",
    )
    title = models.CharField(max_length=500, blank=True, default="", verbose_name="Bezeichnung")
    plan_status = models.CharField(
        max_length=20,
        choices=PlanBoundarySource.PLAN_STATUS_CHOICES,
        default=PlanBoundarySource.PLAN_STATUS_IN_FORCE,
        verbose_name="Planstand",
    )
    document_url = models.URLField(max_length=1000, blank=True, default="", verbose_name="Planseite")
    geometry = models.JSONField(verbose_name="Umring (GeoJSON, WGS84)")
    bbox_south = models.FloatField()
    bbox_north = models.FloatField()
    bbox_west = models.FloatField()
    bbox_east = models.FloatField()
    point_lat = models.FloatField(verbose_name="Punkt im Umring (Breite)")
    point_lon = models.FloatField(verbose_name="Punkt im Umring (Länge)")
    area_m2 = models.FloatField(blank=True, null=True, verbose_name="Fläche (m²)")
    fetched_at = models.DateTimeField(verbose_name="Abgerufen am")

    class Meta:
        db_table = "insight_plan_boundaries"
        verbose_name = "Amtlicher Umring"
        verbose_name_plural = "Amtliche Umringe"
        ordering = ["body__name", "number_key", "change_number"]
        constraints = [
            models.UniqueConstraint(fields=["source", "feature_key"], name="uniq_planboundary_source_key"),
        ]
        indexes = [
            models.Index(fields=["body", "number_key"], name="idx_planboundary_body_number"),
            # Vorfilter der Umkreissuche (Box des Umrings schneidet die Box des Suchkreises)
            models.Index(fields=["body", "bbox_south", "bbox_north"], name="idx_planboundary_body_bbox"),
        ]

    def __str__(self) -> str:
        suffix = f", {self.change_number}. Änderung" if self.change_number else ""
        return f"Bebauungsplan Nr. {self.plan_number}{suffix}"


class PaperPlanReference(models.Model):
    """Bebauungsplan, den der Titel eines Vorgangs nennt, mit den zugeordneten amtlichen Umringen.

    Eine Zeile ohne Umring (``match = none``) ist eine protokollierte Lücke: Die Plannummer steht im
    Titel, die Quellen der Kommune kennen sie aber nicht.
    """

    MATCH_CHANGE = "change"
    MATCH_PROCEDURE = "procedure"
    MATCH_PLAN = "plan"
    MATCH_BASE_PLAN = "base_plan"
    MATCH_NONE = "none"
    MATCH_CHOICES = [
        (MATCH_CHANGE, "Umring der genannten Änderung"),
        (MATCH_PROCEDURE, "Umring des laufenden Verfahrens"),
        (MATCH_PLAN, "Umring des Plans"),
        (MATCH_BASE_PLAN, "Umring des Ursprungsplans"),
        (MATCH_NONE, "Kein Umring gefunden"),
    ]

    paper = models.ForeignKey(
        OParlPaper,
        on_delete=models.CASCADE,
        related_name="plan_references",
        verbose_name="Vorgang",
    )
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="plan_references",
        verbose_name="Kommune",
    )
    number_key = models.CharField(max_length=100, verbose_name="Plannummer (normalisiert)")
    number_label = models.CharField(max_length=100, verbose_name="Plannummer (Titel)")
    change_number = models.CharField(max_length=20, blank=True, default="", verbose_name="Änderung")
    match = models.CharField(
        max_length=12, choices=MATCH_CHOICES, default=MATCH_NONE, db_index=True, verbose_name="Zuordnung"
    )
    boundaries = models.ManyToManyField(
        PlanBoundary,
        blank=True,
        related_name="paper_references",
        db_table="insight_paper_plan_reference_boundaries",
        verbose_name="Umringe",
    )
    matched_at = models.DateTimeField(verbose_name="Zuordnung vom")

    class Meta:
        db_table = "insight_paper_plan_references"
        verbose_name = "Bebauungsplan eines Vorgangs"
        verbose_name_plural = "Bebauungspläne der Vorgänge"
        ordering = ["-matched_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["paper", "number_key", "change_number"], name="uniq_paperplanref_paper_number"
            ),
        ]
        indexes = [
            models.Index(fields=["body", "number_key"], name="idx_paperplanref_body_number"),
        ]

    def __str__(self) -> str:
        suffix = f", {self.change_number}. Änderung" if self.change_number else ""
        return f"Bebauungsplan Nr. {self.number_label}{suffix}"


# =============================================================================
# Tile Cache für performante Karten
# =============================================================================


# Standard-Zoomstufen für die Kachelberechnung (unveränderlich, daher als Modulkonstante)
DEFAULT_TILE_ZOOM_LEVELS = range(10, 17)


class TileCache(models.Model):
    """
    Cache für Map-Tiles.

    Tiles werden lokal gespeichert für maximale Performance.
    Ein wöchentlicher Cronjob aktualisiert die Tiles für alle Kommunen.
    """

    # Tile-Koordinaten
    z = models.PositiveIntegerField(db_index=True)  # Zoom Level
    x = models.PositiveIntegerField(db_index=True)  # X-Koordinate
    y = models.PositiveIntegerField(db_index=True)  # Y-Koordinate

    # Tile-Daten
    tile_data = models.BinaryField()  # PNG Binärdaten
    content_type = models.CharField(max_length=50, default="image/png")

    # Metadaten
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    fetched_from = models.CharField(max_length=255, default="openstreetmap")

    class Meta:
        db_table = "tile_cache"
        verbose_name = "Tile Cache"
        verbose_name_plural = "Tile Cache"
        unique_together = [["z", "x", "y"]]
        indexes = [
            models.Index(fields=["z", "x", "y"]),
        ]

    def __str__(self):
        return f"Tile {self.z}/{self.x}/{self.y}"

    @classmethod
    def get_tile(cls, z, x, y):
        """Holt ein Tile aus dem Cache oder None."""
        try:
            tile = cls.objects.get(z=z, x=x, y=y)
            return tile.tile_data, tile.content_type
        except cls.DoesNotExist:
            return None, None

    @classmethod
    def store_tile(cls, z, x, y, tile_data, content_type="image/png", source="openstreetmap"):
        """Speichert ein Tile im Cache."""
        tile, created = cls.objects.update_or_create(
            z=z,
            x=x,
            y=y,
            defaults={
                "tile_data": tile_data,
                "content_type": content_type,
                "fetched_from": source,
            },
        )
        return tile

    @classmethod
    def tiles_for_bbox(cls, bbox_north, bbox_south, bbox_east, bbox_west, zoom_levels=DEFAULT_TILE_ZOOM_LEVELS):
        """
        Berechnet alle Tile-Koordinaten für eine Bounding Box.

        Gibt eine Liste von (z, x, y) Tupeln zurück.
        """
        import math

        def lat_lon_to_tile(lat, lon, zoom):
            lat_rad = math.radians(lat)
            n = 2.0**zoom
            x = int((lon + 180.0) / 360.0 * n)
            y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
            return x, y

        tiles = []
        for z in zoom_levels:
            x_min, y_max = lat_lon_to_tile(bbox_south, bbox_west, z)
            x_max, y_min = lat_lon_to_tile(bbox_north, bbox_east, z)

            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    tiles.append((z, x, y))

        return tiles


# =============================================================================
# Contact Requests (Public Contact Form)
# =============================================================================


class ContactRequest(models.Model):
    """
    A contact request from the public contact form.

    Stores inquiries from non-authenticated users and can be linked to
    support tickets when processed by administrators.
    """

    SUBJECT_CHOICES = [
        ("demo", "Demo anfragen"),
        ("preise", "Preisanfrage"),
        ("support", "Support"),
        ("datenschutz", "Datenschutz"),
        ("sonstiges", "Sonstiges"),
    ]

    STATUS_CHOICES = [
        ("new", "Neu"),
        ("read", "Gelesen"),
        ("in_progress", "In Bearbeitung"),
        ("replied", "Beantwortet"),
        ("converted", "Zu Ticket konvertiert"),
        ("closed", "Geschlossen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Contact information
    name = models.CharField(max_length=255, verbose_name="Name")
    email = models.EmailField(verbose_name="E-Mail")
    organization_name = models.CharField(max_length=255, blank=True, verbose_name="Organisation")

    # Request details
    subject = models.CharField(max_length=50, choices=SUBJECT_CHOICES, default="sonstiges", verbose_name="Betreff")
    message = models.TextField(verbose_name="Nachricht")

    # Status tracking
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new", verbose_name="Status")

    # Link to support ticket (if converted)
    linked_ticket = models.ForeignKey(
        "work.SupportTicket",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_requests",
        verbose_name="Verknüpftes Ticket",
    )

    # Admin notes (internal)
    admin_notes = models.TextField(blank=True, verbose_name="Interne Notizen")

    # Metadata
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name="IP-Adresse")
    user_agent = models.TextField(blank=True, verbose_name="User Agent")

    # Email tracking
    notification_sent = models.BooleanField(default=False, verbose_name="Benachrichtigung gesendet")
    confirmation_sent = models.BooleanField(default=False, verbose_name="Bestätigung gesendet")

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Erstellt am")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Aktualisiert am")

    class Meta:
        db_table = "contact_requests"
        verbose_name = "Kontaktanfrage"
        verbose_name_plural = "Kontaktanfragen"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.get_subject_display()} ({self.created_at.strftime('%d.%m.%Y')})"


# =============================================================================
# Public Questions (Ratsfragen - Abgeordnetenwatch-Stil)
# =============================================================================


class PublicQuestion(HashedTokenMixin, models.Model):
    """
    Öffentliche Frage an ein Ratsmitglied.

    Workflow: Formular → E-Mail-Verifizierung → Moderation → Veröffentlichung
    → Ratsmitglied antwortet über Token-Link → Antwort-Moderation → Öffentlich

    Vom Bestätigungslink steht nur der SHA-256-Hash in der Datenbank (apps/common/tokens.py). Der
    Antwortlink bleibt im Klartext, weil die Erinnerungsmail ihn erneut verschickt.
    """

    token_field = "verification_token"

    STATUS_CHOICES = [
        ("unverified", "E-Mail nicht bestätigt"),
        ("pending", "Wartet auf Moderation"),
        ("published", "Veröffentlicht"),
        ("rejected", "Abgelehnt"),
    ]

    ANSWER_STATUS_CHOICES = [
        ("none", "Keine Antwort"),
        ("pending", "Antwort wartet auf Moderation"),
        ("published", "Antwort veröffentlicht"),
    ]

    TOPIC_CHOICES = [
        ("verkehr", "Verkehr & Mobilität"),
        ("bauen", "Bauen, Wohnen & Stadtentwicklung"),
        ("umwelt", "Umwelt & Klima"),
        ("bildung", "Bildung, Kinder & Jugend"),
        ("soziales", "Soziales & Gesundheit"),
        ("kultur", "Kultur, Sport & Freizeit"),
        ("finanzen", "Finanzen & Haushalt"),
        ("wirtschaft", "Wirtschaft & Arbeit"),
        ("sicherheit", "Sicherheit & Ordnung"),
        ("digitales", "Digitalisierung & Verwaltung"),
        ("sonstiges", "Sonstiges"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="public_questions")
    recipient = models.ForeignKey(OParlPerson, on_delete=models.CASCADE, related_name="public_questions")
    topic = models.CharField(max_length=30, choices=TOPIC_CHOICES, default="sonstiges", verbose_name="Themenbereich")

    # Fragesteller:in (kein Account nötig)
    questioner_name = models.CharField(max_length=200, verbose_name="Name")
    questioner_email = models.EmailField(verbose_name="E-Mail")
    questioner_city = models.CharField(max_length=100, blank=True, verbose_name="Wohnort")

    # Inhalt
    subject = models.CharField(max_length=300, verbose_name="Betreff")
    question_text = models.TextField(verbose_name="Frage")

    # Moderation
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="unverified")
    moderated_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="moderated_questions",
    )
    moderated_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, verbose_name="Ablehnungsgrund")
    published_at = models.DateTimeField(null=True, blank=True, verbose_name="Veröffentlicht am")

    # Antwort
    answer_text = models.TextField(blank=True, verbose_name="Antwort")
    answered_at = models.DateTimeField(null=True, blank=True)
    answer_status = models.CharField(
        max_length=20,
        choices=ANSWER_STATUS_CHOICES,
        default="none",
    )

    # Tokens
    verification_token = models.CharField(
        max_length=64,
        unique=True,
        default=unusable_token_hash,
        editable=False,
        verbose_name="Bestätigungs-Token (SHA-256)",
    )
    answer_token = models.UUIDField(default=uuid.uuid4, unique=True)

    # DSGVO
    privacy_accepted = models.BooleanField(default=False)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "insight_public_questions"
        verbose_name = "Öffentliche Frage"
        verbose_name_plural = "Öffentliche Fragen"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["body", "status", "-created_at"]),
            models.Index(fields=["body", "topic"]),
            models.Index(fields=["verification_token"]),
            models.Index(fields=["answer_token"]),
        ]

    def __str__(self):
        return f"{self.subject} (von {self.questioner_name})"

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("insight_core:insight:question_detail", kwargs={"pk": self.id})

    @property
    def is_answered(self) -> bool:
        return self.answer_status == "published" and bool(self.answer_text)

    @property
    def days_open(self) -> int:
        """Tage seit Veröffentlichung ohne veröffentlichte Antwort (0 wenn beantwortet)."""
        if self.is_answered or not self.published_at:
            return 0
        return max(0, (timezone.now() - self.published_at).days)

    @property
    def response_days(self) -> int | None:
        """Antwortdauer in Tagen (Veröffentlichung -> Antwort), sonst None."""
        if not self.is_answered or not self.published_at or not self.answered_at:
            return None
        return max(0, (self.answered_at - self.published_at).days)


# =============================================================================
# Chat Usage (Rate Limiting & Abuse Review)
# =============================================================================


class ChatUsage(models.Model):
    """Tracks chat usage for rate limiting and abuse review."""

    FILTER_RESULT_CHOICES = [
        ("passed", "Bestanden"),
        ("pii_blocked", "PII blockiert"),
        ("spam_blocked", "Spam blockiert"),
        ("injection_blocked", "Injection blockiert"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_key = models.CharField(max_length=40, db_index=True)
    ip_address = models.GenericIPAddressField(db_index=True)
    user = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chat_usage",
    )
    message = models.TextField(verbose_name="Nachricht")
    filter_result = models.CharField(
        max_length=20,
        choices=FILTER_RESULT_CHOICES,
        default="passed",
        verbose_name="Filter-Ergebnis",
    )
    tokens_used = models.IntegerField(default=0, verbose_name="Tokens verbraucht")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "chat_usage"
        verbose_name = "Chat-Nutzung"
        verbose_name_plural = "Chat-Nutzungen"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ip_address", "created_at"]),
            models.Index(fields=["session_key", "created_at"]),
        ]

    def __str__(self):
        return f"Chat {self.created_at:%d.%m.%Y %H:%M} ({self.filter_result})"


# =============================================================================
# Bookmarks (Merkliste)
# =============================================================================


class Bookmark(models.Model):
    """Merkliste: User kann Vorgänge, Sitzungen, Gremien und Personen speichern."""

    ENTITY_TYPE_CHOICES = [
        ("person", "Person"),
        ("paper", "Vorgang"),
        ("meeting", "Sitzung"),
        ("organization", "Gremium"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="bookmarks")
    entity_type = models.CharField(max_length=20, choices=ENTITY_TYPE_CHOICES)
    entity_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "insight_bookmarks"
        unique_together = ("user", "entity_type", "entity_id")
        ordering = ["-created_at"]
        verbose_name = "Merkliste-Eintrag"
        verbose_name_plural = "Merkliste-Einträge"

    def __str__(self):
        return f"{self.user} → {self.get_entity_type_display()} {self.entity_id}"


# =============================================================================
# Insight Subscriptions (E-Mail-Digest)
# =============================================================================


class InsightSubscriber(models.Model):
    """E-Mail-Abonnent für Insight-Benachrichtigungen (kein Login nötig)."""

    FREQUENCY_CHOICES = [
        ("weekly", "Wöchentlich"),
        ("biweekly", "Alle 2 Wochen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    body = models.ForeignKey(OParlBody, on_delete=models.CASCADE, related_name="subscribers")
    token = models.UUIDField(unique=True, default=uuid.uuid4)

    # Double Opt-In
    confirmed = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    # Nachbarschaft-Abo
    neighborhood_active = models.BooleanField(default=False)
    neighborhood_lat = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    neighborhood_lon = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    neighborhood_name = models.CharField(max_length=300, null=True, blank=True)
    neighborhood_radius = models.IntegerField(default=500)

    # Suchbegriff-Abo
    keyword_active = models.BooleanField(default=False)
    keyword = models.CharField(max_length=200, null=True, blank=True)

    # Gemerkte Elemente (nur für eingeloggte User)
    bookmarks_active = models.BooleanField(default=False)
    user = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="insight_subscriptions"
    )

    # Digest-Frequenz
    digest_frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default="weekly")

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "insight_subscribers"
        unique_together = ("email", "body")
        verbose_name = "Insight-Abonnent"
        verbose_name_plural = "Insight-Abonnenten"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} ({self.body.get_display_name()})"

    @property
    def is_active(self):
        return self.confirmed and self.unsubscribed_at is None


class SubscriptionAlert(models.Model):
    """Einzelne Benachrichtigung, die im nächsten Digest verschickt wird."""

    ALERT_TYPE_CHOICES = [
        ("neighborhood", "Nachbarschaft"),
        ("keyword", "Suchbegriff"),
        ("bookmark", "Gemerkt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(InsightSubscriber, on_delete=models.CASCADE, related_name="alerts")
    alert_type = models.CharField(max_length=20, choices=ALERT_TYPE_CHOICES)

    entity_type = models.CharField(max_length=50)
    entity_id = models.UUIDField()
    entity_title = models.CharField(max_length=500)
    entity_url = models.CharField(max_length=500)

    context = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_in_digest = models.ForeignKey(
        "DigestLog", on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts"
    )

    class Meta:
        db_table = "insight_subscription_alerts"
        unique_together = ("subscriber", "entity_type", "entity_id")
        verbose_name = "Abo-Benachrichtigung"
        verbose_name_plural = "Abo-Benachrichtigungen"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_alert_type_display()}: {self.entity_title}"


class DecisionSubscription(models.Model):
    """
    Abo eines einzelnen Beschlusses im öffentlichen Beschluss-Tracking (Issue #48).
    Double-Opt-In wie beim Insight-Digest; keine Anmeldung nötig.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True)
    agenda_item = models.ForeignKey(
        "session.SessionAgendaItem", on_delete=models.CASCADE, related_name="insight_subscriptions"
    )
    token = models.UUIDField(unique=True, default=uuid.uuid4)
    confirmed = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "insight_decision_subscriptions"
        verbose_name = "Beschluss-Abo"
        verbose_name_plural = "Beschluss-Abos"
        constraints = [models.UniqueConstraint(fields=["agenda_item", "email"], name="uniq_decision_subscription")]

    def __str__(self):
        return f"{self.email} → {self.agenda_item_id}"

    @property
    def is_active(self):
        return self.confirmed and self.unsubscribed_at is None


class DigestLog(models.Model):
    """Protokoll verschickter Digest-E-Mails."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(InsightSubscriber, on_delete=models.CASCADE, related_name="digest_logs")
    sent_at = models.DateTimeField(auto_now_add=True)
    alert_count = models.IntegerField()
    success = models.BooleanField(default=True)
    error = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "insight_digest_logs"
        verbose_name = "Digest-Protokoll"
        verbose_name_plural = "Digest-Protokolle"
        ordering = ["-sent_at"]

    def __str__(self):
        return f"Digest {self.sent_at:%d.%m.%Y} → {self.subscriber.email} ({self.alert_count} Alerts)"


class PageFeedback(models.Model):
    """Rückmeldung „War diese Seite hilfreich?“ am Seitenende des Bürgerportals.

    Gespeichert werden nur Antwort, optionaler Satz, Seitentyp, Pfad, Kommune und der Tag – keine
    IP-Adresse, keine Uhrzeit, kein Cookie. Gegen Massenabgaben zählt ``throttle`` grob je Adresse
    im Cache. „Anonym“ sagen wir trotzdem nicht: Die Zugriffsprotokolle des Webservers halten wie bei jedem
    Aufruf Adresse, Zeit und Seite fest, und der Satz kann Personenbezug enthalten. Nach ``services.page_feedback.RETENTION_DAYS`` löscht ein täglicher Auftrag die Einträge.
    """

    COMMENT_MAX_LENGTH = 500

    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="page_feedback",
        null=True,
        blank=True,
        verbose_name="Kommune",
    )
    page_type = models.CharField(max_length=50, db_index=True, verbose_name="Seitentyp")
    path = models.CharField(max_length=255, blank=True, default="", verbose_name="Seite")
    helpful = models.BooleanField(verbose_name="Hilfreich")
    comment = models.TextField(max_length=COMMENT_MAX_LENGTH, blank=True, default="", verbose_name="Ergänzung")
    created_on = models.DateField(auto_now_add=True, db_index=True, verbose_name="Tag")

    class Meta:
        db_table = "insight_page_feedback"
        verbose_name = "Rückmeldung zu einer Seite"
        verbose_name_plural = "Rückmeldungen zu Seiten"
        ordering = ["-created_on", "-id"]

    def __str__(self) -> str:
        return f"{'Ja' if self.helpful else 'Nein'} – {self.page_type} ({self.created_on:%d.%m.%Y})"


# =============================================================================
# Fraktionszuordnung ohne OParl-Fraktion (Issue #916)
# =============================================================================


class PersonFraktion(models.Model):
    """Fraktion einer Person in einer Körperschaft, wenn das RIS sie nicht über OParl liefert (Issue #916).

    Viele Kommunen führen Fraktionen nicht als Gremium mit Mitgliedschaften. Die Zuordnung entsteht aus der
    Einblendung einer Live-Übertragung (``einblendung``), von Hand im Admin (``hand``) oder aus OParl (``oparl``).
    Angezeigt wird nur eine bestätigte Zuordnung, und nur, wenn OParl selbst keine Fraktion nennt; die Quelle bleibt
    öffentlich erkennbar (``hinweis``). Vorschläge warten im Admin auf Bestätigung. Regeln der automatischen
    Übernahme: ``services/fraktionen.py``.

    Eigene Tabelle statt Spalten an ``oparl_*``: Der Ingestor schreibt diese Tabellen und kennt die Zuordnung nicht.
    Verweise auf den RIS-Bestand nie mit ``CASCADE`` (ADR ``docs/adr/20260929-fremdschluessel-ris-bestand.md``):
    Person und Körperschaft ``PROTECT``, das optionale Gremium ``SET_NULL``.
    """

    QUELLE_EINBLENDUNG = "einblendung"
    QUELLE_HAND = "hand"
    QUELLE_OPARL = "oparl"
    QUELLE_CHOICES = [
        (QUELLE_EINBLENDUNG, "Einblendung der Live-Übertragung"),
        (QUELLE_HAND, "Von Hand gepflegt"),
        (QUELLE_OPARL, "Ratsinformationssystem (OParl)"),
    ]
    #: Öffentlicher Hinweis auf die Quelle (Personenliste und Personenseite)
    HINWEISE = {
        QUELLE_EINBLENDUNG: "laut Einblendung der Live-Übertragung",
        QUELLE_HAND: "redaktionell gepflegt",
        QUELLE_OPARL: "laut Ratsinformationssystem",
    }

    STATUS_BESTAETIGT = "bestaetigt"
    STATUS_VORSCHLAG = "vorschlag"
    STATUS_ABGELEHNT = "abgelehnt"
    STATUS_CHOICES = [
        (STATUS_BESTAETIGT, "Bestätigt"),
        (STATUS_VORSCHLAG, "Vorschlag"),
        (STATUS_ABGELEHNT, "Abgelehnt"),
    ]

    BEZEICHNUNG_MAX_LENGTH = 200

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person = models.ForeignKey(
        OParlPerson, on_delete=models.PROTECT, related_name="fraktionen_lokal", verbose_name="Person"
    )
    body = models.ForeignKey(
        OParlBody, on_delete=models.PROTECT, related_name="person_fraktionen", verbose_name="Körperschaft"
    )
    bezeichnung = models.CharField("Fraktion", max_length=BEZEICHNUNG_MAX_LENGTH)
    organisation = models.ForeignKey(
        OParlOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="person_fraktionen",
        verbose_name="Gremium im RIS",
        help_text="Optional: die Fraktion als Gremium im Ratsinformationssystem, falls es sie dort gibt.",
    )
    partei = models.CharField("Partei", max_length=200, blank=True, default="")
    gueltig_ab = models.DateField("Gültig ab", blank=True, null=True)
    gueltig_bis = models.DateField("Gültig bis", blank=True, null=True)
    quelle = models.CharField("Quelle", max_length=20, choices=QUELLE_CHOICES, default=QUELLE_HAND)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default=STATUS_BESTAETIGT)
    belege = models.PositiveIntegerField(
        "Lesungen", default=0, help_text="Wie oft die Einblendung einer Live-Übertragung diese Fraktion gezeigt hat."
    )
    zuletzt_gesehen = models.DateTimeField("Zuletzt gelesen", blank=True, null=True)
    angelegt = models.DateTimeField("Angelegt", auto_now_add=True)
    geaendert = models.DateTimeField("Geändert", auto_now=True)

    class Meta:
        db_table = "insight_person_fraktionen"
        verbose_name = "Fraktionszuordnung"
        verbose_name_plural = "Fraktionszuordnungen"
        ordering = ["-geaendert"]
        constraints = [
            # Höchstens eine laufende bestätigte Zuordnung je Person und Körperschaft
            models.UniqueConstraint(
                fields=["person", "body"],
                condition=Q(status="bestaetigt", gueltig_bis__isnull=True),
                name="personfraktion_eine_laufende",
            ),
            # Ein offener Vorschlag je Bezeichnung: weitere Lesungen zählen ihn hoch
            models.UniqueConstraint(
                models.F("person"),
                models.F("body"),
                Lower("bezeichnung"),
                condition=Q(status="vorschlag"),
                name="personfraktion_ein_vorschlag_je_bezeichnung",
            ),
            models.CheckConstraint(
                condition=Q(gueltig_ab__isnull=True)
                | Q(gueltig_bis__isnull=True)
                | Q(gueltig_bis__gte=F("gueltig_ab")),
                name="personfraktion_zeitraum",
            ),
        ]
        indexes = [models.Index(fields=["body", "status"], name="personfraktion_body_status")]

    def __str__(self) -> str:
        return f"{self.person} – {self.bezeichnung}"

    def clean(self) -> None:
        super().clean()
        if self.gueltig_ab and self.gueltig_bis and self.gueltig_bis < self.gueltig_ab:
            raise ValidationError({"gueltig_bis": "Das Ende liegt vor dem Beginn."})

    @property
    def hinweis(self) -> str:
        """Öffentlicher Hinweis auf die Quelle, z. B. „laut Einblendung der Live-Übertragung“."""
        return self.HINWEISE.get(self.quelle, "")


class PersonFraktionBeleg(models.Model):
    """Wortmeldung einer Live-Übertragung, deren Einblendung schon als Fraktion verbucht ist (Issue #915, #916).

    Macht die Übernahme aus ``ris.broadcast.speaker_changed`` idempotent (``services/fraktionen_live.py``): Jede
    Wortmeldung zählt höchstens einmal als Lesung, auch wenn das Ereignis erneut zugestellt oder die Ableitung für
    vorhandene Wortmeldungen nachgeholt wird. Gespeichert werden nur Kennungen: kein gelesener Text, keine Zeit der
    Wortmeldung. Die Kennung der Wortmeldung ist bewusst kein Fremdschlüssel; die Wortmeldungen gehören ``hub.live``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wortmeldung = models.UUIDField("Wortmeldung", unique=True, help_text="Kennung der Wortmeldung in hub.live")
    zuordnung = models.ForeignKey(
        PersonFraktion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="belege_wortmeldungen",
        verbose_name="Fraktionszuordnung",
    )
    angelegt = models.DateTimeField("Verbucht", auto_now_add=True)

    class Meta:
        db_table = "insight_person_fraktion_belege"
        verbose_name = "Beleg einer Fraktionszuordnung"
        verbose_name_plural = "Belege von Fraktionszuordnungen"

    def __str__(self) -> str:
        return str(self.wortmeldung)
