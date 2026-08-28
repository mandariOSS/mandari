# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Motion/Antrag views for the Work module.
"""

import logging
import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

logger = logging.getLogger("apps.work.motions")

from apps.common.mixins import WorkViewMixin

from ..models import (
    DocumentFolder,
    FolderGuestShare,
    Motion,
    MotionType,
)
from ._helpers import _can_manage_folder, _flatten_folder_tree, _get_org_folder_or_404


class MotionListView(WorkViewMixin, TemplateView):
    """List of documents (formerly motions)."""

    template_name = "work/motions/list.html"
    permission_required = "motions.view"

    def get_context_data(self, **kwargs):
        from apps.tenants.models import Membership, Topic

        context = super().get_context_data(**kwargs)
        context["active_nav"] = "documents"

        # Base queryset - exclude deleted items by default
        motions = Motion.objects.filter(organization=self.organization).exclude(status="deleted")

        # Ordner-Filter (?ordner=<id>): Ordner inkl. aller Unterordner.
        # Fremde/ungültige Ordner → 404 (Org-Grenze).
        current_folder = None
        folder_param = self.request.GET.get("ordner")
        if folder_param:
            current_folder = _get_org_folder_or_404(self.organization, folder_param)
            subtree_ids = [current_folder.id] + [f.id for f in current_folder.get_descendants()]
            motions = motions.filter(folder_id__in=subtree_ids)
        context["current_folder"] = current_folder
        context["folder_breadcrumbs"] = current_folder.get_ancestors() if current_folder else []

        # Filter by status (Mehrfachauswahl möglich: ?status=a&status=b)
        statuses = [s for s in self.request.GET.getlist("status") if s]
        if statuses:
            motions = motions.filter(status__in=statuses)
            context["selected_status"] = statuses[0]
            context["selected_statuses"] = statuses

        # Filter by type
        motion_type = self.request.GET.get("type")
        if motion_type:
            motions = motions.filter(motion_type=motion_type)
            context["selected_type"] = motion_type

        # Filter by topic
        topic_id = self.request.GET.get("thema")
        if topic_id:
            motions = motions.filter(topics__id=topic_id)
            context["selected_topic"] = topic_id

        # Filter by responsible ("me" oder Membership-ID)
        responsible = self.request.GET.get("verantwortlich")
        if responsible == "me":
            motions = motions.filter(responsible=self.membership)
            context["selected_responsible"] = "me"
        elif responsible:
            motions = motions.filter(responsible__id=responsible)
            context["selected_responsible"] = responsible

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            motions = motions.filter(Q(title__icontains=search) | Q(summary__icontains=search))
            context["search_query"] = search

        # Filter by author (only own motions)
        if self.request.GET.get("mine") == "1":
            motions = motions.filter(author=self.membership)
            context["filter_mine"] = True

        # Order
        order = self.request.GET.get("order", "-updated_at")
        if order in ["-updated_at", "-created_at", "title", "-title", "due_date"]:
            motions = motions.order_by(order)

        motions = motions.distinct()

        # Select/prefetch related for tracker, chips and progress columns
        related = ("author__user", "responsible__user", "document_type", "template")
        prefetches = ("topics", "checklist_items", "approvals")

        # Hauptanträge paginieren, Änderungsanträge (parent_motion) unter
        # ihrem Hauptantrag eingerückt anzeigen
        parents = motions.filter(parent_motion__isnull=True).select_related(*related).prefetch_related(*prefetches)

        paginator = Paginator(parents, 20)
        page = self.request.GET.get("page", 1)
        page_obj = paginator.get_page(page)
        context["motions"] = page_obj
        context["paginator"] = paginator

        # Kinder der Seite laden und gruppiert anhängen
        amendments = (
            Motion.objects.filter(organization=self.organization, parent_motion__in=list(page_obj))
            .exclude(status="deleted")
            .select_related(*related)
            .prefetch_related(*prefetches)
            .order_by("-updated_at")
        )
        amendments_by_parent = {}
        for amendment in amendments:
            amendments_by_parent.setdefault(amendment.parent_motion_id, []).append(amendment)

        motion_rows = []
        for motion in page_obj:
            motion_rows.append({"motion": motion, "is_amendment": False})
            for amendment in amendments_by_parent.get(motion.id, []):
                motion_rows.append({"motion": amendment, "is_amendment": True})
        context["motion_rows"] = motion_rows

        # Statistics (exclude deleted)
        today = timezone.localdate()
        all_motions = Motion.objects.filter(organization=self.organization).exclude(status="deleted")
        context["stats"] = {
            "total": all_motions.count(),
            "draft": all_motions.filter(status="draft").count(),
            "submitted": all_motions.filter(status="submitted").count(),
            "completed": all_motions.filter(status="completed").count(),
            "in_consultation": all_motions.filter(status__in=["at_admin", "on_agenda"]).count(),
            "overdue": all_motions.filter(due_date__lt=today)
            .exclude(status__in=["completed", "rejected", "archived"])
            .count(),
        }

        # Filter out 'deleted' from visible status choices
        context["status_choices"] = [(value, label) for value, label in Motion.STATUS_CHOICES if value != "deleted"]
        context["type_choices"] = Motion.LEGACY_TYPE_CHOICES

        # Also get custom document types for this organization
        context["document_types"] = MotionType.objects.filter(organization=self.organization, is_active=True).order_by(
            "sort_order", "name"
        )

        # Filter-Dropdowns: Themen und Mitglieder der Organisation
        context["org_topics"] = Topic.objects.filter(organization=self.organization)
        context["org_members"] = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .select_related("user")
            .order_by("user__first_name", "user__last_name")
        )

        # === Ordner-Spalte ===
        # Zähler sichtbarkeitsabhängig: nur Dokumente, die der aktuelle
        # Nutzer sehen darf (Ordner machen nichts sichtbar!)
        from django.db.models import Count

        visible = Motion.visible_to(self.membership)
        # order_by() leert die Meta-Sortierung (-updated_at), sonst wandert
        # updated_at ins GROUP BY und die Zähler zerfallen in Einzelgruppen
        direct_counts = {
            row["folder"]: row["n"]
            for row in visible.order_by().values("folder").annotate(n=Count("id", distinct=True))
        }

        flattened = _flatten_folder_tree(self.organization)

        # Teilbaum-Summen: Kinder vor Eltern aggregieren (Vorordnung rückwärts)
        totals = {folder.id: direct_counts.get(folder.id, 0) for folder, _ in flattened}
        for folder, _depth in reversed(flattened):
            if folder.parent_id in totals:
                totals[folder.parent_id] += totals[folder.id]

        context["folder_entries"] = [
            {
                "folder": folder,
                "depth": depth,
                "indent": (depth - 1) * 14,
                "count": totals[folder.id],
                "can_manage": _can_manage_folder(self.membership, folder),
            }
            for folder, depth in flattened
        ]
        context["root_document_count"] = visible.count()
        context["can_create_folder"] = self.membership.has_permission("motions.create")
        context["folder_color_choices"] = DocumentFolder.COLOR_CHOICES

        # Aktive Filter (ohne ordner/page) für Ordner-Links erhalten
        preserved = self.request.GET.copy()
        preserved.pop("ordner", None)
        preserved.pop("page", None)
        context["filter_query"] = preserved.urlencode()

        return context


class DocumentFolderCreateView(WorkViewMixin, View):
    """Neuen Dokumentordner anlegen (im aktuell gewählten Ordner)."""

    permission_required = "motions.create"

    def post(self, request, *args, **kwargs):
        from django.core.exceptions import ValidationError

        name = request.POST.get("name", "").strip()
        parent_id = request.POST.get("parent", "").strip()
        color = request.POST.get("color", "").strip()
        if color not in dict(DocumentFolder.COLOR_CHOICES):
            color = ""

        parent = _get_org_folder_or_404(self.organization, parent_id) if parent_id else None
        list_url = reverse("work:documents", kwargs={"org_slug": self.organization.slug})
        back_url = f"{list_url}?ordner={parent.id}" if parent else list_url

        if not name:
            messages.error(request, "Name ist erforderlich.")
            return redirect(back_url)

        folder = DocumentFolder(
            organization=self.organization,
            name=name,
            parent=parent,
            color=color,
            created_by=self.membership,
            position=DocumentFolder.objects.filter(organization=self.organization, parent=parent).count(),
        )
        try:
            folder.full_clean()
        except ValidationError as e:
            messages.error(request, " ".join(m for msgs in e.message_dict.values() for m in msgs))
            return redirect(back_url)

        folder.save()
        messages.success(request, f"Ordner '{folder.name}' erstellt.")
        return redirect(f"{list_url}?ordner={folder.id}")


class DocumentFolderUpdateView(WorkViewMixin, View):
    """Ordner umbenennen, Farbe ändern oder verschieben ("Verschieben nach")."""

    permission_required = ["motions.create", "organization.edit"]
    permission_require_all = False

    def post(self, request, *args, **kwargs):
        from django.core.exceptions import ValidationError

        folder = _get_org_folder_or_404(self.organization, kwargs.get("folder_id"))
        if not _can_manage_folder(self.membership, folder):
            raise PermissionDenied("Keine Berechtigung für diesen Ordner.")

        list_url = reverse("work:documents", kwargs={"org_slug": self.organization.slug})
        back_url = f"{list_url}?ordner={folder.id}"

        name = request.POST.get("name", "").strip()
        if name:
            folder.name = name

        color = request.POST.get("color", "").strip()
        if color in dict(DocumentFolder.COLOR_CHOICES) or color == "":
            folder.color = color

        # Verschieben: "" = Wurzelebene, sonst Ziel-Ordner der Organisation
        if "parent" in request.POST:
            parent_id = request.POST.get("parent", "").strip()
            folder.parent = _get_org_folder_or_404(self.organization, parent_id) if parent_id else None

        try:
            folder.full_clean()
        except ValidationError as e:
            messages.error(request, " ".join(m for msgs in e.message_dict.values() for m in msgs))
            return redirect(back_url)

        folder.save()
        messages.success(request, f"Ordner '{folder.name}' aktualisiert.")
        return redirect(back_url)


class DocumentFolderDeleteView(WorkViewMixin, View):
    """
    Ordner löschen: Inhalte (Dokumente + Unterordner) wandern zum
    übergeordneten Ordner bzw. zur Wurzel "Alle Dokumente".
    """

    permission_required = ["motions.create", "organization.edit"]
    permission_require_all = False

    def post(self, request, *args, **kwargs):
        folder = _get_org_folder_or_404(self.organization, kwargs.get("folder_id"))
        if not _can_manage_folder(self.membership, folder):
            raise PermissionDenied("Keine Berechtigung für diesen Ordner.")

        parent = folder.parent
        name = folder.name
        folder.delete()

        target = parent.name if parent else "Alle Dokumente"
        messages.success(request, f"Ordner '{name}' gelöscht. Inhalte wurden nach '{target}' verschoben.")

        list_url = reverse("work:documents", kwargs={"org_slug": self.organization.slug})
        return redirect(f"{list_url}?ordner={parent.id}" if parent else list_url)


class FolderGuestShareUpdateView(WorkViewMixin, View):
    """
    Ordner für einen Nutzer (insbesondere Gast) freigeben.

    Die Freigabe gilt rekursiv für alle Unterordner und enthaltenen
    Dokumente — auch künftig hinzukommende (FolderGuestShare).
    """

    permission_required = ["guests.manage", "motions.share"]
    permission_require_all = False

    def post(self, request, *args, **kwargs):
        from apps.accounts.models import User
        from apps.tenants.models import Membership

        folder = _get_org_folder_or_404(self.organization, kwargs.get("folder_id"))
        if not _can_manage_folder(self.membership, folder):
            return JsonResponse({"error": "Keine Berechtigung für diesen Ordner."}, status=403)

        email = request.POST.get("email", "").strip().lower()
        level = request.POST.get("level", "view")
        if level not in dict(FolderGuestShare.LEVEL_CHOICES):
            level = "view"

        user = User.objects.filter(email=email).first() if email else None
        if user is None:
            return JsonResponse({"error": f"Benutzer '{email}' nicht gefunden."}, status=400)

        # Nur Nutzer mit aktivem Zugang zu DIESER Organisation — sonst wäre
        # die Freigabe wirkungslos bzw. liefe an der Org-Grenze vorbei.
        target = Membership.objects.filter(user=user, organization=self.organization, is_active=True).first()
        if target is None:
            return JsonResponse({"error": "Der Nutzer hat keinen Zugang zu dieser Organisation."}, status=400)
        # Ordner-Freigaben wirken ausschließlich für Gastzugänge – Mitglieder
        # sehen Dokumente über deren Sichtbarkeit (Motion.visibility/MotionShare).
        if not target.is_guest:
            return JsonResponse(
                {
                    "error": "Ordner-Freigaben gelten nur für Gastzugänge. "
                    "Mitglieder sehen Dokumente über die Sichtbarkeit."
                },
                status=400,
            )

        FolderGuestShare.objects.update_or_create(
            folder=folder,
            user=user,
            defaults={"level": level, "created_by": request.user},
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True})
        messages.success(request, f"Ordner '{folder.name}' für {email} freigegeben.")
        list_url = reverse("work:documents", kwargs={"org_slug": self.organization.slug})
        return redirect(f"{list_url}?ordner={folder.id}")


class FolderGuestShareRemoveView(WorkViewMixin, View):
    """Ordner-Freigabe entziehen."""

    permission_required = ["guests.manage", "motions.share"]
    permission_require_all = False

    def post(self, request, *args, **kwargs):
        share = get_object_or_404(
            FolderGuestShare,
            id=kwargs.get("share_id"),
            folder__organization=self.organization,
        )
        if not _can_manage_folder(self.membership, share.folder):
            return JsonResponse({"error": "Keine Berechtigung für diesen Ordner."}, status=403)

        folder = share.folder
        share.delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True})
        messages.success(request, f"Ordner-Freigabe für '{folder.name}' entfernt.")
        list_url = reverse("work:documents", kwargs={"org_slug": self.organization.slug})
        return redirect(f"{list_url}?ordner={folder.id}")


class MotionFolderMoveView(WorkViewMixin, View):
    """
    Dokumente in einen Ordner verschieben (einzeln oder Mehrfachauswahl).

    Verschoben werden nur Dokumente, die der Nutzer bearbeiten darf —
    die Sichtbarkeit der Dokumente ändert sich dadurch NICHT.
    """

    permission_required = "motions.edit"

    def post(self, request, *args, **kwargs):
        folder_id = request.POST.get("folder", "").strip()
        folder = _get_org_folder_or_404(self.organization, folder_id) if folder_id else None

        motion_ids = []
        for raw_id in request.POST.getlist("motion_ids"):
            try:
                motion_ids.append(uuid.UUID(raw_id))
            except (ValueError, AttributeError):
                continue

        motions = Motion.objects.filter(organization=self.organization, id__in=motion_ids).exclude(status="deleted")

        moved = 0
        for motion in motions:
            if motion.can_edit(self.membership):
                motion.folder = folder
                motion.save(update_fields=["folder", "updated_at"])
                moved += 1

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True, "moved": moved})

        target = folder.name if folder else "Alle Dokumente"
        messages.success(request, f"{moved} Dokument(e) nach '{target}' verschoben.")
        list_url = reverse("work:documents", kwargs={"org_slug": self.organization.slug})
        return redirect(f"{list_url}?ordner={folder.id}" if folder else list_url)
