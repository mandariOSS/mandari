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
    withdrawn_q,
)
from ..services import file_delivery
from ._helpers import ActiveBodyRequiredMixin, get_active_body, page_number

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

    # 1. Consultations für alle Papers (von mandari Session zurückgenommenes bleibt überall außen vor)
    consultations_by_paper = {}
    if paper_ids:
        consultations = OParlConsultation.objects.filter(paper_id__in=paper_ids).exclude(withdrawn_q())
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

    visible_meetings = OParlMeeting.objects.exclude(withdrawn_q()).prefetch_related("organizations")
    if meeting_ext_ids:
        meetings = visible_meetings.filter(external_id__in=meeting_ext_ids)
        for m in meetings:
            meetings_by_ext_id[m.external_id] = m
            meetings_by_pk[m.pk] = m
            all_meeting_pks.add(m.pk)

    if meeting_fk_ids:
        missing = meeting_fk_ids - all_meeting_pks
        if missing:
            fk_meetings = visible_meetings.filter(pk__in=missing)
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
        for ai in OParlAgendaItem.objects.filter(external_id__in=agenda_ext_ids).exclude(withdrawn_q()):
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
        page_num = page_number(self.request, maximum=100_000)

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
import tempfile
import threading

import httpx
from django.conf import settings
from django.views.decorators.clickjacking import xframe_options_exempt

from .. import throttle
from ..services import safe_fetch

logger = logging.getLogger(__name__)

#: Gleichzeitige Abrufe beim Quell-RIS je Prozess: Langsame Quellen dürfen nicht alle Worker binden
_LIVE_FETCH_SLOTS = threading.BoundedSemaphore(throttle.setting("FILE_PROXY_MAX_CONCURRENT"))
#: Bis zu dieser Größe bleibt ein Live-Abruf im Speicher, darüber in einer temporären Datei
_SPOOL_BYTES = 2 * 1024 * 1024


def _file_proxy_error(title, message, status=200):
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
    response = HttpResponse(html, content_type="text/html; charset=utf-8", status=status)
    response["X-Frame-Options"] = "ALLOWALL"
    response["Cache-Control"] = "no-store"
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

    file_obj = get_object_or_404(
        OParlFile.objects.select_related("body").defer("text_content", "raw_json", "body__raw_json"), id=file_id
    )
    if file_obj.withdrawn_by_publisher:
        from ._withdrawn import withdrawn_response

        return withdrawn_response(request, file_obj)
    force_download = request.GET.get("download") == "1"
    filename = file_obj.file_name or file_obj.name or "dokument.pdf"

    local = file_cache.local_file(file_obj)
    if local is not None:
        # FileResponse schließt die Datei nach dem Streaming selbst
        response = FileResponse(open(local, "rb"))  # noqa: SIM115
        # Nur passive Formate im Browser, alles andere als Download (fremde Quelle, gemeinsamer Ursprung)
        file_delivery.apply(
            response, file_cache.content_type_for(file_obj, "application/pdf"), filename, download=force_download
        )
        response["Cache-Control"] = "public, max-age=86400"
        response["X-Mandari-Cache"] = "hit"
        return response

    url = file_obj.download_url or file_obj.access_url
    if not url:
        raise Http404("Keine Download-URL verfügbar")

    # Quellen-Schonung (Issue #89): eine mehrfach unerreichbare Quelle wird nicht bei jedem
    # Vorschau-Aufruf erneut angefragt — das hält Ratenlimits/Sperren nur am Leben.
    if file_cache.source_paused(file_obj.body):
        response = _file_proxy_error(
            "Ratsinformationssystem derzeit nicht erreichbar",
            "Das Ratsinformationssystem dieser Kommune antwortet seit mehreren Abrufen nicht. "
            "Wir schonen die Quelle und holen das Dokument automatisch nach, sobald sie wieder "
            "erreichbar ist. Es lag noch nicht in unserem Zwischenspeicher.",
        )
        response.status_code = 503
        response["Retry-After"] = "3600"
        return response

    # Abrufe beim Quell-RIS sind begrenzt: je IP-Adresse und Minute sowie gleichzeitig je Prozess.
    # Dateien aus dem Zwischenspeicher (oben) zählen nicht mit.
    if throttle.hit(
        "file-live",
        throttle.client_ip(request),
        limit=throttle.setting("FILE_PROXY_FETCHES_PER_IP_MINUTE"),
        window=throttle.MINUTE,
    ):
        response = _file_proxy_error(
            "Zu viele Abrufe",
            "Von deinem Anschluss kamen in kurzer Zeit sehr viele Dokumentabrufe. Bitte warte einen Moment.",
            status=429,
        )
        response["Retry-After"] = "60"
        return response
    if not _LIVE_FETCH_SLOTS.acquire(timeout=2):
        response = _file_proxy_error(
            "Gerade viele Abrufe",
            "Das Dokument lag noch nicht in unserem Zwischenspeicher, und gerade laufen viele Abrufe "
            "bei Ratsinformationssystemen. Bitte versuche es gleich noch einmal.",
            status=503,
        )
        response["Retry-After"] = "30"
        return response
    try:
        return _fetch_live(file_obj, url, filename, force_download)
    finally:
        _LIVE_FETCH_SLOTS.release()


def _fetch_live(file_obj, url, filename, force_download):
    """Datei beim Quell-RIS abrufen (Größe, Dauer und Ziel begrenzt) und ausliefern (Write-Through)."""
    from django.http import FileResponse

    from apps.common.db_connections import release_idle_thread_connections

    from ..services import file_cache

    read_timeout = float(getattr(settings, "FILE_PROXY_TIMEOUT_SECONDS", 15))
    headers = file_cache.download_headers(file_obj.body)
    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)  # noqa: SIM115 – FileResponse schließt sie
    # Während des Abrufs keine Datenbankverbindung festhalten (Pool)
    release_idle_thread_connections()
    try:
        download = safe_fetch.download_to(
            spool,
            url,
            max_bytes=file_cache.max_bytes(),
            total_seconds=throttle.setting("FILE_PROXY_TOTAL_SECONDS"),
            timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0),
            headers=headers,
            user_agent=file_cache.USER_AGENT,
        )
    except httpx.HTTPStatusError as e:
        spool.close()
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
    except safe_fetch.TooLargeError:
        spool.close()
        return _file_proxy_error(
            "Datei zu groß für die Vorschau",
            "Dieses Dokument ist größer, als die Vorschau direkt abrufen kann. "
            "Bitte lade es beim Ratsinformationssystem der Kommune herunter.",
            status=413,
        )
    except safe_fetch.DeadlineExceededError:
        spool.close()
        return _file_proxy_error(
            "Abruf dauert zu lange",
            "Das Ratsinformationssystem liefert das Dokument gerade sehr langsam. Bitte versuche es später erneut.",
            status=504,
        )
    except httpx.RequestError as exc:
        spool.close()
        if isinstance(exc, safe_fetch.BlockedDestinationError):
            logger.warning("Dokument %s: Download-Adresse nicht öffentlich erreichbar, Abruf gesperrt", file_obj.id)
        return _file_proxy_error(
            "Server nicht erreichbar",
            "Das Ratsinformationssystem ist momentan nicht erreichbar und dieses Dokument lag noch nicht "
            "in unserem Zwischenspeicher. Wir legen Dokumente laufend im Zwischenspeicher ab — "
            "bitte versuche es später erneut.",
        )

    spool.seek(0)
    head = spool.read(512)
    content_type = file_cache.content_type_for(
        file_obj, (download.content_type or "application/octet-stream").split(";")[0]
    )
    if file_cache.looks_like_html(head) and "html" not in (file_obj.mime_type or "").lower():
        spool.close()
        return _file_proxy_error(
            "Quelle liefert derzeit keine Datei",
            "Das Ratsinformationssystem antwortet mit einer Hinweisseite statt mit dem Dokument "
            "(z. B. Wartung). Bitte versuche es später erneut.",
        )

    # Write-Through: beim nächsten Aufruf kommt die Datei von der Platte (nur gelistete Kommunen)
    try:
        if file_cache.caches_body(file_obj.body) and file_cache.has_room_for(download.size):
            file_cache.store_stream(file_obj, spool, content_type=content_type)
    except Exception as exc:  # Cache-Fehler dürfen die Auslieferung nie verhindern
        logger.warning("Dokument %s konnte nicht zwischengespeichert werden: %s", file_obj.id, exc)

    spool.seek(0)
    response = FileResponse(spool)
    file_delivery.apply(response, content_type, filename, download=force_download)
    response["Cache-Control"] = "public, max-age=86400"
    response["X-Mandari-Cache"] = "miss"
    return response
