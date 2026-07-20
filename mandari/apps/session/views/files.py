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
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from .. import audit
from ..models import (
    SessionAgendaItem,
    SessionFile,
    SessionMeeting,
    SessionPaper,
)
from ..permissions import SessionMixin, SessionPermissionChecker
from ..services import file_service

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


def _file_parent(session_file: SessionFile):
    """Elternobjekt einer Anlage (Vorlage, TOP oder Sitzung)."""
    if session_file.paper_id:
        return session_file.paper
    if session_file.agenda_item_id:
        return session_file.agenda_item
    if session_file.meeting_id:
        return session_file.meeting
    return None


def can_view_file(session_user, session_file: SessionFile) -> bool:
    """
    Prüft, ob ein Nutzer eine Anlage sehen/herunterladen darf.

    Regeln:
    - Basis-Sichtberechtigung des Elternobjekts (view_papers/view_meetings)
    - NÖ-Anlage oder NÖ-Elternobjekt: zusätzlich die NÖ-Berechtigung
    """
    if session_user is None:
        return False
    checker = SessionPermissionChecker(session_user)

    parent = _file_parent(session_file)
    if session_file.paper_id:
        base_perm, np_perm = "view_papers", "view_non_public_papers"
        parent_public = parent.is_public if parent else True
    elif session_file.agenda_item_id:
        base_perm, np_perm = "view_meetings", "view_non_public_meetings"
        parent_public = (parent.is_public and parent.meeting.is_public) if parent else True
    elif session_file.meeting_id:
        base_perm, np_perm = "view_meetings", "view_non_public_meetings"
        parent_public = parent.is_public if parent else True
    else:
        # Anlage ohne Elternobjekt: restriktiv behandeln
        base_perm, np_perm = "view_papers", "view_non_public_papers"
        parent_public = True

    if not checker.has_permission(base_perm):
        return False
    if not session_file.is_public or not parent_public:
        return checker.has_permission(np_perm)
    return True


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
                file=uploaded,
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
            session_file.save()
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

        audit.log_event("download", session_file)

        try:
            handle = session_file.file.open("rb")
        except (FileNotFoundError, ValueError):
            raise Http404("Datei nicht gefunden")

        response = FileResponse(
            handle,
            as_attachment=True,
            filename=session_file.name,
            content_type=session_file.mime_type or "application/octet-stream",
        )
        response["X-Content-Type-Options"] = "nosniff"
        return response


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

        session_file.file = uploaded
        session_file.name = uploaded.name
        session_file.mime_type = mime_type
        session_file.size = uploaded.size
        session_file.text_content = file_service.extract_text(data, mime_type, uploaded.name)
        session_file.version = old_version + 1
        session_file.save()

        audit.log_event(
            "replace",
            session_file,
            changes={
                "datei": {"alt": old_name, "neu": session_file.name},
                "version": {"alt": old_version, "neu": session_file.version},
            },
        )

        messages.success(request, f"Anlage wurde ersetzt (Version {session_file.version}).")
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
