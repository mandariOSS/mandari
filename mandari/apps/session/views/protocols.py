# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Niederschrift-Workflow für das Session RIS (Issue #31).

Views für:
- Protokoll-Ansicht je Sitzung (Status, TOP-Struktur, Teilnehmerverzeichnis, Berichtigungen)
- Anlegen + Bearbeiten (allgemeiner Teil Ö/NÖ, TOP-weise Protokolltexte und
  Beschlussergebnisse, Unterschriften-Block)
- Workflow-Aktionen: zur Prüfung geben -> genehmigen (mit Genehmigungsvermerk und TOP der
  Folgesitzung) / zurückweisen -> veröffentlichen (öffentliche Fassung über OParl und im
  Bürgerportal) -> Veröffentlichung zurücknehmen; je Mandant ohne Genehmigungsschritt
- Berichtigung nach der Genehmigung (Issue #318)
- Niederschrift-PDF (Ö-Fassung und interne NÖ-Fassung)
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionAgendaItem, SessionMeeting, SessionProtocolCorrection, SessionTextBlock, SessionVote
from ..permissions import SessionViewMixin
from ..services import (
    agenda_service,
    four_eyes_service,
    protocol_correction_service,
    protocol_lock,
    protocol_service,
    voting_service,
)

_VOTE_RESULTS = {choice[0] for choice in SessionAgendaItem._meta.get_field("vote_result").choices}
_COUNT_FIELDS = ("votes_yes", "votes_no", "votes_abstain")
#: Farbe des Status-Badges
STATUS_TONES = {"draft": "gray", "review": "amber", "approved": "blue", "published": "green"}


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
    """Protokoll-Ansicht: Status, Workflow-Aktionen, TOP-Struktur, Teilnehmer, Berichtigungen."""

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

        can_approve = self.has_permission("approve_protocols")
        corrections = (
            protocol_correction_service.internal_rows(protocol, can_view_np=can_view_np, actor=self.session_user)
            if protocol
            else []
        )
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
                "can_approve": can_approve,
                "locked": bool(protocol and protocol.is_locked),
                "status_tone": STATUS_TONES.get(protocol.status, "gray") if protocol else "gray",
                "direct_mode": self.session_tenant.protocol_direct_publication,
                "corrections": corrections,
                "can_correct": bool(protocol and protocol.is_locked and can_approve),
            }
        )
        # Genehmigungsvermerk: TOP „Genehmigung der Niederschrift“ bzw. Folgesitzung des Gremiums
        if protocol and protocol.status == "review" and can_approve:
            context["approval_items"] = protocol_service.approval_candidates(meeting, include_non_public=can_view_np)
            # Folgesitzungen des Gremiums – nichtöffentliche nur mit NÖ-Sichtrecht (wie approval_candidates)
            context["approval_meetings"] = (
                SessionMeeting.objects.filter(
                    tenant=self.session_tenant,
                    organization=meeting.organization,
                    start__gt=meeting.start,
                )
                .visible_to(self.session_permissions)
                .order_by("start")[:20]
            )
            # Vier-Augen-Prinzip und Vertretung (Issue #222): Hinweis statt wirkungslosem Knopf
            context["freigabe"] = four_eyes_service.evaluate(
                four_eyes_service.PROCESS_PROTOCOL, protocol, self.session_user
            )
        # Lesezugriff auf nichtöffentliche Niederschriftteile protokollieren (Issue #221)
        np_corrections = any(not row["correction"].is_public for row in corrections)
        if can_view_np and (not meeting.is_public or agenda["non_public"] or context["content_np"] or np_corrections):
            audit.log_read(
                self.request,
                protocol or meeting,
                tenant=self.session_tenant,
                user=self.session_user,
                changes={"umfang": "Niederschrift, nichtöffentlicher Teil"},
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
            messages.error(
                request,
                "Genehmigte/veröffentlichte Niederschriften sind nicht mehr bearbeitbar. "
                "Korrekturen laufen über eine Berichtigung.",
            )
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
                # Textbausteine für die Protokollführung (Issue #85)
                "text_blocks": SessionTextBlock.objects.filter(
                    tenant=self.session_tenant,
                    is_active=True,
                    category__in=["protocol", "general"],
                ),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        meeting, protocol = self._load()
        if protocol is None:
            messages.error(request, "Für diese Sitzung existiert noch kein Protokoll.")
            return _protocol_redirect(self, meeting)
        if protocol.status not in ("draft", "review"):
            messages.error(
                request,
                "Genehmigte/veröffentlichte Niederschriften sind nicht mehr bearbeitbar. "
                "Korrekturen laufen über eine Berichtigung.",
            )
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
        # Stimmrecht (Issue #318): Stimmenzahlen gegen die stimmberechtigten Anwesenden prüfen
        assessed = voting_service.eligibility(meeting)
        for item in items:
            prefix = str(item.pk)
            if f"protocol_note_{prefix}" not in request.POST:
                continue
            item.protocol_note = request.POST.get(f"protocol_note_{prefix}", "")
            item.resolution_text = request.POST.get(f"resolution_text_{prefix}", item.resolution_text)
            vote = request.POST.get(f"vote_result_{prefix}", item.vote_result)
            if vote in _VOTE_RESULTS:
                item.vote_result = vote
            counts = {field: getattr(item, field) for field in _COUNT_FIELDS}
            for field in _COUNT_FIELDS:
                raw = request.POST.get(f"{field}_{prefix}", "")
                if raw.isdigit():
                    counts[field] = min(int(raw), 9999)
            if any(counts[field] != getattr(item, field) for field in _COUNT_FIELDS):
                check = voting_service.check_counts(
                    item, counts["votes_yes"], counts["votes_no"], counts["votes_abstain"], assessed=assessed
                )
                if check.exceeded:
                    (messages.error if check.hard else messages.warning)(request, check.message)
                if not check.hard:
                    for field, value in counts.items():
                        setattr(item, field, value)
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
    publish (genehmigt -> veröffentlicht; ohne Genehmigungsschritt aus der Prüfung),
    unpublish (veröffentlicht -> genehmigt, Rücknahme aus OParl und Bürgerportal).
    """

    http_method_names = ["post"]

    # Aktion -> benötigte Berechtigung
    ACTION_PERMS = {
        "submit": "edit_protocols",
        "reject": "approve_protocols",
        "approve": "approve_protocols",
        "publish": "approve_protocols",
        "unpublish": "approve_protocols",
    }

    def check_view_permissions(self):
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
        try:
            message = protocol_service.perform_action(
                protocol,
                action,
                user=self.session_user,
                data=request.POST,
                request=request,
                include_non_public=self.has_permission("view_non_public_meetings"),
            )
        except protocol_service.WorkflowError as exc:
            messages.error(request, exc.user_message)
        else:
            messages.success(request, message)
        return _protocol_redirect(self, meeting)


class ProtocolCorrectionView(SessionViewMixin, TemplateView):
    """
    Berichtigung einer genehmigten Niederschrift (Issue #318): Formular je Gegenstand
    (``?top=<id>``, ``?teil=allgemein`` oder ``?teil=allgemein-noe``) und Antrag.
    """

    template_name = "session/protocols/correction_form.html"
    permission_required = "approve_protocols"

    TARGETS = {
        "allgemein": SessionProtocolCorrection.TARGET_GENERAL,
        "allgemein-noe": SessionProtocolCorrection.TARGET_GENERAL_NP,
    }

    def _load(self):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        protocol = getattr(meeting, "protocol", None)
        if protocol is None or not protocol.is_locked:
            return meeting, protocol, None, None
        params = self.request.POST if self.request.method == "POST" else self.request.GET
        can_view_np = self.has_permission("view_non_public_meetings")
        item = None
        target = self.TARGETS.get(params.get("teil", ""), "")
        if params.get("top"):
            target = SessionProtocolCorrection.TARGET_ITEM
            qs = meeting.agenda_items.select_related("meeting", "parent")
            if not can_view_np:
                qs = qs.filter(is_public=True)
            try:
                item = qs.filter(pk=params.get("top")).first()
            except (ValueError, ValidationError):
                item = None
            if item is None:
                return meeting, protocol, None, None
        if not target or (target == SessionProtocolCorrection.TARGET_GENERAL_NP and not can_view_np):
            return meeting, protocol, None, None
        return meeting, protocol, target, item

    def _form_url(self, meeting, item):
        query = {"top": item.pk} if item is not None else {"teil": self.request.POST.get("teil", "")}
        path = reverse(
            "session:meeting_protocol_correction",
            kwargs={"tenant_slug": self.session_tenant.slug, "meeting_id": meeting.id},
        )
        return f"{path}?{urlencode(query)}"

    def get(self, request, *args, **kwargs):
        meeting, protocol, target, _item = self._load()
        if target is None:
            messages.error(request, "Berichtigungen gibt es nur für genehmigte Niederschriften und ihre Teile.")
            return _protocol_redirect(self, meeting)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting, protocol, target, item = self._load()
        can_view_np = self.has_permission("view_non_public_meetings")
        specs = protocol_correction_service.field_specs(target, item, can_view_np=can_view_np)
        source = item if item is not None else protocol
        values = protocol_correction_service.current_values(source, specs)
        voters = []
        if item is not None and item.voting_method in protocol_correction_service.INDIVIDUAL_METHODS:
            votes = protocol_correction_service.current_votes(item)
            for attendance in voting_service.eligibility(meeting).voting:
                attendance.current_vote = votes.get(str(attendance.person_id), "")
                voters.append(attendance)
        public_scope = (
            protocol_correction_service.item_is_public(item)
            if item is not None
            else target == SessionProtocolCorrection.TARGET_GENERAL and meeting.is_public
        )
        context.update(
            {
                "meeting": meeting,
                "protocol": protocol,
                "target": target,
                "item": item,
                "fields": [{"spec": spec, "value": values[spec.name]} for spec in specs],
                "voters": voters,
                "vote_choices": SessionVote.VOTE_CHOICES,
                "result_choices": SessionAgendaItem._meta.get_field("vote_result").choices,
                "public_scope": public_scope,
                "four_eyes": four_eyes_service.required(self.session_tenant, four_eyes_service.PROCESS_CORRECTION),
                "teil": self.request.GET.get("teil", ""),
            }
        )
        if any(spec.encrypted for spec in specs) or not public_scope:
            audit.log_read(
                self.request,
                protocol,
                tenant=self.session_tenant,
                user=self.session_user,
                changes={"umfang": "Niederschrift, Berichtigung nichtöffentlicher Teile"},
            )
        return context

    def post(self, request, *args, **kwargs):
        meeting, protocol, target, item = self._load()
        if target is None:
            messages.error(request, "Berichtigungen gibt es nur für genehmigte Niederschriften und ihre Teile.")
            return _protocol_redirect(self, meeting)
        try:
            outcome = protocol_correction_service.propose(
                protocol,
                target=target,
                item=item,
                data=request.POST,
                reason=request.POST.get("reason", ""),
                user=self.session_user,
                can_view_np=self.has_permission("view_non_public_meetings"),
                request=request,
            )
        except (protocol_correction_service.CorrectionError, four_eyes_service.ApprovalError) as exc:
            messages.error(request, exc.user_message)
            return redirect(self._form_url(meeting, item))
        for warning in outcome.warnings:
            messages.warning(request, warning)
        if outcome.applied:
            messages.success(request, "Die Berichtigung ist wirksam und in der Niederschrift vermerkt.")
        else:
            messages.success(
                request,
                "Die Berichtigung ist beantragt. Wirksam wird sie, sobald eine zweite Person sie bestätigt "
                "(Vier-Augen-Prinzip).",
            )
        return _protocol_redirect(self, meeting)


class ProtocolCorrectionDecisionView(SessionViewMixin, View):
    """Beantragte Berichtigung bestätigen oder ablehnen (Vier-Augen-Prinzip, Issue #318)."""

    permission_required = "approve_protocols"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, meeting_id, correction_id, decision):
        meeting = _get_meeting(self, meeting_id)
        qs = SessionProtocolCorrection.objects.select_related("protocol__meeting__tenant", "agenda_item")
        if not self.has_permission("view_non_public_meetings"):
            qs = qs.filter(is_public=True)
        correction = get_object_or_404(qs, pk=correction_id, protocol__meeting=meeting)
        try:
            if decision == "confirm":
                protocol_correction_service.confirm(correction, user=self.session_user, request=request)
                messages.success(request, "Die Berichtigung ist bestätigt und wirksam.")
            elif decision == "reject":
                protocol_correction_service.reject(
                    correction, user=self.session_user, note=request.POST.get("note", ""), request=request
                )
                messages.success(request, "Die Berichtigung wurde abgelehnt.")
            else:
                raise PermissionDenied("Unbekannte Aktion")
        except (protocol_correction_service.CorrectionError, four_eyes_service.ApprovalError) as exc:
            messages.error(request, exc.user_message)
        except protocol_lock.ProtocolLockedError as exc:
            messages.error(request, exc.user_message)
        return _protocol_redirect(self, meeting)


class ProtocolPdfView(SessionViewMixin, TemplateView):
    """
    Niederschrift-PDF.

    - Ö-Fassung (Standard): nur öffentliche Inhalte, für view_protocols
    - Interne NÖ-Fassung (?fassung=intern): zusätzlich view_non_public_meetings
    """

    permission_required = "view_protocols"

    def get(self, request, *args, **kwargs):
        meeting = _get_meeting(self, self.kwargs["meeting_id"])
        protocol = getattr(meeting, "protocol", None)
        if protocol is None:
            messages.error(request, "Für diese Sitzung existiert noch kein Protokoll.")
            return _protocol_redirect(self, meeting)

        internal = request.GET.get("fassung") == "intern"
        if internal and not self.has_permission("view_non_public_meetings"):
            raise PermissionDenied("Fehlende Berechtigung für die interne Fassung")

        pdf_bytes = protocol_service.build_protocol_pdf(protocol, internal=internal)
        if internal:
            # Interne Fassung enthält den nichtöffentlichen Teil (Issue #221)
            audit.log_read(
                request,
                protocol,
                tenant=self.session_tenant,
                user=self.session_user,
                action="download",
                changes={"dokument": "Niederschrift, interne Fassung (PDF)"},
            )
        filename = "niederschrift-intern.pdf" if internal else "niederschrift.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
