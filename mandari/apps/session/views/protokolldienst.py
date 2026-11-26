# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbindung an einen Protokolldienst (``docs/PROTOKOLLDIENST.md``).

- ``ProtokolldienstEinstellungenView``: Einstellungen › Datenschutz und Zugänge › Protokolldienst (Recht
  ``manage_settings``): einschalten, Adresse und API-Schlüssel des Dienstes, Rückgabeschlüssel erzeugen (genau
  einmal sichtbar), Verbindung prüfen, Stand der Zustellung. Jede Änderung steht im Prüfprotokoll, nie ein Schlüssel.
- ``NiederschriftVorschlagView``: Vorschlag des Dienstes für einen TOP übernehmen oder verwerfen (Recht
  ``edit_protocols``; Regeln in ``protokolldienst.vorschlaege``).
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponseBase
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionMeeting, SessionProtokolldienst, SessionProtokolldienstMeldung, SessionTenant
from ..permissions import SessionViewMixin
from ..protokolldienst import client, einstellungen, vorschlaege
from ..rahmen import neues_design

SESSION_KEY_NEW_KEY = "session_protokolldienst_schluessel_einmal"


def _sitzungsschluessel(tenant: SessionTenant) -> str:
    """Ablage des einmal sichtbaren Schlüssels je Mandant: Die Seite eines anderen Mandanten sieht ihn nie."""
    return f"{SESSION_KEY_NEW_KEY}:{tenant.pk}"


#: Fehlercodes der Zustellung → Text für die Seite
FEHLERTEXTE: dict[str, str] = {
    "anmeldung": "Der Protokolldienst hat den API-Schlüssel abgelehnt.",
    "abgelehnt": "Der Protokolldienst hat Meldungen abgelehnt.",
    "sitzung_unbekannt": "Der Protokolldienst kennt eine Sitzung noch nicht (Übernahme per OParl abwarten).",
    "ueberlastet": "Der Protokolldienst war überlastet.",
    "dienstfehler": "Der Protokolldienst hat mit einem Fehler geantwortet.",
    "zeitgrenze": "Der Protokolldienst hat nicht rechtzeitig geantwortet.",
    "verbindung": "Der Protokolldienst war nicht erreichbar.",
    "top_unbekannt": "Ein Abstimmungsergebnis ließ sich keinem TOP beim Protokolldienst zuordnen.",
    "ausgeschaltet": "Die Anbindung war ausgeschaltet.",
    "nur_https": "Der API-Schlüssel geht nur über https; http ist in dieser Installation nicht freigegeben.",
}


class _MandantMixin(SessionViewMixin):
    @property
    def tenant(self) -> SessionTenant:
        return cast(SessionTenant, self.session_tenant)


class ProtokolldienstEinstellungenView(_MandantMixin, TemplateView):
    template_name = "session/settings/protokolldienst.html"
    permission_required = "manage_settings"

    def get_template_names(self) -> list[str]:
        if neues_design(self.session_tenant):
            return ["session/neu/einstellungen/protokolldienst.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = cast(Any, super()).get_context_data(**kwargs)
        konfiguration = einstellungen.fuer_mandant(self.tenant)
        wartend = SessionProtokolldienstMeldung.objects.filter(
            meeting__tenant=self.tenant, status=SessionProtokolldienstMeldung.WARTEND
        ).count()
        context.update(
            {
                "konfiguration": konfiguration,
                "neuer_schluessel": self.request.session.pop(_sitzungsschluessel(self.tenant), None),
                "wartend": wartend,
                "letzter_fehler": FEHLERTEXTE.get(konfiguration.last_error, "") if konfiguration else "",
                "freigegebene_hosts": sorted(einstellungen.erlaubte_hosts()),
            }
        )
        return context

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        aktion = str(request.POST.get("aktion", ""))
        if aktion == "schluessel":
            self._schluessel(request)
        elif aktion == "pruefen":
            self._pruefen(request)
        else:
            self._speichern(request)
        return redirect("session:settings_protokolldienst", tenant_slug=self.tenant.slug)

    def _log(self, request: HttpRequest, konfiguration: SessionProtokolldienst, changes: dict[str, Any]) -> None:
        audit.log_event(
            "update",
            konfiguration,
            tenant=self.tenant,
            user=self.session_user,
            request=request,
            object_repr="Anbindung an den Protokolldienst",
            changes=changes,
        )

    def _speichern(self, request: HttpRequest) -> None:
        try:
            konfiguration, aenderungen = einstellungen.speichern(
                self.tenant,
                enabled=request.POST.get("enabled") == "on",
                base_url=str(request.POST.get("base_url", "")),
                api_key=str(request.POST.get("api_key", "")).strip() or None,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return
        if aenderungen:
            self._log(request, konfiguration, aenderungen)
        messages.success(request, "Die Anbindung an den Protokolldienst ist gespeichert.")

    def _schluessel(self, request: HttpRequest) -> None:
        konfiguration = einstellungen.fuer_mandant(self.tenant)
        if konfiguration is None:
            konfiguration = SessionProtokolldienst.objects.create(tenant=self.tenant)
        roh = einstellungen.rueckgabe_schluessel_erzeugen(konfiguration)
        self._log(request, konfiguration, {"rueckgabeschluessel": "neu erzeugt", "anfang": roh[:8]})
        request.session[_sitzungsschluessel(self.tenant)] = roh
        messages.success(
            request, "Neuer Rückgabeschlüssel erzeugt. Er wird nur jetzt angezeigt; der bisherige gilt nicht mehr."
        )

    def _pruefen(self, request: HttpRequest) -> None:
        konfiguration = einstellungen.fuer_mandant(self.tenant)
        if konfiguration is None or not (konfiguration.base_url and konfiguration.api_key_encrypted):
            messages.error(request, "Bitte zuerst Adresse und API-Schlüssel des Protokolldienstes speichern.")
            return
        try:
            client.verbindung_pruefen(konfiguration)
        except client.ProtokolldienstError as fehler:
            messages.error(request, FEHLERTEXTE.get(fehler.code, "Der Protokolldienst ist nicht erreichbar."))
            return
        messages.success(request, "Der Protokolldienst ist erreichbar und hat den API-Schlüssel angenommen.")


class NiederschriftVorschlagView(_MandantMixin, View):
    permission_required = "edit_protocols"
    http_method_names = ["post"]

    def post(
        self, request: HttpRequest, tenant_slug: str, meeting_id: Any, vorschlag_id: Any, entscheidung: str
    ) -> HttpResponseBase:
        queryset = SessionMeeting.objects.filter(tenant=self.tenant).visible_to(self.session_permissions)
        meeting = get_object_or_404(queryset.select_related("tenant"), pk=meeting_id)
        rechte, nutzer = self.session_permissions, self.session_user
        try:
            if entscheidung == "uebernehmen":
                hinweise = vorschlaege.uebernehmen(
                    meeting, vorschlag_id, permissions=rechte, session_user=nutzer, request=request
                )
                for hinweis in hinweise:
                    messages.warning(request, hinweis)
                messages.success(request, "Der Vorschlag ist in die Niederschrift übernommen.")
            elif entscheidung == "verwerfen":
                vorschlaege.verwerfen(meeting, vorschlag_id, permissions=rechte, session_user=nutzer, request=request)
                messages.info(request, "Der Vorschlag ist verworfen; die Niederschrift bleibt unverändert.")
            else:
                messages.error(request, "Unbekannte Entscheidung.")
        except vorschlaege.VorschlagError as exc:
            messages.error(request, exc.user_message)
        return redirect("session:meeting_protocol", tenant_slug=self.tenant.slug, meeting_id=meeting.pk)
