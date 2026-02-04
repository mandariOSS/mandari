# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Permission system for the Work module.

Implements Role-Based Access Control (RBAC) with:
- Permission definitions by category
- Default roles with predefined permissions
- Permission checking utilities

Based on typical German faction (Fraktion) structures with roles like:
- Fraktionsvorsitz (Chair)
- Fraktionsmitglieder (Council Members)
- Sachkundige Bürger*innen (Expert Citizens)
- Bezirksvertreter*innen (District Representatives)
- Fraktionspersonal (Staff)
- Parteimitglieder (Party Members)
- Öffentlichkeit (Public)
"""

from typing import List, Set


# =============================================================================
# PERMISSION DEFINITIONS
# =============================================================================

PERMISSIONS = {
    # === DASHBOARD ===
    "dashboard.view": "Dashboard anzeigen",

    # === SITZUNGEN (Meetings - public council/committee meetings from RIS) ===
    "meetings.view": "Öffentliche Sitzungen anzeigen",
    "meetings.view_non_public": "Nicht-öffentliche Sitzungen anzeigen",
    "meetings.prepare": "Sitzungen vorbereiten (Notizen, Redebeiträge)",
    "meetings.notes": "Notizen zu Sitzungen erstellen",

    # === FRAKTIONSSITZUNGEN (Internal faction meetings) ===
    "faction.view_public": "Öffentlichen Teil der Fraktionssitzung anzeigen",
    "faction.view_non_public": "Nicht-öffentlichen Teil anzeigen (nur Vereidigte)",
    "faction.create": "Fraktionssitzungen erstellen",
    "faction.edit": "Fraktionssitzungen bearbeiten",
    "faction.delete": "Fraktionssitzungen löschen",
    "faction.start": "Fraktionssitzung starten/beenden",
    "faction.invite": "Einladungen zu Fraktionssitzungen versenden",
    "faction.manage": "Fraktionssitzungen vollständig verwalten (inkl. Status)",

    # === TAGESORDNUNG (Agenda) ===
    "agenda.view": "Tagesordnung anzeigen",
    "agenda.create": "Tagesordnungspunkte direkt erstellen",
    "agenda.propose": "Tagesordnungspunkte vorschlagen (zur Genehmigung)",
    "agenda.suggest": "Themen/Anliegen einreichen (längere Frist)",
    "agenda.edit": "Tagesordnungspunkte bearbeiten",
    "agenda.delete": "Tagesordnungspunkte löschen",
    "agenda.approve": "Tagesordnungspunkte genehmigen",
    "agenda.reorder": "Tagesordnung umordnen",

    # === STIMMRECHT & REDERECHT ===
    "voting.participate": "Stimmrecht bei Abstimmungen",
    "speaking.automatic": "Automatisches Rederecht",
    "speaking.in_topic": "Rederecht im eigenen Themenbereich",
    "speaking.on_request": "Rederecht auf Anfrage",
    "speaking.grant": "Rederecht an andere erteilen",

    # === ANTRÄGE (Motions) ===
    "motions.view": "Anträge anzeigen",
    "motions.view_drafts": "Entwürfe anzeigen",
    "motions.create": "Anträge erstellen",
    "motions.edit": "Eigene Anträge bearbeiten",
    "motions.edit_all": "Alle Anträge bearbeiten",
    "motions.delete": "Anträge löschen",
    "motions.approve": "Anträge freigeben",
    "motions.submit_to_ris": "Anträge ans RIS übermitteln",
    "motions.share": "Anträge mit anderen Organisationen teilen",
    "motions.comment": "Anträge kommentieren",

    # === PROTOKOLLE (Protocols) ===
    "protocols.view_public": "Öffentliche Protokolle anzeigen",
    "protocols.view_full": "Vollständige Protokolle anzeigen",
    "protocols.create": "Protokolle erstellen",
    "protocols.edit": "Protokolle bearbeiten",
    "protocols.approve": "Protokolle freigeben",
    "protocols.publish": "Protokolle veröffentlichen",

    # === AUFGABEN (Tasks) ===
    "tasks.view": "Aufgaben anzeigen",
    "tasks.view_all": "Alle Aufgaben anzeigen",
    "tasks.create": "Aufgaben erstellen",
    "tasks.assign": "Aufgaben zuweisen",
    "tasks.edit": "Eigene Aufgaben bearbeiten",
    "tasks.manage": "Alle Aufgaben verwalten",

    # === RIS (Read-only view of public council data) ===
    "ris.view": "RIS-Daten anzeigen (öffentlich)",
    "ris.notes": "RIS-Notizen erstellen",
    "ris.subscribe": "RIS-Benachrichtigungen aktivieren",

    # === ARBEITSGRUPPEN (Working Groups) ===
    "workgroups.view": "Arbeitsgruppen anzeigen",
    "workgroups.join": "Arbeitsgruppen beitreten",
    "workgroups.create": "Arbeitsgruppen erstellen",
    "workgroups.manage": "Arbeitsgruppen verwalten",

    # === DOKUMENTE & DATEIEN ===
    "documents.view_public": "Öffentliche Dokumente anzeigen",
    "documents.view_internal": "Interne Dokumente anzeigen",
    "documents.view_confidential": "Vertrauliche Dokumente anzeigen",
    "documents.upload": "Dokumente hochladen",
    "documents.delete": "Dokumente löschen",
    "documents.manage": "Dokumentenablage verwalten",

    # === MITGLIEDERVERWALTUNG (Member Management) ===
    "members.view": "Mitgliederliste anzeigen",
    "members.view_details": "Mitgliederdetails anzeigen",
    "members.invite": "Mitglieder einladen",
    "members.edit": "Mitglieder bearbeiten",
    "members.remove": "Mitglieder entfernen",
    "members.manage_roles": "Rollen zuweisen",

    # === ORGANISATION ===
    "organization.view": "Organisationseinstellungen anzeigen",
    "organization.edit": "Organisationseinstellungen bearbeiten",
    "organization.manage_roles": "Rollen verwalten (erstellen/bearbeiten)",
    "organization.api_tokens": "API-Tokens verwalten",
    "organization.audit_log": "Audit-Log anzeigen",
    "organization.admin": "Vollständige Administration",

    # === GÄSTE & ÖFFENTLICHKEIT ===
    "guests.invite": "Gäste zu Sitzungen einladen",
    "guests.manage": "Gästeliste verwalten",

    # === SUPPORT ===
    "support.view": "Support anzeigen",
    "support.create": "Support-Tickets erstellen",
    "support.admin": "Support administrieren",
}


# =============================================================================
# PERMISSION CATEGORIES (for UI grouping)
# =============================================================================

PERMISSION_CATEGORIES = {
    "dashboard": {
        "name": "Dashboard",
        "icon": "dashboard",
        "permissions": ["dashboard.view"],
    },
    "faction": {
        "name": "Fraktionssitzungen",
        "icon": "groups",
        "permissions": [
            "faction.view_public", "faction.view_non_public",
            "faction.create", "faction.edit", "faction.delete",
            "faction.start", "faction.invite", "faction.manage",
        ],
    },
    "agenda": {
        "name": "Tagesordnung",
        "icon": "list_alt",
        "permissions": [
            "agenda.view", "agenda.create", "agenda.propose", "agenda.suggest",
            "agenda.edit", "agenda.delete", "agenda.approve", "agenda.reorder",
        ],
    },
    "voting_speaking": {
        "name": "Stimm- & Rederecht",
        "icon": "record_voice_over",
        "permissions": [
            "voting.participate",
            "speaking.automatic", "speaking.in_topic", "speaking.on_request",
            "speaking.grant",
        ],
    },
    "motions": {
        "name": "Anträge",
        "icon": "description",
        "permissions": [
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.edit_all", "motions.delete",
            "motions.approve", "motions.submit_to_ris", "motions.share",
            "motions.comment",
        ],
    },
    "protocols": {
        "name": "Protokolle",
        "icon": "article",
        "permissions": [
            "protocols.view_public", "protocols.view_full",
            "protocols.create", "protocols.edit",
            "protocols.approve", "protocols.publish",
        ],
    },
    "meetings": {
        "name": "Ratssitzungen (RIS)",
        "icon": "event",
        "permissions": [
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
        ],
    },
    "tasks": {
        "name": "Aufgaben",
        "icon": "task_alt",
        "permissions": [
            "tasks.view", "tasks.view_all", "tasks.create",
            "tasks.assign", "tasks.edit", "tasks.manage",
        ],
    },
    "ris": {
        "name": "RIS-Daten",
        "icon": "account_balance",
        "permissions": ["ris.view", "ris.notes", "ris.subscribe"],
    },
    "workgroups": {
        "name": "Arbeitsgruppen",
        "icon": "workspaces",
        "permissions": [
            "workgroups.view", "workgroups.join",
            "workgroups.create", "workgroups.manage",
        ],
    },
    "documents": {
        "name": "Dokumente",
        "icon": "folder",
        "permissions": [
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential", "documents.upload",
            "documents.delete", "documents.manage",
        ],
    },
    "members": {
        "name": "Mitglieder",
        "icon": "people",
        "permissions": [
            "members.view", "members.view_details",
            "members.invite", "members.edit", "members.remove",
            "members.manage_roles",
        ],
    },
    "organization": {
        "name": "Organisation",
        "icon": "settings",
        "permissions": [
            "organization.view", "organization.edit",
            "organization.manage_roles", "organization.api_tokens",
            "organization.audit_log", "organization.admin",
        ],
    },
    "guests": {
        "name": "Gäste",
        "icon": "person_add",
        "permissions": ["guests.invite", "guests.manage"],
    },
    "support": {
        "name": "Support",
        "icon": "help",
        "permissions": ["support.view", "support.create", "support.admin"],
    },
}


# =============================================================================
# DEFAULT ROLES
# Based on typical German faction structure (see Volt Münster Handreichung)
# =============================================================================

DEFAULT_ROLES = {
    # -------------------------------------------------------------------------
    # ADMIN - Full system access
    # -------------------------------------------------------------------------
    "admin": {
        "name": "Administrator",
        "description": "Vollständiger Zugriff auf alle Funktionen der Organisation.",
        "permissions": list(PERMISSIONS.keys()),
        "is_system_role": True,
        "is_admin": True,
        "priority": 100,
        "color": "#dc2626",  # Red
    },

    # -------------------------------------------------------------------------
    # FRAKTIONSVORSITZ - Faction Chair
    # Manages meetings, grants speaking rights, approves agenda
    # -------------------------------------------------------------------------
    "faction_chair": {
        "name": "Fraktionsvorsitz",
        "description": (
            "Leitet die Fraktionssitzungen, erteilt Rederecht, genehmigt "
            "Tagesordnungspunkte und hat weitreichende Verwaltungsrechte."
        ),
        "permissions": [
            # Dashboard
            "dashboard.view",
            # Faction meetings - full control
            "faction.view_public", "faction.view_non_public",
            "faction.create", "faction.edit", "faction.delete",
            "faction.start", "faction.invite", "faction.manage",
            # Agenda - full control including approval
            "agenda.view", "agenda.create", "agenda.edit", "agenda.delete",
            "agenda.approve", "agenda.reorder",
            # Voting & Speaking
            "voting.participate",
            "speaking.automatic", "speaking.grant",
            # Motions
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.edit_all", "motions.approve",
            "motions.submit_to_ris", "motions.share", "motions.comment",
            # Protocols
            "protocols.view_public", "protocols.view_full",
            "protocols.create", "protocols.edit",
            "protocols.approve", "protocols.publish",
            # Meetings (RIS)
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            # Tasks
            "tasks.view", "tasks.view_all", "tasks.create",
            "tasks.assign", "tasks.manage",
            # RIS
            "ris.view", "ris.notes", "ris.subscribe",
            # Workgroups
            "workgroups.view", "workgroups.join",
            "workgroups.create", "workgroups.manage",
            # Documents
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential", "documents.upload",
            "documents.delete", "documents.manage",
            # Members
            "members.view", "members.view_details",
            "members.invite", "members.edit", "members.manage_roles",
            # Organization
            "organization.view", "organization.edit",
            "organization.api_tokens", "organization.audit_log",
            # Guests
            "guests.invite", "guests.manage",
            # Support
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 95,
        "color": "#7c3aed",  # Purple
    },

    # -------------------------------------------------------------------------
    # STELLVERTRETENDER VORSITZ - Vice Chair
    # Same as chair but slightly lower priority
    # -------------------------------------------------------------------------
    "faction_vice_chair": {
        "name": "Stellv. Vorsitz",
        "description": (
            "Stellvertretende Sitzungsleitung mit denselben Rechten "
            "wie der Vorsitz, vertritt bei Abwesenheit."
        ),
        "permissions": [
            # Same as faction_chair
            "dashboard.view",
            "faction.view_public", "faction.view_non_public",
            "faction.create", "faction.edit",
            "faction.start", "faction.invite", "faction.manage",
            "agenda.view", "agenda.create", "agenda.edit",
            "agenda.approve", "agenda.reorder",
            "voting.participate",
            "speaking.automatic", "speaking.grant",
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.edit_all", "motions.approve",
            "motions.submit_to_ris", "motions.share", "motions.comment",
            "protocols.view_public", "protocols.view_full",
            "protocols.create", "protocols.edit", "protocols.approve",
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            "tasks.view", "tasks.view_all", "tasks.create",
            "tasks.assign", "tasks.manage",
            "ris.view", "ris.notes", "ris.subscribe",
            "workgroups.view", "workgroups.join",
            "workgroups.create", "workgroups.manage",
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential", "documents.upload",
            "members.view", "members.view_details",
            "members.invite", "members.manage_roles",
            "organization.view",
            "guests.invite", "guests.manage",
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 90,
        "color": "#8b5cf6",  # Lighter purple
    },

    # -------------------------------------------------------------------------
    # FRAKTIONSMITGLIED - Faction Member (Council Member)
    # Full participation, voting rights, can add agenda items directly
    # -------------------------------------------------------------------------
    "faction_member": {
        "name": "Fraktionsmitglied",
        "description": (
            "Gewähltes Ratsmitglied mit vollem Stimm- und Rederecht. "
            "Kann Tagesordnungspunkte direkt erstellen und über die "
            "Tagesordnung mitentscheiden."
        ),
        "permissions": [
            "dashboard.view",
            # Faction - full access
            "faction.view_public", "faction.view_non_public",
            # Agenda - can create directly (Hoheit über TO)
            "agenda.view", "agenda.create", "agenda.edit",
            # Full voting and speaking rights
            "voting.participate",
            "speaking.automatic",
            # Motions - create and edit own
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.share", "motions.comment",
            # Protocols
            "protocols.view_public", "protocols.view_full",
            # Meetings (RIS)
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            # Tasks
            "tasks.view", "tasks.view_all", "tasks.create", "tasks.assign",
            # RIS
            "ris.view", "ris.notes", "ris.subscribe",
            # Workgroups
            "workgroups.view", "workgroups.join", "workgroups.create",
            # Documents
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential", "documents.upload",
            # Members
            "members.view", "members.view_details",
            # Guests
            "guests.invite",
            # Support
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 80,
        "color": "#2563eb",  # Blue
    },

    # -------------------------------------------------------------------------
    # SACHKUNDIGE/R BÜRGER/IN - Expert Citizen
    # Advisory role, can view non-public (after oath), propose agenda items
    # -------------------------------------------------------------------------
    "expert_citizen": {
        "name": "Sachkundige/r Bürger/in",
        "description": (
            "Feste fachliche Unterstützung in bestimmten Themen- und "
            "Ausschussbereichen. Kann Tagesordnungspunkte vorschlagen "
            "(72h Frist, zur Genehmigung). Rederecht im Themenbereich. "
            "Kein Stimmrecht."
        ),
        "permissions": [
            "dashboard.view",
            # Faction - full access (after oath)
            "faction.view_public", "faction.view_non_public",
            # Agenda - can PROPOSE (needs approval)
            "agenda.view", "agenda.propose",
            # NO voting, speaking in topic area
            "speaking.in_topic",
            # Motions - can view, create, edit own, and comment
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.comment",
            # Protocols - full access
            "protocols.view_public", "protocols.view_full",
            # Meetings (RIS)
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            # Tasks - own tasks
            "tasks.view", "tasks.create", "tasks.edit",
            # RIS
            "ris.view", "ris.notes", "ris.subscribe",
            # Workgroups
            "workgroups.view", "workgroups.join",
            # Documents
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential",
            # Members
            "members.view",
            # Support
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 60,
        "color": "#0891b2",  # Cyan
    },

    # -------------------------------------------------------------------------
    # BEZIRKSVERTRETER/IN - District Representative
    # Similar to Expert Citizen, different context
    # -------------------------------------------------------------------------
    "district_representative": {
        "name": "Bezirksvertreter/in",
        "description": (
            "Feste fachliche Unterstützung aus der Bezirksvertretung. "
            "Kann Tagesordnungspunkte vorschlagen (72h Frist, zur Genehmigung). "
            "Rederecht im Themenbereich. Kein Stimmrecht."
        ),
        "permissions": [
            "dashboard.view",
            # Faction - full access (after oath)
            "faction.view_public", "faction.view_non_public",
            # Agenda - can PROPOSE (needs approval)
            "agenda.view", "agenda.propose",
            # NO voting, speaking in topic area
            "speaking.in_topic",
            # Motions - can view, create, edit own, and comment
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.comment",
            # Protocols - full access
            "protocols.view_public", "protocols.view_full",
            # Meetings (RIS)
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            # Tasks - own tasks
            "tasks.view", "tasks.create", "tasks.edit",
            # RIS
            "ris.view", "ris.notes", "ris.subscribe",
            # Workgroups
            "workgroups.view", "workgroups.join",
            # Documents
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential",
            # Members
            "members.view",
            # Support
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 60,
        "color": "#059669",  # Emerald
    },

    # -------------------------------------------------------------------------
    # FRAKTIONSPERSONAL - Faction Staff
    # Support role, organization & documentation
    # -------------------------------------------------------------------------
    "faction_staff": {
        "name": "Fraktionspersonal",
        "description": (
            "Unterstützung bei Organisation, Dokumentation und inhaltlicher "
            "Vorbereitung. Zugang zum nicht-öffentlichen Teil nach Verpflichtung. "
            "Redebeiträge nach Absprache. Kein Stimmrecht."
        ),
        "permissions": [
            "dashboard.view",
            # Faction - full access (after oath)
            "faction.view_public", "faction.view_non_public",
            "faction.create", "faction.edit",  # Can help organize
            # Agenda - can create (administrative support)
            "agenda.view", "agenda.create", "agenda.edit",
            # NO voting, speaking on request
            "speaking.on_request",
            # Motions - full view, can help create
            "motions.view", "motions.view_drafts",
            "motions.create", "motions.edit", "motions.comment",
            # Protocols - create and edit (main documentation task)
            "protocols.view_public", "protocols.view_full",
            "protocols.create", "protocols.edit",
            # Meetings (RIS)
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            # Tasks - manage all (administrative)
            "tasks.view", "tasks.view_all", "tasks.create",
            "tasks.assign", "tasks.manage",
            # RIS
            "ris.view", "ris.notes", "ris.subscribe",
            # Workgroups
            "workgroups.view", "workgroups.join",
            # Documents - full internal access
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential", "documents.upload",
            "documents.manage",
            # Members
            "members.view", "members.view_details",
            # Organization - view settings
            "organization.view",
            # Guests
            "guests.invite", "guests.manage",
            # Support
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 55,
        "color": "#ca8a04",  # Yellow/Amber
    },

    # -------------------------------------------------------------------------
    # PARTEIMITGLIED - Party Member (NOT on council)
    # Public access only, can suggest topics
    # HINWEIS: Aktuell noch kein aktiver Zugang - Rolle für spätere Nutzung
    # -------------------------------------------------------------------------
    "party_member": {
        "name": "Parteimitglied",
        "description": (
            "Zugang zum öffentlichen Teil der Fraktionssitzung (zukünftig). "
            "Kann Themen für die Tagesordnung vorschlagen (72h Frist). "
            "Rederecht auf Anfrage, wenn eingeräumt. Kein Stimmrecht. "
            "Kein Zugang zu nicht-öffentlichen Inhalten. "
            "HINWEIS: Aktuell noch nicht freigeschaltet."
        ),
        "permissions": [
            "dashboard.view",
            # Faction - PUBLIC ONLY
            "faction.view_public",
            # Agenda - can SUGGEST only (longer process)
            "agenda.view", "agenda.suggest",
            # Speaking on request only
            "speaking.on_request",
            # Motions - view public only
            "motions.view", "motions.comment",
            # Protocols - public only
            "protocols.view_public",
            # Meetings (RIS) - public
            "meetings.view",
            # Tasks - own only
            "tasks.view",
            # RIS
            "ris.view",
            # Workgroups
            "workgroups.view",
            # Documents - public only
            "documents.view_public",
            # Members - basic list
            "members.view",
            # Support
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 30,
        "color": "#6366f1",  # Indigo (party color)
    },

    # -------------------------------------------------------------------------
    # HINWEIS: Gäste und interessierte Bürger*innen haben KEINEN Zugang
    # zu Mandari Work. Sie können nur über das öffentliche Insight-Portal
    # oder per E-Mail Themen einreichen.
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # GESCHÄFTSFÜHRUNG - Managing Director (if applicable)
    # Administrative without voting
    # -------------------------------------------------------------------------
    "managing_director": {
        "name": "Geschäftsführung",
        "description": (
            "Geschäftsführende Verwaltung der Organisation. "
            "Volle administrative Rechte, aber kein Stimmrecht bei "
            "politischen Entscheidungen."
        ),
        "permissions": [
            "dashboard.view",
            # Faction - full access
            "faction.view_public", "faction.view_non_public",
            "faction.create", "faction.edit", "faction.manage",
            # Agenda - full control
            "agenda.view", "agenda.create", "agenda.edit",
            "agenda.delete", "agenda.reorder",
            # NO voting, can speak
            "speaking.automatic",
            # Motions - full edit access
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.edit_all",
            "motions.submit_to_ris", "motions.share", "motions.comment",
            # Protocols - full
            "protocols.view_public", "protocols.view_full",
            "protocols.create", "protocols.edit",
            "protocols.approve", "protocols.publish",
            # Meetings
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            # Tasks - full
            "tasks.view", "tasks.view_all", "tasks.create",
            "tasks.assign", "tasks.manage",
            # RIS
            "ris.view", "ris.notes", "ris.subscribe",
            # Workgroups - full
            "workgroups.view", "workgroups.join",
            "workgroups.create", "workgroups.manage",
            # Documents - full
            "documents.view_public", "documents.view_internal",
            "documents.view_confidential", "documents.upload",
            "documents.delete", "documents.manage",
            # Members - full management
            "members.view", "members.view_details",
            "members.invite", "members.edit", "members.remove",
            "members.manage_roles",
            # Organization - full admin
            "organization.view", "organization.edit",
            "organization.manage_roles", "organization.api_tokens",
            "organization.audit_log",
            # Guests
            "guests.invite", "guests.manage",
            # Support
            "support.view", "support.create", "support.admin",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 85,
        "color": "#0d9488",  # Teal
    },

    # -------------------------------------------------------------------------
    # AG-SPRECHER/IN - Working Group Speaker
    # Leads a working group
    # -------------------------------------------------------------------------
    "workgroup_speaker": {
        "name": "AG-Sprecher/in",
        "description": (
            "Leitet eine Arbeitsgruppe. Kann AG-Sitzungen organisieren "
            "und Ergebnisse für die Fraktionssitzung vorbereiten."
        ),
        "permissions": [
            "dashboard.view",
            # Faction - full access
            "faction.view_public", "faction.view_non_public",
            # Agenda - can propose from AG
            "agenda.view", "agenda.propose",
            # Speaking in topic
            "speaking.automatic",
            # Motions
            "motions.view", "motions.view_drafts", "motions.create",
            "motions.edit", "motions.comment",
            # Protocols
            "protocols.view_public", "protocols.view_full",
            # Meetings
            "meetings.view", "meetings.view_non_public",
            "meetings.prepare", "meetings.notes",
            # Tasks
            "tasks.view", "tasks.view_all", "tasks.create", "tasks.assign",
            # RIS
            "ris.view", "ris.notes", "ris.subscribe",
            # Workgroups - manage own
            "workgroups.view", "workgroups.join",
            "workgroups.create", "workgroups.manage",
            # Documents
            "documents.view_public", "documents.view_internal",
            "documents.upload",
            # Members
            "members.view", "members.view_details",
            # Support
            "support.view", "support.create",
        ],
        "is_system_role": True,
        "is_admin": False,
        "priority": 50,
        "color": "#f59e0b",  # Amber
    },
}


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_all_permissions() -> List[str]:
    """Get list of all permission codenames."""
    return list(PERMISSIONS.keys())


def get_permission_choices() -> List[tuple]:
    """Get permission choices for Django model fields."""
    return [(code, name) for code, name in PERMISSIONS.items()]


def get_permissions_by_category() -> dict:
    """
    Get permissions grouped by category.

    Returns:
        Dict with category info and permission details
    """
    result = {}
    for cat_code, cat_info in PERMISSION_CATEGORIES.items():
        result[cat_code] = {
            "name": cat_info["name"],
            "icon": cat_info.get("icon", ""),
            "permissions": [
                {"code": perm, "name": PERMISSIONS.get(perm, perm)}
                for perm in cat_info["permissions"]
                if perm in PERMISSIONS
            ],
        }
    return result


def get_role_info(role_key: str) -> dict:
    """Get info about a default role."""
    return DEFAULT_ROLES.get(role_key, {})


def get_all_default_roles() -> dict:
    """Get all default role definitions."""
    return DEFAULT_ROLES


class PermissionChecker:
    """
    Utility class for checking user permissions.

    Usage:
        checker = PermissionChecker(membership)
        if checker.has_permission("motions.create"):
            ...

        # Or with multiple permissions
        if checker.has_any_permission(["motions.edit", "motions.edit_all"]):
            ...
    """

    def __init__(self, membership):
        """
        Initialize with a membership instance.

        Args:
            membership: Membership model instance
        """
        self.membership = membership
        self._permissions: Set[str] = None
        self._denied: Set[str] = None

    @property
    def permissions(self) -> Set[str]:
        """Get the set of granted permissions."""
        if self._permissions is None:
            self._load_permissions()
        return self._permissions

    @property
    def denied_permissions(self) -> Set[str]:
        """Get the set of explicitly denied permissions."""
        if self._denied is None:
            self._load_permissions()
        return self._denied

    def _load_permissions(self):
        """Load permissions from membership."""
        self._permissions = set()
        self._denied = set()

        # Get denied permissions first (highest priority)
        for perm in self.membership.denied_permissions.all():
            self._denied.add(perm.codename)

        # Get individual permissions
        for perm in self.membership.individual_permissions.all():
            if perm.codename not in self._denied:
                self._permissions.add(perm.codename)

        # Get role-based permissions
        for role in self.membership.roles.all():
            if role.is_admin:
                # Admin role grants all permissions
                for perm_code in PERMISSIONS.keys():
                    if perm_code not in self._denied:
                        self._permissions.add(perm_code)
            else:
                for perm in role.permissions.all():
                    if perm.codename not in self._denied:
                        self._permissions.add(perm.codename)

    def has_permission(self, permission: str) -> bool:
        """
        Check if the user has a specific permission.

        Args:
            permission: Permission codename (e.g., "motions.create")

        Returns:
            True if the permission is granted
        """
        # Check for explicit denial first
        if permission in self.denied_permissions:
            return False

        # Check if admin (has all permissions)
        if self.membership.roles.filter(is_admin=True).exists():
            return True

        return permission in self.permissions

    def has_any_permission(self, permissions: List[str]) -> bool:
        """
        Check if the user has any of the given permissions.

        Args:
            permissions: List of permission codenames

        Returns:
            True if at least one permission is granted
        """
        return any(self.has_permission(p) for p in permissions)

    def has_all_permissions(self, permissions: List[str]) -> bool:
        """
        Check if the user has all of the given permissions.

        Args:
            permissions: List of permission codenames

        Returns:
            True if all permissions are granted
        """
        return all(self.has_permission(p) for p in permissions)

    def is_admin(self) -> bool:
        """Check if the user is an admin."""
        return self.membership.roles.filter(is_admin=True).exists()

    def has_voting_rights(self) -> bool:
        """Check if the user has voting rights."""
        return self.has_permission("voting.participate")

    def has_speaking_rights(self) -> bool:
        """Check if the user has automatic speaking rights."""
        return self.has_permission("speaking.automatic")

    def can_access_non_public(self) -> bool:
        """
        Check if user can access non-public faction content.

        Requires BOTH:
        - faction.view_non_public permission
        - is_sworn_in flag on membership (Verpflichtungserklärung)
        """
        return (
            self.has_permission("faction.view_non_public")
            and getattr(self.membership, "is_sworn_in", False)
        )

    def can_propose_agenda_items(self) -> bool:
        """Check if user can propose agenda items (needs approval)."""
        return self.has_any_permission(["agenda.create", "agenda.propose"])

    def can_create_agenda_items_directly(self) -> bool:
        """Check if user can create agenda items directly (no approval needed)."""
        return self.has_permission("agenda.create")

    def can_approve_agenda_items(self) -> bool:
        """Check if user can approve agenda proposals."""
        return self.has_permission("agenda.approve")

    def can_start_faction_meeting(self) -> bool:
        """Check if user can start/end faction meetings."""
        return self.has_permission("faction.start")

    def can_invite_to_faction_meeting(self) -> bool:
        """Check if user can send faction meeting invitations."""
        return self.has_permission("faction.invite")

    def can_manage_faction_meeting(self) -> bool:
        """Check if user can fully manage faction meetings (status changes, etc.)."""
        return self.has_permission("faction.manage")

    def can_create_protocols(self) -> bool:
        """Check if user can create protocols during meetings."""
        return self.has_permission("protocols.create")

    def can_edit_protocols(self) -> bool:
        """Check if user can edit protocols after meetings."""
        return self.has_permission("protocols.edit")
