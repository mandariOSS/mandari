# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Assistent „Mandant anlegen“ (Issue #317).

Schlanke Oberfläche über demselben Service wie der Befehl ``session_create_tenant``
(``services/tenant_provisioning.py``). Schritt 1 übernimmt ein Profil als Vorbelegung, Schritt 2
zeigt alle Angaben zum Prüfen und Anpassen. Nur für Staff mit dem Recht, Mandanten anzulegen
(Superuser haben es immer); der Django-Admin selbst sperrt Nicht-Staff und fremde Netze.

Meldungen entstehen aus bekannten Werten; Ausnahmen landen nur im Betriebslog.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django import forms
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from unfold.widgets import (
    UnfoldAdminEmailInputWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextInputWidget,
    UnfoldBooleanSwitchWidget,
)

from apps.session.services import numbering_service, tenant_provisioning
from apps.session.services.tenant_provisioning import PresetKatalog, Profil, TenantSpec

if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin

logger = logging.getLogger(__name__)

TEMPLATE = "admin/session/sessiontenant/provision.html"
FEHLER_ALLGEMEIN = "Der Mandant ließ sich nicht anlegen. Die Ursache steht im Betriebsprotokoll."
FEHLER_PRESETS = "Die Preset-Datei der Mandanten ist ungültig. Die Ursache steht im Betriebsprotokoll."


def date_widget() -> Any:
    """Datumsfeld des Browsers (ISO-Wert, kein Kalender-Skript nötig)."""
    widget = UnfoldAdminTextInputWidget()
    widget.input_type = "date"
    return widget


class ProfileForm(forms.Form):
    """Schritt 1: Profil als Vorbelegung wählen (GET, ändert nichts)."""

    profil = forms.ChoiceField(label="Profil", required=False, widget=UnfoldAdminSelectWidget)

    def __init__(self, *args: Any, katalog: PresetKatalog, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        feld: Any = self.fields["profil"]
        feld.choices = [("", "Ohne Profil"), *((key, profil.label) for key, profil in katalog.profiles.items())]


class TenantProvisioningForm(forms.Form):
    """Alle Angaben ausdrücklich – das Profil belegt nur vor."""

    name = forms.CharField(label="Name", max_length=255, widget=UnfoldAdminTextInputWidget)
    slug = forms.SlugField(
        label="URL-Kürzel",
        max_length=100,
        help_text="Kleinbuchstaben, Ziffern und Bindestriche; erscheint in /session/<kürzel>/",
        widget=UnfoldAdminTextInputWidget,
    )
    short_name = forms.CharField(label="Kurzname", max_length=50, required=False, widget=UnfoldAdminTextInputWidget)
    body_type = forms.ChoiceField(label="Körperschaftstyp", required=False, widget=UnfoldAdminSelectWidget)
    ags = forms.CharField(
        label="Amtlicher Gemeindeschlüssel",
        max_length=8,
        required=False,
        help_text="2, 3, 5 oder 8 Ziffern",
        widget=UnfoldAdminTextInputWidget,
    )
    numbering = forms.ChoiceField(label="Nummernkreis", widget=UnfoldAdminSelectWidget)
    term_name = forms.CharField(label="Aktuelle Wahlperiode", max_length=255, widget=UnfoldAdminTextInputWidget)
    term_number = forms.IntegerField(
        label="Nummer der Wahlperiode",
        required=False,
        min_value=1,
        max_value=32767,
        help_text="Pflicht bei Nummernkreisen je Wahlperiode, z. B. 22 für Drucksachen 22-0001",
        widget=UnfoldAdminIntegerFieldWidget,
    )
    term_start = forms.DateField(label="Beginn", widget=date_widget())
    term_end = forms.DateField(label="Ende", widget=date_widget())
    committees = forms.ChoiceField(label="Gremienvorlage", required=False, widget=UnfoldAdminSelectWidget)
    admin_email = forms.EmailField(
        label="E-Mail des ersten Administrators",
        help_text="Vorhandenes Konto wird Mitglied; sonst geht eine Einladung per E-Mail hinaus.",
        widget=UnfoldAdminEmailInputWidget,
    )
    dry_run = forms.BooleanField(
        label="Nur prüfen",
        required=False,
        help_text="Führt alle Schritte aus und rollt sie zurück; keine E-Mail.",
        widget=UnfoldBooleanSwitchWidget,
    )

    def __init__(self, *args: Any, katalog: PresetKatalog, **kwargs: Any) -> None:
        from apps.session.models import SessionTenant

        super().__init__(*args, **kwargs)
        self.katalog = katalog
        felder: dict[str, Any] = self.fields
        felder["body_type"].choices = [("", "–"), *SessionTenant.BODY_TYPE_CHOICES]
        felder["numbering"].choices = [(key, preset.label) for key, preset in numbering_service.PRESETS.items()]
        felder["committees"].choices = [
            ("", "Keine Gremien anlegen"),
            *((key, vorlage.label) for key, vorlage in katalog.committee_templates.items()),
        ]

    def to_spec(self) -> TenantSpec:
        daten = self.cleaned_data
        return TenantSpec(
            name=str(daten["name"]).strip(),
            slug=str(daten["slug"]).strip(),
            admin_email=str(daten["admin_email"]).strip().lower(),
            short_name=str(daten.get("short_name") or "").strip(),
            body_type=str(daten.get("body_type") or ""),
            ags=str(daten.get("ags") or "").strip(),
            numbering=str(daten["numbering"]),
            term_name=str(daten["term_name"]).strip(),
            term_number=daten.get("term_number"),
            term_start=daten.get("term_start"),
            term_end=daten.get("term_end"),
            committees=str(daten.get("committees") or ""),
        )


def initial_from_profile(profil: Profil | None) -> dict[str, Any]:
    if profil is None:
        return {"numbering": "standard"}
    return {
        "body_type": profil.body_type,
        "numbering": profil.numbering,
        "term_name": profil.term_name,
        "term_number": profil.term_number,
        "term_start": profil.term_start,
        "term_end": profil.term_end,
        "committees": profil.committees,
    }


def actor_for(request: HttpRequest) -> str:
    """Wer im Admin gehandelt hat – für das Audit-Log des Mandanten."""
    return f"Django-Admin ({request.user.get_username()})"


def provision_view(model_admin: ModelAdmin[Any], request: HttpRequest) -> HttpResponse:
    """Assistent „Mandant anlegen“; ``admin_site.admin_view`` hat Anmeldung und Staff geprüft."""
    user: Any = request.user
    if not (user.is_superuser or model_admin.has_add_permission(request)):
        raise PermissionDenied
    try:
        katalog = tenant_provisioning.load_presets()
    except tenant_provisioning.ProvisioningError:
        logger.exception("Preset-Datei der Mandanten ist ungültig.")
        katalog = PresetKatalog(profiles={}, committee_templates={})
        fehler_katalog = True
    else:
        fehler_katalog = False

    profil_key = str(request.GET.get("profil") or "")
    profil = katalog.profiles.get(profil_key)
    ergebnis = None
    if request.method == "POST":
        form = TenantProvisioningForm(request.POST, katalog=katalog)
        if form.is_valid():
            spec = form.to_spec()
            meldungen = tenant_provisioning.validate_spec(spec, katalog)
            for meldung in meldungen:
                form.add_error(None, meldung)
            if not meldungen:
                try:
                    ergebnis = tenant_provisioning.provision_tenant(
                        spec, catalog=katalog, dry_run=bool(form.cleaned_data.get("dry_run")), actor=actor_for(request)
                    )
                except Exception:
                    logger.exception("Mandant %s ließ sich im Admin-Assistenten nicht anlegen.", spec.slug)
                    form.add_error(None, FEHLER_ALLGEMEIN)
    else:
        form = TenantProvisioningForm(initial=initial_from_profile(profil), katalog=katalog)

    context = {
        **model_admin.admin_site.each_context(request),
        "title": "Mandant anlegen",
        "opts": model_admin.model._meta,
        "form": form,
        "profile_form": ProfileForm(initial={"profil": profil_key if profil else ""}, katalog=katalog),
        "ergebnis": ergebnis,
        "fehler_katalog": FEHLER_PRESETS if fehler_katalog else "",
    }
    return TemplateResponse(request, TEMPLATE, context)
