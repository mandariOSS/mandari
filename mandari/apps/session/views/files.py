# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datei-Upload und Anlagenverwaltung für das Session RIS (Issue #25).

Anlagen können an Vorlagen, Sitzungen und Tagesordnungspunkte gehängt
werden — jeweils mit Ö/NÖ-Kennzeichnung je Anlage.

Sicherheit:
- Nichtöffentliche Anlagen sind ausschließlich über die geschützte
  Download-View erreichbar (kein direktes Media-URL-Leak; /media/ blockt
  den Pfad session/files/).
- Upload/Ersetzen/Löschen erfordern die Edit-Berechtigung des jeweiligen
  Elternobjekts, NÖ-Downloads die entsprechende NÖ-Sichtberechtigung.

Fassungen (Issue #226): Ersetzen legt eine neue Fassung an, die bisherige bleibt im Verlauf
abrufbar – mit genau der Sichtbarkeit der Anlage. Gespeichert wird über
``file_version_service`` (Deduplizierung je Mandant über SHA-256). Die Datenschutz-Löschung
eines Inhalts braucht das Einstellungsrecht.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic.base import ContextMixin

from .. import audit
from ..models import (
    SessionAgendaItem,
    SessionFile,
    SessionFileBlob,
    SessionFileVersion,
    SessionMeeting,
    SessionPaper,
)
from ..permissions import SessionMixin, SessionPermissionChecker, SessionViewMixin
from ..services import file_service, file_version_service
from .nexturl import safe_next_url
from .paper_versions import protected_download

# =============================================================================
# HELPERS
# =============================================================================


def _resolve_target(session_tenant, target_type: str, target_id):
    """Zielobjekt (Vorlage/Sitzung/TOP) tenant-sicher auflösen."""
    if target_type == "paper":
        return get_object_or_404(SessionPaper, pk=target_id, tenant=session_tenant)
    if target_type == "meeting":
        return get_object_or_404(SessionMeeting, pk=target_id, tenant=session_tenant)
    if target_type == "agenda_item":
        return get_object_or_404(SessionAgendaItem, pk=target_id, meeting__tenant=session_tenant)
    raise Http404("Unbekannter Anlagen-Typ")


def _edit_permission(file_or_target) -> str:
    """Benötigte Edit-Berechtigung für ein Datei-Elternobjekt."""
    if isinstance(file_or_target, SessionPaper) or getattr(file_or_target, "paper_id", None):
        return "edit_papers"
    return "edit_meetings"


def can_view_file(session_user, session_file: SessionFile) -> bool:
    """
    Prüft, ob ein Nutzer eine Anlage sehen/herunterladen darf.

    Regeln (file_service.file_visible, dieselbe Prüfung nutzt die Sitzungsmappe):
    - Basis-Sichtberechtigung des Elternobjekts (view_papers/view_meetings)
    - NÖ-Anlage oder NÖ-Elternobjekt: zusätzlich die NÖ-Berechtigung
    """
    if session_user is None:
        return False
    return file_service.file_visible(SessionPermissionChecker(session_user).permissions, session_file)


def _download_changes(session_file: SessionFile, **extra) -> dict:
    """Angaben zum Download im Protokoll: Kennzeichen „nichtöffentlich“ (Issue #221), nie Inhalte."""
    changes = dict(extra)
    if file_service.is_non_public(session_file):
        changes["nichtoeffentlich"] = True
    return changes


def _redirect_to_parent(tenant_slug: str, session_file: SessionFile):
    """Nach einer Datei-Aktion zurück zur Detailseite des Elternobjekts."""
    if session_file.paper_id:
        return redirect("session:paper_detail", tenant_slug=tenant_slug, paper_id=session_file.paper_id)
    if session_file.agenda_item_id:
        return redirect(
            "session:meeting_detail",
            tenant_slug=tenant_slug,
            meeting_id=session_file.agenda_item.meeting_id,
        )
    if session_file.meeting_id:
        return redirect("session:meeting_detail", tenant_slug=tenant_slug, meeting_id=session_file.meeting_id)
    return redirect("session:dashboard", tenant_slug=tenant_slug)


# =============================================================================
# VIEWS
# =============================================================================


class FileUploadView(SessionMixin, View):
    """Mehrfach-Upload von Anlagen an Vorlage, Sitzung oder TOP."""

    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        target_type = request.POST.get("target_type", "")
        target_id = request.POST.get("target_id", "")
        target = _resolve_target(self.session_tenant, target_type, target_id)

        checker = SessionPermissionChecker(self.session_user)
        if not checker.has_permission(_edit_permission(target)):
            raise PermissionDenied("Fehlende Berechtigung")

        uploads = request.FILES.getlist("files")
        if not uploads:
            messages.error(request, "Keine Dateien ausgewählt.")
            return self._redirect(tenant_slug, target_type, target)

        is_public = request.POST.get("is_public") == "on"

        created = 0
        for uploaded in uploads:
            try:
                file_service.validate_upload(uploaded)
            except file_service.FileValidationError as exc:
                messages.error(request, str(exc))
                continue
            except Exception:
                messages.error(request, f"Datei '{uploaded.name}' wurde vom Virenscan abgelehnt.")
                continue

            mime_type = uploaded.content_type or file_service.guess_mime_type(uploaded.name)
            data = uploaded.read()
            uploaded.seek(0)

            session_file = SessionFile(
                tenant=self.session_tenant,
                name=uploaded.name,
                mime_type=mime_type,
                size=uploaded.size,
                is_public=is_public,
                created_by=self.session_user,
                text_content=file_service.extract_text(data, mime_type, uploaded.name),
            )
            if target_type == "paper":
                session_file.paper = target
            elif target_type == "meeting":
                session_file.meeting = target
            else:
                session_file.agenda_item = target
            # Speichert Inhalt (dedupliziert) und Anlage, legt Fassung 1 an (Issue #226)
            file_version_service.attach_upload(session_file, uploaded, user=self.session_user)
            created += 1

        if created:
            messages.success(request, f"{created} Anlage{'n' if created != 1 else ''} hochgeladen.")
        return self._redirect(tenant_slug, target_type, target)

    def _redirect(self, tenant_slug, target_type, target):
        if target_type == "paper":
            return redirect("session:paper_detail", tenant_slug=tenant_slug, paper_id=target.pk)
        if target_type == "agenda_item":
            return redirect("session:meeting_detail", tenant_slug=tenant_slug, meeting_id=target.meeting_id)
        return redirect("session:meeting_detail", tenant_slug=tenant_slug, meeting_id=target.pk)


class FileDownloadView(SessionMixin, View):
    """Geschützte Auslieferung von Anlagen (kein direktes Media-URL-Leak)."""

    def get(self, request, tenant_slug, file_id):
        session_file = get_object_or_404(SessionFile, pk=file_id, tenant=self.session_tenant)

        if not can_view_file(self.session_user, session_file):
            raise PermissionDenied("Keine Berechtigung für diese Anlage")

        # Jeder Download ist ein Lesezugriff; nichtöffentliche sind gekennzeichnet (Issue #221)
        audit.log_event("download", session_file, changes=_download_changes(session_file))

        try:
            handle = session_file.file.open("rb")
        except (FileNotFoundError, ValueError):
            raise Http404("Datei nicht gefunden") from None

        return protected_download(handle, session_file.name, session_file.mime_type)


class FileUpdateView(SessionMixin, View):
    """Ö/NÖ-Kennzeichnung und Namen einer Anlage ändern (mit Audit-Eintrag)."""

    http_method_names = ["post"]

    def post(self, request, tenant_slug, file_id):
        session_file = get_object_or_404(SessionFile, pk=file_id, tenant=self.session_tenant)

        checker = SessionPermissionChecker(self.session_user)
        if not checker.has_permission(_edit_permission(session_file)):
            raise PermissionDenied("Fehlende Berechtigung")

        if "name" in request.POST and request.POST["name"].strip():
            session_file.name = request.POST["name"].strip()[:500]
        session_file.is_public = request.POST.get("is_public") == "on"
        session_file.save()

        messages.success(request, f"Anlage „{session_file.name}“ wurde aktualisiert.")
        return _redirect_to_parent(tenant_slug, session_file)


class FileReplaceView(SessionMixin, View):
    """Anlage durch eine neue Datei ersetzen (Versionierung + Audit)."""

    http_method_names = ["post"]

    def post(self, request, tenant_slug, file_id):
        session_file = get_object_or_404(SessionFile, pk=file_id, tenant=self.session_tenant)

        checker = SessionPermissionChecker(self.session_user)
        if not checker.has_permission(_edit_permission(session_file)):
            raise PermissionDenied("Fehlende Berechtigung")

        uploaded = request.FILES.get("file")
        if not uploaded:
            messages.error(request, "Keine Datei ausgewählt.")
            return _redirect_to_parent(tenant_slug, session_file)

        try:
            file_service.validate_upload(uploaded)
        except file_service.FileValidationError as exc:
            messages.error(request, str(exc))
            return _redirect_to_parent(tenant_slug, session_file)
        except Exception:
            messages.error(request, f"Datei '{uploaded.name}' wurde vom Virenscan abgelehnt.")
            return _redirect_to_parent(tenant_slug, session_file)

        old_name = session_file.name
        old_version = session_file.version

        mime_type = uploaded.content_type or file_service.guess_mime_type(uploaded.name)
        data = uploaded.read()
        uploaded.seek(0)

        # Neue Fassung; die bisherige bleibt im Verlauf abrufbar (Issue #226)
        new_version = file_version_service.replace_content(
            session_file,
            uploaded,
            user=self.session_user,
            mime_type=mime_type,
            text_content=file_service.extract_text(data, mime_type, uploaded.name),
        )
        if new_version is None:
            messages.info(request, "Die Datei gleicht der aktuellen Fassung – es wurde nichts geändert.")
            return _redirect_to_parent(tenant_slug, session_file)

        audit.log_event(
            "replace",
            session_file,
            changes={
                "datei": {"alt": old_name, "neu": session_file.name},
                "version": {"alt": old_version, "neu": session_file.version},
            },
        )

        messages.success(
            request,
            f"Anlage wurde ersetzt (Version {session_file.version}). Die bisherige Fassung bleibt im Verlauf abrufbar.",
        )
        return _redirect_to_parent(tenant_slug, session_file)


class FileDeleteView(SessionMixin, View):
    """Anlage löschen (Audit-Eintrag über delete-Signal)."""

    http_method_names = ["post"]

    def post(self, request, tenant_slug, file_id):
        session_file = get_object_or_404(SessionFile, pk=file_id, tenant=self.session_tenant)

        checker = SessionPermissionChecker(self.session_user)
        if not checker.has_permission(_edit_permission(session_file)):
            raise PermissionDenied("Fehlende Berechtigung")

        response = _redirect_to_parent(tenant_slug, session_file)
        name = session_file.name
        session_file.delete()
        messages.success(self.request, f"Anlage „{name}“ wurde gelöscht.")
        return response


# =============================================================================
# FASSUNGEN EINER ANLAGE (Issue #226)
# =============================================================================


class _FileVersionMixin(SessionViewMixin, ContextMixin, View):
    """Anlage mandantengefiltert laden; Sichtbarkeit genau wie die Anlage selbst."""

    def get_visible_file(self, file_id):
        session_file = get_object_or_404(
            SessionFile.objects.select_related("paper", "meeting", "agenda_item__meeting"),
            pk=file_id,
            tenant=self.session_tenant,
        )
        if not can_view_file(self.session_user, session_file):
            raise PermissionDenied("Keine Berechtigung für diese Anlage")
        return session_file


class FileVersionListView(_FileVersionMixin):
    """Verlauf einer Anlage: aktuelle und frühere Fassungen zum Herunterladen."""

    http_method_names = ["get"]

    def get(self, request, tenant_slug, file_id):
        session_file = self.get_visible_file(file_id)
        checker = SessionPermissionChecker(self.session_user)
        older = file_version_service.history(session_file)
        known = {version.number for version in older}
        context = self.get_context_data(
            session_file=session_file,
            parent=file_service.file_parent(session_file),
            older_versions=older,
            # Bestand vor der Versionierung: frühere Nummern ohne gespeicherten Inhalt
            history_incomplete=any(number not in known for number in range(1, session_file.version)),
            purgeable=(
                file_version_service.purgeable_blob_ids(version.blob for version in older)
                if checker.has_permission("manage_settings")
                else set()
            ),
        )
        return render(request, "session/files/versions.html", context)


class FileVersionDownloadView(_FileVersionMixin):
    """Frühere Fassung einer Anlage herunterladen (protokolliert)."""

    http_method_names = ["get"]

    def get(self, request, tenant_slug, file_id, number):
        session_file = self.get_visible_file(file_id)
        version = get_object_or_404(
            SessionFileVersion.objects.select_related("blob"), session_file=session_file, number=number
        )
        handle = file_version_service.open_blob(version.blob)
        if handle is None:
            raise Http404("Der Inhalt dieser Fassung ist nicht mehr vorhanden.")
        audit.log_event("download", session_file, changes=_download_changes(session_file, fassung=version.number))
        return protected_download(handle, version.name, version.mime_type)


class FileContentPurgeView(SessionViewMixin, View):
    """
    Inhalt endgültig löschen (Datenschutz), auch aus gesicherten Fassungen – nur mit dem
    Einstellungsrecht, mit Grund und Audit-Eintrag. Nicht für den aktuellen Inhalt einer
    Anlage und nicht für die beschlossene Fassung (``file_version_service.purge_blocker``).
    """

    http_method_names = ["post"]
    permission_required = "manage_settings"

    def post(self, request, tenant_slug, blob_id):
        blob = get_object_or_404(SessionFileBlob, pk=blob_id, tenant=self.session_tenant)
        try:
            file_version_service.purge_content(blob, user=self.session_user, reason=request.POST.get("reason", ""))
        except file_version_service.PurgeRefusedError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Der Inhalt wurde endgültig gelöscht. Die Fassungen zeigen ihn als gelöscht an.")
        next_url = safe_next_url(request, tenant_slug)
        if next_url:
            return redirect(next_url)
        return redirect("session:dashboard", tenant_slug=tenant_slug)
