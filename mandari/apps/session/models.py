# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session RIS Models.

Implements the data model for the administrative RIS (Ratsinformationssystem).

Key concepts:
- SessionTenant: The administrative unit (Kommune/Verwaltung)
- All data is tenant-isolated using tenant_id foreign keys
- Sensitive data is encrypted using AES-256-GCM
- Models extend OParl entities with non-public fields

Security:
- Row-level security via tenant_id filtering
- Encrypted fields for non-public content
- Audit logging for all changes
"""

import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.common.encryption import EncryptedTextField, EncryptionMixin, exclude_key_fields_from_save
from apps.common.formatting import human_size
from apps.common.tokens import HashedTokenMixin, unusable_token_hash

from .visibility import AgendaItemQuerySet, FileQuerySet, MeetingQuerySet, PaperQuerySet

# Ladung mit Rückmeldung (Issue #225)
DELIVERY_CHANNEL_CHOICES = [
    ("email", "E-Mail"),
    ("portal", "Portal (mandari Work)"),
    ("letter", "Brief"),
]
# Herkunft einer Empfangsbestätigung bzw. Rückmeldung
RESPONSE_SOURCE_CHOICES = [
    ("link", "Rückmeldelink"),
    ("portal", "Portal"),
    ("staff", "Sitzungsdienst"),
]


def new_response_nonce() -> str:
    """Zufallswert je Ladungsempfänger; fließt in den signierten Rückmeldelink ein (Issue #225)."""
    import secrets

    return secrets.token_urlsafe(12)


# =============================================================================
# LANDESPROFIL (Issue #138)
# =============================================================================


class SessionStateProfile(models.Model):
    """
    Landesprofil: Was das Kommunalverfassungsrecht eines Landes zu hybriden und digitalen Sitzungen sagt.

    Referenzdaten für alle Mandanten, gepflegt in ``apps/session/presets/landesprofile.json`` und per
    Datenmigration bzw. ``manage.py session_state_profiles --sync`` übernommen. Quellen, Normen und
    Stand stehen je Land in der Datei und in ``docs/SESSION_SITZUNGSFORMAT_LANDESRECHT.md``.
    Keine Rechtsberatung: „ungeklärt“ heißt, die Recherche hat keine gesicherte Aussage ergeben.

    Grundlage für Sitzungsformat (#138), Teilnahmeart (#139) und Selbst-Abstimmung (#141).
    """

    RULE_REGULAR = "regular"
    RULE_EMERGENCY = "emergency"
    RULE_NONE = "none"
    RULE_UNCLEAR = "unclear"
    RULE_CHOICES = [
        (RULE_REGULAR, "zulässig (Regelbetrieb)"),
        (RULE_EMERGENCY, "nur in Notlagen"),
        (RULE_NONE, "nicht vorgesehen"),
        (RULE_UNCLEAR, "ungeklärt"),
    ]
    BASIS_HAUPTSATZUNG = "hauptsatzung"
    BASIS_GESCHAEFTSORDNUNG = "geschaeftsordnung"
    BASIS_BESCHLUSS = "beschluss"
    BASIS_CHOICES = [
        (BASIS_HAUPTSATZUNG, "Hauptsatzung"),
        (BASIS_GESCHAEFTSORDNUNG, "Geschäftsordnung"),
        (BASIS_BESCHLUSS, "Beschluss des Gremiums"),
        (RULE_UNCLEAR, "ungeklärt"),
    ]
    CHAIR_CHOICES = [
        ("required", "muss im Sitzungsraum anwesend sein"),
        ("not_required", "darf zugeschaltet sein"),
        (RULE_UNCLEAR, "ungeklärt"),
    ]
    # Regeln für Wahlen, geheime Abstimmungen und geheimhaltungspflichtige Beratungen (Issues #139, #754):
    # „ausgeschlossen“ nimmt nur die Zugeschalteten heraus (z. B. Bayern, Hessen); „in der Sitzung unzulässig“
    # sperrt den Vorgang für die ganze Sitzung, sobald jemand zugeschaltet teilnimmt (z. B. § 64 Abs. 3 Satz 6
    # NKomVG) – die Zugeschalteten abzuschalten genügt dort nicht.
    REMOTE_VOTE_EXCLUDED = "excluded"
    REMOTE_VOTE_MEETING = "meeting"
    REMOTE_VOTE_CHOICES = [
        ("allowed", "zulässig"),
        (REMOTE_VOTE_EXCLUDED, "für Zugeschaltete ausgeschlossen"),
        (REMOTE_VOTE_MEETING, "in der Sitzung unzulässig, sobald jemand zugeschaltet ist"),
        ("conditional", "nur unter Bedingungen"),
        (RULE_UNCLEAR, "ungeklärt"),
    ]
    ELECTIONS_ALL = "all"
    ELECTIONS_SECRET_ONLY = "secret_only"
    ELECTION_SCOPE_CHOICES = [
        (ELECTIONS_ALL, "alle Wahlen"),
        (ELECTIONS_SECRET_ONLY, "nur geheime Wahlen"),
    ]
    VERIFICATION_CHOICES = [
        ("wortlaut", "Gesetzeswortlaut eingesehen"),
        ("teilweise", "teilweise Wortlaut, teilweise Sekundärquellen"),
        ("sekundaer", "nur Sekundärquellen"),
    ]

    code = models.CharField(max_length=2, primary_key=True, verbose_name="Länderkürzel")
    name = models.CharField(max_length=60, verbose_name="Land")
    law = models.CharField(max_length=255, verbose_name="Kommunalverfassung")

    hybrid_council = models.CharField(max_length=20, choices=RULE_CHOICES, verbose_name="Hybrid: Rat/Vertretung")
    hybrid_committees = models.CharField(max_length=20, choices=RULE_CHOICES, verbose_name="Hybrid: Ausschüsse")
    digital_council = models.CharField(max_length=20, choices=RULE_CHOICES, verbose_name="Digital: Rat/Vertretung")
    digital_committees = models.CharField(max_length=20, choices=RULE_CHOICES, verbose_name="Digital: Ausschüsse")
    legal_basis = models.CharField(
        max_length=20,
        choices=BASIS_CHOICES,
        verbose_name="Voraussetzung im Regelbetrieb",
        help_text="Worin die Kommune hybride Sitzungen zulassen muss",
    )
    chair_present = models.CharField(max_length=20, choices=CHAIR_CHOICES, verbose_name="Vorsitz")
    remote_elections = models.CharField(
        max_length=20, choices=REMOTE_VOTE_CHOICES, verbose_name="Wahlen für Zugeschaltete"
    )
    remote_secret_votes = models.CharField(
        max_length=20, choices=REMOTE_VOTE_CHOICES, verbose_name="Geheime Abstimmungen für Zugeschaltete"
    )
    # Issue #754: DB-Defaults für den Rückfall per Image (älterer Code legt Profile ohne diese Spalten an)
    remote_elections_scope = models.CharField(
        max_length=20,
        choices=ELECTION_SCOPE_CHOICES,
        default=ELECTIONS_ALL,
        db_default=ELECTIONS_ALL,
        verbose_name="Regel für Wahlen gilt für",
        help_text="z. B. Niedersachsen: nur geheime Wahlen (§ 67 Satz 2 NKomVG); offene Wahlen bleiben möglich",
    )
    remote_secrecy_matters = models.CharField(
        max_length=20,
        choices=REMOTE_VOTE_CHOICES,
        default=RULE_UNCLEAR,
        db_default=RULE_UNCLEAR,
        verbose_name="Geheimhaltungspflichtige Angelegenheiten mit Zugeschalteten",
        help_text="Beratung von Angelegenheiten, deren Geheimhaltung gesetzlich vorgeschrieben oder behördlich "
        "angeordnet ist (z. B. § 6 Abs. 3 Satz 1 NKomVG)",
    )
    remote_vote_norm = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_default="",
        verbose_name="Norm zu Wahlen und geheimen Abstimmungen",
        help_text="Erscheint im Hinweis, wenn eine Abstimmung gesperrt ist",
    )
    excluded_committee_kinds = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Ausgenommene Ausschussarten",
        help_text="Ausschussarten (SessionOrganization.committee_kind), für die der Regelbetrieb nicht gilt",
    )
    excluded_committee_rule = models.CharField(
        max_length=20,
        choices=RULE_CHOICES,
        default=RULE_NONE,
        verbose_name="Regel für ausgenommene Ausschüsse",
        help_text="z. B. NRW: Hauptausschuss nur in Notlagen hybrid (§ 47a statt § 58a GO NRW)",
    )
    emergency_needs_local_basis = models.BooleanField(
        default=False,
        verbose_name="Notlage nur mit örtlicher Rechtsgrundlage",
        help_text="Auch Sitzungen in einer Notlage setzen eine Regelung in Hauptsatzung bzw. Geschäftsordnung voraus",
    )
    approved_systems_required = models.BooleanField(
        default=False, verbose_name="Nur zugelassene Konferenz- und Abstimmungssysteme"
    )
    public_registration_required = models.BooleanField(
        default=False, verbose_name="Digitale Öffentlichkeit nur nach Anmeldung"
    )
    norm_regular = models.CharField(max_length=255, blank=True, verbose_name="Norm (Regelbetrieb)")
    norm_emergency = models.CharField(max_length=255, blank=True, verbose_name="Norm (Notlage)")
    emergency_requirements = models.TextField(blank=True, verbose_name="Voraussetzungen in der Notlage")
    excluded_matters = models.TextField(blank=True, verbose_name="Ausschlüsse (Sitzungen, Gegenstände, Wahlen)")
    public_rule = models.TextField(blank=True, verbose_name="Öffentlichkeit")
    notes = models.TextField(blank=True, verbose_name="Hinweise")
    sources = models.JSONField(default=list, blank=True, verbose_name="Quellen")
    as_of = models.DateField(verbose_name="Stand der Recherche")
    verification = models.CharField(max_length=20, choices=VERIFICATION_CHOICES, verbose_name="Prüftiefe")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_state_profiles"
        verbose_name = "Landesprofil"
        verbose_name_plural = "Landesprofile"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def excluded_committee_kind_labels(self) -> list[str]:
        """Ausgenommene Ausschussarten als Bezeichnungen (Anzeige)."""
        labels = dict(SessionOrganization.COMMITTEE_KIND_CHOICES)
        return [labels.get(kind, kind) for kind in self.excluded_committee_kinds or []]


# =============================================================================
# TENANT MODEL
# =============================================================================

#: Arten einer Körperschaft (Issue #317, erweitert mit #756) – gemeinsam für Mandant und Körperschaft. Die
#: Bezeichnungen der älteren Werte erscheinen als ``classification`` in OParl und bleiben deshalb unverändert.
BODY_TYPES = [
    ("stadt", "Stadt"),
    ("kreisfreie_stadt", "Kreisfreie Stadt"),
    ("grosse_selbstaendige_stadt", "Große selbständige Stadt"),
    ("gemeinde", "Gemeinde"),
    ("einheitsgemeinde", "Einheitsgemeinde"),
    ("selbstaendige_gemeinde", "Selbständige Gemeinde"),
    ("samtgemeinde", "Samtgemeinde"),
    ("mitgliedsgemeinde", "Mitgliedsgemeinde"),
    ("kreis", "Kreis bzw. Landkreis"),
    ("region", "Region"),
    ("bezirk", "Bezirk"),
    ("gemeindeverband", "Gemeindeverband"),
    ("regionalverband", "Regionalverband"),
    ("zweckverband", "Zweckverband"),
    ("kommunale_gesellschaft", "Kommunale Gesellschaft"),
    ("sonstige", "Sonstige Körperschaft"),
]


class SessionTenant(models.Model):
    """
    Administrative unit that uses Session RIS.

    This represents a Kommune/Verwaltung that manages their own RIS.
    Can be linked to an OParlBody for public data synchronization.

    Security: All Session data is isolated by tenant_id.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Basic Info
    name = models.CharField(max_length=255, verbose_name="Name")
    slug = models.SlugField(max_length=100, unique=True, verbose_name="URL-Slug")
    short_name = models.CharField(max_length=50, blank=True, verbose_name="Kurzname")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Körperschaft (Issue #317): Art und Amtlicher Gemeindeschlüssel. Die Art erscheint in der OParl-API
    # als ``classification`` des Body, der Schlüssel als ``ags``. Bei Bestandsmandanten leer. Seit #756 führt
    # ein Mandant (die Verwaltung) eine oder mehrere Körperschaften (SessionBody); die Standardkörperschaft
    # übernimmt diese Angaben bei der Anlage, für die Veröffentlichung bleiben sie bis #758 hier maßgeblich.
    BODY_TYPE_CHOICES = BODY_TYPES
    body_type = models.CharField(
        max_length=30,
        choices=BODY_TYPE_CHOICES,
        blank=True,
        null=True,
        verbose_name="Körperschaftstyp",
    )
    ags = models.CharField(
        max_length=8,
        blank=True,
        null=True,
        verbose_name="Amtlicher Gemeindeschlüssel",
        help_text="8 Stellen für Gemeinden, 5 für Kreise, 2 oder 3 für Länder und Regierungsbezirke",
    )

    # OParl Connection (optional - for public data sync)
    oparl_body = models.OneToOneField(
        "insight_core.OParlBody",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_tenant",
        verbose_name="OParl-Kommune",
        help_text="Verknüpfung zur öffentlichen OParl-Schnittstelle",
    )

    # Branding
    logo = models.ImageField(
        upload_to="session/tenants/logos/",
        blank=True,
        null=True,
        verbose_name="Logo",
    )
    primary_color = models.CharField(max_length=7, default="#1e40af", verbose_name="Primärfarbe")
    secondary_color = models.CharField(max_length=7, default="#3b82f6", verbose_name="Sekundärfarbe")

    # Contact
    contact_email = models.EmailField(blank=True, verbose_name="Kontakt-E-Mail")
    contact_phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon")
    website = models.URLField(blank=True, verbose_name="Website")
    address = models.TextField(blank=True, verbose_name="Adresse")

    # Encryption Key (encrypted with master key)
    encryption_key = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="Verschlüsselungsschlüssel",
        help_text="AES-256 Schlüssel, verschlüsselt mit Master-Key",
    )
    encryption_key_previous = models.BinaryField(
        blank=True,
        null=True,
        editable=False,
        verbose_name="Vorheriger Verschlüsselungsschlüssel",
        help_text="Nur während eines Schlüsselwechsels gesetzt, nur zum Lesen; mit dem Hauptschlüssel verschlüsselt",
    )

    # Settings
    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")

    # Freischaltung der OParl-Schnittstelle (Issue #319): Leer heißt nicht freigeschaltet – die
    # Schnittstelle unter /session/<slug>/api/oparl/ antwortet mit 404, das Bürgerportal registriert
    # keine Quelle. Neue Mandanten starten gesperrt (Einführung, Testdaten, Umstieg); der Bestand
    # wurde bei der Einführung des Schalters mit dem Datum der Anlage freigeschaltet.
    oparl_public_since = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="OParl-Schnittstelle öffentlich seit",
        help_text="Leer: Die OParl-Schnittstelle ist nicht freigeschaltet und antwortet mit 404",
    )
    # Lizenz der offenen Daten (OParl 1.1 ``license`` an System und Body): URL der Lizenz, leer heißt
    # keine Angabe. Die Verwaltung legt sie in den Einstellungen fest (Karte „OParl-Schnittstelle“,
    # services/oparl_access.py). DB-seitiger Default, damit ein älteres Image weiter Mandanten anlegen kann.
    oparl_license = models.URLField(
        max_length=255,
        blank=True,
        default="",
        db_default="",
        verbose_name="Lizenz der offenen Daten",
        help_text="URL der Lizenz, unter der die OParl-Schnittstelle die Daten anbietet; leer: keine Angabe",
    )
    oparl_license_valid_since = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Lizenz gültig seit",
        help_text="Zeitpunkt, seit dem die angegebene Lizenz gilt (OParl ``licenseValidSince``)",
    )
    # Kennung des Mandanten als Datenbereitsteller bei GovData (``dcatde:contributorID`` im Datenkatalog nach
    # DCAT-AP.de, docs/DCAT_KATALOG.md). GovData vergibt sie bei der Anmeldung; leer heißt keine Angabe.
    # DB-seitiger Default, damit ein älteres Image weiter Mandanten anlegen kann.
    dcat_contributor_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_default="",
        validators=[
            RegexValidator(
                r"^http://dcat-ap\.de/def/contributors/[A-Za-z0-9_.-]+$",
                "Bitte die Kennung aus der Liste von GovData angeben (http://dcat-ap.de/def/contributors/…).",
            )
        ],
        verbose_name="Kennung bei GovData",
        help_text=(
            "Kennung als Datenbereitsteller (http://dcat-ap.de/def/contributors/…), vergeben bei der Anmeldung bei "
            "GovData; erscheint im Datenkatalog an jedem Datensatz. Leer: keine Angabe"
        ),
    )

    # Bürgerportal-Veröffentlichung (Issue #36): Erst wenn der Mandant den
    # Schalter aktiviert, wird seine OParl-API als Quelle im Insight-Ingestor
    # registriert und die öffentlichen Daten fließen ins Bürgerportal.
    insight_publish = models.BooleanField(
        default=False,
        verbose_name="Im Bürgerportal veröffentlichen",
        help_text="Registriert die OParl-API dieses Mandanten als Quelle für das Insight-Bürgerportal",
    )
    # Veröffentlichung beendet (Issue #618): Was mit dem gespiegelten Bestand geschieht, solange
    # insight_publish aus ist. Leer ist der alte Stand (Quelle aus, Bestand ohne Hinweis sichtbar);
    # DB-seitiger Default, damit ein älteres Image weiter Mandanten anlegen kann.
    PORTAL_END_PAUSED = "paused"
    PORTAL_END_ARCHIVED = "archived"
    PORTAL_END_WITHDRAWN = "withdrawn"
    PORTAL_END_CHOICES = [
        (PORTAL_END_PAUSED, "Vorübergehend abgeschaltet"),
        (PORTAL_END_ARCHIVED, "Als Archiv behalten"),
        (PORTAL_END_WITHDRAWN, "Dauerhaft zurückgenommen"),
    ]
    #: Diese Möglichkeiten behalten Einstieg und Bestand im Bürgerportal (mit Hinweis)
    PORTAL_END_KEEPS_ENTRY = (PORTAL_END_PAUSED, PORTAL_END_ARCHIVED)
    insight_end_mode = models.CharField(
        max_length=20,
        choices=PORTAL_END_CHOICES,
        blank=True,
        default="",
        db_default="",
        verbose_name="Veröffentlichung im Bürgerportal beendet",
        help_text="Wie das Bürgerportal den Bestand zeigt, solange nicht veröffentlicht wird",
    )
    # Öffentliches Beschluss-Tracking (Issue #48): Opt-in der Verwaltung.
    implementation_publish = models.BooleanField(
        default=False,
        verbose_name="Umsetzungsstand im Bürgerportal veröffentlichen",
        help_text="Zeigt den Umsetzungsstand öffentlicher, angenommener Beschlüsse in Insight („Was wurde aus …?“)",
    )
    # Bezeichnung der Vorlagennummer in Oberfläche und Dokumenten (Nummernkreise, Issue #150):
    # „Drucksache“ (Hamburger Bezirke), „Vorlagen-Nr.“ (NRW-Kommunen) …
    reference_label = models.CharField(
        max_length=40,
        default="Vorlagen-Nr.",
        verbose_name="Bezeichnung der Vorlagennummer",
        help_text="z. B. „Drucksache“ oder „Vorlagen-Nr.“",
    )
    # Rechte mit Geltungsbereich (Issue #772): Erst mit dem Schalter wirken befristete Zuweisungen und Zuweisungen
    # für Körperschaften, Gremien oder Ämter. Ohne Schalter gilt genau das bisherige Rollenmodell.
    # DB-Default für den Rückfall per Image.
    scoped_permissions_enabled = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="Rechte mit Geltungsbereich",
        help_text="Befristete Rollenzuweisungen und Zuweisungen für Körperschaften, Gremien oder Ämter wirken lassen",
    )

    # Zwei-Faktor-Pflicht für alle Nutzer (Admins und Nutzer mit Verwaltungsrechten sind immer verpflichtet)
    require_2fa = models.BooleanField(
        default=False,
        verbose_name="2FA für alle Nutzer erforderlich",
        help_text="Alle Session-Nutzer dieses Mandanten müssen einen zweiten Faktor einrichten",
    )

    # Vier-Augen-Prinzip je Vorgangsart (Issue #222): Wer einen Vorgang erstellt oder zuletzt
    # inhaltlich bearbeitet hat, gibt ihn nicht selbst frei. Standard: an für Vorgänge mit
    # finanziellen Auswirkungen (Sitzungsgeld, Pauschalen), aus für die übrigen.
    FOUR_EYES_PAPER_CHOICES = [
        ("off", "Aus"),
        ("financial", "Nur bei finanziellen Auswirkungen"),
        ("always", "Immer"),
    ]
    four_eyes_papers = models.CharField(
        max_length=10,
        choices=FOUR_EYES_PAPER_CHOICES,
        default="off",
        verbose_name="Vier-Augen-Prinzip: Vorlagenfreigabe",
    )
    four_eyes_protocols = models.BooleanField(default=False, verbose_name="Vier-Augen-Prinzip: Niederschrift")
    four_eyes_allowances = models.BooleanField(
        default=True, verbose_name="Vier-Augen-Prinzip: Sitzungsgeld und Pauschalen"
    )
    four_eyes_forwardings = models.BooleanField(
        default=False, verbose_name="Vier-Augen-Prinzip: Übergabe von Beschlussauszügen"
    )

    # Genehmigungsweg der Niederschrift (Issue #318): Genehmigung in der Folgesitzung (Standard)
    # oder direkte Veröffentlichung ohne Genehmigungsschritt (z. B. Freischaltung im RIS).
    # Leer bedeutet Standard, damit bestehende Mandanten ohne Umschreiben der Tabelle bleiben.
    PROTOCOL_APPROVAL_FOLLOW_UP = "follow_up"
    PROTOCOL_APPROVAL_DIRECT = "direct"
    PROTOCOL_APPROVAL_CHOICES = [
        (PROTOCOL_APPROVAL_FOLLOW_UP, "Genehmigung in der Folgesitzung"),
        (PROTOCOL_APPROVAL_DIRECT, "Direkte Veröffentlichung ohne Genehmigungsschritt"),
    ]
    protocol_approval_mode = models.CharField(
        max_length=20,
        choices=PROTOCOL_APPROVAL_CHOICES,
        blank=True,
        null=True,
        verbose_name="Genehmigungsweg der Niederschrift",
        help_text="Leer: Genehmigung in der Folgesitzung",
    )

    # Fristen-Erinnerungen (Issue #83): Vorlaufzeiten und An/Aus je Typ.
    # Nur abweichende Werte werden gespeichert; Defaults siehe
    # REMINDER_DEFAULTS bzw. reminder_config().
    REMINDER_DEFAULTS = {
        "invitation_enabled": True,
        "invitation_days_before": 3,
        "paper_enabled": True,
        "paper_days_before": 3,
        "rsvp_enabled": True,
        "rsvp_days_before": 5,
        "resolution_enabled": True,
        "resolution_days_before": 7,
    }
    # Erinnerungen zu Ladungen (Issue #619): Anlass, Empfängerkreis und mehrere Zeitpunkte, gemeinsam
    # für den Knopf in der Übersicht und den täglichen Lauf. Gespeichert in reminder_settings,
    # Standard wie bisher (fehlende Zu-/Absage, alle Geladenen, 5 Tage vorher).
    RSVP_REASON_RESPONSE = "response"
    RSVP_REASON_ACKNOWLEDGEMENT = "acknowledgement"
    RSVP_REASON_BOTH = "both"
    RSVP_REASON_CHOICES = [
        (RSVP_REASON_RESPONSE, "Zu- oder Absage fehlt"),
        (RSVP_REASON_ACKNOWLEDGEMENT, "Empfangsbestätigung fehlt (und keine Rückmeldung)"),
        (RSVP_REASON_BOTH, "Empfangsbestätigung oder Zu-/Absage fehlt"),
    ]
    RSVP_AUDIENCE_ALL = "all"
    RSVP_AUDIENCE_MEMBERS = "members"
    RSVP_AUDIENCE_VOTING = "voting"
    RSVP_AUDIENCE_CHOICES = [
        (RSVP_AUDIENCE_ALL, "Alle Geladenen"),
        (RSVP_AUDIENCE_MEMBERS, "Mitglieder der Gremien (ohne Gäste)"),
        (RSVP_AUDIENCE_VOTING, "Nur stimmberechtigte Mitglieder und ihre Vertretungen"),
    ]
    #: Höchstens so viele Erinnerungszeitpunkte je Sitzung
    RSVP_MAX_STAGES = 3
    reminder_settings = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Erinnerungs-Einstellungen",
        help_text="Abweichungen von den Standard-Vorlaufzeiten für Fristen-Erinnerungen",
    )

    # Sitzungsformate (Issue #138): Landesprofil und Nachweis der örtlichen Rechtsgrundlage für
    # hybride Sitzungen im Regelbetrieb (Hauptsatzung oder Geschäftsordnung mit Datum und Fundstelle).
    # Ohne Landesprofil bleiben nur Präsenzsitzungen möglich.
    state_profile = models.ForeignKey(
        SessionStateProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tenants",
        verbose_name="Landesprofil",
        help_text="Kommunalverfassungsrecht des Landes für hybride und digitale Sitzungen",
    )
    hybrid_basis_kind = models.CharField(
        max_length=20,
        choices=[
            (SessionStateProfile.BASIS_HAUPTSATZUNG, "Hauptsatzung"),
            (SessionStateProfile.BASIS_GESCHAEFTSORDNUNG, "Geschäftsordnung"),
            (SessionStateProfile.BASIS_BESCHLUSS, "Beschluss des Gremiums"),
        ],
        blank=True,
        default="",
        db_default="",
        verbose_name="Örtliche Rechtsgrundlage",
    )
    hybrid_basis_date = models.DateField(
        blank=True,
        null=True,
        verbose_name="Datum der Rechtsgrundlage",
        help_text="Beschluss- oder Inkrafttretensdatum der Regelung",
    )
    hybrid_basis_reference = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_default="",
        verbose_name="Fundstelle",
        help_text="z. B. „§ 7 Hauptsatzung, Amtsblatt 2024 Nr. 5“",
    )
    digital_public_registration_days = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        verbose_name="Anmeldefrist digitale Öffentlichkeit (Tage)",
        help_text="Frist laut Geschäftsordnung, bis zu der sich Zuhörende für den geschützten Zugang anmelden",
    )

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_tenants"
        verbose_name = "Session-Mandant"
        verbose_name_plural = "Session-Mandanten"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.slug:
            self.slug = slugify(self.name)
        # Schlüsselspalten nie über ein allgemeines save() zurückschreiben (Schlüsselwechsel)
        super().save(*args, **exclude_key_fields_from_save(self, kwargs))

    @property
    def oparl_public(self) -> bool:
        """Ist die OParl-Schnittstelle freigeschaltet? (Issue #319; Voraussetzung fürs Bürgerportal)."""
        return self.oparl_public_since is not None

    def clean(self) -> None:
        super().clean()
        if self.insight_publish and not self.oparl_public:
            raise ValidationError(
                "Im Bürgerportal veröffentlicht nur ein Mandant mit freigeschalteter OParl-Schnittstelle."
            )

    @property
    def protocol_direct_publication(self) -> bool:
        """Niederschriften ohne Genehmigungsschritt veröffentlichen? (Issue #318)."""
        return self.protocol_approval_mode == self.PROTOCOL_APPROVAL_DIRECT

    @property
    def hybrid_basis_documented(self) -> bool:
        """Ist die örtliche Rechtsgrundlage vollständig nachgewiesen (Art, Datum, Fundstelle)? (Issue #138)."""
        return bool(self.hybrid_basis_kind and self.hybrid_basis_date and self.hybrid_basis_reference.strip())

    @property
    def hybrid_basis_label(self) -> str:
        """Nachweis als Zeile, z. B. „Hauptsatzung vom 12.03.2024, § 7 (Amtsblatt 2024 Nr. 5)“."""
        if not self.hybrid_basis_documented:
            return ""
        assert self.hybrid_basis_date is not None  # durch hybrid_basis_documented geprüft
        return (
            f"{self.get_hybrid_basis_kind_display()} vom {self.hybrid_basis_date:%d.%m.%Y}, "
            f"{self.hybrid_basis_reference.strip()}"
        )

    def reminder_config(self) -> dict:
        """
        Erinnerungs-Einstellungen mit Defaults zusammenführen (Issues #83, #619).

        Zusätzlich zu den Vorlaufzeiten: ``rsvp_days`` (Zeitpunkte der Erinnerung zur Ladung, absteigend,
        ohne Angabe ``[rsvp_days_before]``), ``rsvp_reason`` und ``rsvp_audience``. ``rsvp_days_before``
        ist der früheste Zeitpunkt (ältere Auswertungen lesen nur ihn).
        """
        config: dict[str, Any] = dict(self.REMINDER_DEFAULTS)
        stored = self.reminder_settings if isinstance(self.reminder_settings, dict) else {}
        for key, default in self.REMINDER_DEFAULTS.items():
            value = stored.get(key, default)
            if key.endswith("_enabled"):
                config[key] = bool(value)
            else:
                try:
                    config[key] = max(0, min(60, int(value)))
                except (TypeError, ValueError):
                    config[key] = default
        days = self.parse_rsvp_days(stored.get("rsvp_days"))
        config["rsvp_days"] = days or [config["rsvp_days_before"]]
        config["rsvp_days_before"] = config["rsvp_days"][0]
        reasons = {key for key, _ in self.RSVP_REASON_CHOICES}
        audiences = {key for key, _ in self.RSVP_AUDIENCE_CHOICES}
        reason = stored.get("rsvp_reason")
        audience = stored.get("rsvp_audience")
        config["rsvp_reason"] = reason if reason in reasons else self.RSVP_REASON_RESPONSE
        config["rsvp_audience"] = audience if audience in audiences else self.RSVP_AUDIENCE_ALL
        return config

    @classmethod
    def parse_rsvp_days(cls, value: Any) -> list[int]:
        """Zeitpunkte aus Liste oder Text („7, 2“): ganze Tage 0–60, ohne Doppelte, absteigend, höchstens drei."""
        if isinstance(value, str):
            parts: list[Any] = [part for part in value.replace(";", ",").replace(" ", ",").split(",") if part]
        elif isinstance(value, list | tuple):
            parts = list(value)
        else:
            return []
        days: set[int] = set()
        for part in parts:
            try:
                days.add(max(0, min(60, int(part))))
            except (TypeError, ValueError):
                continue
        return sorted(days, reverse=True)[: cls.RSVP_MAX_STAGES]

    def get_encryption_organization(self):
        """Required for EncryptionMixin compatibility."""
        return self


# =============================================================================
# KÖRPERSCHAFTEN IM MANDANTEN (Issue #756)
# =============================================================================


class SessionBody(models.Model):
    """
    Körperschaft im Mandanten: die rechtliche Einheit, deren Gremien tagen (Issue #756).

    Der Mandant ist die Verwaltung, die den Sitzungsdienst führt – eine Samtgemeinde etwa für sich und ihre
    Mitgliedsgemeinden. Gremien, Vorlagen und Nummernkreise gehören zur Körperschaft; Personen, Konten,
    Rollen, Abläufe, Schlüssel und Prüfprotokoll zur Verwaltung. Jeder Mandant hat genau eine
    Standardkörperschaft (``is_default``); Bestandsmandanten bekommen sie per Migration aus den Angaben am
    Mandanten. Entscheidung und Abgrenzung: docs/adr/20261002-koerperschaften-im-mandanten.md.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="bodies",
        verbose_name="Mandant",
    )
    name = models.CharField(max_length=255, verbose_name="Name", help_text="z. B. „Gemeinde Musterdorf“")
    short_name = models.CharField(
        max_length=50,
        blank=True,
        default="",
        verbose_name="Kurzname",
        help_text="z. B. „MD“; Wert des Platzhalters {koerperschaft} in Nummernkreisen",
    )
    slug = models.SlugField(
        max_length=100,
        verbose_name="Kurzkennung",
        help_text="Eindeutig im Mandanten; erscheint in Filteradressen",
    )
    body_type = models.CharField(
        max_length=30,
        choices=BODY_TYPES,
        blank=True,
        default="",
        verbose_name="Art der Körperschaft",
    )
    ags = models.CharField(
        max_length=8,
        blank=True,
        default="",
        validators=[RegexValidator(r"^(\d{2}|\d{3}|\d{5}|\d{8})$", "2, 3, 5 oder 8 Ziffern.")],
        verbose_name="Amtlicher Gemeindeschlüssel",
        help_text="8 Stellen für Gemeinden, 5 für Kreise",
    )
    rgs = models.CharField(
        max_length=12,
        blank=True,
        default="",
        validators=[RegexValidator(r"^(\d{9}|\d{12})$", "9 oder 12 Ziffern.")],
        verbose_name="Regionalschlüssel",
        help_text="12 Stellen; bei Mitgliedsgemeinden mit dem Verbandsschlüssel der Samtgemeinde",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Übergeordnete Körperschaft",
        help_text="z. B. die Samtgemeinde einer Mitgliedsgemeinde",
    )
    is_default = models.BooleanField(
        default=False,
        verbose_name="Standardkörperschaft",
        help_text="Körperschaft, die die Verwaltung trägt; Vorgabe beim Anlegen und für ältere Daten",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_bodies"
        verbose_name = "Körperschaft"
        verbose_name_plural = "Körperschaften"
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "slug"], name="uniq_session_body_slug"),
            # Genau eine Standardkörperschaft je Mandant (höchstens eine per Datenbank, mindestens eine per
            # Migration und Anlage)
            models.UniqueConstraint(
                fields=["tenant"], condition=models.Q(is_default=True), name="uniq_session_body_default"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def label(self) -> str:
        """Kurzname, sonst Name – für Filter, Spalten und Auswahl."""
        return self.short_name or self.name

    def clean(self) -> None:
        super().clean()
        if self.parent_id is not None:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "Eine Körperschaft kann sich nicht selbst untergeordnet sein."})
            if self.parent is not None and self.parent.tenant_id != self.tenant_id:
                raise ValidationError({"parent": "Die übergeordnete Körperschaft muss zum selben Mandanten gehören."})
        if self.is_default and not self.is_active:
            raise ValidationError({"is_active": "Die Standardkörperschaft lässt sich nicht deaktivieren."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.slug and self.tenant_id is not None:
            from .services import body_service

            # Die eigene Zeile ausnehmen: Wer die Kurzkennung einer bestehenden Körperschaft leert, bekommt
            # dieselbe wieder (sonst „md-2“ statt „md“, und bestehende Filteradressen änderten sich).
            self.slug = body_service.free_slug(
                SessionBody, self.tenant_id, self.short_name or self.name, exclude_pk=self.pk
            )
        super().save(*args, **kwargs)


# =============================================================================
# USER & PERMISSION MODELS
# =============================================================================


class SessionRole(models.Model):
    """
    Role definition for Session users.

    Roles define what actions a user can perform within Session.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="roles",
        verbose_name="Mandant",
    )

    name = models.CharField(max_length=100, verbose_name="Name")
    description = models.TextField(blank=True, verbose_name="Beschreibung")

    # Permission flags (explicit for clarity and security)
    # Dashboard
    can_view_dashboard = models.BooleanField(default=True, verbose_name="Dashboard anzeigen")

    # Meetings
    can_view_meetings = models.BooleanField(default=True, verbose_name="Sitzungen anzeigen")
    can_create_meetings = models.BooleanField(default=False, verbose_name="Sitzungen erstellen")
    can_edit_meetings = models.BooleanField(default=False, verbose_name="Sitzungen bearbeiten")
    can_delete_meetings = models.BooleanField(default=False, verbose_name="Sitzungen löschen")
    can_view_non_public_meetings = models.BooleanField(
        default=False, verbose_name="Nicht-öffentliche Sitzungen anzeigen"
    )

    # Papers
    can_view_papers = models.BooleanField(default=True, verbose_name="Vorlagen anzeigen")
    can_create_papers = models.BooleanField(default=False, verbose_name="Vorlagen erstellen")
    can_edit_papers = models.BooleanField(default=False, verbose_name="Vorlagen bearbeiten")
    can_delete_papers = models.BooleanField(default=False, verbose_name="Vorlagen löschen")
    can_approve_papers = models.BooleanField(default=False, verbose_name="Vorlagen freigeben")
    can_view_non_public_papers = models.BooleanField(default=False, verbose_name="Nicht-öffentliche Vorlagen anzeigen")

    # Applications (from parties)
    can_view_applications = models.BooleanField(default=True, verbose_name="Anträge anzeigen")
    can_process_applications = models.BooleanField(default=False, verbose_name="Anträge bearbeiten")

    # Protocols
    can_view_protocols = models.BooleanField(default=True, verbose_name="Protokolle anzeigen")
    can_create_protocols = models.BooleanField(default=False, verbose_name="Protokolle erstellen")
    can_edit_protocols = models.BooleanField(default=False, verbose_name="Protokolle bearbeiten")
    can_approve_protocols = models.BooleanField(default=False, verbose_name="Protokolle freigeben")

    # Attendance & Allowances
    can_manage_attendance = models.BooleanField(default=False, verbose_name="Anwesenheit verwalten")
    # Sitzungscockpit (Issue #140): Sitzungsleitung und Protokollführung steuern die laufende Sitzung
    # (TOP aufrufen, Anwesenheitswechsel, Störungen, Abstimmung öffnen und schließen). Ohne das Recht
    # zeigt das Cockpit nur die Mitlese-Ansicht. DB-Default für den Rückfall per Image.
    can_conduct_meetings = models.BooleanField(
        default=False, db_default=False, verbose_name="Sitzungen leiten (Cockpit)"
    )
    can_manage_allowances = models.BooleanField(default=False, verbose_name="Sitzungsgelder verwalten")

    # Administration
    can_manage_users = models.BooleanField(default=False, verbose_name="Benutzer verwalten")
    can_manage_organizations = models.BooleanField(default=False, verbose_name="Gremien verwalten")
    can_manage_settings = models.BooleanField(default=False, verbose_name="Einstellungen verwalten")
    can_manage_devices = models.BooleanField(default=False, verbose_name="Endgeräte verwalten")

    # Kontrollrechte (Issue #221): nicht in der Administrator-Vollmacht enthalten (Funktionstrennung)
    can_view_audit_log = models.BooleanField(default=False, verbose_name="Audit-Log anzeigen")
    can_export_audit_log = models.BooleanField(default=False, verbose_name="Audit-Log exportieren und prüfen")

    # API Access
    can_access_api = models.BooleanField(default=False, verbose_name="API-Zugang")
    can_access_oparl_api = models.BooleanField(default=True, verbose_name="OParl-API-Zugang")

    # Role metadata
    is_admin = models.BooleanField(
        default=False,
        verbose_name="Administrator",
        help_text="Hat alle Berechtigungen",
    )
    is_system_role = models.BooleanField(
        default=False,
        verbose_name="Systemrolle",
        help_text="Kann nicht gelöscht werden",
    )
    priority = models.PositiveIntegerField(default=50, verbose_name="Priorität")
    color = models.CharField(max_length=7, default="#6b7280", verbose_name="Farbe")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_roles"
        verbose_name = "Session-Rolle"
        verbose_name_plural = "Session-Rollen"
        unique_together = ["tenant", "name"]
        ordering = ["-priority", "name"]

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

    #: Kontrollrechte (Issue #221): Protokoll einsehen und exportieren. Sie sind bewusst NICHT in der
    #: Administrator-Vollmacht enthalten, sondern werden je Rolle vergeben (Revision, Datenschutz).
    AUDIT_PERMISSIONS = frozenset({"view_audit_log", "export_audit_log"})

    def has_permission(self, permission: str) -> bool:
        """Check if role has a specific permission."""
        if self.is_admin and permission not in self.AUDIT_PERMISSIONS:
            return True
        return getattr(self, f"can_{permission}", False)

    @classmethod
    def create_default_roles(cls, tenant: SessionTenant) -> dict:
        """Create default roles for a new tenant."""
        return {
            key: cls.objects.create(tenant=tenant, is_system_role=True, **values)
            for key, values in cls.default_role_definitions().items()
        }

    @classmethod
    def ensure_default_roles(cls, tenant: SessionTenant) -> dict:
        """
        Fehlende Standardrollen ergänzen, vorhandene (gleicher Name) unverändert lassen (Issue #317).

        Für das idempotente Anlegen eines Mandanten und für Bestandsmandanten, denen z. B. die
        Kontrollrollen „Revision“ und „Datenschutz“ (Issue #221) noch fehlen. Gibt die neu
        angelegten Rollen zurück.
        """
        vorhanden = set(cls.objects.filter(tenant=tenant).values_list("name", flat=True))
        return {
            key: cls.objects.create(tenant=tenant, is_system_role=True, **values)
            for key, values in cls.default_role_definitions().items()
            if values["name"] not in vorhanden
        }

    @classmethod
    def default_role_definitions(cls) -> dict[str, dict[str, Any]]:
        """Alle Standardrollen in Anlagereihenfolge: Fachrollen, danach die Kontrollrollen."""
        return {**cls.BASE_ROLES, **cls.CONTROL_ROLES}

    #: Fachliche Standardrollen jedes Mandanten
    BASE_ROLES: dict[str, dict[str, Any]] = {
        "admin": {
            "name": "Administrator",
            "description": "Vollzugriff auf alle Funktionen",
            "is_admin": True,
            "priority": 100,
            "color": "#dc2626",
        },
        "clerk": {
            "name": "Sachbearbeiter",
            "description": "Kann Sitzungen und Vorlagen verwalten",
            "priority": 70,
            "color": "#7c3aed",
            "can_view_meetings": True,
            "can_create_meetings": True,
            "can_edit_meetings": True,
            "can_view_non_public_meetings": True,
            "can_view_papers": True,
            "can_create_papers": True,
            "can_edit_papers": True,
            "can_view_non_public_papers": True,
            "can_view_applications": True,
            "can_process_applications": True,
            "can_view_protocols": True,
            "can_create_protocols": True,
            "can_edit_protocols": True,
            "can_manage_attendance": True,
            "can_conduct_meetings": True,
        },
        "recorder": {
            "name": "Protokollant",
            "description": "Kann Protokolle erstellen und bearbeiten",
            "priority": 60,
            "color": "#2563eb",
            "can_view_meetings": True,
            "can_view_non_public_meetings": True,
            "can_view_papers": True,
            "can_view_non_public_papers": True,
            "can_view_protocols": True,
            "can_create_protocols": True,
            "can_edit_protocols": True,
            "can_manage_attendance": True,
            "can_conduct_meetings": True,
        },
        "viewer": {
            "name": "Lesezugriff",
            "description": "Nur Anzeige von Informationen",
            "priority": 10,
            "color": "#6b7280",
            "can_view_meetings": True,
            "can_view_papers": True,
            "can_view_applications": True,
            "can_view_protocols": True,
        },
    }

    #: Standardrollen für die Protokollkontrolle (Issue #221). Nur das Protokoll, keine Fachrechte:
    #: Wer prüft, braucht keinen Zugriff auf Sitzungs- oder Vorlageninhalte.
    CONTROL_ROLES: dict[str, dict[str, Any]] = {
        "revision": {
            "name": "Revision",
            "description": "Rechnungsprüfung: Protokoll einsehen, exportieren und auf Manipulation prüfen",
            "priority": 20,
            "color": "#0f766e",
            "can_view_meetings": False,
            "can_view_papers": False,
            "can_view_applications": False,
            "can_view_protocols": False,
            "can_access_oparl_api": False,
            "can_view_audit_log": True,
            "can_export_audit_log": True,
        },
        "privacy": {
            "name": "Datenschutz",
            "description": "Datenschutzbeauftragte: Protokoll einsehen, exportieren und auf Manipulation prüfen",
            "priority": 20,
            "color": "#0369a1",
            "can_view_meetings": False,
            "can_view_papers": False,
            "can_view_applications": False,
            "can_view_protocols": False,
            "can_access_oparl_api": False,
            "can_view_audit_log": True,
            "can_export_audit_log": True,
        },
    }


class SessionUser(models.Model):
    """
    User membership in a Session tenant.

    Links Django users to Session tenants with role-based permissions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="session_memberships",
        verbose_name="Benutzer",
    )
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="users",
        verbose_name="Mandant",
    )

    # Roles (multiple allowed)
    roles = models.ManyToManyField(
        SessionRole,
        blank=True,
        related_name="users",
        verbose_name="Rollen",
    )

    # Optional link to OParl person
    oparl_person = models.ForeignKey(
        "insight_core.OParlPerson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_users",
        verbose_name="OParl-Person",
    )

    # User-specific settings
    settings = models.JSONField(default=dict, blank=True, verbose_name="Einstellungen")

    # Ämterstruktur (Issue #81): Zuordnung zu Ämtern/Fachbereichen
    # (SessionOrganization mit organization_type="department") für die
    # Mitzeichnung von Vorlagen.
    departments = models.ManyToManyField(
        "SessionOrganization",
        blank=True,
        related_name="assigned_users",
        verbose_name="Ämter/Fachbereiche",
    )

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name="Beigetreten")
    last_access = models.DateTimeField(blank=True, null=True, verbose_name="Letzter Zugriff")

    class Meta:
        db_table = "session_users"
        verbose_name = "Session-Benutzer"
        verbose_name_plural = "Session-Benutzer"
        unique_together = ["user", "tenant"]
        ordering = ["-joined_at"]

    def __str__(self):
        return f"{self.user.email} @ {self.tenant.name}"

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission through any role."""
        return any(role.has_permission(permission) for role in self.roles.all())

    def is_admin(self) -> bool:
        """Check if user is an administrator."""
        return self.roles.filter(is_admin=True).exists()


class SessionInvitation(HashedTokenMixin, models.Model):
    """
    Einladung eines Benutzers in einen Session-Mandanten (Issue #27).

    Vorbild: Work-Einladungsflow (tenants.UserInvitation). Ermöglicht das
    Einladen per E-Mail mit vorbelegten Rollen — auch für Personen, die
    noch kein Konto haben.

    Gespeichert wird nur der SHA-256-Hash des Tokens (apps/common/tokens.py); das Token steht einmal
    in der Einladungsmail. Erneutes Senden erzeugt einen neuen Link.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="invitations",
        verbose_name="Mandant",
    )

    email = models.EmailField(verbose_name="E-Mail")
    token = models.CharField(
        max_length=64, unique=True, default=unusable_token_hash, editable=False, verbose_name="Token (SHA-256)"
    )

    # Vorbelegte Rollen
    roles = models.ManyToManyField(
        SessionRole,
        blank=True,
        related_name="invitations",
        verbose_name="Rollen",
    )

    invited_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
        verbose_name="Eingeladen von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(verbose_name="Gültig bis")
    accepted_at = models.DateTimeField(blank=True, null=True, verbose_name="Angenommen am")
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_session_invitations",
        verbose_name="Angenommen von",
    )

    class Meta:
        db_table = "session_invitations"
        verbose_name = "Session-Einladung"
        verbose_name_plural = "Session-Einladungen"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Einladung für {self.email} zu {self.tenant.name}"

    @property
    def is_valid(self) -> bool:
        """Einladung noch offen und nicht abgelaufen?"""
        return self.accepted_at is None and timezone.now() < self.expires_at

    @classmethod
    def create_for_tenant(cls, tenant, email: str, invited_by=None, roles=None, valid_days: int = 7):
        """Neue Einladung; das Token für den Link steht nur in ``plain_token`` (gespeichert wird der Hash)."""
        from datetime import timedelta

        invitation = cls(
            tenant=tenant,
            email=email.lower().strip(),
            invited_by=invited_by,
            expires_at=timezone.now() + timedelta(days=valid_days),
        )
        invitation.issue_token()
        invitation.save()
        if roles:
            for role in roles:
                if role.tenant_id != tenant.id:
                    invitation.delete()
                    raise ValueError(f"Rolle '{role.name}' gehört nicht zu diesem Mandanten.")
            invitation.roles.set(roles)
        return invitation


class SessionDelegation(models.Model):
    """
    Vertretung eines Session-Nutzers für einen Zeitraum (Issue #222).

    Wirkt nur vom ersten bis einschließlich zum letzten Tag und nur im gewählten Umfang:

    - Freigaben und Genehmigungen: Vorlagen freigeben und Niederschriften genehmigen,
      soweit die vertretene Person es selbst darf
    - Arbeitsvorrat: offene Mitzeichnungen der Ämter der vertretenen Person
    - Benachrichtigungen: E-Mails an die vertretene Person gehen in Kopie an die Vertretung

    Die Vertretung darf nie mehr als die vertretene Person. Rechte aus einer Vertretung werden
    nicht weitergereicht (keine Kettenvertretung), und das Vier-Augen-Prinzip gilt auch hier.
    Aufgehobene Vertretungen bleiben als Nachweis erhalten.
    """

    SCOPE_FIELDS = {
        "approvals": "scope_approvals",
        "worklist": "scope_worklist",
        "notifications": "scope_notifications",
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="delegations",
        verbose_name="Mandant",
    )
    principal = models.ForeignKey(
        SessionUser,
        on_delete=models.CASCADE,
        related_name="delegations_given",
        verbose_name="Vertretene Person",
    )
    deputy = models.ForeignKey(
        SessionUser,
        on_delete=models.CASCADE,
        related_name="delegations_received",
        verbose_name="Vertretung",
    )
    start_date = models.DateField(verbose_name="Von")
    end_date = models.DateField(verbose_name="Bis einschließlich")

    # Umfang
    scope_approvals = models.BooleanField(default=True, verbose_name="Freigaben und Genehmigungen")
    scope_worklist = models.BooleanField(default=True, verbose_name="Arbeitsvorrat")
    scope_notifications = models.BooleanField(default=True, verbose_name="Benachrichtigungen")

    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Eingetragen von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="Aufgehoben am")
    revoked_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Aufgehoben von",
    )

    class Meta:
        db_table = "session_delegations"
        verbose_name = "Vertretung"
        verbose_name_plural = "Vertretungen"
        ordering = ["start_date", "created_at"]
        indexes = [
            models.Index(fields=["tenant", "deputy", "end_date"]),
            models.Index(fields=["tenant", "principal", "end_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(principal=models.F("deputy")),
                name="session_delegation_not_self",
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="session_delegation_period",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.deputy.user.email} vertritt {self.principal.user.email} "
            f"({self.start_date:%d.%m.%Y}–{self.end_date:%d.%m.%Y})"
        )

    @property
    def state(self) -> str:
        """Zustand für die Übersicht: aktiv, geplant, beendet oder aufgehoben."""
        if self.revoked_at is not None:
            return "revoked"
        today = timezone.localdate()
        if self.start_date > today:
            return "planned"
        return "active" if self.end_date >= today else "ended"

    @property
    def scope_labels(self) -> list[str]:
        """Beschriftungen des gewählten Umfangs."""
        return [
            str(self._meta.get_field(name).verbose_name) for name in self.SCOPE_FIELDS.values() if getattr(self, name)
        ]


class SessionRoleAssignment(models.Model):
    """
    Rollenzuweisung: eine Rolle für ein Konto, einen Geltungsbereich und einen Zeitraum (Issue #772).

    Bis auf die Aufhebung unveränderlich; eine Änderung ist Aufhebung plus neue Zuweisung. Aufgehobene
    Zuweisungen bleiben als Nachweis. Im Übergang bleibt ``SessionUser.roles`` maßgeblich für mandantenweite,
    unbefristete Rollen; zu jedem solchen Paar gibt es eine gespiegelte Zuweisung (``apps.session.rechte``).

    Die Fremdschlüssel löschen in PostgreSQL selbst mit (Migration ``*_rollenzuweisungen_spiegeln``), damit ein
    älteres Image, das diese Tabelle nicht kennt, Konten, Rollen und Mandanten weiter löschen kann.
    """

    SCOPE_TENANT = "mandant"
    SCOPE_BODY = "koerperschaft"
    SCOPE_ORGANIZATION = "gremium"
    SCOPE_DEPARTMENT = "amt"
    SCOPE_CHOICES = [
        (SCOPE_TENANT, "Mandant"),
        (SCOPE_BODY, "Körperschaft"),
        (SCOPE_ORGANIZATION, "Gremium"),
        (SCOPE_DEPARTMENT, "Amt"),
    ]

    SOURCE_MANUAL = "manuell"
    SOURCE_MANDATE = "mandat"
    SOURCE_POSITION = "stelle"
    SOURCE_DELEGATION = "vertretung"
    SOURCE_MIGRATION = "migration"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manuell"),
        (SOURCE_MANDATE, "Mandat"),
        (SOURCE_POSITION, "Stelle"),
        (SOURCE_DELEGATION, "Vertretung"),
        (SOURCE_MIGRATION, "Migration"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="role_assignments",
        verbose_name="Mandant",
    )
    user = models.ForeignKey(
        SessionUser,
        on_delete=models.CASCADE,
        related_name="role_assignments",
        verbose_name="Konto",
    )
    role = models.ForeignKey(
        SessionRole,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name="Rolle",
    )
    scope_type = models.CharField(
        max_length=20,
        choices=SCOPE_CHOICES,
        default=SCOPE_TENANT,
        verbose_name="Geltungsbereich",
    )
    scope_id = models.UUIDField(
        null=True,
        blank=True,
        verbose_name="Kennung des Geltungsbereichs",
        help_text="Körperschaft, Gremium oder Amt; leer für den ganzen Mandanten",
    )
    valid_from = models.DateField(null=True, blank=True, verbose_name="Gültig ab")
    valid_until = models.DateField(null=True, blank=True, verbose_name="Gültig bis einschließlich")
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_MANUAL,
        verbose_name="Quelle",
    )
    note = models.CharField(max_length=500, blank=True, verbose_name="Vermerk")

    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Angelegt von",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Angelegt am")
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="Aufgehoben am")
    revoked_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Aufgehoben von",
    )

    class Meta:
        db_table = "session_role_assignments"
        verbose_name = "Rollenzuweisung"
        verbose_name_plural = "Rollenzuweisungen"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["tenant", "user", "revoked_at"], name="session_rz_konto_idx"),
        ]
        constraints = [
            # Höchstens eine aktive Spiegelzuweisung (mandantenweit, unbefristet) je Konto und Rolle
            models.UniqueConstraint(
                fields=["user", "role"],
                condition=models.Q(
                    revoked_at__isnull=True,
                    scope_type="mandant",
                    valid_from__isnull=True,
                    valid_until__isnull=True,
                ),
                name="session_rz_spiegel_eindeutig",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_from__isnull=True)
                | models.Q(valid_until__isnull=True)
                | models.Q(valid_until__gte=models.F("valid_from")),
                name="session_rz_zeitraum",
            ),
            models.CheckConstraint(
                condition=models.Q(scope_type="mandant", scope_id__isnull=True)
                | (~models.Q(scope_type="mandant") & models.Q(scope_id__isnull=False)),
                name="session_rz_bereich",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.role.name} für {self.user.user.email} ({self.get_scope_type_display()})"

    @property
    def is_mirror(self) -> bool:
        """Spiegel eines Eintrags in ``SessionUser.roles``: mandantenweit und unbefristet."""
        return self.scope_type == self.SCOPE_TENANT and self.valid_from is None and self.valid_until is None


# =============================================================================
# MANDANTENGRUPPEN UND LEITSTELLE (Issue #317)
# =============================================================================


class SessionTenantGroup(models.Model):
    """
    Mandantengruppe mit Leitstelle (Issue #317), z. B. die Bezirksämter eines Stadtstaats.

    Die Leitstelle sieht auf einer Übersichtsseite die Arbeitsvorräte, Fristen, Sitzungen und
    Kennzahlen aller Mandanten der Gruppe. Die Gruppe öffnet keinen Zugang zu den Mandanten selbst:
    Arbeiten, Lesen einzelner Vorgänge und Nichtöffentliches gibt es nur über eine echte
    Mitgliedschaft im Mandanten (SessionUser). Schlüssel und Ö/NÖ-Rechte bleiben je Mandant getrennt.

    Gepflegt wird die Gruppe im Django-Admin (Superuser bzw. Staff mit Modellrechten).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, verbose_name="Name")
    slug = models.SlugField(max_length=100, unique=True, verbose_name="URL-Kürzel")
    description = models.TextField(blank=True, verbose_name="Beschreibung")
    tenants = models.ManyToManyField(
        SessionTenant,
        through="SessionTenantGroupTenant",
        related_name="tenant_groups",
        blank=True,
        verbose_name="Mandanten",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_tenant_groups"
        verbose_name = "Mandantengruppe"
        verbose_name_plural = "Mandantengruppen"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SessionTenantGroupTenant(models.Model):
    """
    Zugehörigkeit eines Mandanten zu einer Mandantengruppe (Issue #317).

    Ein Mandant gehört höchstens einer Gruppe an (``tenant`` ist eindeutig): So bleibt für jeden
    Mandanten klar, welche Leitstelle seine Übersicht sieht, und die Liste der Personen mit
    mandantenübergreifender Sicht wächst nicht über mehrere Gruppen unbemerkt an.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.ForeignKey(
        SessionTenantGroup,
        on_delete=models.CASCADE,
        related_name="tenant_links",
        verbose_name="Mandantengruppe",
    )
    tenant = models.OneToOneField(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="group_link",
        verbose_name="Mandant",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_tenant_group_tenants"
        verbose_name = "Mandant der Gruppe"
        verbose_name_plural = "Mandanten der Gruppe"
        ordering = ["tenant__name"]

    def __str__(self) -> str:
        return f"{self.tenant} in {self.group}"


class SessionTenantGroupMembership(models.Model):
    """
    Mitgliedschaft in der Leitstelle einer Mandantengruppe (Issue #317).

    Die Gruppenrolle gilt nur für die Leitstellen-Übersicht. Sie ersetzt keine Rolle im Mandanten
    und öffnet keine Mandantenseite.
    """

    ROLE_LEITSTELLE = "leitstelle"
    ROLE_KENNZAHLEN = "kennzahlen"
    ROLE_CHOICES = [
        (ROLE_LEITSTELLE, "Leitstelle (Arbeitsvorräte, Fristen, Sitzungen, Kennzahlen, Suche)"),
        (ROLE_KENNZAHLEN, "Kennzahlen (nur Zählwerte je Mandant)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.ForeignKey(
        SessionTenantGroup,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Mandantengruppe",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="session_group_memberships",
        verbose_name="Benutzer",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_LEITSTELLE, verbose_name="Gruppenrolle")
    note = models.CharField(max_length=255, blank=True, verbose_name="Vermerk", help_text="z. B. Anlass der Freigabe")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_tenant_group_memberships"
        verbose_name = "Leitstellen-Mitgliedschaft"
        verbose_name_plural = "Leitstellen-Mitgliedschaften"
        ordering = ["group__name", "user__email"]
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], name="uniq_session_tenant_group_member"),
        ]

    def __str__(self) -> str:
        return f"{self.user} – {self.group} ({self.get_role_display()})"

    @property
    def sees_worklists(self) -> bool:
        """Listen, Fristen, Sitzungen und Suche – nicht nur Zählwerte."""
        return self.role == self.ROLE_LEITSTELLE


# =============================================================================
# ORGANIZATION MODELS
# =============================================================================


class SessionOrganization(models.Model):
    """
    Organization/Committee within Session.

    Extends OParlOrganization with Session-specific fields.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="organizations",
        verbose_name="Mandant",
    )
    # Körperschaft (Issue #756): Pflicht im Modell (save() setzt die Standardkörperschaft), in der Datenbank
    # nullbar, damit ein älteres Image auf dem neuen Schema weiter Gremien anlegen kann. Leer gilt beim Lesen
    # als Standardkörperschaft (body_service.body_q); jeder migrate-Lauf ordnet solche Nachzügler zu.
    body = models.ForeignKey(
        SessionBody,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="organizations",
        verbose_name="Körperschaft",
    )

    # OParl link (optional)
    oparl_organization = models.OneToOneField(
        "insight_core.OParlOrganization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_organization",
        verbose_name="OParl-Gremium",
    )

    # Basic info
    name = models.CharField(max_length=500, verbose_name="Name")
    short_name = models.CharField(max_length=100, blank=True, verbose_name="Kurzname")
    organization_type = models.CharField(
        max_length=100,
        choices=[
            ("committee", "Ausschuss"),
            ("council", "Rat"),
            ("faction", "Fraktion"),
            ("advisory", "Beirat"),
            ("commission", "Kommission"),
            ("department", "Amt/Fachbereich"),
            ("other", "Sonstiges"),
        ],
        default="committee",
        verbose_name="Typ",
    )
    # Gesetzlich besonders geregelte Ausschüsse (Issue #138): Landesprofile nehmen sie teils von hybriden
    # Sitzungen aus, z. B. NRW Haupt-, Finanz- und Rechnungsprüfungsausschuss (§ 58a i. V. m. § 57 Abs. 2 GO NRW).
    # Leer heißt „nicht eingeordnet“; „anderer Ausschuss“ ist die geprüfte Aussage, dass keine besondere
    # Art zutrifft. Profile mit ausgenommenen Ausschussarten verlangen die Einordnung, bevor ein Ausschuss
    # hybrid oder digital tagt, dessen Name auf eine dieser Arten hindeutet (meeting_format_service).
    COMMITTEE_KIND_MAIN = "main"
    COMMITTEE_KIND_FINANCE = "finance"
    COMMITTEE_KIND_AUDIT = "audit"
    COMMITTEE_KIND_ORDINARY = "ordinary"
    COMMITTEE_KIND_CHOICES = [
        (COMMITTEE_KIND_MAIN, "Hauptausschuss"),
        (COMMITTEE_KIND_FINANCE, "Finanzausschuss"),
        (COMMITTEE_KIND_AUDIT, "Rechnungsprüfungsausschuss"),
        (COMMITTEE_KIND_ORDINARY, "Anderer Ausschuss (keine besondere Art)"),
    ]
    committee_kind = models.CharField(
        max_length=20,
        choices=COMMITTEE_KIND_CHOICES,
        blank=True,
        default="",
        db_default="",
        verbose_name="Gesetzliche Ausschussart",
        help_text="Für Sitzungsformate: Haupt-, Finanz- und Rechnungsprüfungsausschuss haben im Kommunalrecht "
        "teils besondere Regeln",
    )

    # Hierarchy
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="Übergeordnetes Gremium",
    )

    # Members
    members = models.ManyToManyField(
        "SessionPerson",
        through="SessionOrganizationMembership",
        through_fields=("organization", "person"),
        related_name="organizations",
        verbose_name="Mitglieder",
    )

    # Settings
    default_meeting_location = models.CharField(max_length=255, blank=True, verbose_name="Standardort für Sitzungen")
    default_meeting_start_time = models.TimeField(blank=True, null=True, verbose_name="Standardzeit für Sitzungen")

    # Sitzungsdienst-Stammdaten
    meeting_frequency = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Sitzungsturnus",
        help_text="z. B. monatlich, 6-wöchentlich, nach Bedarf",
    )
    invitation_period_days = models.PositiveIntegerField(
        default=7,
        verbose_name="Ladungsfrist (Tage)",
        help_text="Frist zwischen Ladung und Sitzung gemäß Geschäftsordnung",
    )
    target_member_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Mitgliederzahl (Soll)",
    )

    # Allowance settings
    allowance_amount = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name="Sitzungsgeld",
    )
    allowance_currency = models.CharField(max_length=3, default="EUR", verbose_name="Währung")

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    start_date = models.DateField(blank=True, null=True, verbose_name="Startdatum")
    end_date = models.DateField(blank=True, null=True, verbose_name="Enddatum")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_organizations"
        verbose_name = "Gremium"
        verbose_name_plural = "Gremien"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.body_id is not None and self.tenant_id is not None and self.body.tenant_id != self.tenant_id:
            raise ValidationError({"body": "Die Körperschaft muss zum Mandanten des Gremiums gehören."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Ohne Angabe gehört ein Gremium zur Standardkörperschaft (Issue #756) – auf jedem Anlageweg
        if self.body_id is None and self.tenant_id is not None:
            from .services import body_service

            self.body = body_service.default_body(self.tenant)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "body"}
        super().save(*args, **kwargs)


class SessionPerson(EncryptionMixin, models.Model):
    """
    Person within Session (council members, etc.).

    Extends OParlPerson with Session-specific fields.

    Security: Kontaktdaten (Telefon, Adresse) und Bankdaten (Kontoinhaber,
    IBAN, BIC) werden AES-256-GCM-verschlüsselt gespeichert (Tenant-Key).
    Zugriff über die generierten Accessoren, z. B.
    person.set_bank_iban_encrypted("DE...") / person.get_bank_iban_decrypted().
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="persons",
        verbose_name="Mandant",
    )

    # OParl link (optional)
    oparl_person = models.OneToOneField(
        "insight_core.OParlPerson",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_person",
        verbose_name="OParl-Person",
    )

    # Basic info
    title = models.CharField(max_length=50, blank=True, verbose_name="Titel")
    given_name = models.CharField(max_length=100, verbose_name="Vorname")
    family_name = models.CharField(max_length=100, verbose_name="Nachname")
    form_of_address = models.CharField(max_length=50, blank=True, verbose_name="Anrede")

    # Contact
    # E-Mail bleibt bewusst Klartext: Sie wird in der Personensuche gefiltert
    # (PersonListView, email__icontains) und über die öffentliche OParl-API
    # veröffentlicht, sofern die Person eingewilligt hat — verschlüsselte Felder sind nicht filterbar.
    email = models.EmailField(blank=True, verbose_name="E-Mail")
    # Kontaktdaten veröffentlichen (Issue #319): Die E-Mail erscheint in der OParl-Schnittstelle (und damit
    # im Bürgerportal) nur mit Kennzeichen, Datum und Nachweis der Einwilligung. Telefon und Adresse werden
    # nie veröffentlicht. DB-seitige Defaults, damit ein älteres Image weiter Personen anlegen kann.
    contact_publish = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="Kontaktdaten veröffentlichen",
        help_text="E-Mail-Adresse in der OParl-Schnittstelle und im Bürgerportal zeigen",
    )
    contact_consent_date = models.DateField(
        null=True,
        blank=True,
        verbose_name="Einwilligung vom",
    )
    contact_consent_evidence = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_default="",
        verbose_name="Nachweis der Einwilligung",
        help_text="z. B. „Schriftliche Erklärung, abgelegt in der Mandatsakte“",
    )
    phone_encrypted = EncryptedTextField(verbose_name="Telefon (verschlüsselt)")
    address_encrypted = EncryptedTextField(verbose_name="Adresse (verschlüsselt)")

    # Bank details for allowances (AES-256-GCM, Tenant-Key)
    bank_account_holder_encrypted = EncryptedTextField(verbose_name="Kontoinhaber (verschlüsselt)")
    bank_iban_encrypted = EncryptedTextField(verbose_name="IBAN (verschlüsselt)")
    bank_bic_encrypted = EncryptedTextField(verbose_name="BIC (verschlüsselt)")

    # Zustellweg für Ladungen (Issue #225): E-Mail mit Unterlagen, Portal
    # (Hinweis-Mail ohne Anhänge, Unterlagen und Rückmeldung in mandari Work)
    # oder Brief (keine Mail, Serienbrief-Export für den Postversand)
    delivery_channel = models.CharField(
        max_length=10,
        choices=DELIVERY_CHANNEL_CHOICES,
        default="email",
        verbose_name="Zustellweg für Ladungen",
    )

    # Status
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    start_date = models.DateField(blank=True, null=True, verbose_name="Mandatsbeginn")
    end_date = models.DateField(blank=True, null=True, verbose_name="Mandatsende")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_persons"
        verbose_name = "Person"
        verbose_name_plural = "Personen"
        ordering = ["family_name", "given_name"]

    def __str__(self):
        if self.title:
            return f"{self.title} {self.given_name} {self.family_name}"
        return f"{self.given_name} {self.family_name}"

    @property
    def display_name(self):
        """Full name for display."""
        parts = []
        if self.title:
            parts.append(self.title)
        parts.append(self.given_name)
        parts.append(self.family_name)
        return " ".join(parts)

    @property
    def published_email(self) -> str:
        """E-Mail für die öffentliche OParl-Schnittstelle – nur mit Einwilligung (Issue #319), sonst leer."""
        return self.email if self.contact_publish else ""


class SessionOrganizationMembership(models.Model):
    """
    Membership of a person in an organization.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Gremium",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name="Person",
    )

    role = models.CharField(
        max_length=100,
        choices=[
            ("member", "Mitglied"),
            ("chair", "Vorsitzende/r"),
            ("deputy_chair", "Stellv. Vorsitzende/r"),
            ("expert_citizen", "Sachkundige/r Bürger/in"),
            ("advisor", "Beratendes Mitglied"),
            ("guest", "Gast"),
        ],
        default="member",
        verbose_name="Funktion",
    )

    start_date = models.DateField(blank=True, null=True, verbose_name="Von")
    end_date = models.DateField(blank=True, null=True, verbose_name="Bis")

    # Voting rights
    has_voting_rights = models.BooleanField(default=True, verbose_name="Stimmberechtigt")

    # Vertreterregelung: Diese Mitgliedschaft vertritt eine andere Person
    substitute_for = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="substituted_by_memberships",
        verbose_name="Vertretung für",
        help_text="Wird als Stellvertreter/in dieser Person geführt",
    )

    # Wahlperiode (Issue #39): Besetzungen werden je Periode geführt —
    # nach dem Periodenwechsel bleiben Alt-Besetzungen unter der alten
    # Periode auffindbar (Archiv)
    legislative_term = models.ForeignKey(
        "SessionLegislativeTerm",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memberships",
        verbose_name="Wahlperiode",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_organization_memberships"
        verbose_name = "Gremienmitgliedschaft"
        verbose_name_plural = "Gremienmitgliedschaften"
        unique_together = ["organization", "person", "start_date"]

    def __str__(self):
        return f"{self.person} - {self.organization} ({self.role})"


# =============================================================================
# MEETING MODELS
# =============================================================================


class SessionMeeting(EncryptionMixin, models.Model):
    """
    Meeting/Session within Session RIS.

    Extends OParlMeeting with non-public fields and workflow support.
    """

    # Sichtbarkeit nichtöffentlicher Sitzungen: SessionMeeting.objects.visible_to(permissions)
    objects = MeetingQuerySet.as_manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="meetings",
        verbose_name="Mandant",
    )

    # OParl link (optional - for public sync)
    oparl_meeting = models.OneToOneField(
        "insight_core.OParlMeeting",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_meeting",
        verbose_name="OParl-Sitzung",
    )

    # Basic info
    name = models.CharField(max_length=500, verbose_name="Name")
    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="meetings",
        verbose_name="Gremium",
    )
    # Gemeinsame Sitzung mehrerer Gremien (Issue #317): weitere Gremien desselben Mandanten neben
    # dem federführenden Gremium. Wirkt auf Anzeige, Ladung, Anwesenheit, OParl und Gremienfilter.
    joint_organizations = models.ManyToManyField(
        SessionOrganization,
        blank=True,
        related_name="joint_meetings",
        verbose_name="Weitere beteiligte Gremien",
        help_text="Gemeinsame Sitzung: Gremien, die zusätzlich zum federführenden Gremium tagen",
    )

    # Wahlperiode (Issue #39) — wird bei Anlage automatisch aus dem
    # Sitzungsdatum abgeleitet, bleibt beim Periodenwechsel erhalten
    legislative_term = models.ForeignKey(
        "SessionLegislativeTerm",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meetings",
        verbose_name="Wahlperiode",
    )

    # Date/Time
    start = models.DateTimeField(verbose_name="Beginn")
    end = models.DateTimeField(blank=True, null=True, verbose_name="Ende")
    actual_start = models.DateTimeField(blank=True, null=True, verbose_name="Tatsächlicher Beginn")
    actual_end = models.DateTimeField(blank=True, null=True, verbose_name="Tatsächliches Ende")

    # Location
    location = models.CharField(max_length=500, blank=True, verbose_name="Ort")
    room = models.CharField(max_length=100, blank=True, verbose_name="Raum")
    street_address = models.CharField(max_length=255, blank=True, verbose_name="Straße")
    postal_code = models.CharField(max_length=10, blank=True, verbose_name="PLZ")
    locality = models.CharField(max_length=100, blank=True, verbose_name="Stadt")

    # Sitzungsformat (Issue #138): präsent, hybrid (Zuschaltung einzelner Mitglieder) oder digital
    # (alle zugeschaltet). Zulässigkeit prüft meeting_format_service gegen das Landesprofil des Mandanten.
    FORMAT_PRESENCE = "presence"
    FORMAT_HYBRID = "hybrid"
    FORMAT_DIGITAL = "digital"
    FORMAT_CHOICES = [
        (FORMAT_PRESENCE, "Präsenzsitzung"),
        (FORMAT_HYBRID, "Hybride Sitzung"),
        (FORMAT_DIGITAL, "Digitale Sitzung"),
    ]
    format = models.CharField(
        max_length=20,
        choices=FORMAT_CHOICES,
        default=FORMAT_PRESENCE,
        db_default=FORMAT_PRESENCE,
        verbose_name="Sitzungsformat",
    )
    format_reason = models.TextField(
        blank=True,
        default="",
        db_default="",
        verbose_name="Begründung des Sitzungsformats",
        help_text="Pflicht bei digitalen Sitzungen und bei hybriden Sitzungen, die das Landesrecht nur in "
        "Notlagen zulässt: Notlage und zugrunde liegender Beschluss",
    )
    # Zugangsweg für zugeschaltete Mitglieder (z. B. Konferenzraum und Einwahl): verschlüsselt, erscheint
    # nur in der Ladung an die Mitglieder, nie in öffentlichen Dokumenten oder der OParl-API.
    remote_access_encrypted = EncryptedTextField(blank=True, null=True, verbose_name="Zugangsweg für Zugeschaltete")
    public_access_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        db_default="",
        verbose_name="Übertragung für die Öffentlichkeit",
        help_text="Adresse des Livestreams bzw. der Anmeldeseite für den geschützten Zugang",
    )
    public_access_note = models.CharField(
        max_length=500,
        blank=True,
        default="",
        db_default="",
        verbose_name="Hinweis für die Öffentlichkeit",
        help_text="z. B. „Anmeldung zum geschützten Zugang bis zwei Tage vor der Sitzung per E-Mail“",
    )

    # Status
    meeting_state = models.CharField(
        max_length=50,
        choices=[
            ("draft", "Entwurf"),
            ("scheduled", "Geplant"),
            ("invitation_sent", "Einladung versandt"),
            ("in_progress", "Laufend"),
            ("completed", "Abgeschlossen"),
            ("cancelled", "Abgesagt"),
        ],
        default="draft",
        verbose_name="Status",
    )
    cancelled = models.BooleanField(default=False, verbose_name="Abgesagt")
    cancellation_reason = models.TextField(blank=True, verbose_name="Absagegrund")

    # Visibility
    is_public = models.BooleanField(
        default=True,
        verbose_name="Öffentlich",
        help_text="Wird über OParl-API veröffentlicht",
    )

    # Non-public internal notes (encrypted)
    internal_notes_encrypted = EncryptedTextField(blank=True, null=True, verbose_name="Interne Notizen")

    # Invitation
    invitation_sent_at = models.DateTimeField(blank=True, null=True, verbose_name="Einladung versandt am")
    invitation_text = models.TextField(blank=True, verbose_name="Einladungstext")

    # Workflow
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_meetings",
        verbose_name="Erstellt von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_meetings"
        verbose_name = "Sitzung"
        verbose_name_plural = "Sitzungen"
        ordering = ["-start"]

    def __str__(self):
        return f"{self.organization.name}: {self.name}"

    def clean(self) -> None:
        """
        Das Ende liegt nach dem Beginn – sonst rechnen ICS-Export und Kollisionsprüfung der Jahresplanung mit
        einem leeren Zeitfenster. Hier statt im Formular, damit Sitzungsformular und Admin gleich prüfen.
        """
        super().clean()
        if self.start and self.end and self.end <= self.start:
            raise ValidationError({"end": "Das Ende muss nach dem Beginn der Sitzung liegen."})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """
        Neue Sitzungen ohne Wahlperiode bekommen sie aus dem Sitzungsdatum (Issue #39).

        Hier statt im Anlageformular, damit jeder Anlageweg sie setzt – Serienplanung, Demo- und Lastdaten,
        Schnittstellen. Archiv, Sitzungsliste und Suche zählen Sitzungen über diese Zuordnung.

        Absage: Häkchen ``cancelled`` und Status ``cancelled`` beschreiben denselben Sachverhalt und werden
        abgeglichen – sagt eines von beiden ab, ist die Sitzung abgesagt. Kalender, Abo-Feed, Dashboard und
        Erinnerungen prüfen das Häkchen, Statusfilter und Rückmeldelink den Status.
        """
        if self._state.adding and self.legislative_term_id is None and self.tenant_id and hasattr(self.start, "date"):
            start = timezone.localtime(self.start) if timezone.is_aware(self.start) else self.start
            self.legislative_term = SessionLegislativeTerm.for_date(self.tenant, start.date())
        self.sync_cancellation()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and {"cancelled", "meeting_state"} & set(update_fields):
            kwargs["update_fields"] = {*update_fields, "cancelled", "meeting_state"}
        super().save(*args, **kwargs)

    def sync_cancellation(self) -> None:
        """Häkchen und Status der Absage angleichen (abgesagt gewinnt)."""
        if self.meeting_state == "cancelled":
            self.cancelled = True
        elif self.cancelled:
            self.meeting_state = "cancelled"

    def align_cancellation_input(self, cleaned: dict[str, Any], changed: Iterable[str]) -> None:
        """
        Formulare mit Status und Häkchen (Sitzungsformular, Admin): Wer nur eines von beiden ändert, ändert das
        andere mit – auch die Rücknahme der Absage (wieder „Geplant“ bzw. „Einladung versandt“). Ändern sich
        beide widersprüchlich, gilt die Absage wie in :meth:`sync_cancellation`. ``self`` ist der bisherige Stand.
        """
        state, cancelled = cleaned.get("meeting_state"), bool(cleaned.get("cancelled"))
        changed = set(changed)
        if "meeting_state" in changed and "cancelled" not in changed:
            cleaned["cancelled"] = state == "cancelled"
        elif "cancelled" in changed and "meeting_state" not in changed:
            if cancelled:
                cleaned["meeting_state"] = "cancelled"
            elif state == "cancelled":
                cleaned["meeting_state"] = "invitation_sent" if self.invitation_sent_at else "scheduled"
        elif cancelled or state == "cancelled":
            cleaned["cancelled"], cleaned["meeting_state"] = True, "cancelled"

    @property
    def is_cancelled(self) -> bool:
        return bool(self.cancelled) or self.meeting_state == "cancelled"

    def assign_legislative_term(self) -> None:
        """
        Wahlperiode nachführen, wenn eine Sitzung verschoben wird (Issue #39): Liegt das neue Datum außerhalb
        der bisherigen Periode, gilt die Periode, die es enthält. Ohne passende Periode bleibt eine gesetzte
        Periode stehen; fehlt sie, gilt der Rückfall von :meth:`SessionLegislativeTerm.for_date`. Neue Sitzungen
        erhalten die Periode in :meth:`save`.
        """
        if not hasattr(self.start, "date") or self.tenant_id is None:
            return
        start = timezone.localtime(self.start) if timezone.is_aware(self.start) else self.start
        day = start.date()
        current = self.legislative_term if self.legislative_term_id else None
        if current is not None and current.contains(day):
            return
        terms = list(SessionLegislativeTerm.objects.filter(tenant_id=self.tenant_id))
        match = next((term for term in terms if term.contains(day)), None)
        if match is not None:
            self.legislative_term = match
        elif current is None and terms:
            self.legislative_term = SessionLegislativeTerm.for_date(self.tenant, day)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Sitzungen mit genehmigter Niederschrift bleiben erhalten (Issue #318)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_meeting_delete(self)
        return super().delete(*args, **kwargs)

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.tenant

    # --- Gemeinsame Sitzung mehrerer Gremien (Issue #317) -------------------------------------
    #: Annotation aus ``SessionMeeting.with_joint_flag``: hat die Sitzung weitere Gremien?
    JOINT_FLAG = "has_joint_organizations"

    @classmethod
    def with_joint_flag(cls, queryset: Any) -> Any:
        """
        Sitzungen vorab markieren, ob weitere Gremien beteiligt sind – ohne zusätzliche Abfrage.

        Listen und Detailseiten zeigen die weiteren Gremien nur für markierte Sitzungen und laden
        sie nur dann nach (Performance-Budgets).
        """
        through = cls.joint_organizations.through
        return queryset.annotate(
            **{cls.JOINT_FLAG: models.Exists(through.objects.filter(sessionmeeting_id=models.OuterRef("pk")))}
        )

    @property
    def joint_organization_list(self) -> list[Any]:
        """Weitere beteiligte Gremien, nach Namen sortiert (leer bei einer gewöhnlichen Sitzung)."""
        cached = getattr(self, "_joint_organization_list", None)
        if cached is not None:
            return list(cached)
        prefetched = getattr(self, "_prefetched_objects_cache", {}).get("joint_organizations")
        if prefetched is not None:
            items = list(prefetched)
        elif self._state.adding or getattr(self, self.JOINT_FLAG, True) in (False, 0):
            items = []
        else:
            items = list(self.joint_organizations.all())
        items.sort(key=lambda org: org.name)
        self._joint_organization_list = items
        return list(items)

    @property
    def is_joint(self) -> bool:
        """Gemeinsame Sitzung mehrerer Gremien?"""
        return bool(self.joint_organization_list)

    @property
    def participating_organizations(self) -> list[Any]:
        """Federführendes Gremium zuerst, danach die weiteren beteiligten Gremien."""
        return [self.organization, *self.joint_organization_list]

    @property
    def participating_organization_ids(self) -> list[Any]:
        return [self.organization_id, *(org.pk for org in self.joint_organization_list)]

    @property
    def organizations_label(self) -> str:
        """Gremien für Anzeige, Ladung und Kalender, z. B. „Bauausschuss und Umweltausschuss“."""
        names = [org.name for org in self.participating_organizations]
        if len(names) == 1:
            return names[0]
        return f"{', '.join(names[:-1])} und {names[-1]}"

    @staticmethod
    def organization_q(organization: Any, prefix: str = "") -> models.Q:
        """Filter „Sitzungen dieses Gremiums“ – federführend oder als weiteres Gremium beteiligt."""
        return models.Q(**{f"{prefix}organization": organization}) | models.Q(
            **{f"{prefix}joint_organizations": organization}
        )

    @property
    def invitation_period_days(self) -> int:
        """Ladungsfrist in Tagen; bei gemeinsamen Sitzungen gilt die längste Frist der Gremien."""
        return max((org.invitation_period_days or 0) for org in self.participating_organizations)

    @property
    def invitation_deadline(self):
        """
        Spätester Ladungstermin gemäß Ladungsfrist des Gremiums (Issue #29).

        Beispiel: Sitzung am 20.08., Ladungsfrist 7 Tage -> Ladung bis 13.08.
        Bei gemeinsamen Sitzungen gilt die längste Ladungsfrist der beteiligten Gremien (Issue #317).
        """
        from datetime import timedelta

        period = self.invitation_period_days
        return (timezone.localtime(self.start) - timedelta(days=period)).date()

    @property
    def invitation_overdue(self) -> bool:
        """Ladungsfrist verstrichen, ohne dass eine Einladung versandt wurde?"""
        if self.invitation_sent_at or self.cancelled:
            return False
        if self.meeting_state not in ("draft", "scheduled"):
            return False
        return timezone.localdate() > self.invitation_deadline


class SessionInvitationDispatch(models.Model):
    """
    Versandvorgang einer Ladung/Einladung zu einer Sitzung (Issue #29).

    Dokumentiert gerichtsfest, wer wann welche Art von Ladung (Erstladung
    oder Nachtrags-Tagesordnung) an welchen Empfängerkreis versandt hat.
    Die einzelnen Empfänger inkl. Zustellstatus hängen als
    SessionInvitationRecipient daran.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="invitation_dispatches",
        verbose_name="Sitzung",
    )

    dispatch_type = models.CharField(
        max_length=20,
        choices=[
            ("invitation", "Ladung"),
            ("supplementary", "Nachladung/Nachtrag"),
            # Issue #225: automatische Benachrichtigung der Stellvertretung nach einer Absage
            ("substitution", "Vertretungsanfrage"),
        ],
        default="invitation",
        verbose_name="Versandart",
    )

    subject = models.CharField(max_length=500, verbose_name="Betreff")
    message = models.TextField(blank=True, verbose_name="Anschreiben")

    sent_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="invitation_dispatches",
        verbose_name="Versandt von",
    )
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name="Versandt am")

    class Meta:
        db_table = "session_invitation_dispatches"
        verbose_name = "Ladungsversand"
        verbose_name_plural = "Ladungsversände"
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.get_dispatch_type_display()} für {self.meeting} am {self.sent_at:%d.%m.%Y}"


class SessionInvitationRecipient(models.Model):
    """
    Einzelner Empfänger eines Ladungsversands inkl. Zustellstatus (Issue #29).

    Seit Issue #225 zusätzlich: Zustellweg, Versandzeitpunkt, Empfangsbestätigung
    (nur durch aktive Handlung – Link, Portal oder Eintrag des Sitzungsdienstes,
    nie durch bloßes Öffnen) und Erinnerungen. Die Rückmeldung selbst (Zu-/Absage,
    Vertretungswunsch) steht an der SessionAttendance der Person.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    dispatch = models.ForeignKey(
        SessionInvitationDispatch,
        on_delete=models.CASCADE,
        related_name="recipients",
        verbose_name="Versand",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitation_receipts",
        verbose_name="Person",
    )

    name = models.CharField(max_length=255, verbose_name="Name")
    email = models.EmailField(blank=True, verbose_name="E-Mail")
    membership_role = models.CharField(max_length=100, blank=True, verbose_name="Funktion")

    # Ö/NÖ: hat dieser Empfänger die vollständige (inkl. NÖ-Teil) Tagesordnung erhalten?
    includes_non_public = models.BooleanField(
        default=False,
        verbose_name="Inkl. nichtöffentlicher Teil",
    )

    # Tatsächlich genutzter Zustellweg (Portal ohne Portalzugang fällt auf E-Mail zurück)
    channel = models.CharField(
        max_length=10,
        choices=DELIVERY_CHANNEL_CHOICES,
        default="email",
        verbose_name="Zustellweg",
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ("sent", "Versandt"),
            ("failed", "Fehlgeschlagen"),
            ("letter_pending", "Brief vorzubereiten"),
            ("letter_sent", "Brief versandt"),
        ],
        default="sent",
        verbose_name="Zustellstatus",
    )
    error = models.TextField(blank=True, verbose_name="Fehler")
    sent_at = models.DateTimeField(blank=True, null=True, verbose_name="Versandt am")

    # Vertretungsanfrage: für wen soll diese Person vertreten?
    substitute_for = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="substitution_requests",
        verbose_name="Vertretung für",
    )

    # Empfangs-/Kenntnisnahmebestätigung (Issue #225)
    acknowledged_at = models.DateTimeField(blank=True, null=True, verbose_name="Empfang bestätigt am")
    acknowledged_via = models.CharField(
        max_length=10,
        choices=RESPONSE_SOURCE_CHOICES,
        blank=True,
        verbose_name="Empfang bestätigt über",
    )
    reminder_count = models.PositiveSmallIntegerField(default=0, verbose_name="Erinnerungen")
    last_reminded_at = models.DateTimeField(blank=True, null=True, verbose_name="Zuletzt erinnert am")

    # Geht in den signierten Rückmeldelink ein; ein neuer Wert macht alle bisherigen Links ungültig
    response_nonce = models.CharField(
        max_length=32,
        default=new_response_nonce,
        editable=False,
        verbose_name="Link-Schlüssel",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_invitation_recipients"
        verbose_name = "Ladungsempfänger"
        verbose_name_plural = "Ladungsempfänger"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} <{self.email}> ({self.get_status_display()})"

    @property
    def delivered_at(self):
        """Versandzeitpunkt; Altbestand vor Issue #225 hat nur created_at."""
        if self.status in ("failed", "letter_pending"):
            return None
        return self.sent_at or self.created_at


class SessionAgendaItem(EncryptionMixin, models.Model):
    """
    Agenda item for a meeting.

    Extends OParlAgendaItem with voting results and non-public content.
    """

    # Sichtbarkeit nichtöffentlicher TOPs: SessionAgendaItem.objects.visible_to(permissions)
    objects = AgendaItemQuerySet.as_manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="agenda_items",
        verbose_name="Sitzung",
    )

    # OParl link (optional)
    oparl_agenda_item = models.OneToOneField(
        "insight_core.OParlAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_agenda_item",
        verbose_name="OParl-TOP",
    )

    # Basic info
    number = models.CharField(max_length=20, verbose_name="TOP-Nr.")
    name = models.CharField(max_length=500, verbose_name="Betreff")
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")

    # Hierarchie: Unterpunkte (z. B. 5.1, 5.2)
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="sub_items",
        verbose_name="Übergeordneter TOP",
    )

    # Visibility
    is_public = models.BooleanField(default=True, verbose_name="Öffentlich")

    # Absetzung (dokumentiert statt gelöscht)
    is_withdrawn = models.BooleanField(default=False, verbose_name="Abgesetzt")
    withdrawn_reason = models.TextField(blank=True, verbose_name="Absetzungsgrund")

    # Nachtrag (nach Versand der Ladung hinzugefügt)
    is_supplementary = models.BooleanField(
        default=False,
        verbose_name="Nachtrag",
        help_text="Nach Versand der Einladung hinzugefügt (Nachtragstagesordnung)",
    )

    # Ende-TOP: Standard-TOP am Schluss (z. B. „Verschiedenes“); ergänzte TOPs kommen davor
    is_end_item = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="Ende-TOP",
        help_text="Standard-TOP am Schluss der Tagesordnung; später ergänzte TOPs werden davor eingereiht",
    )

    # Paper reference
    paper = models.ForeignKey(
        "SessionPaper",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agenda_items",
        verbose_name="Vorlage",
    )

    # Voting
    resolution_text = models.TextField(blank=True, verbose_name="Beschlusstext")
    resolution_text_encrypted = EncryptedTextField(
        blank=True,
        null=True,
        verbose_name="Nicht-öffentlicher Beschlusstext",
    )

    # Niederschrift (Issue #31): TOP-weise Protokolltexte
    protocol_note = models.TextField(
        blank=True,
        verbose_name="Protokolltext",
        help_text="Wortbeiträge/Beratungsverlauf zu diesem TOP (öffentlicher Teil der Niederschrift)",
    )
    protocol_note_encrypted = EncryptedTextField(
        blank=True,
        null=True,
        verbose_name="Nicht-öffentlicher Protokolltext",
    )

    # Beschlussregister (Issue #32): fortlaufende Beschlussnummer je Mandant/Jahr
    resolution_number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Beschlussnummer",
        help_text="Wird bei der Beschlussausfertigung vergeben, z. B. B/2026/0012",
    )

    vote_result = models.CharField(
        max_length=50,
        choices=[
            ("pending", "Ausstehend"),
            ("approved", "Angenommen"),
            ("rejected", "Abgelehnt"),
            ("deferred", "Vertagt"),
            ("withdrawn", "Zurückgezogen"),
            ("noted", "Zur Kenntnis genommen"),
        ],
        default="pending",
        verbose_name="Abstimmungsergebnis",
    )
    votes_yes = models.PositiveIntegerField(default=0, verbose_name="Ja-Stimmen")
    votes_no = models.PositiveIntegerField(default=0, verbose_name="Nein-Stimmen")
    votes_abstain = models.PositiveIntegerField(default=0, verbose_name="Enthaltungen")

    # Digitale Abstimmung (Issue #41): Art der Abstimmung. Bei „namentlich"
    # und „offen (einzeln)" werden Einzelstimmen erfasst (SessionVote);
    # bei „geheim" werden bewusst nur Summen gespeichert.
    VOTING_METHOD_CHOICES = [
        ("summary", "Nur Summen"),
        ("open", "Offen (einzeln erfasst)"),
        ("roll_call", "Namentlich"),
        ("secret", "Geheim"),
    ]
    voting_method = models.CharField(
        max_length=20,
        choices=VOTING_METHOD_CHOICES,
        default="summary",
        verbose_name="Abstimmungsart",
    )
    # Wahl (Issue #139): Personalentscheidung. Das Landesprofil regelt, ob Zugeschaltete an Wahlen
    # teilnehmen (``SessionStateProfile.remote_elections``); DB-Default für den Rückfall per Image.
    is_election = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="Wahl",
        help_text="Personalentscheidung, z. B. Wahl in ein Gremium oder Amt",
    )
    # Geheimhaltungspflichtige Angelegenheit (Issue #754): nicht dasselbe wie nichtöffentlich – Personal- und
    # Grundstückssachen sind in der Regel nur nichtöffentlich. Das Landesprofil regelt, ob die Beratung mit
    # Zugeschalteten zulässig ist (``SessionStateProfile.remote_secrecy_matters``).
    requires_secrecy = models.BooleanField(
        default=False,
        db_default=False,
        verbose_name="Geheimhaltungspflichtig",
        help_text="Geheimhaltung gesetzlich vorgeschrieben oder behördlich angeordnet (z. B. § 6 Abs. 3 Satz 1 "
        "NKomVG); nicht dasselbe wie nichtöffentlich",
    )

    # Beschlusskontrolle (Issue #37): Umsetzung nach der Beschlussfassung.
    # Nur für angenommene Beschlüsse relevant; die Verwaltung dokumentiert
    # hier Zuständigkeit, Frist und Erledigung.
    IMPLEMENTATION_CHOICES = [
        ("open", "Offen"),
        ("in_progress", "In Umsetzung"),
        ("done", "Erledigt"),
        ("deferred", "Zurückgestellt"),
    ]
    implementation_status = models.CharField(
        max_length=20,
        choices=IMPLEMENTATION_CHOICES,
        default="open",
        verbose_name="Umsetzungsstand",
    )
    implementation_recipient = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Zuständige Stelle/Amt",
        help_text="z. B. Bauamt, Kämmerei",
    )
    implementation_deadline = models.DateField(blank=True, null=True, verbose_name="Erledigungsfrist")
    implementation_note = models.TextField(blank=True, verbose_name="Erledigungsvermerk")
    # Öffentliches Beschluss-Tracking (Issue #48): getrennt vom internen Vermerk.
    implementation_public = models.BooleanField(
        default=True,
        verbose_name="Umsetzungsstand öffentlich zeigen",
        help_text="Nur wirksam, wenn der Mandant die Veröffentlichung eingeschaltet hat",
    )
    implementation_public_note = models.TextField(
        blank=True,
        verbose_name="Öffentliche Statusmeldung",
        help_text="Kurzer Stand für Bürgerinnen und Bürger — ohne interne Details",
    )
    implementation_updated_at = models.DateTimeField(blank=True, null=True, verbose_name="Umsetzung aktualisiert am")
    implementation_updated_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="resolution_tracking_updates",
        verbose_name="Umsetzung aktualisiert von",
    )

    # Timing: Aufruf und Ende des TOP in der Sitzung; das Sitzungscockpit (Issue #140) füllt beides live
    start_time = models.TimeField(blank=True, null=True, verbose_name="Beginn")
    end_time = models.TimeField(blank=True, null=True, verbose_name="Ende")

    # Abstimmung im Sitzungscockpit (Issue #140): geöffnet und geschlossen von der Sitzungsleitung. Das Ergebnis
    # steht wie bisher in vote_result und den Summen bzw. Einzelstimmen; die Zeitpunkte dokumentieren den Ablauf.
    vote_opened_at = models.DateTimeField(blank=True, null=True, verbose_name="Abstimmung geöffnet")
    vote_closed_at = models.DateTimeField(blank=True, null=True, verbose_name="Abstimmung geschlossen")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_agenda_items"
        verbose_name = "Tagesordnungspunkt"
        verbose_name_plural = "Tagesordnungspunkte"
        ordering = ["order", "number"]

    def __str__(self):
        return f"TOP {self.number}: {self.name}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Nach der Genehmigung der Niederschrift sind Ergebnis, Stimmen und Texte gesperrt (Issue #318)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_agenda_item(self, kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """TOPs einer Sitzung mit genehmigter Niederschrift lassen sich nicht löschen (Issue #318)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_agenda_item_delete(self)
        return super().delete(*args, **kwargs)

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.meeting.tenant

    @property
    def vote_open(self) -> bool:
        """Läuft im Sitzungscockpit gerade eine Abstimmung zu diesem TOP? (Issue #140)."""
        return self.vote_opened_at is not None and self.vote_closed_at is None

    @property
    def implementation_overdue(self) -> bool:
        """Frist verstrichen, aber Beschluss noch nicht erledigt."""
        if not self.implementation_deadline or self.implementation_status == "done":
            return False
        return self.implementation_deadline < timezone.localdate()


class SessionResolutionForwarding(models.Model):
    """
    Versand-/Übergabevermerk eines Beschlussauszugs (Issue #32).

    Dokumentiert nachweisbar, an welche Stelle (Fachamt, extern) der
    Beschlussauszug eines TOP wann übergeben wurde.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    agenda_item = models.ForeignKey(
        SessionAgendaItem,
        on_delete=models.CASCADE,
        related_name="forwardings",
        verbose_name="Tagesordnungspunkt",
    )

    recipient = models.CharField(
        max_length=255,
        verbose_name="Zuständige Stelle/Amt",
        help_text="z. B. Bauamt, Kämmerei, externe Stelle",
    )
    method = models.CharField(
        max_length=20,
        choices=[
            ("email", "E-Mail"),
            ("internal", "Interner Geschäftsgang"),
            ("mail", "Postweg"),
            ("handover", "Persönliche Übergabe"),
        ],
        default="internal",
        verbose_name="Übergabeweg",
    )
    note = models.TextField(blank=True, verbose_name="Vermerk")

    sent_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="resolution_forwardings",
        verbose_name="Übergeben von",
    )
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name="Übergeben am")

    class Meta:
        db_table = "session_resolution_forwardings"
        verbose_name = "Beschlussauszug-Übergabe"
        verbose_name_plural = "Beschlussauszug-Übergaben"
        ordering = ["-sent_at"]

    def __str__(self):
        return f"Auszug TOP {self.agenda_item.number} an {self.recipient} ({self.sent_at:%d.%m.%Y})"


# =============================================================================
# PAPER/APPLICATION MODELS
# =============================================================================


class SessionNumberRange(models.Model):
    """
    Nummernkreis für Vorlagen bzw. Drucksachen eines Mandanten (Issue #150).

    Das Muster setzt sich aus Platzhaltern zusammen (siehe
    ``services/numbering_service.py``): ``{wp}-{lfd:4}`` ergibt „22-0593“ (Hamburger
    Bezirke), ``V/{lfd:4}/{jahr}`` ergibt „V/0599/2024“ (Münster). Der Zähler läuft je
    Zählerbereich (Jahr, Wahlperiode oder nie zurückgesetzt) und wird atomar in
    ``SessionNumberCounter`` hochgezählt. Ein Kreis ohne ``paper_types`` gilt für alle
    Vorlagenarten, für die kein spezieller Kreis existiert.
    """

    RESET_CHOICES = [
        ("yearly", "Jährlich"),
        ("term", "Je Wahlperiode"),
        ("never", "Nie (fortlaufend)"),
    ]
    ASSIGN_CHOICES = [
        ("create", "Beim Anlegen"),
        ("release", "Bei der Freigabe"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="number_ranges",
        verbose_name="Mandant",
    )
    # Geltungsbereich (Issue #756): leer = für alle Körperschaften des Mandanten, sonst nur für Vorlagen dieser
    # Körperschaft; der Kreis der Körperschaft hat Vorrang vor dem allgemeinen (numbering_service.range_for)
    body = models.ForeignKey(
        "SessionBody",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="number_ranges",
        verbose_name="Körperschaft",
        help_text="Leer: gilt für alle Körperschaften ohne eigenen Nummernkreis",
    )
    name = models.CharField(
        max_length=100, verbose_name="Bezeichnung", help_text="z. B. Drucksachen, Anträge der Politik"
    )
    prefix = models.CharField(
        max_length=20, blank=True, verbose_name="Präfix", help_text="Wert des Platzhalters {prefix}"
    )
    pattern = models.CharField(
        max_length=100,
        default="V/{jahr}/{lfd:4}",
        verbose_name="Muster",
        help_text="Platzhalter: {lfd} bzw. {lfd:4}, {jahr}, {jj}, {wp}, {prefix}, {gremium}, {koerperschaft}",
    )
    reset = models.CharField(max_length=10, choices=RESET_CHOICES, default="yearly", verbose_name="Zähler zurücksetzen")
    assign_on = models.CharField(
        max_length=10, choices=ASSIGN_CHOICES, default="create", verbose_name="Nummer vergeben"
    )
    paper_types = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Vorlagenarten",
        help_text="Leer = alle Vorlagenarten ohne eigenen Nummernkreis",
    )
    sub_pattern = models.CharField(
        max_length=60,
        default="{parent}.{sub}",
        verbose_name="Muster für Unternummern",
        help_text="Ergänzungen, Neufassungen, Antworten: Platzhalter {parent} und {sub} bzw. {sub:2}",
    )
    order = models.PositiveSmallIntegerField(default=0, verbose_name="Reihenfolge")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_number_ranges"
        verbose_name = "Nummernkreis"
        verbose_name_plural = "Nummernkreise"
        ordering = ["order", "name"]

    def __str__(self):
        return f"{self.name} ({self.pattern})"


class SessionNumberCounter(models.Model):
    """Stand eines Nummernkreises je Zählerbereich (z. B. „2026“ oder „wp22“); nur aufsteigend."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number_range = models.ForeignKey(
        SessionNumberRange,
        on_delete=models.CASCADE,
        related_name="counters",
        verbose_name="Nummernkreis",
    )
    scope = models.CharField(max_length=120, blank=True, verbose_name="Zählerbereich")
    value = models.PositiveIntegerField(default=0, verbose_name="Zuletzt vergeben")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_number_counters"
        verbose_name = "Zählerstand"
        verbose_name_plural = "Zählerstände"
        constraints = [
            models.UniqueConstraint(fields=["number_range", "scope"], name="uniq_session_number_counter_scope"),
        ]

    def __str__(self):
        return f"{self.number_range.name} [{self.scope or '–'}]: {self.value}"


class SessionPaper(EncryptionMixin, models.Model):
    """
    Paper/Vorlage within Session RIS.

    Extends OParlPaper with workflow and non-public content support.
    """

    # Sichtbarkeit nichtöffentlicher Vorlagen: SessionPaper.objects.visible_to(permissions)
    objects = PaperQuerySet.as_manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="papers",
        verbose_name="Mandant",
    )
    # Körperschaft (Issue #756): aus dem federführenden Gremium bzw. beim Anlegen gewählt, sonst die
    # Standardkörperschaft (save()). In der Datenbank nullbar wie SessionOrganization.body.
    body = models.ForeignKey(
        SessionBody,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="papers",
        verbose_name="Körperschaft",
    )

    # OParl link (optional)
    oparl_paper = models.OneToOneField(
        "insight_core.OParlPaper",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_paper",
        verbose_name="OParl-Vorlage",
    )

    # Vorlagen- bzw. Drucksachennummer. Vergabe über den Nummernkreis (Issue #150), danach
    # unveränderlich; leer, solange die Vergabe noch aussteht (z. B. „bei Freigabe“).
    reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Vorlagennummer",
        help_text="Wird automatisch aus dem Nummernkreis vergeben, z. B. 22-0593 oder V/0599/2024",
    )
    reference_assigned_at = models.DateTimeField(null=True, blank=True, verbose_name="Nummer vergeben am")

    # Unternummern (Issue #150): Ergänzung, Neufassung, Antwort … zu einer Bezugsvorlage
    RELATION_CHOICES = [
        ("supplement", "Ergänzung"),
        ("revision", "Neufassung"),
        ("amendment", "Änderungsantrag"),
        ("answer", "Antwort/Stellungnahme"),
        ("recommendation", "Beschlussempfehlung"),
    ]
    parent_paper = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="child_papers",
        verbose_name="Bezugsvorlage",
    )
    relation_type = models.CharField(max_length=20, choices=RELATION_CHOICES, blank=True, verbose_name="Art des Bezugs")
    sub_number = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="Unternummer")

    # Basic info
    name = models.CharField(max_length=500, verbose_name="Betreff")
    paper_type = models.CharField(
        max_length=100,
        choices=[
            ("proposal", "Beschlussvorlage"),
            ("report", "Mitteilungsvorlage"),
            ("motion", "Antrag"),
            ("inquiry", "Anfrage"),
            ("major_inquiry", "Große Anfrage"),
            ("answer", "Antwort/Stellungnahme"),
            ("recommendation", "Beschlussempfehlung"),
            ("amendment", "Änderungsantrag"),
            ("resolution", "Resolution"),
            ("bylaw", "Satzung"),
            ("budget", "Haushalt"),
            ("other", "Sonstiges"),
        ],
        default="proposal",
        verbose_name="Vorlagenart",
    )

    # Content
    main_text = models.TextField(blank=True, verbose_name="Sachverhalt")
    resolution_text = models.TextField(blank=True, verbose_name="Beschlussvorschlag")

    # Non-public content (encrypted)
    confidential_text_encrypted = EncryptedTextField(blank=True, null=True, verbose_name="Vertraulicher Inhalt")

    # Visibility
    is_public = models.BooleanField(
        default=True,
        verbose_name="Öffentlich",
        help_text="Wird über OParl-API veröffentlicht",
    )

    # Workflow
    status = models.CharField(
        max_length=50,
        choices=[
            ("draft", "Entwurf"),
            ("review", "In Prüfung"),
            ("approved", "Freigegeben"),
            ("scheduled", "Terminiert"),
            ("completed", "Abgeschlossen"),
            ("withdrawn", "Zurückgezogen"),
        ],
        default="draft",
        verbose_name="Status",
    )

    # Dates
    date = models.DateField(blank=True, null=True, verbose_name="Datum")
    deadline = models.DateField(blank=True, null=True, verbose_name="Frist")

    # References
    originator_organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originated_papers",
        verbose_name="Einreichendes Gremium",
    )
    originator_person = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originated_papers",
        verbose_name="Einreichende Person",
    )

    # Consultation chain
    main_organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="main_papers",
        verbose_name="Federführendes Gremium",
    )

    # Ämterstruktur und Mitzeichnung (Issue #81)
    lead_department = models.ForeignKey(
        SessionOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lead_papers",
        verbose_name="Federführendes Amt",
        help_text="Amt/Fachbereich, das die Vorlage erstellt hat",
    )
    # Pflichtangabe vor der Vorlage zur Freigabe; None = noch nicht erfasst
    has_financial_impact = models.BooleanField(null=True, blank=True, verbose_name="Finanzielle Auswirkungen")
    financial_impact_note = models.TextField(
        blank=True,
        verbose_name="Erläuterung der finanziellen Auswirkungen",
        help_text="z. B. Höhe, Haushaltsstelle, Deckung",
    )

    # Source (if from application)
    source_application = models.ForeignKey(
        "SessionApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_papers",
        verbose_name="Ursprungsantrag",
    )

    # Workflow tracking
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_papers",
        verbose_name="Erstellt von",
    )
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_papers",
        verbose_name="Freigegeben von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Freigegeben am")
    # Vier-Augen-Prinzip und Vertretung (Issue #222)
    content_edited_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Zuletzt inhaltlich bearbeitet von",
    )
    approved_on_behalf_of = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Freigegeben in Vertretung für",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_papers"
        verbose_name = "Vorlage"
        verbose_name_plural = "Vorlagen"
        ordering = ["-date", "-created_at"]
        constraints = [
            # Eindeutig je Mandant, sobald vergeben; mehrere Entwürfe ohne Nummer sind erlaubt
            models.UniqueConstraint(
                fields=["tenant", "reference"],
                condition=~models.Q(reference=""),
                name="uniq_session_paper_reference",
            ),
            models.UniqueConstraint(
                fields=["parent_paper", "sub_number"],
                condition=models.Q(sub_number__isnull=False),
                name="uniq_session_paper_sub_number",
            ),
        ]

    def __str__(self):
        return f"{self.reference or 'ohne Nummer'}: {self.name}"

    @property
    def display_reference(self) -> str:
        """Nummer für die Anzeige; vor der Vergabe ein klarer Hinweis statt eines leeren Felds."""
        return self.reference or "Nummer folgt"

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Körperschaft vor der Nummernvergabe: Sie wählt den Nummernkreis und füllt {koerperschaft} (Issue #756)
        if self.body_id is None and self.tenant_id is not None:
            from .services import body_service

            self.body = body_service.body_for_paper(self)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "body"}
        # Nummernvergabe (Issue #150) in derselben Transaktion wie das Speichern: scheitert das
        # Speichern, wird auch der Zähler zurückgesetzt – keine verbrannten Nummern.
        if self.reference:
            super().save(*args, **kwargs)
            return
        from django.db import transaction

        from .services import numbering_service

        with transaction.atomic():
            bestand = not self._state.adding
            vergeben = numbering_service.assign_if_due(self)
            if vergeben:
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = {*update_fields, "reference", "reference_assigned_at", "sub_number"}
            super().save(*args, **kwargs)
            if vergeben and bestand:
                from .services import agenda_service

                # TOPs, die vor der Vergabe aus der Beratungsfolge entstanden sind, tragen jetzt die Nummer
                agenda_service.sync_paper_item_names(self)

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.tenant


class SessionApplication(EncryptionMixin, models.Model):
    """
    Application (Antrag) from political organizations.

    This enables simple form-based submission of applications from
    parties/factions without requiring document uploads.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="applications",
        verbose_name="Mandant",
    )

    # Reference
    reference = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Eingangsnummer",
        help_text="Wird automatisch vergeben",
    )

    # Basic info
    title = models.CharField(max_length=500, verbose_name="Titel")
    application_type = models.CharField(
        max_length=100,
        choices=[
            ("motion", "Antrag"),
            ("inquiry", "Anfrage"),
            ("resolution", "Resolution"),
            ("urgent", "Dringlichkeitsantrag"),
            ("amendment", "Änderungsantrag"),
            ("other", "Sonstiges"),
        ],
        default="motion",
        verbose_name="Art des Antrags",
    )

    # Content
    justification = models.TextField(
        verbose_name="Begründung",
        help_text="Warum soll dieser Antrag beschlossen werden?",
    )
    resolution_proposal = models.TextField(
        verbose_name="Beschlussvorschlag",
        help_text="Was genau soll beschlossen werden?",
    )
    financial_impact = models.TextField(
        blank=True,
        verbose_name="Finanzielle Auswirkungen",
        help_text="Welche Kosten entstehen? (optional)",
    )

    # Additional content (encrypted for non-public)
    additional_info_encrypted = EncryptedTextField(
        blank=True, null=True, verbose_name="Zusätzliche vertrauliche Informationen"
    )

    # Submitter info
    submitting_organization = models.ForeignKey(
        "tenants.Organization",
        on_delete=models.SET_NULL,
        null=True,
        related_name="submitted_applications",
        verbose_name="Einreichende Organisation",
        help_text="Fraktion/Partei, die den Antrag einreicht",
    )
    # Einreichung per API-Token (Issue #316): Nur dieses Token (bzw. ein Token derselben verbundenen
    # Organisation) darf den Rückmeldestand über die Session-API abrufen.
    submitted_via_token = models.ForeignKey(
        "SessionAPIToken",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Eingereicht mit API-Token",
    )
    submitter_name = models.CharField(max_length=255, verbose_name="Name des Einreichers")
    submitter_email = models.EmailField(verbose_name="E-Mail des Einreichers")
    submitter_phone = models.CharField(max_length=50, blank=True, verbose_name="Telefon des Einreichers")

    # Co-signers
    co_signers = models.TextField(
        blank=True,
        verbose_name="Mitunterzeichner",
        help_text="Namen der Mitunterzeichner (einer pro Zeile)",
    )

    # Target committee
    target_organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_applications",
        verbose_name="Zielgremium",
        help_text="An welches Gremium soll der Antrag gehen?",
    )

    # Status
    status = models.CharField(
        max_length=50,
        choices=[
            ("submitted", "Eingereicht"),
            ("received", "Eingegangen"),
            ("in_review", "In Prüfung"),
            ("accepted", "Angenommen"),
            ("rejected", "Abgelehnt"),
            ("converted", "In Vorlage umgewandelt"),
            ("withdrawn", "Zurückgezogen"),
        ],
        default="submitted",
        verbose_name="Status",
    )

    # Processing
    received_at = models.DateTimeField(blank=True, null=True, verbose_name="Eingegangen am")
    received_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_applications",
        verbose_name="Bearbeitet von",
    )
    processing_notes = models.TextField(blank=True, verbose_name="Bearbeitungsnotizen")

    # Urgency
    is_urgent = models.BooleanField(
        default=False,
        verbose_name="Dringend",
        help_text="Soll der Antrag bevorzugt behandelt werden?",
    )
    urgency_reason = models.TextField(blank=True, verbose_name="Begründung der Dringlichkeit")

    # Deadline
    deadline = models.DateField(
        blank=True,
        null=True,
        verbose_name="Gewünschter Beratungstermin",
        help_text="Bis wann soll der Antrag behandelt werden?",
    )

    # Timestamps
    submitted_at = models.DateTimeField(auto_now_add=True, verbose_name="Eingereicht am")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_applications"
        verbose_name = "Antrag"
        verbose_name_plural = "Anträge"
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "reference"],
                condition=~models.Q(reference=""),
                name="uniq_session_application_reference",
            ),
        ]

    def __str__(self):
        return f"{self.reference or 'NEU'}: {self.title}"

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.tenant

    def save(self, *args, **kwargs):
        # Auto-generate reference on first save.
        # Sicherheit: Die Vergabe läuft in einer Transaktion mit Zeilen-Lock
        # auf dem Tenant, damit parallele Einreichungen keine doppelten
        # Eingangsnummern erzeugen. Zusätzlich sichert der UniqueConstraint
        # (tenant, reference) auf DB-Ebene ab.
        if not self.reference:
            from django.db import transaction

            with transaction.atomic():
                # Serialisiert die Nummernvergabe pro Mandant
                SessionTenant.objects.select_for_update().get(pk=self.tenant_id)
                self.reference = self._next_reference()
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def _next_reference(self) -> str:
        """Nächste freie Eingangsnummer für Tenant + Jahr (numerisch ermittelt, Jahr in Ortszeit)."""
        year = timezone.localdate().year
        prefix = f"A/{year}/"
        max_num = 0
        refs = SessionApplication.objects.filter(
            tenant_id=self.tenant_id,
            reference__startswith=prefix,
        ).values_list("reference", flat=True)
        for ref in refs:
            try:
                num = int(ref.rsplit("/", 1)[-1])
            except (TypeError, ValueError):
                continue
            max_num = max(max_num, num)
        return f"{prefix}{max_num + 1:04d}"


# =============================================================================
# LEGISLATIVE TERM (Wahlperioden, Issue #35)
# =============================================================================


class SessionLegislativeTerm(models.Model):
    """
    Wahlperiode eines Mandanten (Issue #35).

    Wird in der Session-OParl-API als ``legislativeTerm`` am Body
    ausgeliefert. Pflege unter Einstellungen → Wahlperioden.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="legislative_terms",
        verbose_name="Mandant",
    )

    name = models.CharField(max_length=255, verbose_name="Name", help_text="z. B. Wahlperiode 2025–2030")
    # Nummer der Wahlperiode für Drucksachennummern wie „22-0593“ (Platzhalter {wp}, Issue #150)
    number = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Nummer",
        help_text="z. B. 22 für die 22. Wahlperiode; Grundlage des Platzhalters {wp} in Nummernkreisen",
    )
    start_date = models.DateField(blank=True, null=True, verbose_name="Beginn")
    end_date = models.DateField(blank=True, null=True, verbose_name="Ende")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_legislative_terms"
        verbose_name = "Wahlperiode"
        verbose_name_plural = "Wahlperioden"
        ordering = ["-start_date", "name"]

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"

    def contains(self, date) -> bool:
        """Liegt das Datum im Zeitraum der Periode (offene Grenzen zählen mit)?"""
        if date is None:
            return False
        if self.start_date and date < self.start_date:
            return False
        if self.end_date and date > self.end_date:
            return False
        return bool(self.start_date or self.end_date)

    def overlaps(self, start, end) -> bool:
        """Überschneidet sich die Periode mit dem Zeitraum [start, end]? Offene Grenzen gelten als unbegrenzt."""
        if self.end_date is not None and start is not None and self.end_date < start:
            return False
        return not (self.start_date is not None and end is not None and end < self.start_date)

    @property
    def is_current(self) -> bool:
        """Umfasst die Periode das heutige Datum?"""
        return self.contains(timezone.localdate())

    @classmethod
    def for_date(cls, tenant, date, *, fallback=True):
        """
        Passende Wahlperiode eines Mandanten zu einem Datum (Issue #39).

        Fallback: aktuelle Periode (enthält heute), sonst die jüngste. Ohne ``fallback`` nur die
        Periode, die das Datum enthält, sonst None (Besetzungen außerhalb jeder Periode).
        Gibt None zurück, wenn der Mandant keine Perioden pflegt.
        """
        terms = list(cls.objects.filter(tenant=tenant))
        if not terms:
            return None
        if date is not None:
            for term in terms:
                if term.contains(date):
                    return term
        return cls.current_for(tenant) if fallback else None

    @classmethod
    def current_for(cls, tenant):
        """Aktuelle Wahlperiode des Mandanten (enthält heute, sonst die jüngste)."""
        terms = list(cls.objects.filter(tenant=tenant))
        if not terms:
            return None
        today = timezone.localdate()
        for term in terms:
            if term.contains(today):
                return term
        # Jüngste Periode: höchstes Startdatum zuerst, Perioden ohne Datum zuletzt
        terms.sort(key=lambda t: (t.start_date is not None, t.start_date or timezone.localdate()), reverse=True)
        return terms[0]


# =============================================================================
# OPARL TOMBSTONES (Issue #35)
# =============================================================================


class SessionOParlTombstone(models.Model):
    """
    Tombstone für die Session-OParl-API (Issue #35, OParl 1.1 §2.8).

    Objekte, die einmal öffentlich über die OParl-API ausgeliefert wurden
    und danach gelöscht oder auf „nicht öffentlich“ gestellt werden,
    hinterlassen hier einen Grabstein. Die API liefert dafür weiterhin
    HTTP 200 mit dem gekürzten Objekt (``deleted: true``) und nimmt die
    Tombstones in ``modified_since``-Listen auf, damit inkrementelle
    Clients (z. B. der Insight-Ingestor) Löschungen zuverlässig mitbekommen.

    Sicherheit: Ein Tombstone enthält KEINE Inhalte — nur Objekttyp, ID
    und Zeitstempel.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="oparl_tombstones",
        verbose_name="Mandant",
    )

    # Objekttyp = URL-Segment der API (meeting, paper, file, agendaitem, …)
    oparl_type = models.CharField(max_length=50, verbose_name="Objekttyp")
    object_id = models.UUIDField(verbose_name="Objekt-ID")

    # created des ursprünglichen Objekts (für das Pflichtfeld created)
    object_created_at = models.DateTimeField(verbose_name="Objekt erstellt am")
    deleted_at = models.DateTimeField(default=timezone.now, verbose_name="Gelöscht am")

    class Meta:
        db_table = "session_oparl_tombstones"
        verbose_name = "OParl-Tombstone"
        verbose_name_plural = "OParl-Tombstones"
        unique_together = ["tenant", "oparl_type", "object_id"]
        indexes = [
            models.Index(fields=["tenant", "oparl_type", "deleted_at"]),
        ]

    def __str__(self):
        return f"Tombstone {self.oparl_type}/{self.object_id} ({self.tenant.slug})"


# =============================================================================
# CONSULTATION MODELS (Beratungsfolge, Issue #34)
# =============================================================================


class SessionConsultation(models.Model):
    """
    Beratungsstation einer Vorlage (Beratungsfolge, Issue #34).

    Bildet den realen Entscheidungsweg einer Vorlage über mehrere Gremien ab
    (z. B. Fachausschuss -> Hauptausschuss -> Rat). Entspricht der OParl-
    Entität ``Consultation``: Rolle (Vorberatung/Anhörung/Entscheidung),
    ``authoritative``-Flag für die entscheidende Station, Verknüpfung zum
    Tagesordnungspunkt, sobald die Station terminiert ist, und Ergebnis je
    Station (wird beim Erfassen des Abstimmungsergebnisses am TOP
    automatisch zurückgeschrieben, siehe signals.sync_consultation_result).
    """

    ROLE_CHOICES = [
        ("preliminary", "Vorberatung"),
        ("hearing", "Anhörung"),
        ("decision", "Entscheidung"),
        ("information", "Kenntnisnahme"),
    ]

    # Ergebnis-Auswahl entspricht SessionAgendaItem.vote_result, damit die
    # Rückschreibung 1:1 möglich ist.
    RESULT_CHOICES = [
        ("pending", "Ausstehend"),
        ("approved", "Angenommen"),
        ("rejected", "Abgelehnt"),
        ("deferred", "Vertagt"),
        ("withdrawn", "Zurückgezogen"),
        ("noted", "Zur Kenntnis genommen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    paper = models.ForeignKey(
        SessionPaper,
        on_delete=models.CASCADE,
        related_name="consultations",
        verbose_name="Vorlage",
    )
    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="consultations",
        verbose_name="Gremium",
    )

    # Terminierung: Zielsitzung und (sobald angelegt) der zugehörige TOP
    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consultations",
        verbose_name="Sitzung",
        help_text="Zielsitzung dieser Beratungsstation (sobald terminiert)",
    )
    agenda_item = models.OneToOneField(
        SessionAgendaItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consultation",
        verbose_name="Tagesordnungspunkt",
    )

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default="preliminary",
        verbose_name="Rolle",
    )
    authoritative = models.BooleanField(
        default=False,
        verbose_name="Entscheidende Beratung",
        help_text="OParl: authoritative — die Station, die abschließend entscheidet",
    )
    order = models.PositiveIntegerField(default=1, verbose_name="Reihenfolge")

    result = models.CharField(
        max_length=50,
        choices=RESULT_CHOICES,
        default="pending",
        verbose_name="Ergebnis",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_consultations"
        verbose_name = "Beratungsstation"
        verbose_name_plural = "Beratungsfolge"
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"{self.paper.reference} – Station {self.order}: {self.organization.name} ({self.get_role_display()})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Das Ergebnis einer Station mit gesperrtem TOP folgt nur dem TOP (Issue #318)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_consultation(self, kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    @property
    def tenant(self):
        """Tenant der Station (für Audit-Attribution und Filterung)."""
        return self.paper.tenant

    @property
    def is_scheduled(self) -> bool:
        """Station terminiert (TOP angelegt)?"""
        return self.agenda_item_id is not None

    @property
    def is_done(self) -> bool:
        """Station abgeschlossen (Ergebnis liegt vor)?"""
        return self.result != "pending"


# =============================================================================
# PROTOCOL MODELS
# =============================================================================


class SessionProtocol(EncryptionMixin, models.Model):
    """
    Meeting protocol.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.OneToOneField(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="protocol",
        verbose_name="Sitzung",
    )

    # Content
    content = models.TextField(blank=True, verbose_name="Protokollinhalt")
    content_encrypted = EncryptedTextField(
        blank=True,
        null=True,
        verbose_name="Nicht-öffentlicher Protokollinhalt",
    )

    # Status
    status = models.CharField(
        max_length=50,
        choices=[
            ("draft", "Entwurf"),
            ("review", "Zur Prüfung"),
            ("approved", "Genehmigt"),
            ("published", "Veröffentlicht"),
        ],
        default="draft",
        verbose_name="Status",
    )

    # Workflow
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_protocols",
        verbose_name="Erstellt von",
    )
    review_requested_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_protocols",
        verbose_name="Zur Prüfung gegeben von",
    )
    review_requested_at = models.DateTimeField(blank=True, null=True, verbose_name="Zur Prüfung gegeben am")
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_protocols",
        verbose_name="Genehmigt von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Genehmigt am")
    # Vier-Augen-Prinzip und Vertretung (Issue #222)
    content_edited_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Zuletzt inhaltlich bearbeitet von",
    )
    approved_on_behalf_of = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Genehmigt in Vertretung für",
    )
    published_at = models.DateTimeField(blank=True, null=True, verbose_name="Veröffentlicht am")

    # Genehmigungsvermerk (Issue #31): Genehmigung erfolgt üblicherweise in
    # der Folgesitzung des Gremiums
    approval_meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_protocols",
        verbose_name="Genehmigt in Sitzung",
        help_text="Folgesitzung, in der die Niederschrift genehmigt wurde",
    )
    approval_note = models.CharField(max_length=500, blank=True, verbose_name="Genehmigungsvermerk")
    # TOP „Genehmigung der Niederschrift“ der Folgesitzung desselben Gremiums (Issue #318)
    approval_agenda_item = models.ForeignKey(
        "SessionAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="protocol_approvals",
        verbose_name="Genehmigt unter TOP",
    )

    # Öffentliche Fassung als Datei an der Sitzung (Issue #318): OParl resultsProtocol und
    # Bürgerportal. Entsteht beim Veröffentlichen, wird bei Berichtigung neu erzeugt und bei
    # Rücknahme gelöscht (Tombstone, sofortige Rücknahme aus dem Bürgerportal).
    public_file = models.OneToOneField(
        "SessionFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="public_protocol",
        verbose_name="Öffentliche Fassung (Datei)",
    )

    # Unterschriften-Block (Vorsitz + Protokollführung)
    chair_name = models.CharField(max_length=255, blank=True, verbose_name="Vorsitz (Unterschrift)")
    recorder_name = models.CharField(max_length=255, blank=True, verbose_name="Protokollführung (Unterschrift)")

    # Verlauf (eröffnet, geschlossen) und Behandlungszeiten der TOPs aus dem Sitzungscockpit (Issue #140)
    # ausweisen. Neue Niederschriften ja; bei der Einführung bereits genehmigte bzw. veröffentlichte nicht,
    # damit sich ihr Inhalt nicht nachträglich ändert (Migration 0050, DB-Default für ältere Stände).
    show_timings = models.BooleanField(default=True, db_default=False, verbose_name="Verlauf und TOP-Zeiten ausweisen")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_protocols"
        verbose_name = "Protokoll"
        verbose_name_plural = "Protokolle"

    def __str__(self):
        return f"Protokoll: {self.meeting}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Genehmigte Niederschrift: Inhalt gesperrt, kein Rückfall in Entwurf oder Prüfung (Issue #318)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_protocol(self, kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Eine genehmigte Niederschrift lässt sich nicht löschen (Issue #318)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_protocol_delete(self)
        return super().delete(*args, **kwargs)

    def get_encryption_organization(self):
        """Return tenant for encryption."""
        return self.meeting.tenant

    @property
    def is_locked(self) -> bool:
        """Genehmigt oder veröffentlicht: Ergebnis, Stimmen und Texte sind schreibgeschützt (Issue #318)."""
        from apps.session.services import protocol_lock

        return self.status in protocol_lock.LOCKED_STATUSES


class SessionProtocolCorrection(EncryptionMixin, models.Model):
    """
    Berichtigung einer genehmigten Niederschrift (Issue #318).

    Nach der Genehmigung sind Ergebnis, Stimmen und Texte gesperrt; Korrekturen laufen nur über
    diesen dokumentierten Vorgang mit Grund und geänderten Werten. Ist das Vier-Augen-Prinzip
    für Niederschriften aktiv, wird eine Berichtigung erst wirksam, wenn eine zweite Person sie
    bestätigt; sonst sofort.

    Datenschutz: ``changes`` enthält nur unverschlüsselte Werte und dient der Anzeige, bei
    Berichtigungen des öffentlichen Teils auch in der öffentlichen Niederschrift. Die vollständigen
    alten und neuen Werte (auch nichtöffentliche) liegen verschlüsselt in ``payload_encrypted``.
    Der Grund einer Berichtigung des nichtöffentlichen Teils steht nur verschlüsselt in
    ``reason_encrypted``.
    """

    STATUS_PENDING = "pending"
    STATUS_APPLIED = "applied"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Zur Bestätigung"),
        (STATUS_APPLIED, "Wirksam"),
        (STATUS_REJECTED, "Abgelehnt"),
    ]
    TARGET_ITEM = "item"
    TARGET_GENERAL = "general"
    TARGET_GENERAL_NP = "general_np"
    TARGET_CHOICES = [
        (TARGET_ITEM, "Tagesordnungspunkt"),
        (TARGET_GENERAL, "Allgemeiner Teil"),
        (TARGET_GENERAL_NP, "Allgemeiner Teil (nichtöffentlich)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    protocol = models.ForeignKey(
        SessionProtocol,
        on_delete=models.CASCADE,
        related_name="corrections",
        verbose_name="Niederschrift",
    )
    target = models.CharField(max_length=20, choices=TARGET_CHOICES, verbose_name="Gegenstand")
    agenda_item = models.ForeignKey(
        "SessionAgendaItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="protocol_corrections",
        verbose_name="Tagesordnungspunkt",
    )
    is_public = models.BooleanField(
        default=False,
        verbose_name="Betrifft den öffentlichen Teil",
        help_text="Erscheint mit Datum und Grund in der öffentlichen Niederschrift",
    )
    reason = models.TextField(blank=True, verbose_name="Grund")
    reason_encrypted = EncryptedTextField(verbose_name="Grund (nichtöffentlich, verschlüsselt)")
    changes = models.JSONField(default=list, blank=True, verbose_name="Änderungen (Anzeige)")
    payload_encrypted = EncryptedTextField(verbose_name="Alte und neue Werte (verschlüsselt)")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name="Status")
    requested_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Berichtigt von",
    )
    requested_on_behalf_of = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Berichtigt in Vertretung für",
    )
    requested_at = models.DateTimeField(default=timezone.now, verbose_name="Berichtigt am")
    decided_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Bestätigt bzw. abgelehnt von",
    )
    decided_on_behalf_of = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Entschieden in Vertretung für",
    )
    decided_at = models.DateTimeField(blank=True, null=True, verbose_name="Entschieden am")
    decision_note = models.CharField(max_length=500, blank=True, verbose_name="Vermerk zur Entscheidung")
    applied_at = models.DateTimeField(blank=True, null=True, verbose_name="Wirksam seit")

    class Meta:
        db_table = "session_protocol_corrections"
        verbose_name = "Berichtigung der Niederschrift"
        verbose_name_plural = "Berichtigungen der Niederschrift"
        ordering = ["requested_at"]
        indexes = [models.Index(fields=["protocol", "status"], name="session_corr_protocol_status")]

    def __str__(self):
        subject = f"TOP {self.agenda_item.number}" if self.agenda_item_id and self.agenda_item else "Allgemeiner Teil"
        return f"Berichtigung vom {timezone.localtime(self.requested_at):%d.%m.%Y}, {subject}"

    def get_encryption_organization(self):
        """Mandanten-Schlüssel der Sitzung."""
        return self.protocol.meeting.tenant

    @property
    def tenant(self):
        """Mandant (Audit-Attribution)."""
        return self.protocol.meeting.tenant

    @property
    def subject_label(self) -> str:
        """Gegenstand für Anzeige und Niederschrift, z. B. „TOP 3“."""
        if self.target == self.TARGET_ITEM and self.agenda_item_id and self.agenda_item is not None:
            return f"TOP {self.agenda_item.number}"
        return self.get_target_display()


# =============================================================================
# ATTENDANCE & ALLOWANCE MODELS
# =============================================================================


class SessionAttendance(EncryptionMixin, models.Model):
    """
    Attendance record for a meeting.

    Rückmeldungen der Mandatstragenden (Issue #225) landen hier: Zusage
    (status="confirmed") oder Absage (status="declined", optional mit Grund und
    Vertretungswunsch) mit Zeitstempel und Herkunft (Link, Portal, Sitzungsdienst).
    Der Grund ist verschlüsselt und nur für den Sitzungsdienst sichtbar – weder
    OParl noch öffentliche Seiten geben ihn aus.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="attendances",
        verbose_name="Sitzung",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="attendances",
        verbose_name="Person",
    )

    # Attendance status
    status = models.CharField(
        max_length=50,
        choices=[
            ("invited", "Eingeladen"),
            ("confirmed", "Zugesagt"),
            ("declined", "Abgesagt"),
            ("present", "Anwesend"),
            ("absent", "Abwesend"),
            ("excused", "Entschuldigt"),
            ("left_early", "Vorzeitig gegangen"),
            ("joined_late", "Verspätet"),
        ],
        default="invited",
        verbose_name="Status",
    )

    # Teilnahmeart (Issue #139): im Sitzungsraum oder per Bild-Ton-Übertragung zugeschaltet (nur hybride
    # und digitale Sitzungen). Bei Zugeschalteten sind Ankunft und Abgang Zuschaltung und Trennung;
    # Unterbrechungen stehen als Störungsvermerke (SessionAttendanceDisruption) daneben.
    PARTICIPATION_IN_PERSON = "in_person"
    PARTICIPATION_REMOTE = "remote"
    PARTICIPATION_CHOICES = [
        (PARTICIPATION_IN_PERSON, "Vor Ort"),
        (PARTICIPATION_REMOTE, "Zugeschaltet"),
    ]
    participation_mode = models.CharField(
        max_length=10,
        choices=PARTICIPATION_CHOICES,
        default=PARTICIPATION_IN_PERSON,
        db_default=PARTICIPATION_IN_PERSON,
        verbose_name="Teilnahmeart",
    )

    # Timing
    arrival_time = models.TimeField(blank=True, null=True, verbose_name="Ankunft")
    departure_time = models.TimeField(blank=True, null=True, verbose_name="Abgang")
    # Unterbrechungen (Issue #140): gegangen und zurückgekommen, je Zeitraum {"left": "18:30", "returned": "18:50"}.
    # Das Sitzungscockpit schreibt sie beim Wechsel „zurück“; die Niederschrift weist sie aus
    # (participation_service.participation_note). NULL: Zeile aus einem älteren Stand, gleichbedeutend mit leer.
    interruptions = models.JSONField(
        default=list, blank=True, null=True, verbose_name="Unterbrechungen der Anwesenheit"
    )

    # Notes
    notes = models.TextField(blank=True, verbose_name="Notizen")
    excuse_reason = models.TextField(blank=True, verbose_name="Entschuldigungsgrund")

    # Role in this meeting
    role = models.CharField(
        max_length=50,
        choices=[
            ("member", "Mitglied"),
            ("chair", "Vorsitz"),
            ("deputy_chair", "Stellv. Vorsitz"),
            ("guest", "Gast"),
            ("expert", "Sachverständige/r"),
            ("recorder", "Protokollant/in"),
        ],
        default="member",
        verbose_name="Funktion",
    )

    # Voting rights (can be different from organization membership)
    has_voting_rights = models.BooleanField(default=True, verbose_name="Stimmberechtigt")

    # Rückmeldung der Person (Issue #225)
    responded_at = models.DateTimeField(blank=True, null=True, verbose_name="Rückmeldung am")
    response_source = models.CharField(
        max_length=10,
        choices=RESPONSE_SOURCE_CHOICES,
        blank=True,
        verbose_name="Rückmeldung über",
    )
    substitute_requested = models.BooleanField(default=False, verbose_name="Vertretung erbeten")
    response_reason_encrypted = EncryptedTextField(verbose_name="Grund der Absage (verschlüsselt)")
    substitutes_notified_at = models.DateTimeField(
        blank=True, null=True, verbose_name="Stellvertretung benachrichtigt am"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_attendances"
        verbose_name = "Anwesenheit"
        verbose_name_plural = "Anwesenheiten"
        unique_together = ["meeting", "person"]
        ordering = ["person__family_name"]

    def __str__(self):
        return f"{self.person} - {self.meeting}: {self.status}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Nach der Genehmigung der Niederschrift ist die Anwesenheit gesperrt (Teilnehmerverzeichnis)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_attendance(self, kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Zeilen einer Sitzung mit genehmigter Niederschrift lassen sich nicht entfernen."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_attendance_delete(self)
        return super().delete(*args, **kwargs)

    @property
    def is_remote(self) -> bool:
        """Per Bild-Ton-Übertragung zugeschaltet? (Issue #139)."""
        return self.participation_mode == self.PARTICIPATION_REMOTE

    def get_encryption_organization(self):
        """Mandanten-Schlüssel der Sitzung für den Absagegrund."""
        return self.meeting.tenant


class SessionAttendanceDisruption(models.Model):
    """
    Störungsvermerk einer zugeschalteten Person (Issue #139).

    Zugeschaltete gelten nur als anwesend, solange sie sehen und hören und gesehen und gehört werden.
    Eine Störung (Beginn, Ende, Ursache) nimmt die Person für ihre Dauer aus der Beschlussfähigkeit und
    der Stimmabgabe; ohne Ende dauert sie an. Niederschrift und Anwesenheitsliste weisen sie mit Zeiten
    und Ursache aus – den freien Vermerk nur intern.
    """

    CAUSE_CONNECTION = "connection"
    CAUSE_AUDIO = "audio"
    CAUSE_VIDEO = "video"
    CAUSE_OTHER = "other"
    CAUSE_CHOICES = [
        (CAUSE_CONNECTION, "Verbindung abgebrochen"),
        (CAUSE_AUDIO, "Ton gestört"),
        (CAUSE_VIDEO, "Bild gestört"),
        (CAUSE_OTHER, "Sonstige Ursache"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    attendance = models.ForeignKey(
        SessionAttendance,
        on_delete=models.CASCADE,
        related_name="disruptions",
        verbose_name="Anwesenheit",
    )
    started_at = models.TimeField(verbose_name="Beginn")
    ended_at = models.TimeField(blank=True, null=True, verbose_name="Ende", help_text="Leer: Die Störung dauert an")
    cause = models.CharField(max_length=20, choices=CAUSE_CHOICES, default=CAUSE_CONNECTION, verbose_name="Ursache")
    note = models.CharField(max_length=255, blank=True, verbose_name="Vermerk", help_text="Nur intern")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_attendance_disruptions"
        verbose_name = "Störungsvermerk"
        verbose_name_plural = "Störungsvermerke"
        ordering = ["started_at", "created_at"]

    def __str__(self):
        return f"{self.attendance.person}: {self.get_cause_display()} ab {self.started_at:%H:%M}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Störungsvermerke einer genehmigten Niederschrift sind gesperrt (Teilnehmerverzeichnis)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_disruption(self, kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        from apps.session.services import protocol_lock

        protocol_lock.guard_disruption(self, deleting=True)
        return super().delete(*args, **kwargs)

    @property
    def meeting_id(self):
        """Sitzung der Störung (Audit, Sperre und öffentliche Fassung der Niederschrift)."""
        return self.attendance.meeting_id

    @property
    def ongoing(self) -> bool:
        return self.ended_at is None

    @property
    def duration_minutes(self) -> int | None:
        """Dauer in Minuten (über Mitternacht fortgesetzt); ``None``, solange die Störung andauert."""
        if self.ended_at is None:
            return None
        start = self.started_at.hour * 60 + self.started_at.minute
        end = self.ended_at.hour * 60 + self.ended_at.minute
        return (end - start) % (24 * 60)

    def covers(self, moment) -> bool:
        """Dauert die Störung zu diesem Zeitpunkt (Uhrzeit) an?"""
        if self.ended_at is None:
            return moment >= self.started_at
        if self.ended_at >= self.started_at:
            return self.started_at <= moment < self.ended_at
        return moment >= self.started_at or moment < self.ended_at


class SessionAllowanceRate(models.Model):
    """
    Entschädigungssatz je Gremium und Funktion (Issue #38).

    Der Abrechnungslauf zieht für jede anrechenbare Anwesenheit den Satz
    der Funktion (Vorsitz, Mitglied, …) im Gremium heran; ohne
    Funktions-Satz gilt das Standard-Sitzungsgeld des Gremiums
    (``SessionOrganization.allowance_amount``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="allowance_rates",
        verbose_name="Gremium",
    )
    role = models.CharField(
        max_length=50,
        choices=[
            ("member", "Mitglied"),
            ("chair", "Vorsitz"),
            ("deputy_chair", "Stellv. Vorsitz"),
            ("expert", "Sachverständige/r"),
            ("recorder", "Protokollant/in"),
        ],
        default="member",
        verbose_name="Funktion",
    )
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Betrag")
    currency = models.CharField(max_length=3, default="EUR", verbose_name="Währung")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_allowance_rates"
        verbose_name = "Entschädigungssatz"
        verbose_name_plural = "Entschädigungssätze"
        unique_together = ["organization", "role"]
        ordering = ["organization__name", "role"]

    def __str__(self):
        return f"{self.organization.name} / {self.get_role_display()}: {self.amount} {self.currency}"


class SessionAllowance(models.Model):
    """
    Allowance payment for meeting attendance.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    attendance = models.OneToOneField(
        SessionAttendance,
        on_delete=models.CASCADE,
        related_name="allowance",
        verbose_name="Anwesenheit",
    )

    # Amount
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Betrag")
    currency = models.CharField(max_length=3, default="EUR", verbose_name="Währung")

    # Status
    status = models.CharField(
        max_length=50,
        choices=[
            ("pending", "Ausstehend"),
            ("approved", "Genehmigt"),
            ("paid", "Ausgezahlt"),
            ("cancelled", "Storniert"),
        ],
        default="pending",
        verbose_name="Status",
    )

    # Vier-Augen-Prinzip (Issue #38): Wer den Abrechnungslauf erzeugt hat,
    # darf die entstandenen Positionen nicht selbst genehmigen
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_allowances",
        verbose_name="Erzeugt von",
    )

    # Payment tracking
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_allowances",
        verbose_name="Genehmigt von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Genehmigt am")
    paid_at = models.DateTimeField(blank=True, null=True, verbose_name="Ausgezahlt am")

    # Export reference (for accounting systems)
    export_reference = models.CharField(max_length=100, blank=True, verbose_name="Export-Referenz")
    export_date = models.DateTimeField(blank=True, null=True, verbose_name="Export-Datum")

    notes = models.TextField(blank=True, verbose_name="Notizen")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_allowances"
        verbose_name = "Sitzungsgeld"
        verbose_name_plural = "Sitzungsgelder"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.attendance.person}: {self.amount} {self.currency}"


# =============================================================================
# FILE MODELS
# =============================================================================


class SessionFile(models.Model):
    """
    File/document attachment.

    Files can be attached to papers, agenda items, or meetings.
    """

    # Sichtbarkeit nach der Anlagenregel (file_service.file_visible): SessionFile.objects.visible_to(permissions)
    objects = FileQuerySet.as_manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="files",
        verbose_name="Mandant",
    )

    # File info
    name = models.CharField(max_length=500, verbose_name="Name")
    file = models.FileField(
        upload_to="session/files/%Y/%m/",
        verbose_name="Datei",
    )
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="MIME-Typ")
    size = models.PositiveBigIntegerField(default=0, verbose_name="Größe (Bytes)")

    # Extracted text (for search)
    text_content = models.TextField(blank=True, verbose_name="Textinhalt")

    # Nummer der aktuellen Fassung; Ersetzen erhöht sie, frühere Fassungen in SessionFileVersion (Issue #226)
    version = models.PositiveIntegerField(default=1, verbose_name="Version")

    # Visibility
    is_public = models.BooleanField(default=True, verbose_name="Öffentlich")

    # Relationships
    paper = models.ForeignKey(
        SessionPaper,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="files",
        verbose_name="Vorlage",
    )
    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="files",
        verbose_name="Sitzung",
    )
    agenda_item = models.ForeignKey(
        SessionAgendaItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="files",
        verbose_name="Tagesordnungspunkt",
    )
    # Anhänge eines aus mandari Work eingereichten Antrags (Issue #584): nichtöffentlich, bei der
    # Umwandlung in eine Vorlage hängen sie zusätzlich an der Vorlage
    application = models.ForeignKey(
        "SessionApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="files",
        verbose_name="Antrag",
    )

    # OParl link
    oparl_file = models.OneToOneField(
        "insight_core.OParlFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_file",
        verbose_name="OParl-Datei",
    )

    # Metadata
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_files",
        verbose_name="Hochgeladen von",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_files"
        verbose_name = "Datei"
        verbose_name_plural = "Dateien"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def size_human(self) -> str:
        """Human-readable file size."""
        return human_size(self.size)


# =============================================================================
# FASSUNGEN VON VORLAGEN UND ANLAGEN (Issue #226)
# =============================================================================


class ImmutableRecord(models.Model):
    """
    Revisionssicherer Eintrag: nach dem Anlegen weder änderbar noch einzeln löschbar (Issue #226).

    Verschwinden kann er nur mit seinem Elternobjekt (Vorlage, Anlage, Mandant) über die
    Kaskade der Datenbank – solange es die Vorlage gibt, bleibt jede gesicherte Fassung,
    insbesondere die beschlossene, genau so, wie sie entstanden ist.
    """

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValueError(f"{self._meta.verbose_name} ist unveränderlich und kann nicht geändert werden.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise ValueError(f"{self._meta.verbose_name} ist unveränderlich und kann nicht einzeln gelöscht werden.")


class SessionFileBlob(models.Model):
    """
    Gespeicherter Dateiinhalt, je Mandant eindeutig über die SHA-256-Prüfsumme (Issue #226).

    Speicherkonzept: Jeder Inhalt liegt je Mandant genau einmal im Speicher. Die Anlage
    (``SessionFile.file``), ihre Fassungen und die Fassungen der Vorlagen verweisen auf
    denselben Speichernamen – wer eine Anlage durch eine schon bekannte Datei ersetzt oder
    eine ältere Fassung wiederherstellt, legt nichts doppelt ab. Gelöscht wird ein Inhalt
    erst, wenn nichts mehr auf ihn verweist (``file_version_service.collect_garbage``).

    Datenschutz-Löschung (``purged_at``): Der Inhalt verschwindet aus dem Speicher,
    Prüfsumme und Größe bleiben als Nachweis, Fassungen zeigen „Inhalt gelöscht“.

    Die Dateien liegen unter ``session/files/`` und sind nie direkt über /media/ abrufbar
    (PROTECTED_MEDIA_PREFIXES), nur über die zugriffsgeprüften Download-Views.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="file_blobs",
        verbose_name="Mandant",
    )
    sha256 = models.CharField(max_length=64, verbose_name="SHA-256")
    size = models.PositiveBigIntegerField(default=0, verbose_name="Größe (Bytes)")
    file = models.FileField(upload_to="session/files/%Y/%m/", blank=True, verbose_name="Datei")
    created_at = models.DateTimeField(auto_now_add=True)

    purged_at = models.DateTimeField(null=True, blank=True, verbose_name="Inhalt gelöscht am")
    purged_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Inhalt gelöscht von",
    )
    purge_reason = models.CharField(max_length=300, blank=True, verbose_name="Grund der Löschung")

    class Meta:
        db_table = "session_file_blobs"
        verbose_name = "Dateiinhalt"
        verbose_name_plural = "Dateiinhalte"
        constraints = [
            # Gelöschte Inhalte zählen nicht: Wird dieselbe Datei später neu hochgeladen, entsteht ein neuer Eintrag
            models.UniqueConstraint(
                fields=["tenant", "sha256"],
                condition=models.Q(purged_at__isnull=True),
                name="uniq_session_file_blob_sha256",
            ),
        ]
        indexes = [models.Index(fields=["file"], name="session_blob_file_idx")]

    def __str__(self) -> str:
        return f"Dateiinhalt {self.sha256[:12]}"

    @property
    def is_available(self) -> bool:
        return self.purged_at is None and bool(self.file)


class SessionFileVersion(ImmutableRecord):
    """
    Fassung einer Anlage (Issue #226): jeder gespeicherte Inhalt mit Nummer, Zeitpunkt und Urheber.

    Beim Ersetzen entsteht eine neue Fassung, die bisherige bleibt abrufbar. Sichtbar ist eine
    Fassung genau für die, die die Anlage selbst sehen dürfen (``file_service.file_visible``).
    Anlagen aus der Zeit vor der Versionierung werden beim ersten Bedarf nachträglich erfasst
    (``file_version_service.ensure_current_version``) – ältere Inhalte kennt das System dann nicht.
    Mit der Anlage verschwindet ihr Verlauf; Inhalte, die in einer Fassung der Vorlage stecken,
    bleiben dort erhalten (``SessionPaperVersionFile``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="file_versions",
        verbose_name="Mandant",
    )
    session_file = models.ForeignKey(
        SessionFile,
        on_delete=models.CASCADE,
        related_name="versions",
        verbose_name="Anlage",
    )
    number = models.PositiveIntegerField(verbose_name="Fassung")
    blob = models.ForeignKey(
        SessionFileBlob,
        on_delete=models.RESTRICT,
        related_name="file_versions",
        verbose_name="Inhalt",
    )
    name = models.CharField(max_length=500, verbose_name="Name")
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="MIME-Typ")
    size = models.PositiveBigIntegerField(default=0, verbose_name="Größe (Bytes)")
    note = models.CharField(max_length=200, blank=True, verbose_name="Hinweis")
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Erfasst von",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Erfasst am")

    class Meta:
        db_table = "session_file_versions"
        verbose_name = "Anlagen-Fassung"
        verbose_name_plural = "Anlagen-Fassungen"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["session_file", "number"], name="uniq_session_file_version_number"),
        ]

    def __str__(self) -> str:
        return f"{self.name} – Fassung {self.number}"

    @property
    def size_human(self) -> str:
        return human_size(self.size)


class SessionPaperVersion(ImmutableRecord):
    """
    Fassung einer Vorlage (Issue #226): unveränderlicher Stand von Texten, Angaben und Anlagen.

    Entsteht automatisch bei jedem Workflow-Übergang (Statuswechsel, egal wo ausgelöst) und bei
    jedem erfassten Beratungsergebnis, zusätzlich von Hand („Fassung sichern“). Wiederherstellen
    legt eine neue Fassung an – die Historie wird nie überschrieben.

    Die beschlossene Fassung (``is_resolved``) ist je Vorlage eindeutig: der Stand, zu dem die
    entscheidende Beratung „Angenommen“ erfasst hat. Wie jede Fassung ist sie unveränderlich;
    ihr Inhalt lässt sich auch nicht per Datenschutz-Löschung entfernen.

    Sichtbarkeit: wie die Vorlage und zusätzlich wie zum Zeitpunkt der Fassung – was damals
    nichtöffentlich war, bleibt es für die Historie (``paper_version_service.version_visible``).
    Der verschlüsselte „Vertrauliche Inhalt“ ist bewusst nicht Teil der Fassung.
    """

    TRIGGER_TRANSITION = "transition"
    TRIGGER_CONSULTATION = "consultation"
    TRIGGER_MANUAL = "manual"
    TRIGGER_BACKUP = "backup"
    TRIGGER_RESTORE = "restore"
    TRIGGER_CHOICES = [
        (TRIGGER_TRANSITION, "Workflow-Übergang"),
        (TRIGGER_CONSULTATION, "Beratungsergebnis"),
        (TRIGGER_MANUAL, "Von Hand gesichert"),
        (TRIGGER_BACKUP, "Sicherung vor Wiederherstellung"),
        (TRIGGER_RESTORE, "Wiederherstellung"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="paper_versions",
        verbose_name="Mandant",
    )
    paper = models.ForeignKey(
        SessionPaper,
        on_delete=models.CASCADE,
        related_name="versions",
        verbose_name="Vorlage",
    )
    number = models.PositiveIntegerField(verbose_name="Fassung")
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES, verbose_name="Anlass")
    note = models.CharField(max_length=300, blank=True, verbose_name="Bemerkung")
    status = models.CharField(max_length=50, verbose_name="Status der Vorlage")
    previous_status = models.CharField(max_length=50, blank=True, verbose_name="Vorheriger Status")
    is_resolved = models.BooleanField(default=False, verbose_name="Beschlossene Fassung")
    agenda_item = models.ForeignKey(
        SessionAgendaItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paper_versions",
        verbose_name="Tagesordnungspunkt",
    )
    restored_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Wiederhergestellt aus",
    )
    fingerprint = models.CharField(max_length=64, verbose_name="Fingerabdruck")

    # Stand der Vorlage
    name = models.CharField(max_length=500, verbose_name="Betreff")
    reference = models.CharField(max_length=100, blank=True, verbose_name="Vorlagennummer")
    paper_type = models.CharField(max_length=100, verbose_name="Vorlagenart")
    is_public = models.BooleanField(verbose_name="Öffentlich")
    date = models.DateField(null=True, blank=True, verbose_name="Datum")
    deadline = models.DateField(null=True, blank=True, verbose_name="Frist")
    main_text = models.TextField(blank=True, verbose_name="Sachverhalt")
    resolution_text = models.TextField(blank=True, verbose_name="Beschlussvorschlag")
    has_financial_impact = models.BooleanField(null=True, blank=True, verbose_name="Finanzielle Auswirkungen")
    financial_impact_note = models.TextField(blank=True, verbose_name="Erläuterung der finanziellen Auswirkungen")
    details = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Weitere Angaben",
        help_text="Gremien, Amt, Urheber und Bezug im Wortlaut der Fassung, dazu die Kennungen",
    )

    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Gesichert von",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Gesichert am")

    class Meta:
        db_table = "session_paper_versions"
        verbose_name = "Vorlagen-Fassung"
        verbose_name_plural = "Vorlagen-Fassungen"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["paper", "number"], name="uniq_session_paper_version_number"),
            # Genau eine beschlossene Fassung je Vorlage
            models.UniqueConstraint(
                fields=["paper"],
                condition=models.Q(is_resolved=True),
                name="uniq_session_paper_resolved_version",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reference or 'ohne Nummer'} – Fassung {self.number}"

    @property
    def status_display(self) -> str:
        """Status der Vorlage zum Zeitpunkt der Fassung, im Wortlaut der Oberfläche."""
        return str(dict(SessionPaper._meta.get_field("status").choices or []).get(self.status, self.status))

    @property
    def paper_type_display(self) -> str:
        return str(dict(SessionPaper._meta.get_field("paper_type").choices or []).get(self.paper_type, self.paper_type))


class SessionPaperVersionFile(ImmutableRecord):
    """
    Anlage, wie sie in einer Fassung der Vorlage enthalten ist (Issue #226).

    Verweist direkt auf den Inhalt (``SessionFileBlob``), nicht auf die Anlage: Wird die Anlage
    später ersetzt oder gelöscht, bleibt die Fassung vollständig. ``attachment_id`` hält die
    Kennung der Anlage fest (für den Vergleich und die Sichtbarkeit); ``blob`` ist leer, wenn
    der Inhalt beim Sichern nicht lesbar war.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.ForeignKey(
        SessionPaperVersion,
        on_delete=models.CASCADE,
        related_name="files",
        verbose_name="Fassung",
    )
    attachment_id = models.UUIDField(verbose_name="Anlage")
    file_version_number = models.PositiveIntegerField(verbose_name="Fassung der Anlage")
    blob = models.ForeignKey(
        SessionFileBlob,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="paper_version_files",
        verbose_name="Inhalt",
    )
    name = models.CharField(max_length=500, verbose_name="Name")
    mime_type = models.CharField(max_length=100, blank=True, verbose_name="MIME-Typ")
    size = models.PositiveBigIntegerField(default=0, verbose_name="Größe (Bytes)")
    sha256 = models.CharField(max_length=64, blank=True, verbose_name="SHA-256")
    is_public = models.BooleanField(verbose_name="Öffentlich")
    position = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")

    class Meta:
        db_table = "session_paper_version_files"
        verbose_name = "Anlage einer Vorlagen-Fassung"
        verbose_name_plural = "Anlagen einer Vorlagen-Fassung"
        ordering = ["position"]

    def __str__(self) -> str:
        return self.name

    @property
    def size_human(self) -> str:
        return human_size(self.size)


# =============================================================================
# SITZUNGSMAPPE (Issue #218)
# =============================================================================


class SessionMeetingPackage(models.Model):
    """
    Sitzungsmappe einer Sitzung in einer Fassung (Issue #218).

    Gesamt-PDF (Deckblatt, Inhaltsverzeichnis, Lesezeichen, fortlaufende Seitenzählung) und
    ZIP-Paket aller Unterlagen, je Sitzung in der öffentlichen und der nichtöffentlichen
    Fassung. Erzeugt wird im Hintergrund (Management-Command ``build_meeting_packages``),
    Seitenaufrufe legen nur die Anforderung an.

    Fassungen: Ein Fingerabdruck der Eingaben (Tagesordnung, Vorlagen, Anlagen) erkennt
    Änderungen; die nächste Anforderung danach erzeugt eine neue Fassung mit eigenem Stand,
    ältere bleiben abrufbar. ``contents`` merkt sich die enthaltenen Objekte, damit eine
    ältere Fassung gesperrt wird, sobald ein enthaltener Teil gelöscht oder (öffentliche
    Fassung) nichtöffentlich wurde.

    Die Dateien liegen unter ``session/files/`` und sind damit nie direkt über /media/
    abrufbar (PROTECTED_MEDIA_PREFIXES), nur über die zugriffsgeprüfte Download-View.
    """

    VARIANT_PUBLIC = "public"
    VARIANT_INTERNAL = "internal"
    VARIANT_CHOICES = [
        (VARIANT_PUBLIC, "Öffentliche Fassung"),
        (VARIANT_INTERNAL, "Nichtöffentliche Fassung"),
    ]

    STATUS_REQUESTED = "requested"
    STATUS_BUILDING = "building"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Angefordert"),
        (STATUS_BUILDING, "In Arbeit"),
        (STATUS_READY, "Fertig"),
        (STATUS_FAILED, "Fehlgeschlagen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="meeting_packages",
        verbose_name="Mandant",
    )
    meeting = models.ForeignKey(
        SessionMeeting,
        on_delete=models.CASCADE,
        related_name="packages",
        verbose_name="Sitzung",
    )
    variant = models.CharField(max_length=20, choices=VARIANT_CHOICES, verbose_name="Fassung")
    version = models.PositiveIntegerField(verbose_name="Fassungsnummer")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_REQUESTED, verbose_name="Status")

    fingerprint = models.CharField(
        max_length=64,
        blank=True,
        verbose_name="Fingerabdruck",
        help_text="SHA-256 über die Eingaben (Tagesordnung, Vorlagen, Anlagen) dieser Fassung",
    )
    content_as_of = models.DateTimeField(null=True, blank=True, verbose_name="Stand")
    contents = models.JSONField(default=dict, blank=True, verbose_name="Enthaltene Objekte")

    pdf_file = models.FileField(upload_to="session/files/mappen/%Y/%m/", blank=True, verbose_name="Gesamt-PDF")
    zip_file = models.FileField(upload_to="session/files/mappen/%Y/%m/", blank=True, verbose_name="ZIP-Paket")
    pdf_size = models.PositiveBigIntegerField(default=0, verbose_name="Größe PDF (Bytes)")
    zip_size = models.PositiveBigIntegerField(default=0, verbose_name="Größe ZIP (Bytes)")
    page_count = models.PositiveIntegerField(default=0, verbose_name="Seiten")
    embedded_count = models.PositiveIntegerField(default=0, verbose_name="Eingebundene Anlagen")
    referenced_count = models.PositiveIntegerField(default=0, verbose_name="Anlagen als Verweisseite")

    error = models.TextField(blank=True, verbose_name="Fehler")
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="Versuche")

    requested_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_meeting_packages",
        verbose_name="Angefordert von",
    )
    requested_at = models.DateTimeField(default=timezone.now, verbose_name="Angefordert am")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Begonnen am")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Fertig am")

    class Meta:
        db_table = "session_meeting_packages"
        verbose_name = "Sitzungsmappe"
        verbose_name_plural = "Sitzungsmappen"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "variant", "version"], name="uniq_session_meeting_package_version"
            ),
        ]
        indexes = [models.Index(fields=["status", "requested_at"])]

    def __str__(self) -> str:
        return f"Sitzungsmappe {self.meeting.name} – {self.get_variant_display()}, Fassung {self.version}"

    @property
    def is_internal(self) -> bool:
        return self.variant == self.VARIANT_INTERNAL

    @property
    def in_progress(self) -> bool:
        return self.status in (self.STATUS_REQUESTED, self.STATUS_BUILDING)


# =============================================================================
# AUDIT LOG
# =============================================================================


class SessionAuditLog(models.Model):
    """
    Revisionssicheres Protokoll des Mandanten (Issues #23, #221).

    Erfasst Änderungen an Kernobjekten, Lesezugriffe auf nichtöffentliche Inhalte, Anmeldungen,
    Stimmabgaben, Mitzeichnungen, Pauschalen sowie Rollen- und Rechteänderungen. Jeder Eintrag ist
    Glied einer Hash-Kette je Mandant (``seq``, ``prev_hash``, ``entry_hash``, siehe
    ``apps/common/audit_chain.py``); nachträgliche Änderungen erkennt ``verify_audit_chain``.
    """

    ACTION_CHOICES = [
        ("create", "Erstellt"),
        ("update", "Geändert"),
        ("delete", "Gelöscht"),
        ("view", "Angesehen"),
        ("download", "Heruntergeladen"),
        ("approve", "Freigegeben"),
        ("publish", "Veröffentlicht"),
        ("invitation_sent", "Einladung versandt"),
        ("withdraw", "Abgesetzt"),
        ("replace", "Ersetzt"),
        ("login", "Anmeldung"),
        ("logout", "Abmeldung"),
        # Issue #221: direkte, sprechende Einträge
        ("login_failed", "Anmeldung fehlgeschlagen"),
        ("vote", "Stimmabgabe erfasst"),
        ("vote_result", "Abstimmungsergebnis festgestellt"),
        ("cosign", "Mitzeichnung entschieden"),
        ("allowance_created", "Entschädigung festgesetzt"),
        ("allowance_approved", "Entschädigung genehmigt"),
        ("allowance_paid", "Entschädigung ausgezahlt"),
        ("allowance_cancelled", "Entschädigung storniert"),
        ("roles_changed", "Rollen geändert"),
        ("permissions_changed", "Rechte geändert"),
        ("audit_view", "Protokoll eingesehen"),
        ("audit_export", "Protokoll exportiert"),
        ("audit_verify", "Protokoll geprüft"),
        ("audit_archive", "Protokoll archiviert"),
        # Issue #318: Niederschrift nach der Genehmigung
        ("unpublish", "Veröffentlichung zurückgenommen"),
        ("protocol_correction", "Niederschrift berichtigt"),
        ("protocol_correction_requested", "Berichtigung der Niederschrift beantragt"),
        ("protocol_correction_rejected", "Berichtigung der Niederschrift abgelehnt"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        verbose_name="Mandant",
    )

    # Actor
    user = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_logs",
        verbose_name="Benutzer",
    )
    # Handlung aus einer Vertretung (Issue #222): „in Vertretung für …“
    on_behalf_of = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="In Vertretung für",
    )
    # Unveränderliche Kopien der Nutzer-IDs (Issue #221): Die Hash-Kette sichert diese Referenzen.
    # ``user`` und ``on_behalf_of`` werden beim Löschen eines Nutzers geleert und sind deshalb
    # nicht Teil des Hashes.
    user_ref = models.UUIDField(blank=True, null=True, editable=False, verbose_name="Benutzer-Referenz")
    on_behalf_of_ref = models.UUIDField(
        blank=True, null=True, editable=False, verbose_name="Referenz „in Vertretung für“"
    )
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name="IP-Adresse")
    user_agent = models.TextField(blank=True, verbose_name="User-Agent")

    # Action
    action = models.CharField(max_length=50, choices=ACTION_CHOICES, verbose_name="Aktion")

    # Target
    model_name = models.CharField(max_length=100, verbose_name="Modell")
    object_id = models.UUIDField(verbose_name="Objekt-ID")
    object_repr = models.CharField(max_length=500, blank=True, verbose_name="Objekt-Beschreibung")

    # Changes (JSON diff)
    changes = models.JSONField(default=dict, blank=True, verbose_name="Änderungen")

    # Zeitpunkt: setzt die Hash-Kette beim Schreiben (unter der Sperre des Kettenkopfs)
    created_at = models.DateTimeField(default=timezone.now, editable=False, verbose_name="Zeitpunkt")

    # Hash-Kette je Mandant (Issue #221); NULL = Altbestand, noch nicht verkettet
    seq = models.BigIntegerField(blank=True, null=True, editable=False, verbose_name="Laufende Nummer")
    prev_hash = models.CharField(max_length=64, blank=True, null=True, editable=False, verbose_name="Vorgänger-Hash")
    entry_hash = models.CharField(max_length=64, blank=True, null=True, editable=False, verbose_name="Eintrags-Hash")

    class Meta:
        db_table = "session_audit_logs"
        verbose_name = "Audit-Eintrag"
        verbose_name_plural = "Audit-Log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "model_name", "object_id"]),
            models.Index(fields=["tenant", "user", "created_at"]),
            models.Index(fields=["tenant", "created_at"], name="session_audit_tenant_created"),
        ]
        constraints = [
            # Keine Verzweigung der Kette, auch wenn die Sperre einmal fehlen sollte (Issue #221)
            models.UniqueConstraint(
                fields=["tenant", "seq"], condition=models.Q(seq__isnull=False), name="uniq_session_audit_seq"
            ),
        ]

    def __str__(self):
        return f"{self.user}: {self.action} {self.model_name} {self.object_repr}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Revisionssicherheit: Einträge sind unveränderbar; neue Einträge laufen über die Hash-Kette."""
        if not self._state.adding:
            raise ValueError("Audit-Einträge sind unveränderbar und können nicht aktualisiert werden.")
        from apps.common import audit_chain

        if audit_chain.is_chain_insert(self):
            super().save(*args, **kwargs)
            return
        audit_chain.append(audit_chain.SESSION, self)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        """Revisionssicherheit: Einträge können nicht gelöscht werden."""
        raise ValueError("Audit-Einträge sind unveränderbar und können nicht gelöscht werden.")


# =============================================================================
# FRISTEN-ERINNERUNGEN (Issue #83)
# =============================================================================


class SessionReminderLog(models.Model):
    """
    Protokoll versendeter Fristen-Erinnerungen (Issue #83).

    Der unique-Constraint auf (tenant, kind, dedup_key) macht den
    Erinnerungslauf idempotent: mehrfaches Ausführen am selben Tag
    erzeugt keine doppelten E-Mails.
    """

    KIND_CHOICES = [
        ("invitation_upcoming", "Ladungsfrist läuft ab"),
        ("invitation_overdue", "Ladungsfrist verstrichen"),
        ("paper_deadline", "Vorlagenfrist läuft ab"),
        ("attendance_rsvp", "Rückmeldung zur Sitzung fehlt"),
        ("resolution_followup", "Wiedervorlage Beschlusskontrolle"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="reminder_logs",
        verbose_name="Mandant",
    )
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, verbose_name="Erinnerungstyp")
    dedup_key = models.CharField(
        max_length=255,
        verbose_name="Deduplizierungs-Schlüssel",
        help_text="Objekt-/Frist-Bezug, verhindert doppelte Erinnerungen",
    )
    recipients = models.JSONField(default=list, blank=True, verbose_name="Empfänger")
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name="Versendet am")

    class Meta:
        db_table = "session_reminder_logs"
        verbose_name = "Erinnerungs-Protokoll"
        verbose_name_plural = "Erinnerungs-Protokolle"
        constraints = [models.UniqueConstraint(fields=["tenant", "kind", "dedup_key"], name="uniq_session_reminder")]
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.get_kind_display()} ({self.dedup_key})"


# =============================================================================
# MONATLICHE PAUSCHALEN (EntschVO NRW)
# =============================================================================


class SessionMonthlyRate(models.Model):
    """
    Monatliche Pauschale nach EntschVO NRW: Aufwandsentschädigung
    (Voll-/Teilpauschale) sowie Funktionszulagen (z. B. Fraktionsvorsitz
    als Vielfaches der Vollpauschale, § 5 EntschVO NRW).

    Die Beträge pflegt die Verwaltung selbst — sie hängen von
    Gemeindegröße und Hauptsatzung ab und steigen jährlich (+2 % ab 2025).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="monthly_rates",
        verbose_name="Mandant",
    )
    name = models.CharField(
        max_length=200,
        verbose_name="Bezeichnung",
        help_text="z. B. Aufwandsentschädigung (Teilpauschale), Zulage Fraktionsvorsitz",
    )
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Betrag/Monat")
    legal_basis = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Rechtsgrundlage",
        help_text="z. B. § 2 Abs. 1 EntschVO NRW",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_monthly_rates"
        verbose_name = "Monatliche Pauschale"
        verbose_name_plural = "Monatliche Pauschalen"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.amount} €)"


class SessionPersonMonthlyRate(models.Model):
    """Zuordnung einer monatlichen Pauschale zu einer Person (mit Zeitraum)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="monthly_rates",
        verbose_name="Person",
    )
    rate = models.ForeignKey(
        SessionMonthlyRate,
        on_delete=models.CASCADE,
        related_name="assignments",
        verbose_name="Pauschale",
    )
    start_date = models.DateField(blank=True, null=True, verbose_name="Von")
    end_date = models.DateField(blank=True, null=True, verbose_name="Bis")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_person_monthly_rates"
        verbose_name = "Pauschalen-Zuordnung"
        verbose_name_plural = "Pauschalen-Zuordnungen"
        constraints = [models.UniqueConstraint(fields=["person", "rate"], name="uniq_session_person_monthly_rate")]

    def __str__(self):
        return f"{self.person.display_name}: {self.rate.name}"

    def active_in_month(self, first_of_month) -> bool:
        """Gilt die Zuordnung in diesem Monat (Stichtag Monatserster)?"""
        import calendar as _calendar
        from datetime import date as _date

        last_of_month = _date(
            first_of_month.year,
            first_of_month.month,
            _calendar.monthrange(first_of_month.year, first_of_month.month)[1],
        )
        if self.start_date and self.start_date > last_of_month:
            return False
        return not (self.end_date and self.end_date < first_of_month)


class SessionMonthlyAllowance(models.Model):
    """Abgerechnete monatliche Pauschale (ein Posten je Person, Pauschale und Monat)."""

    STATUS_CHOICES = [
        ("pending", "Ausstehend"),
        ("approved", "Genehmigt"),
        ("paid", "Ausgezahlt"),
        ("cancelled", "Storniert"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="monthly_allowances",
        verbose_name="Mandant",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="monthly_allowances",
        verbose_name="Person",
    )
    rate = models.ForeignKey(
        SessionMonthlyRate,
        on_delete=models.PROTECT,
        related_name="allowances",
        verbose_name="Pauschale",
    )
    period = models.DateField(verbose_name="Monat", help_text="Jeweils der Monatserste")
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Betrag")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", verbose_name="Status")
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="approved_monthly_allowances",
        verbose_name="Genehmigt von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Genehmigt am")
    paid_at = models.DateTimeField(blank=True, null=True, verbose_name="Ausgezahlt am")
    export_reference = models.CharField(max_length=100, blank=True, verbose_name="Export-Referenz")
    export_date = models.DateTimeField(blank=True, null=True, verbose_name="Export-Datum")
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_monthly_allowances",
        verbose_name="Erstellt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_monthly_allowances"
        verbose_name = "Monats-Pauschale"
        verbose_name_plural = "Monats-Pauschalen"
        constraints = [
            models.UniqueConstraint(fields=["person", "rate", "period"], name="uniq_session_monthly_allowance")
        ]
        ordering = ["-period", "person__family_name"]

    def __str__(self):
        return f"{self.person.display_name}: {self.rate.name} {self.period:%m/%Y}"


class SessionExportCounter(models.Model):
    """
    Laufende Nummer der SEPA-Export-Referenzen (``SG-JJJJ-NNNN``) je Mandant und Jahr (Issue #428).

    Ein gemeinsamer Zähler für Sitzungsgeld und Monatspauschalen. Vergeben wird nur unter Sperre
    dieser Zeile (``allowance_service.next_export_reference``); der Stand steigt nur.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="export_counters",
        verbose_name="Mandant",
    )
    year = models.PositiveSmallIntegerField(verbose_name="Jahr")
    value = models.PositiveIntegerField(default=0, verbose_name="Zuletzt vergeben")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_export_counters"
        verbose_name = "Zählerstand Export-Referenz"
        verbose_name_plural = "Zählerstände Export-Referenz"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "year"], name="uniq_session_export_counter_year"),
        ]

    def __str__(self):
        return f"{self.tenant} {self.year}: {self.value}"


# =============================================================================
# ENDGERÄTE FÜR DIE DIGITALE RATSARBEIT
# =============================================================================


class SessionDevice(models.Model):
    """
    Endgerät für die digitale Ratsarbeit (z. B. iPad), das die Verwaltung
    an Mandatsträger ausgibt. Ausgabe/Rückgabe werden dokumentiert
    (Übergabeprotokoll als PDF, Historie in SessionDeviceLog).
    """

    STATUS_CHOICES = [
        ("in_stock", "Im Bestand"),
        ("issued", "Ausgegeben"),
        ("defect", "Defekt"),
        ("retired", "Ausgemustert"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="devices",
        verbose_name="Mandant",
    )
    label = models.CharField(max_length=200, verbose_name="Bezeichnung", help_text="z. B. iPad 10. Gen, 64 GB")
    serial_number = models.CharField(max_length=100, blank=True, verbose_name="Seriennummer")
    inventory_number = models.CharField(max_length=100, blank=True, verbose_name="Inventarnummer")
    accessories = models.CharField(
        max_length=300, blank=True, verbose_name="Zubehör", help_text="z. B. Hülle, Tastatur, Netzteil"
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="in_stock", verbose_name="Status")
    issued_to = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="devices",
        verbose_name="Ausgegeben an",
    )
    issued_at = models.DateTimeField(blank=True, null=True, verbose_name="Ausgegeben am")
    note = models.TextField(blank=True, verbose_name="Notiz")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_devices"
        verbose_name = "Endgerät"
        verbose_name_plural = "Endgeräte"
        ordering = ["label", "inventory_number"]

    def __str__(self):
        return f"{self.label} ({self.inventory_number or self.serial_number or 'ohne Nr.'})"


class SessionDeviceLog(models.Model):
    """Historie eines Endgeräts (Ausgabe, Rückgabe, Defekt, Vermerke)."""

    ACTION_CHOICES = [
        ("created", "Angelegt"),
        ("issued", "Ausgegeben"),
        ("returned", "Zurückgenommen"),
        ("defect", "Defekt gemeldet"),
        ("retired", "Ausgemustert"),
        ("note", "Vermerk"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    device = models.ForeignKey(
        SessionDevice,
        on_delete=models.CASCADE,
        related_name="logs",
        verbose_name="Endgerät",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, verbose_name="Aktion")
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="device_logs",
        verbose_name="Person",
    )
    note = models.TextField(blank=True, verbose_name="Vermerk")
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="device_logs",
        verbose_name="Erfasst von",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Zeitpunkt")

    class Meta:
        db_table = "session_device_logs"
        verbose_name = "Geräte-Historie"
        verbose_name_plural = "Geräte-Historien"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.device}: {self.get_action_display()}"


class SessionDeviceGrant(models.Model):
    """
    Einmaliger Endgeräte-Zuschuss für die digitale Ratsarbeit
    (Alternative zur Geräteausgabe, z. B. 300–500 € je Mandatsträger
    laut Ratsbeschluss).
    """

    STATUS_CHOICES = [
        ("pending", "Ausstehend"),
        ("approved", "Genehmigt"),
        ("paid", "Ausgezahlt"),
        ("cancelled", "Storniert"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="device_grants",
        verbose_name="Mandant",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="device_grants",
        verbose_name="Person",
    )
    amount = models.DecimalField(max_digits=8, decimal_places=2, verbose_name="Betrag")
    note = models.TextField(blank=True, verbose_name="Vermerk", help_text="z. B. Ratsbeschluss, Beleg")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", verbose_name="Status")
    approved_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="approved_device_grants",
        verbose_name="Genehmigt von",
    )
    approved_at = models.DateTimeField(blank=True, null=True, verbose_name="Genehmigt am")
    paid_at = models.DateTimeField(blank=True, null=True, verbose_name="Ausgezahlt am")
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_device_grants",
        verbose_name="Erstellt von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_device_grants"
        verbose_name = "Endgeräte-Zuschuss"
        verbose_name_plural = "Endgeräte-Zuschüsse"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.person.display_name}: {self.amount} €"


# =============================================================================
# DIGITALE ABSTIMMUNG UND UMLAUFBESCHLÜSSE (Issue #41)
# =============================================================================


class SessionVote(models.Model):
    """
    Einzelstimme eines Mitglieds zu einem TOP (Issue #41).

    Wird bei namentlicher bzw. offener Einzelabstimmung erfasst. „Befangen"
    dokumentiert das Mitwirkungsverbot nach Gemeindeordnung — die Person
    zählt nicht zur Abstimmung. Bei geheimer Abstimmung werden keine
    Einzelstimmen gespeichert (nur Befangenheits-Vermerke).
    """

    VOTE_CHOICES = [
        ("yes", "Ja"),
        ("no", "Nein"),
        ("abstain", "Enthaltung"),
        ("excluded", "Befangen (Mitwirkungsverbot)"),
        ("not_participating", "Nicht teilgenommen"),
    ]
    # Stimmen, die in die Summenzählung eingehen
    COUNTED_VOTES = ("yes", "no", "abstain")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agenda_item = models.ForeignKey(
        SessionAgendaItem,
        on_delete=models.CASCADE,
        related_name="votes",
        verbose_name="Tagesordnungspunkt",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="votes",
        verbose_name="Person",
    )
    vote = models.CharField(max_length=20, choices=VOTE_CHOICES, verbose_name="Stimme")
    recorded_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="recorded_votes",
        verbose_name="Erfasst von",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_votes"
        verbose_name = "Einzelstimme"
        verbose_name_plural = "Einzelstimmen"
        constraints = [models.UniqueConstraint(fields=["agenda_item", "person"], name="uniq_session_vote")]
        ordering = ["person__family_name", "person__given_name"]

    def __str__(self):
        return f"{self.person.display_name}: {self.get_vote_display()}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Namentliche Stimmen sind nach der Genehmigung der Niederschrift gesperrt (Issue #318)."""
        from apps.session.services import protocol_lock

        protocol_lock.guard_vote(self)
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        from apps.session.services import protocol_lock

        protocol_lock.guard_vote(self)
        return super().delete(*args, **kwargs)


class SessionCircularResolution(models.Model):
    """
    Umlaufbeschluss (Issue #41): Beschlussfassung im schriftlichen Verfahren
    ohne Sitzung. Der Sitzungsdienst erfasst die Rückläufe der Mitglieder
    und stellt das Ergebnis fest.
    """

    STATUS_CHOICES = [
        ("open", "Im Umlauf"),
        ("adopted", "Angenommen"),
        ("rejected", "Abgelehnt"),
        ("cancelled", "Abgebrochen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="circular_resolutions",
        verbose_name="Mandant",
    )
    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="circular_resolutions",
        verbose_name="Gremium",
    )
    reference = models.CharField(max_length=50, blank=True, verbose_name="Umlauf-Nr.", help_text="z. B. U/2026/0001")
    title = models.CharField(max_length=500, verbose_name="Betreff")
    resolution_text = models.TextField(verbose_name="Beschlussvorschlag")
    paper = models.ForeignKey(
        "SessionPaper",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="circular_resolutions",
        verbose_name="Vorlage",
    )
    deadline = models.DateField(verbose_name="Rückmeldefrist")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open", verbose_name="Status")
    result_note = models.TextField(blank=True, verbose_name="Ergebnisvermerk")
    is_public = models.BooleanField(default=True, verbose_name="Öffentlich")
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="circular_resolutions",
        verbose_name="Angelegt von",
    )
    decided_at = models.DateTimeField(blank=True, null=True, verbose_name="Ergebnis festgestellt am")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_circular_resolutions"
        verbose_name = "Umlaufbeschluss"
        verbose_name_plural = "Umlaufbeschlüsse"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference or 'Umlauf'}: {self.title}"

    @property
    def is_overdue(self) -> bool:
        return self.status == "open" and self.deadline < timezone.localdate()


class SessionCircularVote(models.Model):
    """Rücklauf eines Mitglieds zu einem Umlaufbeschluss (Issue #41)."""

    VOTE_CHOICES = [
        ("yes", "Ja"),
        ("no", "Nein"),
        ("abstain", "Enthaltung"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    circular = models.ForeignKey(
        SessionCircularResolution,
        on_delete=models.CASCADE,
        related_name="votes",
        verbose_name="Umlaufbeschluss",
    )
    person = models.ForeignKey(
        SessionPerson,
        on_delete=models.CASCADE,
        related_name="circular_votes",
        verbose_name="Person",
    )
    vote = models.CharField(max_length=20, choices=VOTE_CHOICES, verbose_name="Stimme")
    received_at = models.DateField(verbose_name="Eingegangen am")
    recorded_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="recorded_circular_votes",
        verbose_name="Erfasst von",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_circular_votes"
        verbose_name = "Umlauf-Rücklauf"
        verbose_name_plural = "Umlauf-Rückläufe"
        constraints = [models.UniqueConstraint(fields=["circular", "person"], name="uniq_session_circular_vote")]
        ordering = ["person__family_name", "person__given_name"]

    def __str__(self):
        return f"{self.person.display_name}: {self.get_vote_display()}"


# =============================================================================
# ÄMTERSTRUKTUR UND MITZEICHNUNG (Issue #81)
# =============================================================================


class SessionCosignatureRule(models.Model):
    """
    Standard-Mitzeichnungskette (Issue #81): Welche Ämter müssen eine
    Vorlage welcher Art in welcher Reihenfolge mitzeichnen, bevor sie
    freigegeben werden darf.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="cosignature_rules",
        verbose_name="Mandant",
    )
    paper_type = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Vorlagenart",
        help_text="Leer = gilt für alle Vorlagenarten",
    )
    department = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="cosignature_rules",
        verbose_name="Amt/Fachbereich",
    )
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")
    only_financial = models.BooleanField(
        default=False,
        verbose_name="Nur bei finanziellen Auswirkungen",
        help_text="z. B. Kämmerei: Mitzeichnung nur, wenn die Vorlage finanzielle Auswirkungen hat",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_cosignature_rules"
        verbose_name = "Mitzeichnungsregel"
        verbose_name_plural = "Mitzeichnungsregeln"
        ordering = ["paper_type", "order"]

    def __str__(self):
        scope = self.get_paper_type_label()
        return f"{self.department.name} ({scope}, Position {self.order})"

    def get_paper_type_label(self) -> str:
        if not self.paper_type:
            return "alle Vorlagenarten"
        return dict(SessionPaper._meta.get_field("paper_type").choices).get(self.paper_type, self.paper_type)


class SessionCosignature(models.Model):
    """
    Mitzeichnungsstation einer konkreten Vorlage (Issue #81).

    Wird beim Vorlegen zur Freigabe aus den Mitzeichnungsregeln erzeugt.
    Die Freigabe der Vorlage ist erst möglich, wenn alle Stationen
    mitgezeichnet haben; eine Zurückweisung wirft die Vorlage zurück in
    den Entwurf.
    """

    STATUS_CHOICES = [
        ("pending", "Offen"),
        ("signed", "Mitgezeichnet"),
        ("rejected", "Zurückgewiesen"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    paper = models.ForeignKey(
        "SessionPaper",
        on_delete=models.CASCADE,
        related_name="cosignatures",
        verbose_name="Vorlage",
    )
    department = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        related_name="cosignatures",
        verbose_name="Amt/Fachbereich",
    )
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", verbose_name="Status")
    comment = models.TextField(blank=True, verbose_name="Kommentar")
    decided_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="cosignature_decisions",
        verbose_name="Entschieden von",
    )
    decided_at = models.DateTimeField(blank=True, null=True, verbose_name="Entschieden am")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_cosignatures"
        verbose_name = "Mitzeichnung"
        verbose_name_plural = "Mitzeichnungen"
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"{self.department.name}: {self.get_status_display()}"


# =============================================================================
# TEXTBAUSTEINE UND STANDARD-TAGESORDNUNGSPUNKTE (Issue #85)
# =============================================================================


class SessionStandardAgendaItem(models.Model):
    """
    Standard-Tagesordnungspunkt (Issue #85).

    Wird beim Anlegen einer Sitzung automatisch in die Tagesordnung
    übernommen — z. B. „Eröffnung der Sitzung", „Genehmigung der
    Niederschrift", „Anfragen und Mitteilungen".
    """

    PLACEMENT_CHOICES = [
        ("start", "Am Anfang"),
        ("end", "Am Ende"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="standard_agenda_items",
        verbose_name="Mandant",
    )
    organization = models.ForeignKey(
        SessionOrganization,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="standard_agenda_items",
        verbose_name="Gremium",
        help_text="Leer = gilt für alle Gremien",
    )
    name = models.CharField(max_length=500, verbose_name="Betreff")
    placement = models.CharField(max_length=10, choices=PLACEMENT_CHOICES, default="start", verbose_name="Position")
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")
    is_public = models.BooleanField(default=True, verbose_name="Öffentlich")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_standard_agenda_items"
        verbose_name = "Standard-Tagesordnungspunkt"
        verbose_name_plural = "Standard-Tagesordnungspunkte"
        ordering = ["placement", "order", "name"]

    def __str__(self):
        return self.name


class SessionTextBlock(models.Model):
    """
    Textbaustein (Issue #85) für wiederkehrende Formulierungen in
    Beschlussvorschlägen und Niederschriften. Platzhalter wie {gremium},
    {datum} oder {vorlage} werden beim Einfügen ersetzt.
    """

    CATEGORY_CHOICES = [
        ("resolution", "Beschlusstext"),
        ("protocol", "Protokolltext"),
        ("general", "Allgemein"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="text_blocks",
        verbose_name="Mandant",
    )
    title = models.CharField(max_length=200, verbose_name="Titel")
    content = models.TextField(verbose_name="Text")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="general", verbose_name="Kategorie")
    order = models.PositiveIntegerField(default=0, verbose_name="Reihenfolge")
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_text_blocks"
        verbose_name = "Textbaustein"
        verbose_name_plural = "Textbausteine"
        ordering = ["category", "order", "title"]

    def __str__(self):
        return self.title


# =============================================================================
# API TOKEN MODEL
# =============================================================================


class SessionAPIToken(models.Model):
    """
    API Token for secure access to Session API.

    Used by external systems (e.g., Work module) to submit applications
    or access data via API.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        SessionTenant,
        on_delete=models.CASCADE,
        related_name="api_tokens",
        verbose_name="Mandant",
    )

    # Token info
    name = models.CharField(
        max_length=100,
        verbose_name="Name",
        help_text="Beschreibender Name für den Token",
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        verbose_name="Token",
        help_text="SHA-256 Hash des API-Tokens",
    )
    token_prefix = models.CharField(
        max_length=8,
        editable=False,
        verbose_name="Token-Präfix",
        help_text="Die ersten 8 Zeichen des Tokens zur Identifikation",
    )

    # Permissions
    can_submit_applications = models.BooleanField(
        default=True,
        verbose_name="Anträge einreichen",
        help_text="Erlaubt das Einreichen von Anträgen",
    )
    # Lese-Flags: nur Öffentliches. Nichtöffentliche Daten und interne Notizen liefert die API nie an ein
    # Token (api/v1/auth.TOKEN_PERMISSIONS) – auch nicht bei Tokens, die vor dieser Regel angelegt wurden.
    can_read_meetings = models.BooleanField(
        default=True,
        verbose_name="Öffentliche Sitzungen lesen",
        help_text="Erlaubt das Lesen öffentlicher Sitzungsdaten – nie nichtöffentliche Sitzungen oder interne Notizen",
    )
    can_read_papers = models.BooleanField(
        default=True,
        verbose_name="Öffentliche Vorlagen lesen",
        help_text="Erlaubt das Lesen öffentlicher Vorlagen – nie nichtöffentliche Vorlagen oder deren Texte",
    )

    # Rate limiting
    rate_limit_per_minute = models.PositiveIntegerField(
        default=60,
        verbose_name="Rate-Limit pro Minute",
        help_text="Maximale Anzahl von Anfragen pro Minute",
    )

    # IP restrictions (optional)
    allowed_ips = models.TextField(
        blank=True,
        verbose_name="Erlaubte IP-Adressen",
        help_text="Komma-getrennte Liste erlaubter IP-Adressen (leer = alle)",
    )

    # Metadata
    description = models.TextField(blank=True, verbose_name="Beschreibung")
    created_by = models.ForeignKey(
        SessionUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tokens",
        verbose_name="Erstellt von",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktiv")
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Ablaufdatum",
        help_text="Token läuft zu diesem Zeitpunkt ab (leer = kein Ablauf)",
    )

    # Usage tracking
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name="Zuletzt verwendet")
    last_used_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="Letzte IP-Adresse")
    usage_count = models.PositiveIntegerField(default=0, verbose_name="Verwendungszähler")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_api_tokens"
        verbose_name = "API-Token"
        verbose_name_plural = "API-Tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.token_prefix}...)"

    @classmethod
    def generate_token(cls):
        """Generate a new random API token."""
        import secrets

        return secrets.token_hex(32)  # 64 characters

    @classmethod
    def hash_token(cls, raw_token: str) -> str:
        """Hash a raw token for storage."""
        import hashlib

        return hashlib.sha256(raw_token.encode()).hexdigest()

    @classmethod
    def create_token(cls, tenant: SessionTenant, name: str, **kwargs):
        """Create a new API token. Returns (token_instance, raw_token)."""
        raw_token = cls.generate_token()
        hashed = cls.hash_token(raw_token)
        token = cls.objects.create(
            tenant=tenant,
            name=name,
            token=hashed,
            token_prefix=raw_token[:8],
            **kwargs,
        )
        return token, raw_token

    def is_valid(self) -> bool:
        """Check if token is valid (active and not expired)."""
        if not self.is_active:
            return False
        return not (self.expires_at and self.expires_at < timezone.now())

    def check_ip(self, ip_address: str) -> bool:
        """Check if IP address is allowed."""
        if not self.allowed_ips:
            return True
        allowed = [ip.strip() for ip in self.allowed_ips.split(",")]
        return ip_address in allowed

    def record_usage(self, ip_address: str = None):
        """Record token usage."""
        self.last_used_at = timezone.now()
        self.last_used_ip = ip_address
        self.usage_count += 1
        self.save(update_fields=["last_used_at", "last_used_ip", "usage_count"])
