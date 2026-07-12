# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import TemplateView, View

logger = logging.getLogger("apps.work.motions")

from apps.common.mixins import WorkViewMixin

from ..forms import (
    MotionTemplateForm,
)
from ..models import (
    Motion,
    MotionTemplate,
    MotionType,
    OrganizationLetterhead,
)

# =============================================================================
# Settings Views for Motion Types, Templates, and Letterheads
# =============================================================================


class MotionSettingsView(WorkViewMixin, TemplateView):
    """Overview of motion/document settings."""

    template_name = "work/motions/settings/index.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "documents"

        # Get counts
        from apps.tenants.models import Topic

        context["type_count"] = MotionType.objects.filter(organization=self.organization).count()
        context["template_count"] = MotionTemplate.objects.filter(organization=self.organization).count()
        context["letterhead_count"] = OrganizationLetterhead.objects.filter(organization=self.organization).count()
        context["topic_count"] = Topic.objects.filter(organization=self.organization).count()

        # Branding-Kachel: Logo/Farben aus tenants + Briefkopf-Status
        primary_letterhead = (
            OrganizationLetterhead.objects.filter(organization=self.organization, is_active=True)
            .order_by("-is_default", "name")
            .first()
        )
        if primary_letterhead is None:
            letterhead_status = "keiner"
        elif primary_letterhead.is_generated:
            letterhead_status = "generiert"
        else:
            letterhead_status = "PDF"
        context["primary_letterhead"] = primary_letterhead
        context["letterhead_status"] = letterhead_status

        return context


class MotionTypeListView(WorkViewMixin, TemplateView):
    """List and manage document types."""

    template_name = "work/motions/settings/types.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "types"

        context["types"] = MotionType.objects.filter(organization=self.organization).order_by("sort_order", "name")

        return context


class MotionTypeCreateView(WorkViewMixin, TemplateView):
    """Create a new document type."""

    template_name = "work/motions/settings/type_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "types"
        context["is_new"] = True
        return context

    def post(self, request, *args, **kwargs):
        name = request.POST.get("name", "").strip()
        slug = request.POST.get("slug", "").strip()
        description = request.POST.get("description", "").strip()
        icon = request.POST.get("icon", "file-text").strip()
        color = request.POST.get("color", "blue").strip()
        requires_approval = request.POST.get("requires_approval") == "on"
        is_submittable = request.POST.get("is_submittable") == "on"
        is_default = request.POST.get("is_default") == "on"
        default_checklist = [
            line.strip() for line in request.POST.get("default_checklist", "").splitlines() if line.strip()
        ]

        if not name or not slug:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # Check uniqueness
        if MotionType.objects.filter(organization=self.organization, slug=slug).exists():
            messages.error(request, "Ein Typ mit diesem Kurznamen existiert bereits.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # If setting as default, unset others
        if is_default:
            MotionType.objects.filter(organization=self.organization, is_default=True).update(is_default=False)

        MotionType.objects.create(
            organization=self.organization,
            name=name,
            slug=slug,
            description=description,
            icon=icon,
            color=color,
            requires_approval=requires_approval,
            is_submittable=is_submittable,
            is_default=is_default,
            default_checklist=default_checklist,
        )

        messages.success(request, f"Dokumenttyp '{name}' erstellt.")
        return redirect("work:document_type_list", org_slug=self.organization.slug)


class MotionTypeEditView(WorkViewMixin, TemplateView):
    """Edit a document type."""

    template_name = "work/motions/settings/type_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "types"
        context["is_new"] = False

        motion_type = get_object_or_404(MotionType, id=kwargs.get("type_id"), organization=self.organization)
        context["motion_type"] = motion_type
        context["default_checklist_text"] = "\n".join(motion_type.default_checklist or [])
        return context

    def post(self, request, *args, **kwargs):
        motion_type = get_object_or_404(MotionType, id=kwargs.get("type_id"), organization=self.organization)

        motion_type.name = request.POST.get("name", "").strip()
        motion_type.slug = request.POST.get("slug", "").strip()
        motion_type.description = request.POST.get("description", "").strip()
        motion_type.icon = request.POST.get("icon", "file-text").strip()
        motion_type.color = request.POST.get("color", "blue").strip()
        motion_type.requires_approval = request.POST.get("requires_approval") == "on"
        motion_type.is_submittable = request.POST.get("is_submittable") == "on"
        motion_type.default_checklist = [
            line.strip() for line in request.POST.get("default_checklist", "").splitlines() if line.strip()
        ]
        is_default = request.POST.get("is_default") == "on"

        if is_default and not motion_type.is_default:
            MotionType.objects.filter(organization=self.organization, is_default=True).update(is_default=False)
        motion_type.is_default = is_default

        motion_type.save()

        messages.success(request, f"Dokumenttyp '{motion_type.name}' aktualisiert.")
        return redirect("work:document_type_list", org_slug=self.organization.slug)


class MotionTypeDeleteView(WorkViewMixin, View):
    """Delete a document type."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        motion_type = get_object_or_404(MotionType, id=kwargs.get("type_id"), organization=self.organization)

        # Check if type is in use
        if Motion.objects.filter(document_type=motion_type).exists():
            messages.error(
                request,
                f"Dokumenttyp '{motion_type.name}' wird noch verwendet und kann nicht gelöscht werden.",
            )
        else:
            name = motion_type.name
            motion_type.delete()
            messages.success(request, f"Dokumenttyp '{name}' gelöscht.")

        return redirect("work:document_type_list", org_slug=self.organization.slug)


class TopicListView(WorkViewMixin, TemplateView):
    """List and manage the organization's topic catalog (Themenkatalog)."""

    template_name = "work/motions/settings/topics.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        from apps.tenants.models import Topic

        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "topics"
        context["topics"] = Topic.objects.filter(organization=self.organization)
        context["color_choices"] = Topic.COLOR_CHOICES
        return context

    def post(self, request, *args, **kwargs):
        """Create a new topic (inline form on the list page)."""
        from apps.tenants.models import Topic

        name = request.POST.get("name", "").strip()
        color = request.POST.get("color", "blue").strip()
        if color not in dict(Topic.COLOR_CHOICES):
            color = "blue"

        if not name:
            messages.error(request, "Name ist erforderlich.")
        elif Topic.objects.filter(organization=self.organization, name=name).exists():
            messages.error(request, f"Das Thema '{name}' existiert bereits.")
        else:
            sort_order = Topic.objects.filter(organization=self.organization).count()
            Topic.objects.create(organization=self.organization, name=name, color=color, sort_order=sort_order)
            messages.success(request, f"Thema '{name}' erstellt.")

        return redirect("work:document_topic_list", org_slug=self.organization.slug)


class TopicUpdateView(WorkViewMixin, View):
    """Update a topic (name, color, sort order)."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Topic

        topic = get_object_or_404(Topic, id=kwargs.get("topic_id"), organization=self.organization)

        name = request.POST.get("name", "").strip()
        color = request.POST.get("color", topic.color).strip()

        if not name:
            messages.error(request, "Name ist erforderlich.")
            return redirect("work:document_topic_list", org_slug=self.organization.slug)

        if Topic.objects.filter(organization=self.organization, name=name).exclude(id=topic.id).exists():
            messages.error(request, f"Das Thema '{name}' existiert bereits.")
            return redirect("work:document_topic_list", org_slug=self.organization.slug)

        topic.name = name
        if color in dict(Topic.COLOR_CHOICES):
            topic.color = color
        try:
            topic.sort_order = int(request.POST.get("sort_order", topic.sort_order))
        except (TypeError, ValueError):
            pass
        topic.save()

        messages.success(request, f"Thema '{topic.name}' aktualisiert.")
        return redirect("work:document_topic_list", org_slug=self.organization.slug)


class TopicDeleteView(WorkViewMixin, View):
    """Delete a topic."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Topic

        topic = get_object_or_404(Topic, id=kwargs.get("topic_id"), organization=self.organization)
        name = topic.name
        topic.delete()
        messages.success(request, f"Thema '{name}' gelöscht.")
        return redirect("work:document_topic_list", org_slug=self.organization.slug)


class MotionTemplateListView(WorkViewMixin, TemplateView):
    """List and manage document templates."""

    template_name = "work/motions/settings/templates.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "templates"

        context["templates"] = (
            MotionTemplate.objects.filter(organization=self.organization)
            .select_related("motion_type", "letterhead")
            .order_by("-is_default", "name")
        )

        return context


class MotionTemplateCreateView(WorkViewMixin, TemplateView):
    """Create a new document template."""

    template_name = "work/motions/settings/template_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "templates"
        context["is_new"] = True
        context["form"] = MotionTemplateForm(organization=self.organization)

        context["types"] = MotionType.objects.filter(organization=self.organization, is_active=True)
        context["letterheads"] = OrganizationLetterhead.objects.filter(organization=self.organization, is_active=True)
        return context

    def post(self, request, *args, **kwargs):
        form = MotionTemplateForm(request.POST, organization=self.organization)

        if form.is_valid():
            template = form.save(commit=False)
            template.organization = self.organization

            # Ein Default je Typ: beim Setzen andere Vorlagen desselben Typs zurücksetzen
            if template.is_default:
                MotionTemplate.objects.filter(
                    organization=self.organization, is_default=True, motion_type=template.motion_type
                ).update(is_default=False)

            template.save()
            messages.success(request, f"Vorlage '{template.name}' erstellt.")
            return redirect("work:document_template_list", org_slug=self.organization.slug)

        context = self.get_context_data(**kwargs)
        context["form"] = form
        return self.render_to_response(context)


class MotionTemplateEditView(WorkViewMixin, TemplateView):
    """Edit a document template."""

    template_name = "work/motions/settings/template_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "templates"
        context["is_new"] = False

        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)
        context["template"] = template
        context["form"] = MotionTemplateForm(instance=template, organization=self.organization)

        context["types"] = MotionType.objects.filter(organization=self.organization, is_active=True)
        context["letterheads"] = OrganizationLetterhead.objects.filter(organization=self.organization, is_active=True)
        return context

    def post(self, request, *args, **kwargs):
        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)

        form = MotionTemplateForm(request.POST, instance=template, organization=self.organization)

        if form.is_valid():
            template = form.save(commit=False)

            # Ein Default je Typ: beim Setzen andere Vorlagen desselben Typs zurücksetzen
            if template.is_default:
                MotionTemplate.objects.filter(
                    organization=self.organization, is_default=True, motion_type=template.motion_type
                ).exclude(id=template.id).update(is_default=False)

            template.save()
            messages.success(request, f"Vorlage '{template.name}' aktualisiert.")
            return redirect("work:document_template_list", org_slug=self.organization.slug)

        context = self.get_context_data(**kwargs)
        context["form"] = form
        return self.render_to_response(context)


class MotionTemplateDeleteView(WorkViewMixin, View):
    """Delete a document template."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)

        # Check if template is in use
        if Motion.objects.filter(template=template).exists():
            messages.error(
                request,
                f"Vorlage '{template.name}' wird noch verwendet und kann nicht gelöscht werden.",
            )
        else:
            name = template.name
            template.delete()
            messages.success(request, f"Vorlage '{name}' gelöscht.")

        return redirect("work:document_template_list", org_slug=self.organization.slug)


class LetterheadListView(WorkViewMixin, TemplateView):
    """List and manage letterheads."""

    template_name = "work/motions/settings/letterheads.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "letterheads"

        context["letterheads"] = OrganizationLetterhead.objects.filter(organization=self.organization).order_by(
            "-is_default", "name"
        )

        return context


def _generated_letterhead_defaults(organization) -> dict:
    """
    Sinnvolle Vorbelegung für einen generierten Briefkopf aus den Org-Daten
    (Name, Adresse, Kontakt — falls vorhanden).
    """
    address_lines = [line.strip() for line in (organization.address or "").splitlines() if line.strip()]

    contact_parts = []
    if organization.contact_email:
        contact_parts.append(organization.contact_email)
    if organization.contact_phone:
        contact_parts.append(f"Tel. {organization.contact_phone}")
    if organization.website:
        contact_parts.append(organization.website)

    return {
        "sender_line": " · ".join([organization.name] + address_lines),
        "address_block": "\n".join([organization.name] + address_lines),
        "footer_text": " · ".join(contact_parts),
    }


class LetterheadCreateView(WorkViewMixin, TemplateView):
    """Create a new letterhead."""

    template_name = "work/motions/settings/letterhead_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "letterheads"
        context["is_new"] = True
        context["generated_defaults"] = _generated_letterhead_defaults(self.organization)
        return context

    def post(self, request, *args, **kwargs):
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()
        kind = request.POST.get("kind", "pdf")
        if kind not in dict(OrganizationLetterhead.KIND_CHOICES):
            kind = "pdf"
        pdf_file = request.FILES.get("pdf_file")

        if not name:
            messages.error(request, "Name ist erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        if kind == "pdf":
            if not pdf_file:
                messages.error(request, "Für einen PDF-Briefkopf ist eine PDF-Datei erforderlich.")
                return self.render_to_response(self.get_context_data(**kwargs))
            if not pdf_file.name.lower().endswith(".pdf"):
                messages.error(request, "Nur PDF-Dateien sind erlaubt.")
                return self.render_to_response(self.get_context_data(**kwargs))
        else:
            pdf_file = None

        is_default = request.POST.get("is_default") == "on"
        if is_default:
            OrganizationLetterhead.objects.filter(organization=self.organization, is_default=True).update(
                is_default=False
            )

        OrganizationLetterhead.objects.create(
            organization=self.organization,
            name=name,
            description=description,
            kind=kind,
            pdf_file=pdf_file,
            header_logo_enabled=request.POST.get("header_logo_enabled") == "on",
            sender_line=request.POST.get("sender_line", "").strip(),
            address_block=request.POST.get("address_block", "").strip(),
            footer_text=request.POST.get("footer_text", "").strip(),
            accent_color_enabled=request.POST.get("accent_color_enabled") == "on",
            content_margin_top=int(request.POST.get("content_margin_top", 60)),
            content_margin_left=int(request.POST.get("content_margin_left", 25)),
            content_margin_right=int(request.POST.get("content_margin_right", 20)),
            content_margin_bottom=int(request.POST.get("content_margin_bottom", 30)),
            font_family=request.POST.get("font_family", "Arial").strip(),
            font_size=int(request.POST.get("font_size", 11)),
            is_default=is_default,
        )

        messages.success(request, f"Briefkopf '{name}' erstellt.")
        return redirect("work:document_letterhead_list", org_slug=self.organization.slug)


class LetterheadEditView(WorkViewMixin, TemplateView):
    """Edit a letterhead."""

    template_name = "work/motions/settings/letterhead_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["settings_tab"] = "letterheads"
        context["is_new"] = False

        context["letterhead"] = get_object_or_404(
            OrganizationLetterhead, id=kwargs.get("letterhead_id"), organization=self.organization
        )
        return context

    def post(self, request, *args, **kwargs):
        letterhead = get_object_or_404(
            OrganizationLetterhead, id=kwargs.get("letterhead_id"), organization=self.organization
        )

        letterhead.name = request.POST.get("name", "").strip()
        letterhead.description = request.POST.get("description", "").strip()

        kind = request.POST.get("kind", letterhead.kind)
        if kind in dict(OrganizationLetterhead.KIND_CHOICES):
            letterhead.kind = kind

        # Handle file upload (optional for edit)
        new_file = request.FILES.get("pdf_file")
        if new_file:
            if not new_file.name.lower().endswith(".pdf"):
                messages.error(request, "Nur PDF-Dateien sind erlaubt.")
                return self.render_to_response(self.get_context_data(**kwargs))
            letterhead.pdf_file = new_file

        if letterhead.kind == "pdf" and not letterhead.pdf_file:
            messages.error(request, "Für einen PDF-Briefkopf ist eine PDF-Datei erforderlich.")
            return self.render_to_response(self.get_context_data(**kwargs))

        # Felder für generierten Briefkopf
        letterhead.header_logo_enabled = request.POST.get("header_logo_enabled") == "on"
        letterhead.sender_line = request.POST.get("sender_line", "").strip()
        letterhead.address_block = request.POST.get("address_block", "").strip()
        letterhead.footer_text = request.POST.get("footer_text", "").strip()
        letterhead.accent_color_enabled = request.POST.get("accent_color_enabled") == "on"

        letterhead.content_margin_top = int(request.POST.get("content_margin_top", 60))
        letterhead.content_margin_left = int(request.POST.get("content_margin_left", 25))
        letterhead.content_margin_right = int(request.POST.get("content_margin_right", 20))
        letterhead.content_margin_bottom = int(request.POST.get("content_margin_bottom", 30))
        letterhead.font_family = request.POST.get("font_family", "Arial").strip()
        letterhead.font_size = int(request.POST.get("font_size", 11))

        is_default = request.POST.get("is_default") == "on"
        if is_default and not letterhead.is_default:
            OrganizationLetterhead.objects.filter(organization=self.organization, is_default=True).update(
                is_default=False
            )
        letterhead.is_default = is_default

        letterhead.save()

        messages.success(request, f"Briefkopf '{letterhead.name}' aktualisiert.")
        return redirect("work:document_letterhead_list", org_slug=self.organization.slug)


class LetterheadDeleteView(WorkViewMixin, View):
    """Delete a letterhead."""

    permission_required = "organization.edit"

    def post(self, request, *args, **kwargs):
        letterhead = get_object_or_404(
            OrganizationLetterhead, id=kwargs.get("letterhead_id"), organization=self.organization
        )

        # Check if letterhead is in use
        in_use = (
            Motion.objects.filter(letterhead=letterhead).exists()
            or MotionTemplate.objects.filter(letterhead=letterhead).exists()
        )

        if in_use:
            messages.error(
                request,
                f"Briefkopf '{letterhead.name}' wird noch verwendet und kann nicht gelöscht werden.",
            )
        else:
            name = letterhead.name
            letterhead.delete()
            messages.success(request, f"Briefkopf '{name}' gelöscht.")

        return redirect("work:document_letterhead_list", org_slug=self.organization.slug)


class LetterheadPreviewView(WorkViewMixin, View):
    """
    Live-Vorschau des generierten Briefkopfs im Briefkopf-Formular.

    Rendert das gemeinsame Partial mit Beispieltext aus den (ungespeicherten)
    Formularwerten (per GET-Parametern).
    """

    permission_required = "organization.edit"

    def get(self, request, *args, **kwargs):
        org = self.organization
        params = request.GET

        logo_url = ""
        if params.get("header_logo_enabled") in ("on", "true", "1"):
            logo = org.effective_logo
            if logo:
                try:
                    logo_url = logo.url
                except ValueError:
                    logo_url = ""

        context = {
            "org_name": org.name,
            "primary_color": org.effective_primary_color,
            "accent_enabled": params.get("accent_color_enabled") in ("on", "true", "1"),
            "logo_url": logo_url,
            "sender_line": params.get("sender_line", "").strip(),
            "address_lines": [line for line in params.get("address_block", "").splitlines() if line.strip()],
            "footer_lines": [line for line in params.get("footer_text", "").splitlines() if line.strip()],
            "show_sample": True,
            "show_footer": True,
        }
        return render(request, "work/motions/_generated_letterhead.html", context)


class LetterheadEditorPreviewView(WorkViewMixin, View):
    """
    HTML-Briefkopf-Vorschau für den Dokument-Editor (kind=generated).

    Der Editor rendert dieses Partial über dem Inhalt statt des
    pdfjs-Overlays.
    """

    permission_required = "motions.view"

    def get(self, request, *args, **kwargs):
        from ..export_service import generated_letterhead_context

        letterhead = get_object_or_404(
            OrganizationLetterhead,
            id=kwargs.get("letterhead_id"),
            organization=self.organization,
            kind="generated",
        )
        context = generated_letterhead_context(letterhead)
        context.update({"show_sample": False, "show_footer": False})
        return render(request, "work/motions/_generated_letterhead.html", context)


class MotionTemplatePreviewView(WorkViewMixin, TemplateView):
    """
    Vorschau einer Dokumentvorlage: Inhaltsvorlage + gewählter Briefkopf
    als HTML-Seite (Platzhalter mit Beispielwerten ersetzt).
    """

    template_name = "work/motions/settings/template_preview.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        from django.template.loader import render_to_string

        from ..export_service import apply_placeholders, build_placeholder_values, generated_letterhead_context

        context = super().get_context_data(**kwargs)
        template = get_object_or_404(MotionTemplate, id=kwargs.get("template_id"), organization=self.organization)
        context["template"] = template

        values = build_placeholder_values(self.organization, responsible_name="Erika Musterfrau")
        context["content_html"] = apply_placeholders(template.content_template or "", values)
        context["signature_text"] = apply_placeholders(template.signature_block or "", values)

        letterhead = template.letterhead
        context["letterhead_obj"] = letterhead
        context["letterhead_html"] = ""
        if letterhead and letterhead.is_generated:
            lh_context = generated_letterhead_context(letterhead)
            lh_context.update({"show_sample": False, "show_footer": True})
            context["letterhead_html"] = render_to_string("work/motions/_generated_letterhead.html", lh_context)

        return context
