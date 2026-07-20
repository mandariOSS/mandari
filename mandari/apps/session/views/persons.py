# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    UpdateView,
)

from ..models import (
    SessionPerson,
)
from ..permissions import SessionViewMixin


class SessionPersonForm(forms.ModelForm):
    """
    Personen-Formular mit verschlüsselten Kontakt-/Bankfeldern (Issue #27).

    Sicherheit: Telefon, Adresse und Bankdaten werden ausschließlich über
    die generierten Verschlüsselungs-Accessoren gelesen/geschrieben
    (AES-256-GCM mit Tenant-Key) — niemals als Klartext-Modelfelder.
    Bankdaten sind nur für Berechtigte (manage_allowances) sichtbar.
    """

    phone = forms.CharField(label="Telefon", required=False, max_length=100)
    address = forms.CharField(label="Adresse", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    bank_account_holder = forms.CharField(label="Kontoinhaber/in", required=False, max_length=200)
    bank_iban = forms.CharField(label="IBAN", required=False, max_length=42)
    bank_bic = forms.CharField(label="BIC", required=False, max_length=11)

    class Meta:
        model = SessionPerson
        fields = [
            "title",
            "form_of_address",
            "given_name",
            "family_name",
            "email",
            "is_active",
            "start_date",
            "end_date",
        ]

    def __init__(self, *args, show_bank_fields: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_bank_fields = show_bank_fields
        if not show_bank_fields:
            for field in ("bank_account_holder", "bank_iban", "bank_bic"):
                del self.fields[field]
        # Entschlüsselte Werte vorbelegen (nur bei bestehender Person)
        if self.instance.pk:
            self.fields["phone"].initial = self.instance.get_phone_decrypted()
            self.fields["address"].initial = self.instance.get_address_decrypted()
            if show_bank_fields:
                self.fields["bank_account_holder"].initial = self.instance.get_bank_account_holder_decrypted()
                self.fields["bank_iban"].initial = self.instance.get_bank_iban_decrypted()
                self.fields["bank_bic"].initial = self.instance.get_bank_bic_decrypted()

    def save(self, commit=True):
        person = super().save(commit=False)
        person.set_phone_encrypted(self.cleaned_data.get("phone", ""))
        person.set_address_encrypted(self.cleaned_data.get("address", ""))
        if self.show_bank_fields:
            person.set_bank_account_holder_encrypted(self.cleaned_data.get("bank_account_holder", ""))
            person.set_bank_iban_encrypted(self.cleaned_data.get("bank_iban", "").replace(" ", ""))
            person.set_bank_bic_encrypted(self.cleaned_data.get("bank_bic", ""))
        if commit:
            person.save()
        return person


# =============================================================================
# PERSONS
# =============================================================================


class PersonListView(SessionViewMixin, ListView):
    """List of persons."""

    model = SessionPerson
    template_name = "session/persons/list.html"
    context_object_name = "persons"
    paginate_by = 50
    permission_required = "view_meetings"  # Basic access

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.order_by("family_name", "given_name")

        # Filter by active status
        if self.request.GET.get("active") != "0":
            qs = qs.filter(is_active=True)

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(
                Q(given_name__icontains=search) | Q(family_name__icontains=search) | Q(email__icontains=search)
            )

        return qs


class PersonDetailView(SessionViewMixin, DetailView):
    """Person detail view."""

    model = SessionPerson
    template_name = "session/persons/detail.html"
    context_object_name = "person"
    pk_url_kwarg = "person_id"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        person = self.object

        # Memberships
        context["memberships"] = person.memberships.select_related("organization").order_by("-start_date")

        # Recent attendances
        context["recent_attendances"] = person.attendances.select_related("meeting__organization").order_by(
            "-meeting__start"
        )[:10]

        # Bankdaten nur für Berechtigte entschlüsseln (Sitzungsgeld-Verwaltung)
        context["can_manage_persons"] = self.has_permission("manage_organizations")
        context["can_view_bank_data"] = self.has_permission("manage_allowances")
        if context["can_view_bank_data"]:
            context["bank_account_holder"] = person.get_bank_account_holder_decrypted()
            context["bank_iban"] = person.get_bank_iban_decrypted()
            context["bank_bic"] = person.get_bank_bic_decrypted()

        return context


class PersonFormMixin:
    """Gemeinsame Logik für Personen-Formulare."""

    model = SessionPerson
    form_class = SessionPersonForm
    template_name = "session/persons/form.html"
    permission_required = "manage_organizations"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["show_bank_fields"] = self.has_permission("manage_allowances")
        return kwargs

    def get_success_url(self):
        return reverse(
            "session:person_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "person_id": self.object.id,
            },
        )


class PersonCreateView(PersonFormMixin, SessionViewMixin, CreateView):
    """Person anlegen (Issue #27)."""

    def form_valid(self, form):
        form.instance.tenant = self.session_tenant
        messages.success(self.request, "Person wurde angelegt.")
        return super().form_valid(form)


class PersonUpdateView(PersonFormMixin, SessionViewMixin, UpdateView):
    """Person bearbeiten (Kontaktdaten verschlüsselt, Bankdaten nur für Berechtigte)."""

    pk_url_kwarg = "person_id"

    def form_valid(self, form):
        messages.success(self.request, "Person wurde aktualisiert.")
        return super().form_valid(form)


class PersonDeactivateView(SessionViewMixin, View):
    """Person deaktivieren/reaktivieren (statt Löschen — Historie bleibt)."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, person_id):
        person = get_object_or_404(SessionPerson, pk=person_id, tenant=self.session_tenant)
        person.is_active = not person.is_active
        person.save()
        state = "reaktiviert" if person.is_active else "deaktiviert"
        messages.success(request, f"{person.display_name} wurde {state}.")
        return redirect("session:person_detail", tenant_slug=tenant_slug, person_id=person.id)
