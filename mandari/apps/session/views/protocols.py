# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Niederschrift-Workflow für das Session RIS (Issue #31).

Views für:
- Protokoll-Ansicht je Sitzung (Status, TOP-Struktur, Teilnehmerverzeichnis)
- Anlegen + Bearbeiten (allgemeiner Teil Ö/NÖ, TOP-weise Protokolltexte und
  Beschlussergebnisse, Unterschriften-Block)
- Workflow-Aktionen: zur Prüfung geben -> genehmigen (mit
  Genehmigungsvermerk in Folgesitzung) / zurückweisen -> veröffentlichen
- Niederschrift-PDF (Ö-Fassung und interne NÖ-Fassung)
"""

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionAgendaItem, SessionMeeting
from ..permissions import SessionViewMixin
from ..services import agenda_service, protocol_service

_VOTE_RESULTS = {choice[0] for choice in SessionAgendaItem._meta.get_field("vote_result").choices}


def _get_meeting(view, meeting_id):
    qs = SessionMeeting.objects.filter(tenant=view.session_tenant).select_related("organization", "tenant")
    if not view.has_permission("view_non_public_meetings"):
        qs = qs.filter(is_public=True)
    return get_object_or_404(qs, pk=meeting_id)


def _protocol_redirect(view, meeting):
    return redirect(
        "session:meeting_protocol",
        tenant_slug=view.session_tenant.slug,
        meeting_id=meeting.id,
    )


class ProtocolDetailView(SessionViewMixin, TemplateView):
    """Protokoll-Ansicht: Status, Workflow-Aktionen, TOP-Struktur, Teilnehmer."""

    template_name = "session/protocols/detail.html"
    permission_required = "view_protocols"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        protocol = getattr(meeting, "protocol", None)

        can_view_np = self.has_permission("view_non_public_meetings")
        agenda = agenda_service.grouped_agenda(meeting, include_non_public=can_view_np)

        # NÖ-Protokolltexte für Berechtigte entschlüsseln
        if can_view_np:
            for item in agenda["public"] + agenda["non_public"]:
                item.protocol_note_np = item.get_protocol_note_decrypted()
                for sub in item.children_list:
                    sub.protocol_note_np = sub.get_protocol_note_decrypted()

        # Genehmigungsvermerk: mögliche Folgesitzungen des Gremiums
        approval_meetings = SessionMeeting.objects.filter(
            tenant=self.session_tenant,
            organization=meeting.organization,
            start__gt=meeting.start,
        ).order_by("start")[:20]

        context.update(
            {
                "meeting": meeting,
                "protocol": protocol,
                "agenda_public": agenda["public"],
                "agenda_non_public": agenda["non_public"],
                "participants": protocol_service.participant_directory(meeting),
                "content_np": (protocol.get_content_decrypted() or "") if protocol and can_view_np else "",
                "can_view_np": can_view_np,
                "can_create": self.has_permission("create_protocols"),
                "can_edit": self.has_permission("edit_protocols"),
                "can_approve": self.has_permission("approve_protocols"),
                "approval_meetings": approval_meetings,
            }
        )
        return context


class ProtocolCreateView(SessionViewMixin, View):
    """Protokoll zu einer Sitzung anlegen (vorbefüllt aus den Sitzungsdaten)."""

    permission_required = "create_protocols"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, meeting_id):
        meeting = _get_meeting(self, meeting_id)
        _protocol, created = protocol_service.get_or_create_protocol(meeting, created_by=self.session_user)
        if created:
            messages.success(request, "Protokoll wurde angelegt.")
        else:
            messages.info(request, "Für diese Sitzung existiert bereits ein Protokoll.")
        return _protocol_redirect(self, meeting)


class ProtocolEditView(SessionViewMixin, TemplateView):
    """
    Protokoll bearbeiten: allgemeiner Teil (Ö/NÖ), Unterschriften-Block und
    TOP-weise Protokolltexte + Beschlussergebnisse.
    """

    template_name = "session/protocols/form.html"
    permission_required = "edit_protocols"

    def _load(self):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        protocol = getattr(meeting, "protocol", None)
        return meeting, protocol

    def get(self, request, *args, **kwargs):
        meeting, protocol = self._load()
        if protocol is None:
            messages.error(request, "Für diese Sitzung existiert noch kein Protokoll.")
            return _protocol_redirect(self, meeting)
        if protocol.status not in ("draft", "review"):
            messages.error(request, "Genehmigte/veröffentlichte Niederschriften sind nicht mehr bearbeitbar.")
            return _protocol_redirect(self, meeting)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting, protocol = self._load()
        can_view_np = self.has_permission("view_non_public_meetings")
        agenda = agenda_service.grouped_agenda(meeting, include_non_public=can_view_np)
        if can_view_np:
            for item in agenda["public"] + agenda["non_public"]:
                item.protocol_note_np = item.get_protocol_note_decrypted()
                for sub in item.children_list:
                    sub.protocol_note_np = sub.get_protocol_note_decrypted()
        context.update(
            {
                "meeting": meeting,
                "protocol": protocol,
                "agenda_public": agenda["public"],
                "agenda_non_public": agenda["non_public"],
                "content_np": (protocol.get_content_decrypted() or "") if can_view_np else "",
                "can_view_np": can_view_np,
                "vote_choices": SessionAgendaItem._meta.get_field("vote_result").choices,
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        meeting, protocol = self._load()
        if protocol is None:
            messages.error(request, "Für diese Sitzung existiert noch kein Protokoll.")
            return _protocol_redirect(self, meeting)
        if protocol.status not in ("draft", "review"):
            messages.error(request, "Genehmigte/veröffentlichte Niederschriften sind nicht mehr bearbeitbar.")
            return _protocol_redirect(self, meeting)

        can_view_np = self.has_permission("view_non_public_meetings")

        # Allgemeiner Teil + Unterschriften
        protocol.content = request.POST.get("content", "")
        protocol.chair_name = request.POST.get("chair_name", "").strip()[:255]
        protocol.recorder_name = request.POST.get("recorder_name", "").strip()[:255]
        if can_view_np:
            protocol.set_content_encrypted(request.POST.get("content_np", ""))
        protocol.save()

        # TOP-weise Protokolltexte + Beschlussergebnisse
        items = meeting.agenda_items.all()
        if not can_view_np:
            items = items.filter(is_public=True)
        for item in items:
            prefix = str(item.pk)
            if f"protocol_note_{prefix}" not in request.POST:
                continue
            item.protocol_note = request.POST.get(f"protocol_note_{prefix}", "")
            item.resolution_text = request.POST.get(f"resolution_text_{prefix}", item.resolution_text)
            vote = request.POST.get(f"vote_result_{prefix}", item.vote_result)
            if vote in _VOTE_RESULTS:
                item.vote_result = vote
            for field in ("votes_yes", "votes_no", "votes_abstain"):
                raw = request.POST.get(f"{field}_{prefix}", "")
                if raw.isdigit():
                    setattr(item, field, int(raw))
            if can_view_np:
                np_note = request.POST.get(f"protocol_note_np_{prefix}", None)
                if np_note is not None:
                    item.set_protocol_note_encrypted(np_note)
            item.save()

        messages.success(request, "Protokoll wurde gespeichert.")
        if request.POST.get("continue") == "1":
            return redirect(
                "session:meeting_protocol_edit",
                tenant_slug=self.session_tenant.slug,
                meeting_id=meeting.id,
            )
        return _protocol_redirect(self, meeting)


class ProtocolWorkflowView(SessionViewMixin, View):
    """
    Workflow-Aktionen: submit (Entwurf -> Prüfung), reject (Prüfung -> Entwurf),
    approve (Prüfung -> genehmigt, mit Genehmigungsvermerk/Folgesitzung),
    publish (genehmigt -> veröffentlicht, nur Ö-Fassung).
    """

    http_method_names = ["post"]

    # Aktion -> benötigte Berechtigung
    ACTION_PERMS = {
        "submit": "edit_protocols",
        "reject": "approve_protocols",
        "approve": "approve_protocols",
        "publish": "approve_protocols",
    }

    def check_view_permissions(self):
        from django.core.exceptions import PermissionDenied

        action = self.kwargs.get("action")
        permission = self.ACTION_PERMS.get(action)
        if permission is None:
            raise PermissionDenied("Unbekannte Aktion")
        self.permission_required = permission
        self.check_permissions()

    def post(self, request, tenant_slug, meeting_id, action):
        meeting = _get_meeting(self, meeting_id)
        protocol = getattr(meeting, "protocol", None)
        if protocol is None:
            messages.error(request, "Für diese Sitzung existiert noch kein Protokoll.")
            return _protocol_redirect(self, meeting)

        old_status = protocol.status
        if not protocol_service.apply_transition(protocol, action):
            messages.error(
                request,
                f"Aktion nicht möglich: Statusübergang aus „{protocol.get_status_display()}“ unzulässig.",
            )
            return _protocol_redirect(self, meeting)

        if action == "submit":
            protocol.review_requested_by = self.session_user
            protocol.review_requested_at = timezone.now()
            protocol.save()
            messages.success(request, "Protokoll wurde zur Prüfung gegeben.")

        elif action == "reject":
            comment = request.POST.get("comment", "").strip()
            protocol.save()
            # Audit: Zurückweisung mit Kommentar nachvollziehbar machen
            audit.log_event(
                "update",
                protocol,
                user=self.session_user,
                request=request,
                changes={
                    "status": {"alt": old_status, "neu": protocol.status},
                    "zurueckweisungs_kommentar": comment[:300],
                },
            )
            messages.success(request, "Protokoll wurde mit Anmerkungen zurück in den Entwurf gegeben.")

        elif action == "approve":
            protocol.approved_by = self.session_user
            protocol.approved_at = timezone.now()
            approval_meeting_id = request.POST.get("approval_meeting", "")
            if approval_meeting_id:
                protocol.approval_meeting = SessionMeeting.objects.filter(
                    pk=approval_meeting_id,
                    tenant=self.session_tenant,
                    organization=meeting.organization,
                ).first()
            note = request.POST.get("approval_note", "").strip()[:500]
            if note:
                protocol.approval_note = note
            elif protocol.approval_meeting:
                start_local = timezone.localtime(protocol.approval_meeting.start)
                protocol.approval_note = f"Genehmigt in der Sitzung am {start_local.strftime('%d.%m.%Y')}."
            protocol.save()  # Audit: approve-Aktion über Signal
            messages.success(request, "Niederschrift wurde genehmigt.")

        elif action == "publish":
            protocol.published_at = timezone.now()
            protocol.save()  # Audit: publish-Aktion über Signal
            messages.success(request, "Öffentliche Fassung der Niederschrift wurde veröffentlicht.")

        return _protocol_redirect(self, meeting)


class ProtocolPdfView(SessionViewMixin, TemplateView):
    """
    Niederschrift-PDF.

    - Ö-Fassung (Standard): nur öffentliche Inhalte, für view_protocols
    - Interne NÖ-Fassung (?fassung=intern): zusätzlich view_non_public_meetings
    """

    permission_required = "view_protocols"

    def get(self, request, *args, **kwargs):
        from django.core.exceptions import PermissionDenied

        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        protocol = getattr(meeting, "protocol", None)
        if protocol is None:
            messages.error(request, "Für diese Sitzung existiert noch kein Protokoll.")
            return _protocol_redirect(self, meeting)

        internal = request.GET.get("fassung") == "intern"
        if internal and not self.has_permission("view_non_public_meetings"):
            raise PermissionDenied("Fehlende Berechtigung für die interne Fassung")

        pdf_bytes = protocol_service.build_protocol_pdf(protocol, internal=internal)
        filename = "niederschrift-intern.pdf" if internal else "niederschrift.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
