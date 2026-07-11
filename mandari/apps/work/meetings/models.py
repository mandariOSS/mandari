# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation models for the Work module.

Org-weite Sitzungsvorbereitung mit 5 Sektionen:
1. Position/Beschluss (org-weit, ein Beschluss pro TOP)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar mit Organisation)
4. Fraktionsdiskussion (org-weit, Kommentare)
5. Dokumente (org-weit, Links + Uploads + OParl-Referenzen)
"""

import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField, EncryptionMixin


class MeetingPreparation(EncryptionMixin, models.Model):
    """
    Org-weite Vorbereitung einer OParl-Sitzung.

    EINE Preparation pro Organisation + Sitzung (nicht pro User).
    Alle Mitglieder der Organisation sehen denselben Stand.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="meeting_preparations",
        verbose_name="Organisation",
    )
    meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.CASCADE,
        related_name="work_preparations",
        verbose_name="Sitzung",
    )

    # Phase 1: membership bleibt nullable für Datenmigration, wird in Phase 3 entfernt
    membership = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="meeting_preparations",
        verbose_name="Mitglied",
        null=True,
        blank=True,
    )

    # Org-weite allgemeine Notizen zur Sitzung (verschlüsselt)
    notes_encrypted = EncryptedTextField(
        verbose_name="Allgemeine Notizen",
        help_text="Org-weite Notizen zur Sitzung",
    )

    # Status: seit dem Auto-Save-Umbau ABGELEITET statt manuell gesetzt.
    # Sobald zu mindestens einem TOP inhaltlich gearbeitet wurde (Position,
    # Notiz, Redebeitrag, Dokument), gilt die Sitzung als "in Vorbereitung".
    # prepared_at/prepared_by werden beim ersten inhaltlichen Save automatisch
    # gesetzt (siehe record_activity). Der alte "Als vorbereitet markieren"-
    # Button ist deprecated, der Endpoint funktioniert aber weiterhin.
    is_prepared = models.BooleanField(default=False, verbose_name="Vorbereitet")
    prepared_at = models.DateTimeField(blank=True, null=True, verbose_name="Vorbereitet am")
    prepared_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prepared_meetings",
        verbose_name="Vorbereitet von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sitzungsvorbereitung"
        verbose_name_plural = "Sitzungsvorbereitungen"
        # Phase 1: unique_together entfernt (wird nach Datenmigration in Phase 3 als (org, meeting) gesetzt)
        ordering = ["-meeting__start"]

    def __str__(self):
        return f"{self.organization.name} - {self.meeting}"

    def get_encryption_organization(self):
        return self.organization

    @classmethod
    def record_activity(cls, organization, meeting, membership):
        """
        Markiert die Vorbereitung als "in Vorbereitung" (abgeleiteter Status).

        Wird von allen Schreib-APIs nach einem inhaltlichen Save aufgerufen.
        Idempotent: prepared_at/prepared_by werden nur beim ERSTEN
        inhaltlichen Save gesetzt.
        """
        preparation = cls.objects.filter(organization=organization, meeting=meeting).first()
        if preparation is None:
            preparation = cls.objects.create(
                organization=organization,
                meeting=meeting,
                membership=membership,
            )
        if not preparation.is_prepared:
            from django.utils import timezone

            preparation.is_prepared = True
            preparation.prepared_at = timezone.now()
            preparation.prepared_by = membership
            preparation.save(update_fields=["is_prepared", "prepared_at", "prepared_by", "updated_at"])
        return preparation


# =============================================================================
# Sektion 1: Beschluss / Position (org-weit)
# =============================================================================


class AgendaItemPosition(EncryptionMixin, models.Model):
    """
    Org-weite Position zu einem Tagesordnungspunkt.

    EIN Beschluss pro Organisation + TOP. Alle Mitglieder können
    die Position ändern, bis sie als endgültig markiert wird.
    """

    POSITION_CHOICES = [
        ("open", "Noch offen"),
        ("for", "Zustimmung"),
        ("against", "Ablehnung"),
        ("abstain", "Enthaltung"),
        ("defer", "Vertagen"),
        ("refer", "Verweisen"),
        ("amended", "Mit Änderungsantrag"),
        ("info", "Kenntnisnahme"),
    ]

    OUTCOME_CHOICES = [
        ("", "— noch kein Ergebnis —"),
        ("as_position", "Wie unsere Position"),
        ("accepted", "Angenommen"),
        ("rejected", "Abgelehnt"),
        ("referred", "Verwiesen"),
        ("deferred", "Vertagt"),
        ("noted", "Kenntnisnahme"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Phase 1: preparation bleibt nullable für Datenmigration, wird in Phase 3 entfernt
    preparation = models.ForeignKey(
        MeetingPreparation,
        on_delete=models.CASCADE,
        related_name="positions",
        verbose_name="Vorbereitung",
        null=True,
        blank=True,
    )

    # Phase 1: nullable für Datenmigration, wird in Phase 3 NOT NULL
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="agenda_positions",
        verbose_name="Organisation",
        null=True,
        blank=True,
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_positions",
        verbose_name="Tagesordnungspunkt",
    )

    position = models.CharField(max_length=20, choices=POSITION_CHOICES, default="open", verbose_name="Position")
    is_final = models.BooleanField(
        default=False, verbose_name="Endgültig", help_text="Position als endgültig markieren"
    )

    # Kurze Begründung der Position (org-sichtbar, verschlüsselt)
    reasoning_encrypted = EncryptedTextField(
        verbose_name="Begründung",
        help_text="Kurze Begründung der Position (für die Organisation sichtbar)",
    )

    # Ergebnis der Beratung im Gremium (nachträglich erfasst)
    outcome = models.CharField(
        max_length=20,
        choices=OUTCOME_CHOICES,
        default="",
        blank=True,
        verbose_name="Ergebnis der Beratung",
    )
    set_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="set_positions",
        verbose_name="Gesetzt von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TOP-Position"
        verbose_name_plural = "TOP-Positionen"
        # Phase 1: alte unique_together bleibt, wird in Phase 3 geändert
        ordering = ["agenda_item__number"]

    def __str__(self):
        org = self.organization.name if self.organization_id else "?"
        return f"{org} - {self.agenda_item} ({self.position})"

    @property
    def reasoning(self):
        """Entschlüsselte Begründung für Templates."""
        return self.get_reasoning_decrypted()

    @classmethod
    def get_cross_positions_for_items(cls, organization, agenda_items):
        """
        Positionen derselben Organisation aus ANDEREN Gremien/Sitzungen zur
        selben Vorlage ("Entscheidungen übergreifend").

        Für die übergebenen TOPs werden über OParlConsultation die beratenen
        Vorlagen aufgelöst und alle Positionen der Organisation an TOPs
        anderer Sitzungen zur selben Vorlage geliefert. Die Organisation
        bleibt strikt die Grenze.

        Returns:
            dict: {agenda_item_id des aktuellen TOPs: [{gremium, sitzung,
                   datum, meeting_id, position, position_display, outcome,
                   outcome_display, reasoning, is_final}, ...]}
        """
        from django.db.models import Q

        from insight_core.models import OParlConsultation

        item_ids_by_ext = {item.external_id: item.id for item in agenda_items if item.external_id}
        if not item_ids_by_ext:
            return {}

        # Vorlagen, die von den aktuellen TOPs beraten werden
        paper_items = {}  # paper_id -> {aktuelle agenda_item ids}
        for cons in OParlConsultation.objects.filter(
            agenda_item_external_id__in=item_ids_by_ext.keys(),
            paper__isnull=False,
        ):
            paper_items.setdefault(cons.paper_id, set()).add(item_ids_by_ext[cons.agenda_item_external_id])

        if not paper_items:
            return {}

        # TOPs anderer Sitzungen, die dieselben Vorlagen beraten
        sibling_papers = {}  # sibling external_id -> {paper ids}
        sibling_consultations = (
            OParlConsultation.objects.filter(paper_id__in=paper_items.keys())
            .exclude(agenda_item_external_id__isnull=True)
            .exclude(agenda_item_external_id="")
            .exclude(agenda_item_external_id__in=item_ids_by_ext.keys())
        )
        for cons in sibling_consultations:
            sibling_papers.setdefault(cons.agenda_item_external_id, set()).add(cons.paper_id)

        if not sibling_papers:
            return {}

        # Eine Query: alle inhaltlich gefüllten Positionen der Org an den Geschwister-TOPs
        positions = (
            cls.objects.filter(
                organization=organization,  # Organisation bleibt immer Grenze
                agenda_item__external_id__in=sibling_papers.keys(),
            )
            .filter(~Q(position="open") | ~Q(outcome="") | Q(reasoning_encrypted__isnull=False))
            .select_related("agenda_item", "agenda_item__meeting")
            .prefetch_related("agenda_item__meeting__organizations")
        )

        result = {}
        for pos in positions:
            meeting = pos.agenda_item.meeting
            committee = ""
            if meeting:
                orgs = meeting.organizations.all()
                committee = ", ".join(o.short_name or o.name or "" for o in orgs) or (meeting.name or "")
            entry = {
                "gremium": committee,
                "sitzung": meeting.get_display_name() if meeting else "",
                "datum": meeting.start.isoformat() if meeting and meeting.start else "",
                "meeting_id": str(meeting.id) if meeting else None,
                "position": pos.position,
                "position_display": pos.get_position_display(),
                "outcome": pos.outcome,
                "outcome_display": pos.get_outcome_display() if pos.outcome else "",
                "reasoning": pos.get_reasoning_decrypted(),
                "is_final": pos.is_final,
            }
            for paper_id in sibling_papers.get(pos.agenda_item.external_id, ()):
                for current_item_id in paper_items.get(paper_id, ()):
                    bucket = result.setdefault(current_item_id, [])
                    if entry not in bucket:
                        bucket.append(entry)

        for entries in result.values():
            entries.sort(key=lambda e: e["datum"] or "")

        return result


# =============================================================================
# Sektion 2: Private Notizen (pro User)
# =============================================================================


class AgendaPrivateNote(EncryptionMixin, models.Model):
    """
    Private Notiz eines Mitglieds zu einem TOP.

    Nur der Autor kann diese Notiz sehen.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="private_agenda_notes",
        verbose_name="Organisation",
    )
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="private_agenda_notes",
        verbose_name="Autor",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_private_notes",
        verbose_name="Tagesordnungspunkt",
    )

    content_encrypted = EncryptedTextField(verbose_name="Private Notiz")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Private TOP-Notiz"
        verbose_name_plural = "Private TOP-Notizen"
        unique_together = ["author", "agenda_item"]

    def __str__(self):
        return f"Notiz von {self.author} zu {self.agenda_item}"

    def get_encryption_organization(self):
        return self.organization


# =============================================================================
# Sektion 3: Redebeitrag (pro User, teilbar)
# =============================================================================


class AgendaSpeechNote(EncryptionMixin, models.Model):
    """
    Redebeitrag eines Mitglieds zu einem TOP.

    Ein Textfeld mit Toggle: privat oder mit Fraktion geteilt.
    Geteilte Beiträge sind für alle Org-Mitglieder sichtbar.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="speech_notes",
        verbose_name="Organisation",
    )
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="speech_notes",
        verbose_name="Autor",
    )
    # Phase 1: meeting bleibt für Abwärtskompatibilität, wird in Phase 3 entfernt
    meeting = models.ForeignKey(
        "insight_core.OParlMeeting",
        on_delete=models.CASCADE,
        related_name="work_speech_notes",
        verbose_name="Sitzung",
        null=True,
        blank=True,
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_speech_notes",
        verbose_name="Tagesordnungspunkt",
    )

    # Phase 1: alte Felder behalten, werden in Phase 3 entfernt
    title = models.CharField(max_length=200, blank=True, verbose_name="Titel")
    content = models.TextField(blank=True, verbose_name="Redetext (alt)")
    estimated_duration = models.PositiveIntegerField(default=0, verbose_name="Geschätzte Dauer (Sekunden)")

    # Neues verschlüsseltes Feld. Enthält seit dem Sitzungsvorbereitungs-Umbau
    # HTML (WYSIWYG-Editor). Beim Lesen/Schreiben wird nichts gestrippt;
    # Ausgabe-Views (z.B. Teleprompter) rendern über die strikte Whitelist in
    # apps.work.meetings.sanitize.
    content_encrypted = EncryptedTextField(verbose_name="Redebeitrag")

    # "Dokument als Redebeitrag": Wenn gesetzt, liefert die API den Inhalt
    # des verknüpften Dokuments (read-only, mit can_access-Prüfung) als
    # Redetext statt content_encrypted.
    linked_document = models.ForeignKey(
        "work.Motion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_speech_notes",
        verbose_name="Verknüpftes Dokument",
    )

    # Share-Toggle
    is_shared = models.BooleanField(default=False, verbose_name="Mit Fraktion teilen")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Redebeitrag"
        verbose_name_plural = "Redebeiträge"
        unique_together = ["author", "agenda_item"]
        ordering = ["-created_at"]

    def __str__(self):
        shared = " (geteilt)" if self.is_shared else ""
        return f"Rede: {self.author} zu {self.agenda_item}{shared}"

    def get_encryption_organization(self):
        return self.organization


# =============================================================================
# Sektion 4: Fraktionsdiskussion (org-weit)
# =============================================================================


class AgendaItemNote(EncryptionMixin, models.Model):
    """
    Org-weite Diskussionsnotiz zu einem TOP.

    ARCHITEKTUR-ENTSCHEIDUNG (Sitzungsvorbereitungs-Umbau):
    PaperComment ist DER Diskussions-Thread für TOPs mit Vorlage - Kommentare
    hängen an der Vorlage (OParlPaper) und sind damit automatisch im gesamten
    Beratungsverlauf (allen Gremien) der Organisation sichtbar.
    AgendaItemNote bleibt NUR für TOPs ohne Vorlage (org-lokal) bestehen.
    Bestehende Notizen an TOPs mit Vorlage wurden per Datenmigration nach
    PaperComment überführt; die Originale bleiben erhalten und sind über
    ``migrated_to_paper_comment`` markiert (Queries schließen sie aus, um
    Doppelanzeige zu vermeiden).

    Alle Mitglieder der Organisation sehen und können beitragen.
    Kann als Beschluss oder wichtig markiert werden.

    Sichtbarkeitsstufen:
    - organization: Nur am eigenen TOP sichtbar
    - consulting: Zusätzlich in den Vorbereitungen aller anderen Sitzungen
      sichtbar, deren TOP dieselbe Vorlage berät (Beratungsfolge). Die
      Organisation bleibt dabei immer die Grenze - Notizen sind nie für
      andere Organisationen sichtbar.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="agenda_notes",
        verbose_name="Organisation",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_notes",
        verbose_name="Tagesordnungspunkt",
    )

    VISIBILITY_CHOICES = [
        ("organization", "Meine Organisation"),
        ("consulting", "Beratende Gremien"),
    ]
    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="organization",
        verbose_name="Sichtbarkeit",
    )

    # Inhalt (verschlüsselt)
    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    # Autor
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="authored_notes",
        verbose_name="Autor",
    )

    # Markierungen
    is_decision = models.BooleanField(
        default=False,
        verbose_name="Als Beschluss markiert",
        help_text="Markiert diese Notiz als offiziellen Fraktionsbeschluss",
    )
    is_pinned = models.BooleanField(default=False, verbose_name="Angeheftet")

    # Datenmigration: Notizen an TOPs mit Vorlage wurden nach PaperComment
    # überführt. Das Original bleibt erhalten (nichts löschen!), wird aber
    # markiert, damit UI-Queries es ausschließen (keine Doppelanzeige).
    migrated_to_paper_comment = models.ForeignKey(
        "work.PaperComment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="migrated_from_notes",
        verbose_name="Migriert nach Vorgang-Kommentar",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "TOP-Diskussionsnotiz"
        verbose_name_plural = "TOP-Diskussionsnotizen"
        ordering = ["-is_pinned", "-is_decision", "-created_at"]

    def __str__(self):
        return f"Notiz zu {self.agenda_item}"

    @property
    def content(self):
        """Get decrypted content for templates."""
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.organization

    @classmethod
    def get_consulting_notes_for_items(cls, organization, agenda_items):
        """
        Consulting-Notizen aus anderen Sitzungsvorbereitungen derselben Organisation.

        Findet für die übergebenen TOPs alle Notizen mit Sichtbarkeit
        "consulting", die an TOPs anderer Sitzungen hängen, welche dieselbe
        Vorlage beraten (Lookup über OParlConsultation). Die Organisation
        bleibt dabei immer die Grenze.

        Returns:
            dict: {agenda_item_id des aktuellen TOPs: [AgendaItemNote, ...]}
            Jede Notiz trägt ``origin_meeting`` (OParlMeeting des Herkunfts-TOPs).
        """
        from insight_core.models import OParlConsultation

        item_ids_by_ext = {item.external_id: item.id for item in agenda_items if item.external_id}
        if not item_ids_by_ext:
            return {}

        # Vorlagen, die von den aktuellen TOPs beraten werden
        paper_items = {}  # paper_id -> {aktuelle agenda_item ids}
        for cons in OParlConsultation.objects.filter(
            agenda_item_external_id__in=item_ids_by_ext.keys(),
            paper__isnull=False,
        ):
            paper_items.setdefault(cons.paper_id, set()).add(item_ids_by_ext[cons.agenda_item_external_id])

        if not paper_items:
            return {}

        # TOPs anderer Sitzungen, die dieselben Vorlagen beraten
        sibling_papers = {}  # sibling external_id -> {paper ids}
        sibling_consultations = (
            OParlConsultation.objects.filter(paper_id__in=paper_items.keys())
            .exclude(agenda_item_external_id__isnull=True)
            .exclude(agenda_item_external_id="")
            .exclude(agenda_item_external_id__in=item_ids_by_ext.keys())
        )
        for cons in sibling_consultations:
            sibling_papers.setdefault(cons.agenda_item_external_id, set()).add(cons.paper_id)

        if not sibling_papers:
            return {}

        notes_by_item = {}
        notes = cls.objects.filter(
            organization=organization,  # Organisation bleibt immer Grenze
            visibility="consulting",
            agenda_item__external_id__in=sibling_papers.keys(),
            migrated_to_paper_comment__isnull=True,  # migrierte laufen über PaperComment
        ).select_related("author", "author__user", "agenda_item", "agenda_item__meeting")

        for note in notes:
            note.origin_meeting = note.agenda_item.meeting
            for paper_id in sibling_papers.get(note.agenda_item.external_id, ()):
                for current_item_id in paper_items.get(paper_id, ()):
                    bucket = notes_by_item.setdefault(current_item_id, [])
                    if note not in bucket:
                        bucket.append(note)

        return notes_by_item


# =============================================================================
# Sektion 5: Dokumente (org-weit)
# =============================================================================


class AgendaSupplementaryDocument(models.Model):
    """
    Ergänzendes Dokument zu einem TOP (org-weit).

    Unterstützt drei Typen:
    - link: Externer URL-Link
    - file: Hochgeladene Datei (PDF, DOCX etc.)
    - oparl: Referenz auf ein OParl-Dokument aus dem RIS
    """

    DOCUMENT_TYPE_CHOICES = [
        ("link", "Externer Link"),
        ("file", "Datei-Upload"),
        ("oparl", "OParl-Dokument"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="supplementary_documents",
        verbose_name="Organisation",
    )
    added_by = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="added_documents",
        verbose_name="Hinzugefügt von",
    )
    agenda_item = models.ForeignKey(
        "insight_core.OParlAgendaItem",
        on_delete=models.CASCADE,
        related_name="work_supplementary_documents",
        verbose_name="Tagesordnungspunkt",
    )

    # Anker an der VORLAGE (statt nur am TOP): Dokumente mit paper-Anker
    # können über den gesamten Beratungsverlauf geteilt werden.
    # agenda_item bleibt als Ursprungs-TOP gesetzt.
    paper = models.ForeignKey(
        "insight_core.OParlPaper",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_supplementary_documents",
        verbose_name="Vorlage",
    )
    share_across_committees = models.BooleanField(
        default=False,
        verbose_name="In allen beratenden Gremien sichtbar",
        help_text="Dokument in den Vorbereitungen aller Gremien anzeigen, die diese Vorlage beraten "
        "(nur innerhalb der eigenen Organisation)",
    )

    # Typ
    document_type = models.CharField(
        max_length=10,
        choices=DOCUMENT_TYPE_CHOICES,
        default="link",
        verbose_name="Typ",
    )

    # Gemeinsame Felder
    title = models.CharField(max_length=255, verbose_name="Titel")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Für Links
    url = models.URLField(blank=True, verbose_name="URL")

    # Für Datei-Uploads
    file = models.FileField(upload_to="meetings/documents/%Y/%m/", blank=True, verbose_name="Datei")
    filename = models.CharField(max_length=255, blank=True, verbose_name="Dateiname")
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="MIME-Typ")
    file_size = models.PositiveIntegerField(default=0, verbose_name="Dateigröße (Bytes)")

    # Für OParl-Referenzen
    oparl_file = models.ForeignKey(
        "insight_core.OParlFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_supplementary_refs",
        verbose_name="OParl-Datei",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ergänzendes Dokument"
        verbose_name_plural = "Ergänzende Dokumente"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_document_type_display()})"

    @property
    def display_url(self):
        """Gibt die anzuzeigende URL zurück, je nach Typ."""
        if self.document_type == "link":
            return self.url
        elif self.document_type == "file" and self.file:
            return self.file.url
        elif self.document_type == "oparl" and self.oparl_file:
            return self.oparl_file.access_url
        return ""

    @classmethod
    def visible_for_item(cls, organization, agenda_item, paper_ids=None):
        """
        Sichtbare Dokumente einer Vorbereitung zu einem TOP.

        Liefert: direkte TOP-Anhänge der Organisation PLUS Vorlagen-Anhänge
        derselben Organisation mit share_across_committees=True zu den vom
        TOP beratenen Vorlagen (auch wenn sie in der Vorbereitung eines
        anderen Gremiums hochgeladen wurden). Ohne Flag bleibt ein
        Vorlagen-Anhang auf seinen Ursprungs-TOP beschränkt.
        Die ORG-GRENZE ist strikt: fremde Organisationen sehen nie etwas.
        """
        from django.db.models import Q

        if paper_ids is None:
            paper_ids = [p.id for p in agenda_item.get_papers()]

        q = Q(agenda_item=agenda_item)
        if paper_ids:
            q |= Q(paper_id__in=paper_ids, share_across_committees=True)

        return (
            cls.objects.filter(organization=organization)
            .filter(q)
            .select_related("added_by__user", "oparl_file")
            .distinct()
        )


# =============================================================================
# Paper-Kommentare (gremienübergreifend, unverändert)
# =============================================================================


# Rückwärtskompatibilität: Alias für Views die noch den alten Namen importieren
AgendaDocumentLink = AgendaSupplementaryDocument


class FileAnnotation(EncryptionMixin, models.Model):
    """
    Anmerkung direkt an einer PDF-Datei (seitenbezogen).

    Hängt entweder an einer OParl-Datei aus dem Ratsinformationssystem
    (oparl_file) ODER an einer eigenen hochgeladenen Anlage
    (supplementary_document) — genau einer der beiden Anker ist gesetzt
    (CheckConstraint). Anmerkungen sind org-weit sichtbar (wie
    Fraktionskommentare), löschen darf nur der Autor. Der Inhalt ist
    verschlüsselt (Muster PaperComment).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="file_annotations",
        verbose_name="Organisation",
    )
    oparl_file = models.ForeignKey(
        "insight_core.OParlFile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="work_annotations",
        verbose_name="OParl-Datei",
    )
    supplementary_document = models.ForeignKey(
        AgendaSupplementaryDocument,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="annotations",
        verbose_name="Eigene Anlage",
    )

    page = models.PositiveIntegerField(default=1, verbose_name="Seite")
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="file_annotations",
        verbose_name="Autor",
    )
    content_encrypted = EncryptedTextField(verbose_name="Anmerkung")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Datei-Anmerkung"
        verbose_name_plural = "Datei-Anmerkungen"
        ordering = ["page", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(oparl_file__isnull=False, supplementary_document__isnull=True)
                    | models.Q(oparl_file__isnull=True, supplementary_document__isnull=False)
                ),
                name="fileannotation_exactly_one_anchor",
            ),
        ]

    def __str__(self):
        target = self.oparl_file_id or self.supplementary_document_id
        return f"Anmerkung S.{self.page} an {target}"

    @property
    def content(self):
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.organization


class PaperComment(EncryptionMixin, models.Model):
    """
    Kommentar zu einem OParl-Vorgang (Paper).

    Ermöglicht gremienübergreifende Zusammenarbeit: Kommentare sind an den
    Vorgang gebunden, nicht an einen bestimmten TOP oder eine Sitzung.

    Sichtbarkeitsstufen:
    - private: Nur der Autor
    - organization: Alle Mitglieder der eigenen Organisation
    - consulting: Alle Organisationen, die diesen Vorgang in einem ihrer Gremien beraten
    """

    VISIBILITY_CHOICES = [
        ("private", "Nur ich"),
        ("organization", "Meine Organisation"),
        ("consulting", "Alle beratenden Gremien"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    paper = models.ForeignKey(
        "insight_core.OParlPaper",
        on_delete=models.CASCADE,
        related_name="work_comments",
        verbose_name="Vorgang",
    )
    organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.CASCADE,
        related_name="paper_comments",
        verbose_name="Organisation",
    )
    author = models.ForeignKey(
        "tenants.Membership",
        on_delete=models.CASCADE,
        related_name="paper_comments",
        verbose_name="Autor",
    )

    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default="organization",
        verbose_name="Sichtbarkeit",
    )

    content_encrypted = EncryptedTextField(verbose_name="Inhalt")

    is_recommendation = models.BooleanField(
        default=False,
        verbose_name="Als Empfehlung markiert",
        help_text="Markiert diesen Kommentar als offizielle Empfehlung der Organisation",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Vorgang-Kommentar"
        verbose_name_plural = "Vorgang-Kommentare"
        ordering = ["-is_recommendation", "-created_at"]

    def __str__(self):
        return f"Kommentar zu {self.paper} ({self.visibility})"

    @property
    def content(self):
        return self.get_content_decrypted()

    def get_encryption_organization(self):
        return self.organization

    def is_visible_to(self, membership) -> bool:
        if self.visibility == "private":
            return membership == self.author
        if self.visibility == "organization":
            return membership.organization == self.organization
        if self.visibility == "consulting":
            from insight_core.models import OParlMeeting

            user_committees = membership.oparl_committees.all()
            if not user_committees.exists():
                return membership.organization == self.organization

            committee_external_ids = set(c.external_id for c in user_committees if c.external_id)
            consultations = self.paper.consultations.all()
            meeting_external_ids = [c.meeting_external_id for c in consultations if c.meeting_external_id]

            if meeting_external_ids:
                meetings = OParlMeeting.objects.filter(external_id__in=meeting_external_ids).prefetch_related(
                    "organizations"
                )
                for meeting in meetings:
                    for org in meeting.organizations.all():
                        if org.external_id in committee_external_ids:
                            return True

            return membership.organization == self.organization
        return False

    @classmethod
    def get_visible_comments_for_paper(cls, paper, membership):
        all_comments = cls.objects.filter(paper=paper).select_related("author", "author__user", "organization")
        return [c for c in all_comments if c.is_visible_to(membership)]
