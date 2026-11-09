# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlüsse im Bürgerportal: die Liste der Entscheidungen (Issue #1270) und die Umsetzung „Was wurde aus …?“ (Issue #48).

``/insight/beschluesse/`` (je Kommune ``/insight/k/<slug>/beschluesse/``) zeigt, was die Gremien entschieden haben
(``services.beschlussliste``). Die Beschlusskontrolle mit dem Umsetzungsstand der Verwaltung steht als Reiter
„Umsetzung“ unter ``/insight/beschluesse/umsetzung/``; der Reiter erscheint nur, wenn die Kommune solche Beschlüsse
veröffentlicht. Alte Adressen der Beschlusskontrolle (``?status=…``, ``?offen=1``, ``?jahr=…``, ``?q=…``) führen dorthin.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db.models import Q
from django.forms import ValidationError
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from apps.common.next_url import safe_next_url
from apps.common.params import uuid_param

from .. import publication, throttle
from ..models import DecisionSubscription, OParlBody
from ..portal import get_portal
from ..seo import get_list_seo, get_page_seo
from ..services import beschlussliste, decision_tracking
from ..services.indexierung import liste_der_kommune
from ..services.vorgang_urheber import bezeichnungen_fuer, fraktionen_der_kommune
from ._helpers import ActiveBodyRequiredMixin, FilterInAdresseMixin, get_active_body, link_confirmation, page_number
from .home import zuletzt_beschlossen

PAGE_SIZE = 25
#: Ohne veröffentlichte Beschlüsse: so viele zuletzt beratene Punkte statt einer leeren Liste (Issue #1261)
LEER_ANZAHL = 6

#: Sichtbarkeit der Beschlussseiten → Stand im Bürgerportal (Hinweis, 503, 410; Issue #618)
_STATE_MODES = {
    decision_tracking.ARCHIVED: publication.ARCHIVED,
    decision_tracking.PAUSED: publication.PAUSED,
    decision_tracking.WITHDRAWN: publication.WITHDRAWN,
}


def _apply_state(request, visibility: str, body):
    """
    Beschlussseiten folgen der Veröffentlichung des Mandanten: Hinweisseite (503/410) oder Archiv-Hinweis.

    Gibt die Hinweisseite zurück oder ``None``, wenn die Seite (ggf. mit Archiv-Hinweis) laufen darf.
    """
    mode = _STATE_MODES.get(visibility)
    if mode is None:
        return None
    state = publication.body_state(body.pk) if body is not None else None
    if state is None or state.mode != mode:
        state = publication.BodyState(mode=mode)
    request.insight_publication_state = state
    return publication.state_response(request, state)


#: Parameter, die nur die Beschlusskontrolle kennt: Adressen mit ihnen führen auf den Reiter „Umsetzung“
ALTE_PARAMETER = frozenset({"status", "offen", "jahr", "q"})


def _adresse_der_auswahl(request: HttpRequest, body: OParlBody, auswahl: beschlussliste.Auswahl) -> str:
    """
    Vollständige Adresse der Auswahl zum Teilen: die Liste je Kommune (``/insight/k/<slug>/beschluesse/``), die ohne
    Sitzung dieselbe Auswahl zeigt; auf einem eigenen Host des Bürgerportals und ohne Slug die Adresse der Anfrage.
    """
    portal = get_portal(request)
    pfad = "" if portal is not None and portal.via_host else liste_der_kommune(body, "decision_list")
    return request.build_absolute_uri((pfad or request.path) + auswahl.adresse())


def _feed_der_auswahl(
    body: OParlBody, auswahl: beschlussliste.Auswahl, gremien: dict[str, beschlussliste.Gremium]
) -> dict[str, str]:
    """
    Beschluss-Feed zur Auswahl (Issue #1274, ``views.feeds.decision_feed``): immer mit der Kommune in der Adresse, weil
    Feed-Reader keine Sitzung haben, und mit dem gewählten Gremium. Die übrigen Filter (Ergebnis, Absender, Zeitraum)
    kennt der Feed nicht; er bringt neue Beschlüsse, sobald sie erscheinen.
    """
    from .feeds import abo

    gremium = gremien.get(auswahl.gremium) if auswahl.gremium else None
    abfrage = {"kommune": str(body.pk)} | ({"gremium": str(gremium.id)} if gremium is not None else {})
    titel = f"Beschlüsse: {gremium.name}" if gremium is not None else f"Beschlüsse in {body.get_display_name()}"
    return abo("decision_feed", titel, abfrage=urlencode(abfrage))


class DecisionListView(FilterInAdresseMixin, ActiveBodyRequiredMixin, TemplateView):
    """
    Beschlüsse als Liste der Entscheidungen (Issue #1270): Tagesordnungspunkte mit Ergebnis, nach Sitzung gruppiert,
    neueste zuerst; Umschalter für die Gremiengruppe und Filter in der Adresse (``FilterInAdresseMixin``, htmx tauscht
    nur die Ergebnisse, ``partials/beschluesse_liste.html``).
    """

    template_name = "pages/decisions/list.html"
    teil_vorlage = "partials/beschluesse_liste.html"

    def get(self, request, *args, **kwargs):
        if ALTE_PARAMETER & set(request.GET):
            # Die Abfrage kommt nur mit, solange das Ziel relativ auf dem eigenen Host bleibt
            ziel = reverse("insight_core:insight:decision_tracking")
            return redirect(safe_next_url(request, f"{ziel}?{request.GET.urlencode()}", ziel))
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        auswahl = beschlussliste.Auswahl.aus_anfrage(self.request.GET)
        name = body.get_display_name() if body is not None else ""
        context.update(
            {
                "active_body": body,
                "nav": "decision_list",
                "auswahl": auswahl,
                "nur_liste": self.nur_liste,
                "seo": get_list_seo(
                    self.request,
                    "decision_list",
                    title="Beschlüsse",
                    description=(
                        f"Was Rat, Ausschüsse und Bezirksvertretungen in {name} entschieden haben, neueste zuerst."
                        if body is not None
                        else "Was Rat, Ausschüsse und Bezirksvertretungen entschieden haben, neueste zuerst."
                    ),
                    body=body,
                ).to_dict(),
            }
        )
        if body is not None:
            context.update(self._liste(body, auswahl))
        return context

    def _liste(self, body: OParlBody, auswahl: beschlussliste.Auswahl) -> dict:
        gremien = beschlussliste.gremien_der_kommune(body)
        wahlperiode = beschlussliste.wahlperiode_beginn(body)
        zeit = beschlussliste.zeitraum(auswahl.zeitraum, wahlperiode=wahlperiode)
        fraktionen = fraktionen_der_kommune(body.pk)
        besonders = (beschlussliste.VON_VERWALTUNG, beschlussliste.VON_EINWOHNER)
        bezeichnungen = bezeichnungen_fuer(fraktionen, auswahl.von) if auswahl.von not in besonders else ()
        punkte = beschlussliste.punkte(body, auswahl, zeit=zeit, gremien=gremien, fraktions_bezeichnungen=bezeichnungen)
        blatt, sitzungen = beschlussliste.seite(punkte, page_number(self.request), gremien)

        im_zeitraum = beschlussliste.gremien_im_zeitraum(body, zeit, gremien)
        gewaehlt = gremien.get(auswahl.gremium)
        if gewaehlt is not None and gewaehlt not in im_zeitraum:
            im_zeitraum.append(gewaehlt)
        gremien_auswahl = [
            (titel, [g for g in im_zeitraum if g.gruppe == gruppe])
            for gruppe, titel in (
                (beschlussliste.GRUPPE_RAT, "Rat und Ausschüsse"),
                (beschlussliste.GRUPPE_BEZIRK, "Bezirksvertretungen"),
                ("", "Weitere Gremien"),
            )
        ]
        zeitraeume = {
            wert: text
            for wert, text in beschlussliste.ZEITRAEUME.items()
            if wert != beschlussliste.ZEITRAUM_WAHLPERIODE or wahlperiode is not None
        }
        laenger = beschlussliste.LAENGER.get(auswahl.zeitraum)
        return {
            "anzahl": punkte.count() if blatt.paginator.count else 0,
            "page_obj": blatt,
            "sitzungen": sitzungen,
            "zeitraum": zeit,
            "zeitraeume": zeitraeume,
            "gruppen": beschlussliste.GRUPPEN,
            # Der Umschalter hat nur eine Aufgabe, wenn die Kommune Bezirksvertretungen hat
            "mit_umschalter": any(g.gruppe == beschlussliste.GRUPPE_BEZIRK for g in gremien.values()),
            "gremien_auswahl": [(titel, liste) for titel, liste in gremien_auswahl if liste],
            "von_auswahl": [(f.wert, f.anzeige) for f in fraktionen]
            + [(beschlussliste.VON_VERWALTUNG, "Verwaltung"), (beschlussliste.VON_EINWOHNER, "Bürgeranträge")],
            "ergebnisse": beschlussliste.ERGEBNISSE,
            "filter_zahl": auswahl.weitere_filter,
            "link_auswahl": _adresse_der_auswahl(self.request, body, auswahl),
            "abo_feed": (abo_feed := _feed_der_auswahl(body, auswahl, gremien)),
            "feed_url": abo_feed["hilfe"],
            # Leerzustand: der nächstlängere Zeitraum, sofern es ihn gibt
            "leer_laenger": (
                (auswahl.adresse(zeitraum=laenger), zeitraeume[laenger])
                if laenger is not None and laenger in zeitraeume
                else None
            ),
            # Reiter „Umsetzung“ nur, wenn die Verwaltung Beschlüsse mit Umsetzungsstand veröffentlicht (#1261)
            "umsetzung_vorhanden": decision_tracking.has_public_decisions(body),
        }


class DecisionTrackingView(ActiveBodyRequiredMixin, TemplateView):
    """Reiter „Umsetzung“: Beschlüsse mit dem Umsetzungsstand, den die Verwaltung meldet (Issue #48)."""

    template_name = "pages/decisions/umsetzung.html"

    def get(self, request, *args, **kwargs):
        body = get_active_body(request)
        if body is not None:
            response = _apply_state(request, decision_tracking.body_visibility(body), body)
            if response is not None:
                return response
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        context["active_body"] = body
        context["nav"] = "decision_tracking"
        # Leer, solange die Verwaltung keinen Beschluss mit Umsetzungsstand freigegeben hat – auch wenn sie das
        # Veröffentlichen schon eingeschaltet hat (Issue #1261). Dann statt einer leeren Liste die zuletzt
        # beratenen Punkte der Kommune und der Weg zu Sitzungen und Vorgängen.
        context["publishing"] = body is not None and decision_tracking.has_public_decisions(body)
        # robots und kanonische Adresse wie die übrigen Listen; mit Filtern noindex (urls.py, Issue #914). Die leere
        # Seite gehört nicht in den Index, ihren Links dürfen Suchmaschinen folgen.
        context["seo"] = get_page_seo(
            self.request,
            title="Umsetzung der Beschlüsse",
            description="Was wurde aus den Beschlüssen des Rats? Umsetzungsstand, Fristen und Meldungen der Verwaltung.",
            body=body,
            robots="index, follow" if context["publishing"] else "noindex, follow",
            keywords=["Beschlüsse", "Umsetzung", "Kommunalpolitik", "Transparenz"],
        ).to_dict()
        if not context["publishing"]:
            context["zuletzt_beraten"] = zuletzt_beschlossen(body, anzahl=LEER_ANZAHL) if body is not None else []
            return context
        tenants = decision_tracking.publishing_tenants(body)

        params = self.request.GET
        qs = decision_tracking.public_decisions(body)
        status = params.get("status", "")
        organization_id = params.get("gremium", "")
        year = params.get("jahr", "")
        query = (params.get("q") or "").strip()
        only_open = params.get("offen") == "1"

        if status in decision_tracking.PUBLIC_STATUS_LABELS:
            qs = qs.filter(implementation_status=status)
        if only_open:
            qs = qs.exclude(implementation_status="done")
        if organization_id:
            organization_uuid = uuid_param(organization_id)  # ungültig: kein Treffer statt Serverfehler
            qs = qs.filter(meeting__organization_id=organization_uuid) if organization_uuid else qs.none()
        if year.isdigit():
            qs = qs.filter(meeting__start__year=int(year))
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(resolution_text__icontains=query)
                | Q(resolution_number__icontains=query)
                | Q(implementation_public_note__icontains=query)
            )

        from apps.session.models import SessionOrganization

        page = Paginator(qs, PAGE_SIZE).get_page(params.get("page"))
        items = list(page.object_list)
        for item in items:
            item.public_label = decision_tracking.PUBLIC_STATUS_LABELS.get(
                item.implementation_status, item.get_implementation_status_display()
            )
            # Ohne Hausnummer bei Eingaben aus der Einwohnerschaft und Bauanträgen (Issue #1269)
            item.titel_oeffentlich = decision_tracking.oeffentlicher_name(item)
        context.update(
            {
                "stats": decision_tracking.stats(body),
                "status_labels": decision_tracking.PUBLIC_STATUS_LABELS,
                "items": items,
                "page_obj": page,
                "organizations": SessionOrganization.objects.filter(
                    tenant__in=tenants, is_active=True, organization_type__in=["council", "committee", "advisory"]
                ).order_by("name"),
                "years": sorted(
                    {
                        d.year
                        for d in decision_tracking.public_decisions(body).values_list("meeting__start", flat=True)
                        if d
                    },
                    reverse=True,
                ),
                "filter_status": status,
                "filter_organization": organization_id,
                "filter_year": year,
                "filter_open": only_open,
                "search_query": query,
                "has_filter": bool(status or organization_id or year or query or only_open),
                "today": timezone.localdate(),
            }
        )
        return context


class DecisionDetailView(TemplateView):
    template_name = "pages/decisions/detail.html"

    def _get_item(self):
        from apps.session.models import SessionAgendaItem

        item = get_object_or_404(
            SessionAgendaItem.objects.select_related("meeting__organization", "meeting__tenant__oparl_body", "paper"),
            id=self.kwargs["pk"],
        )
        self.visibility = decision_tracking.visibility(item)
        if self.visibility == decision_tracking.HIDDEN:
            raise Http404("Beschluss nicht öffentlich")
        return item

    def get(self, request, *args, **kwargs):
        item = self._get_item()
        response = _apply_state(request, self.visibility, item.meeting.tenant.oparl_body)
        if response is not None:
            return response
        return self.render_to_response(self.get_context_data(item=item, **kwargs))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        item = kwargs.get("item") or self._get_item()
        body = item.meeting.tenant.oparl_body
        status_label = decision_tracking.PUBLIC_STATUS_LABELS.get(
            item.implementation_status, item.get_implementation_status_display()
        )
        # Kommune der Seite folgt dem Beschluss (Deep-Links aus E-Mails)
        if str(self.request.session.get("active_body_id")) != str(body.id):
            self.request.session["active_body_id"] = str(body.id)
        context.update(
            {
                "active_body": body,
                "nav": "decision_detail",
                "item": item,
                # Name ohne Hausnummer bei Eingaben aus der Einwohnerschaft und Bauanträgen (Issue #1269)
                "titel": decision_tracking.oeffentlicher_name(item),
                "tenant": item.meeting.tenant,
                "timeline": decision_tracking.timeline(item),
                "status_label": status_label,
                "overdue": bool(
                    item.implementation_deadline
                    and item.implementation_status != "done"
                    and item.implementation_deadline < timezone.localdate()
                ),
                "oparl_meeting_id": item.meeting.oparl_meeting_id,
                "oparl_paper_id": item.paper.oparl_paper_id if item.paper_id else None,
                "subscriber_count": DecisionSubscription.objects.filter(
                    agenda_item=item, confirmed=True, unsubscribed_at__isnull=True
                ).count(),
                # Archiv (Issue #618): keine neuen Abos, es kommen keine Meldungen mehr
                "can_subscribe": getattr(self, "visibility", None) == decision_tracking.VISIBLE,
            }
        )
        # Titel mit Kommune, Description, robots, kanonische Adresse und strukturierte Daten aus dem schon
        # Geladenen (Issue #914): Der Umsetzungsstand ist eine eigene, öffentliche Seite
        from ..seo import get_decision_seo

        context["seo"] = get_decision_seo(item, body, str(status_label), self.request).to_dict()
        return context

    def post(self, request, *args, **kwargs):
        """Beschluss abonnieren (Double-Opt-In)."""
        item = self._get_item()
        if self.visibility != decision_tracking.VISIBLE:
            response = _apply_state(request, self.visibility, item.meeting.tenant.oparl_body)
            if response is not None:
                return response
            messages.info(request, "Dieser Beschluss steht im Archiv; es gibt keine neuen Meldungen mehr.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)
        email = (request.POST.get("email") or "").strip().lower()
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Bitte eine gültige E-Mail-Adresse angeben.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)
        if request.POST.get("privacy") != "on":
            messages.error(request, "Bitte der Verarbeitung der E-Mail-Adresse zustimmen.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)
        # Das Formular löst eine Bestätigungsmail aus: je IP-Adresse gedrosselt
        if throttle.mail_ip_exceeded(request):
            messages.error(request, "Es wurden gerade zu viele Benachrichtigungen angefragt. Bitte später erneut.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)

        subscription, created = DecisionSubscription.objects.get_or_create(agenda_item=item, email=email)
        if not created and subscription.confirmed and subscription.unsubscribed_at is None:
            messages.info(request, "Diese Adresse ist für den Beschluss bereits eingetragen.")
            return redirect("insight_core:insight:decision_detail", pk=item.id)
        if not created:
            subscription.unsubscribed_at = None
            subscription.confirmed = False
            subscription.save(update_fields=["unsubscribed_at", "confirmed"])
        # Je Empfängeradresse gedrosselt; die Antwort bleibt gleich
        if not throttle.mail_address_exceeded(email):
            decision_tracking.send_confirmation(subscription)
        messages.success(
            request,
            "Fast geschafft: Wir haben eine E-Mail zur Bestätigung geschickt. Erst nach dem Klick auf den Link "
            "erhalten Sie Benachrichtigungen zu diesem Beschluss.",
        )
        return redirect("insight_core:insight:decision_detail", pk=item.id)


@require_http_methods(["GET", "POST"])
def confirm_decision_subscription(request, token):
    """GET zeigt die Bestätigungsseite, erst der Klick darauf (POST) aktiviert das Abo."""
    subscription = get_object_or_404(DecisionSubscription.objects.select_related("agenda_item__meeting"), token=token)
    already = subscription.confirmed and subscription.unsubscribed_at is None
    if request.method == "GET" and not already:
        return link_confirmation(
            request,
            title="Benachrichtigung aktivieren",
            message="Bitte bestätigen Sie die Benachrichtigung zum Beschluss "
            f"„{decision_tracking.oeffentlicher_name(subscription.agenda_item)}“.",
            button="Benachrichtigung aktivieren",
            icon="bell-ring",
        )
    if not already:
        subscription.confirmed = True
        subscription.confirmed_at = timezone.now()
        subscription.unsubscribed_at = None
        subscription.save(update_fields=["confirmed", "confirmed_at", "unsubscribed_at"])
    return render(
        request,
        "pages/decisions/subscription_status.html",
        {
            "item": subscription.agenda_item,
            "titel": decision_tracking.oeffentlicher_name(subscription.agenda_item),
            "title": "Benachrichtigung aktiv" if not already else "Bereits bestätigt",
            "message": (
                "Sie erhalten eine E-Mail, sobald die Verwaltung einen neuen Umsetzungsstand zu diesem Beschluss meldet."
                if not already
                else "Diese Benachrichtigung war bereits aktiv."
            ),
            "unsubscribe_token": subscription.token,
        },
    )


@require_http_methods(["GET", "POST"])
def unsubscribe_decision(request, token):
    """GET zeigt die Bestätigungsseite, erst der Klick darauf (POST) meldet ab."""
    subscription = get_object_or_404(DecisionSubscription.objects.select_related("agenda_item__meeting"), token=token)
    if request.method == "GET" and subscription.unsubscribed_at is None:
        return link_confirmation(
            request,
            title="Benachrichtigung beenden",
            message="Möchten Sie keine E-Mails mehr zum Beschluss "
            f"„{decision_tracking.oeffentlicher_name(subscription.agenda_item)}“ erhalten?",
            button="Abmelden",
            icon="bell-off",
        )
    if subscription.unsubscribed_at is None:
        subscription.unsubscribed_at = timezone.now()
        subscription.save(update_fields=["unsubscribed_at"])
    return render(
        request,
        "pages/decisions/subscription_status.html",
        {
            "item": subscription.agenda_item,
            "titel": decision_tracking.oeffentlicher_name(subscription.agenda_item),
            "title": "Benachrichtigung beendet",
            "message": "Sie erhalten keine weiteren E-Mails zu diesem Beschluss. Die Adresse wird nicht weiter verwendet.",
            "unsubscribe_token": None,
        },
    )
