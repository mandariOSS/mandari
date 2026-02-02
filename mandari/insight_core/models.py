"""
OParl Database Models (Django ORM)

Migriert von SQLAlchemy zu Django ORM.
"""

import uuid
from django.db import models
from django.core.validators import FileExtensionValidator


class OParlSource(models.Model):
    """Eine registrierte OParl-Datenquelle (z.B. RIS-API einer Stadt)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    url = models.TextField(unique=True)
    contact_email = models.EmailField(max_length=255, blank=True, null=True)
    contact_name = models.CharField(max_length=255, blank=True, null=True)
    website = models.URLField(blank=True, null=True)

    # Sync-Konfiguration
    is_active = models.BooleanField(default=True)
    last_sync = models.DateTimeField(blank=True, null=True)
    last_full_sync = models.DateTimeField(blank=True, null=True)
    sync_config = models.JSONField(default=dict, blank=True)

    # Rohe OParl-Daten
    raw_json = models.JSONField(default=dict, blank=True)

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_sources"
        verbose_name = "OParl-Quelle"
        verbose_name_plural = "OParl-Quellen"

    def __str__(self):
        return self.name


class OParlBody(models.Model):
    """Eine Körperschaft/Kommune."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    source = models.ForeignKey(
        OParlSource,
        on_delete=models.CASCADE,
        related_name="bodies"
    )

    name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=100, blank=True, null=True)
    # Anzeigename für das Frontend (manuell anpassbar)
    display_name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Kurzer Anzeigename für das Frontend (z.B. 'Köln' statt 'Stadt Köln, kreisfreie Stadt')"
    )
    website = models.URLField(blank=True, null=True)
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
        help_text="Logo der Kommune (SVG, PNG, JPG, WebP). Format wird automatisch angepasst."
    )

    # Geografische Daten (für Karten)
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, blank=True, null=True,
        help_text="Breitengrad des Zentrums (z.B. 51.9606649 für Münster)"
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, blank=True, null=True,
        help_text="Längengrad des Zentrums (z.B. 7.6261347 für Münster)"
    )
    # Bounding Box für die Kartenansicht
    bbox_north = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    bbox_south = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    bbox_east = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    bbox_west = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    # OSM Relation ID für automatisches Abrufen der Grenzen
    osm_relation_id = models.BigIntegerField(
        blank=True, null=True,
        help_text="OpenStreetMap Relation ID (z.B. 62591 für Münster)"
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
        db_table = "oparl_bodies"
        verbose_name = "OParl Kommune"
        verbose_name_plural = "OParl Kommunen"
        ordering = ["name"]

    def __str__(self):
        return self.get_display_name()

    def get_display_name(self):
        """Gibt den Anzeigenamen zurück (display_name > short_name > name)."""
        return self.display_name or self.short_name or self.name

    def get_initials(self):
        """Gibt die Initialen für Fallback-Anzeige zurück."""
        name = self.get_display_name()
        return name[:2].upper() if name else "??"


class OParlOrganization(models.Model):
    """Ein Gremium/eine Fraktion."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="organizations"
    )

    name = models.CharField(max_length=500, blank=True, null=True)
    short_name = models.CharField(max_length=100, blank=True, null=True)
    organization_type = models.CharField(max_length=100, blank=True, null=True)
    classification = models.CharField(max_length=100, blank=True, null=True)
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)
    website = models.URLField(blank=True, null=True)

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

    @property
    def is_active(self):
        """Prüft ob das Gremium noch aktiv ist."""
        from django.utils import timezone
        from datetime import date, datetime
        if self.end_date is None:
            return True
        end = self.end_date.date() if isinstance(self.end_date, datetime) else self.end_date
        return end >= timezone.now().date()


class OParlPerson(models.Model):
    """Eine Person (Ratsmitglied)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="persons"
    )

    name = models.CharField(max_length=255, blank=True, null=True)
    family_name = models.CharField(max_length=255, blank=True, null=True)
    given_name = models.CharField(max_length=255, blank=True, null=True)
    title = models.CharField(max_length=100, blank=True, null=True)
    gender = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=100, blank=True, null=True)

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


class OParlMeeting(models.Model):
    """Eine Sitzung."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="meetings"
    )

    name = models.CharField(max_length=500, blank=True, null=True)
    meeting_state = models.CharField(max_length=100, blank=True, null=True)
    cancelled = models.BooleanField(default=False)

    start = models.DateTimeField(blank=True, null=True, db_index=True)
    end = models.DateTimeField(blank=True, null=True)

    location_name = models.CharField(max_length=500, blank=True, null=True)
    location_address = models.TextField(blank=True, null=True)

    # Gremien, die an dieser Sitzung beteiligt sind
    organizations = models.ManyToManyField(
        OParlOrganization,
        related_name="meetings",
        blank=True
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
        db_table = "oparl_meetings"
        verbose_name = "Sitzung"
        verbose_name_plural = "Sitzungen"
        ordering = ["-start"]

    def __str__(self):
        return self.get_display_name()

    def get_display_name(self):
        """Gibt den Namen des Gremiums zurück (IMMER, wenn vorhanden)."""
        # Gremiennamen verwenden (vollständiger Name, keine Abkürzung)
        orgs = self.organizations.all()[:2]
        if orgs:
            org_names = [org.name for org in orgs if org.name]
            if org_names:
                return ", ".join(org_names)

        # Fallback zum Meeting-Namen
        return self.name or "Sitzung"

    def get_organization_names(self):
        """Gibt die Namen der beteiligten Gremien zurück."""
        return [org.short_name or org.name for org in self.organizations.all()]


class OParlPaper(models.Model):
    """Ein Vorgang/eine Vorlage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="papers"
    )

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

    # Zeitstempel
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "oparl_papers"
        verbose_name = "Vorgang"
        verbose_name_plural = "Vorgänge"
        ordering = ["-date", "-oparl_created"]

    def __str__(self):
        if self.reference:
            return f"{self.reference}: {self.name or ''}"
        return self.name or f"Vorgang {self.id}"


class OParlAgendaItem(models.Model):
    """Ein Tagesordnungspunkt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    meeting = models.ForeignKey(
        OParlMeeting,
        on_delete=models.CASCADE,
        related_name="agenda_items"
    )

    number = models.CharField(max_length=50, blank=True, null=True)
    order = models.IntegerField(blank=True, null=True)
    name = models.TextField(blank=True, null=True)
    public = models.BooleanField(default=True)
    result = models.TextField(blank=True, null=True)
    resolution_text = models.TextField(blank=True, null=True)

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
        return OParlPaper.objects.filter(
            consultations__agenda_item_external_id=self.external_id
        ).distinct()

    def get_consultations(self):
        """Liefert alle Consultations für diesen TOP."""
        return OParlConsultation.objects.filter(
            agenda_item_external_id=self.external_id
        ).select_related('paper')


class OParlFile(models.Model):
    """Eine Datei/Anlage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)

    # OParl relationships (must match ingestor schema)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="files",
        blank=True,
        null=True
    )
    paper = models.ForeignKey(
        OParlPaper,
        on_delete=models.CASCADE,
        related_name="files",
        blank=True,
        null=True
    )
    meeting = models.ForeignKey(
        OParlMeeting,
        on_delete=models.CASCADE,
        related_name="files",
        blank=True,
        null=True
    )

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
        if not self.size:
            return ""
        for unit in ["B", "KB", "MB", "GB"]:
            if self.size < 1024:
                return f"{self.size:.1f} {unit}"
            self.size /= 1024
        return f"{self.size:.1f} TB"


class OParlMembership(models.Model):
    """Eine Mitgliedschaft (Person in Gremium)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)

    person = models.ForeignKey(
        OParlPerson,
        on_delete=models.CASCADE,
        related_name="memberships"
    )
    organization = models.ForeignKey(
        OParlOrganization,
        on_delete=models.CASCADE,
        related_name="memberships"
    )

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
        from django.utils import timezone
        from datetime import date, datetime
        if self.end_date is None:
            return True
        end = self.end_date.date() if isinstance(self.end_date, datetime) else self.end_date
        return end >= timezone.now().date()


class OParlLocation(models.Model):
    """Ein Ort/Standort."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="locations",
        blank=True,
        null=True
    )

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


class OParlConsultation(models.Model):
    """Eine Beratung (Verknüpfung Paper-Meeting-AgendaItem)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="consultations",
        blank=True,
        null=True
    )
    paper = models.ForeignKey(
        OParlPaper,
        on_delete=models.CASCADE,
        related_name="consultations",
        blank=True,
        null=True
    )

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

    def __str__(self):
        return f"Beratung {self.role or ''} - {self.paper or self.external_id}"


class OParlLegislativeTerm(models.Model):
    """Eine Wahlperiode."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.TextField(unique=True, db_index=True)
    body = models.ForeignKey(
        OParlBody,
        on_delete=models.CASCADE,
        related_name="legislative_terms",
        blank=True,
        null=True
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
        from django.utils import timezone
        from datetime import date, datetime
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
        verbose_name="Kommune"
    )

    # Der Name des Ortes (z.B. "Hauptausschusszimmer", "Rathaus Festsaal")
    location_name = models.CharField(
        max_length=500,
        verbose_name="Ortsbezeichnung",
        help_text="Der Name wie er in Sitzungen verwendet wird (z.B. 'Hauptausschusszimmer')"
    )

    # Koordinaten
    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        verbose_name="Breitengrad",
        help_text="z.B. 51.9617867"
    )
    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        verbose_name="Längengrad",
        help_text="z.B. 7.6281645"
    )

    # Optionale Zusatzinfos
    address = models.TextField(
        blank=True,
        null=True,
        verbose_name="Adresse",
        help_text="Vollständige Adresse (optional)"
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name="Beschreibung",
        help_text="Zusätzliche Informationen zum Ort"
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


# =============================================================================
# Tile Cache für performante Karten
# =============================================================================

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
            z=z, x=x, y=y,
            defaults={
                "tile_data": tile_data,
                "content_type": content_type,
                "fetched_from": source,
            }
        )
        return tile

    @classmethod
    def tiles_for_bbox(cls, bbox_north, bbox_south, bbox_east, bbox_west, zoom_levels=range(10, 17)):
        """
        Berechnet alle Tile-Koordinaten für eine Bounding Box.

        Gibt eine Liste von (z, x, y) Tupeln zurück.
        """
        import math

        def lat_lon_to_tile(lat, lon, zoom):
            lat_rad = math.radians(lat)
            n = 2.0 ** zoom
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
