# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ..models import (
    OParlAgendaItem,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
)
from ._helpers import ActiveBodyRequiredMixin, get_active_body

# =============================================================================
# Dokumente (Files)
# =============================================================================


def _annotate_files_with_context(files):
    """
    Annotiert Dateien mit Kontext-Info (Gremium, Sitzung, TOP).

    Löst die Kette File → Paper → Consultation → Meeting → Organization auf.
    Hängt `context_info` Dict an jede Datei: {organization_name, meeting, meeting_date, agenda_number}
    """
    # Sammle paper_ids und meeting_ids
    paper_ids = set()
    meeting_fk_ids = set()
    for f in files:
        if f.paper_id:
            paper_ids.add(f.paper_id)
        if f.meeting_id:
            meeting_fk_ids.add(f.meeting_id)

    if not paper_ids and not meeting_fk_ids:
        return

    # 1. Consultations für alle Papers
    consultations_by_paper = {}
    if paper_ids:
        consultations = OParlConsultation.objects.filter(paper_id__in=paper_ids)
        for c in consultations:
            consultations_by_paper.setdefault(c.paper_id, []).append(c)

    # 2. Meetings (aus Consultations + direkte FKs)
    meeting_ext_ids = set()
    for cons_list in consultations_by_paper.values():
        for c in cons_list:
            if c.meeting_external_id:
                meeting_ext_ids.add(c.meeting_external_id)

    meetings_by_ext_id = {}
    meetings_by_pk = {}
    all_meeting_pks = set()

    if meeting_ext_ids:
        meetings = OParlMeeting.objects.filter(external_id__in=meeting_ext_ids).prefetch_related("organizations")
        for m in meetings:
            meetings_by_ext_id[m.external_id] = m
            meetings_by_pk[m.pk] = m
            all_meeting_pks.add(m.pk)

    if meeting_fk_ids:
        missing = meeting_fk_ids - all_meeting_pks
        if missing:
            fk_meetings = OParlMeeting.objects.filter(pk__in=missing).prefetch_related("organizations")
            for m in fk_meetings:
                meetings_by_pk[m.pk] = m

    # 3. AgendaItems für die Consultations
    agenda_ext_ids = set()
    for cons_list in consultations_by_paper.values():
        for c in cons_list:
            if c.agenda_item_external_id:
                agenda_ext_ids.add(c.agenda_item_external_id)

    agenda_items_by_ext_id = {}
    if agenda_ext_ids:
        for ai in OParlAgendaItem.objects.filter(external_id__in=agenda_ext_ids):
            agenda_items_by_ext_id[ai.external_id] = ai

    # 4. Pro Paper die nächste (zukünftige) Consultation wählen, Fallback auf neueste
    now = timezone.now()
    best_by_paper = {}
    for paper_id, cons_list in consultations_by_paper.items():
        # Alle Consultations mit aufgelöstem Meeting sammeln
        candidates = []
        for c in cons_list:
            meeting = meetings_by_ext_id.get(c.meeting_external_id)
            if meeting and meeting.start:
                candidates.append((c, meeting))

        if not candidates:
            continue

        # Bevorzuge nächste zukünftige Sitzung
        future = [(c, m) for c, m in candidates if m.start >= now]
        if future:
            # Nächste zukünftige (früheste)
            future.sort(key=lambda x: x[1].start)
            best = future[0]
        else:
            # Keine zukünftige → neueste vergangene
            candidates.sort(key=lambda x: x[1].start, reverse=True)
            best = candidates[0]

        consultation, meeting = best
        agenda_item = agenda_items_by_ext_id.get(consultation.agenda_item_external_id)
        orgs = meeting.organizations.all()
        org_name = orgs[0].name if orgs else None
        best_by_paper[paper_id] = {
            "organization_name": org_name,
            "meeting": meeting,
            "meeting_date": meeting.start,
            "agenda_number": agenda_item.number if agenda_item else None,
        }

    # 5. Annotiere jede Datei
    for f in files:
        ctx = best_by_paper.get(f.paper_id)
        if not ctx and f.meeting_id:
            # Fallback: Datei hat direkten Meeting-FK (ohne Paper-Kette)
            meeting = meetings_by_pk.get(f.meeting_id)
            if meeting:
                orgs = meeting.organizations.all()
                org_name = orgs[0].name if orgs else None
                ctx = {
                    "organization_name": org_name,
                    "meeting": meeting,
                    "meeting_date": meeting.start,
                    "agenda_number": None,
                }
        f.context_info = ctx


class FileListView(ActiveBodyRequiredMixin, TemplateView):
    """Liste aller Dokumente/Dateien."""

    template_name = "pages/files/list.html"

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["partials/file_list_items.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        q = self.request.GET.get("q", "").strip()
        page_num = int(self.request.GET.get("page", 1))

        if body:
            qs = (
                OParlFile.objects.filter(body=body, deleted=False)
                .select_related("paper")
                .order_by("-file_date", "-created_at")
            )

            if q:
                qs = qs.filter(Q(name__icontains=q) | Q(file_name__icontains=q) | Q(paper__name__icontains=q))

            paginator = Paginator(qs, 30)
            page = paginator.get_page(page_num)

            # Annotiere Dateien mit Kontext (Gremium, Sitzung, TOP)
            _annotate_files_with_context(page.object_list)

            context["files"] = page
            context["paginator"] = paginator
            context["total_count"] = paginator.count

        context["query"] = q

        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Dokumente",
            description="Beschlüsse, Anträge, Berichte und Anlagen der Kommunalpolitik mit Volltextsuche durchsuchen.",
            body=body,
        ).to_dict()
        return context


# =============================================================================
# File Proxy (DSGVO-konform - PDFs im iframe anzeigbar)
# =============================================================================

import logging

import httpx
from django.conf import settings
from django.views.decorators.clickjacking import xframe_options_exempt

logger = logging.getLogger(__name__)


def _file_proxy_error(title, message):
    """Return a styled HTML error page for the file proxy iframe."""
    html = f"""<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f9fafb;color:#374151;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:2rem}}
.card{{max-width:28rem;text-align:center}}
.icon{{width:3rem;height:3rem;margin:0 auto 1rem;color:#9ca3af}}
h1{{font-size:1.125rem;font-weight:600;margin-bottom:.5rem;color:#111827}}
p{{font-size:.875rem;line-height:1.625;color:#6b7280}}
a{{color:#4f46e5;text-decoration:underline}}
@media(prefers-color-scheme:dark){{body{{background:#111827;color:#d1d5db}}h1{{color:#f9fafb}}p{{color:#9ca3af}}.icon{{color:#6b7280}}}}
</style></head>
<body><div class="card">
<svg class="icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
<h1>{title}</h1>
<p>{message}</p>
</div></body></html>"""
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["X-Frame-Options"] = "ALLOWALL"
    return response


@require_GET
@xframe_options_exempt
def file_proxy(request, file_id):
    """
    Datei-Auslieferung für OParl-Dokumente (PDFs) — iframe-fähig und DSGVO-konform.

    Reihenfolge (Issues #87/#86):
    1. Lokale Kopie aus dem Dokument-Cache (FileResponse, kein RIS-Zugriff)
    2. Live-Abruf mit kurzen Timeouts; erfolgreiche Antworten werden
       direkt in den Cache geschrieben (Write-Through)
    3. Freundliche Fehlerseite, wenn die Quelle nicht erreichbar ist
    """
    from django.http import FileResponse

    from ..services import file_cache

    file_obj = get_object_or_404(OParlFile.objects.select_related("body"), id=file_id)
    force_download = request.GET.get("download") == "1"
    filename = file_obj.file_name or file_obj.name or "dokument.pdf"

    local = file_cache.local_file(file_obj)
    if local is not None:
        response = FileResponse(
            open(local, "rb"),
            content_type=file_cache.content_type_for(file_obj, "application/pdf"),
            as_attachment=force_download,
            filename=filename,
        )
        response["Cache-Control"] = "public, max-age=86400"
        response["X-Mandari-Cache"] = "hit"
        return response

    url = file_obj.download_url or file_obj.access_url
    if not url:
        raise Http404("Keine Download-URL verfügbar")

    read_timeout = float(getattr(settings, "FILE_PROXY_TIMEOUT_SECONDS", 15))
    try:
        upstream = httpx.get(
            url,
            timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0),
            follow_redirects=True,
            headers={"User-Agent": file_cache.USER_AGENT},
        )
        upstream.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404 and file_obj.local_status == "none":
            file_obj.local_status = "missing"
            file_obj.local_error = "HTTP 404"
            file_obj.save(update_fields=["local_status", "local_error"])
        return _file_proxy_error(
            "Datei nicht gefunden" if e.response.status_code == 404 else f"Fehler {e.response.status_code}",
            "Die Datei konnte auf dem OParl-Server nicht gefunden werden. "
            "Das liegt oft an veränderten Daten und URLs auf dem Quell-Server. "
            "Die Probleme werden nach unserem nächsten Scan in der Regel gelöst. "
            "Bei längerfristigen Problemen mit bestimmten Dokumenten melde dich bitte bei "
            'unserem Support unter <a href="mailto:support@mandari.de">support@mandari.de</a>.',
        )
    except httpx.RequestError:
        return _file_proxy_error(
            "Server nicht erreichbar",
            "Das Ratsinformationssystem ist momentan nicht erreichbar und dieses Dokument lag noch nicht "
            "in unserem Zwischenspeicher. Wir speichern Dokumente laufend zwischengespeichert ab — "
            "bitte versuche es später erneut.",
        )

    data = upstream.content
    content_type = file_cache.content_type_for(
        file_obj, upstream.headers.get("content-type", "application/octet-stream").split(";")[0]
    )
    if file_cache.looks_like_html(data) and "html" not in (file_obj.mime_type or "").lower():
        return _file_proxy_error(
            "Quelle liefert derzeit keine Datei",
            "Das Ratsinformationssystem antwortet mit einer Hinweisseite statt mit dem Dokument "
            "(z. B. Wartung). Bitte versuche es später erneut.",
        )

    # Write-Through: beim nächsten Aufruf kommt die Datei von der Platte
    try:
        if len(data) <= file_cache.max_bytes() and file_cache.has_room_for(len(data)):
            file_cache.store_bytes(file_obj, data, content_type=content_type)
    except Exception as exc:  # Cache-Fehler dürfen die Auslieferung nie verhindern
        logger.warning("Dokument %s konnte nicht zwischengespeichert werden: %s", file_obj.id, exc)

    response = HttpResponse(data, content_type=content_type)
    if force_download:
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
    else:
        response["Content-Disposition"] = "inline"
    response["Content-Length"] = len(data)
    response["Cache-Control"] = "public, max-age=86400"
    response["X-Mandari-Cache"] = "miss"
    return response
