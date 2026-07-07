# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Provisioning-API für das Billing-/Kundenportal (SaaS-Betrieb).

Erlaubt einem externen, vertrauenswürdigen System (Billing-Portal) das
Anlegen und Verwalten von Organisationen. Authentifizierung über einen
statischen API-Key (Env PROVISIONING_API_KEY, Header
"Authorization: Bearer <key>"). Ohne gesetzten Key ist die API deaktiviert
(alle Requests -> 404), damit Community-Installationen keine offene
Angriffsfläche haben.
"""

import hmac
import json
import logging
import os
import re

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.tenants.models import Organization, Role, UserInvitation

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")


def _configured_key() -> str:
    return os.environ.get("PROVISIONING_API_KEY", "")


def _check_auth(request) -> bool:
    """Konstantzeit-Vergleich des Bearer-Tokens."""
    key = _configured_key()
    if not key:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], key)


class ProvisioningView(View):
    """Basisklasse: API-Key-Pflicht, JSON-Parsing, 404 wenn deaktiviert."""

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        if not _configured_key():
            # API nicht konfiguriert -> nach außen unsichtbar
            raise Http404
        if not _check_auth(request):
            return JsonResponse({"error": "unauthorized"}, status=401)
        return super().dispatch(request, *args, **kwargs)

    def json_body(self, request) -> dict:
        try:
            return json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}


def _invitation_accept_url(invitation: UserInvitation) -> str:
    """Absolute Annahme-URL (gleiches Muster wie die Work-Einladung)."""
    from django.conf import settings as django_settings
    from django.urls import reverse

    base_url = getattr(django_settings, "SITE_URL", "https://mandari.de").rstrip("/")
    return f"{base_url}{reverse('work:accept_invitation', kwargs={'token': invitation.token})}"


def _send_admin_invitation(org: Organization, invitation: UserInvitation, accept_url: str) -> bool:
    """Einladungs-Mail an den künftigen Org-Admin. False bei Versandfehler."""
    from django.core.mail import send_mail

    subject = f"Deine neue mandari work Organisation: {org.name}"
    plain = (
        f"Willkommen bei mandari work!\n\n"
        f"Deine Organisation „{org.name}“ wurde eingerichtet.\n"
        f"Über den folgenden Link richtest du dein Administrator-Konto ein:\n\n"
        f"{accept_url}\n\n"
        f"Der Link ist gültig bis {invitation.expires_at.strftime('%d.%m.%Y')}.\n"
    )
    try:
        send_mail(subject, plain, None, [invitation.email])
        return True
    except Exception as e:
        # Einladung bleibt in der DB — das Portal erhält die URL im Response
        # und kann sie anzeigen bzw. den Versand wiederholen.
        logger.warning(f"[Provisioning] Einladungs-Mail an {invitation.email} fehlgeschlagen: {e}")
        return False


def _org_payload(org: Organization) -> dict:
    return {
        "slug": org.slug,
        "name": org.name,
        "is_active": org.is_active,
        "plan": org.plan,
        "member_limit": org.member_limit,
        "billing_reference": org.billing_reference,
        "member_count": org.memberships.filter(is_active=True).count(),
        "work_url": f"/work/{org.slug}/",
        # Alle verknüpften Kommunen (primärer FK + M2M)
        "bodies": [
            {"slug": body.slug, "name": body.get_display_name()} for body in org.get_all_bodies().order_by("name")
        ],
        # Parteizugehörigkeit (primäre Parteigruppe + M2M)
        "parties": [party.name for party in org.get_all_parties().order_by("name")],
    }


class OrganizationCollectionView(ProvisioningView):
    """
    GET  /api/provisioning/organizations/ — Organisationen auflisten
    POST /api/provisioning/organizations/ — Organisation anlegen
    """

    def get(self, request):
        """Liste aller Organisationen (für Bestandskunden-Import ins Portal)."""
        orgs = []
        for org in Organization.objects.all().order_by("name"):
            payload = _org_payload(org)
            owner = org.owner
            if owner is None:
                # Fallback: ältestes aktives Admin-Mitglied
                admin_membership = (
                    org.memberships.filter(is_active=True, roles__is_admin=True)
                    .select_related("user")
                    .order_by("joined_at")
                    .first()
                )
                owner = admin_membership.user if admin_membership else None
            payload["owner_email"] = owner.email if owner else None
            payload["created_at"] = org.created_at.isoformat()
            orgs.append(payload)
        return JsonResponse({"organizations": orgs, "count": len(orgs)})

    def post(self, request):
        data = self.json_body(request)
        name = (data.get("name") or "").strip()
        slug = (data.get("slug") or "").strip().lower()
        admin_email = (data.get("admin_email") or "").strip().lower()
        plan = (data.get("plan") or "hosted").strip()
        billing_reference = (data.get("billing_reference") or "").strip()
        member_limit = data.get("member_limit")

        errors = {}
        if not name:
            errors["name"] = "erforderlich"
        if not SLUG_RE.match(slug):
            errors["slug"] = "3-40 Zeichen, nur a-z, 0-9 und Bindestrich"
        if not admin_email or "@" not in admin_email:
            errors["admin_email"] = "gültige E-Mail erforderlich"
        if member_limit is not None and (not isinstance(member_limit, int) or member_limit < 1):
            errors["member_limit"] = "positive Zahl oder null"
        if errors:
            return JsonResponse({"error": "validation", "fields": errors}, status=400)

        if Organization.objects.filter(slug=slug).exists():
            return JsonResponse({"error": "slug_taken"}, status=409)

        User = get_user_model()
        system_user = User.objects.filter(is_superuser=True, is_active=True).order_by("date_joined").first()
        if system_user is None:
            logger.error("[Provisioning] Kein Superuser als Einladungs-Absender vorhanden")
            return JsonResponse({"error": "no_system_user"}, status=500)

        with transaction.atomic():
            org = Organization.objects.create(
                name=name,
                slug=slug,
                plan=plan,
                billing_reference=billing_reference,
                member_limit=member_limit,
                is_active=True,
            )
            # Standard-Rollen entstehen automatisch per post_save-Signal
            # (tenants/signals.py) — hier nur die Admin-Rolle heraussuchen.
            admin_role = Role.objects.filter(organization=org, is_admin=True).order_by("priority").first()
            invitation = UserInvitation.create_for_organization(
                organization=org,
                email=admin_email,
                invited_by=system_user,
                roles=[admin_role] if admin_role else None,
                message="Willkommen bei mandari work! Über diese Einladung richtest du dein Konto ein.",
                valid_days=14,
            )

        accept_url = _invitation_accept_url(invitation)
        invitation_sent = _send_admin_invitation(org, invitation, accept_url)

        logger.info(f"[Provisioning] Organisation '{slug}' angelegt (Plan {plan}) für {admin_email}")
        payload = _org_payload(org)
        payload["invitation_sent"] = invitation_sent
        payload["invitation_url"] = accept_url
        return JsonResponse(payload, status=201)


class OrganizationDetailView(ProvisioningView):
    """GET/PATCH /api/provisioning/organizations/<slug>/"""

    def get_org(self, slug: str) -> Organization | None:
        return Organization.objects.filter(slug=slug).first()

    def get(self, request, slug):
        org = self.get_org(slug)
        if org is None:
            return JsonResponse({"error": "not_found"}, status=404)
        return JsonResponse(_org_payload(org))

    def patch(self, request, slug):
        org = self.get_org(slug)
        if org is None:
            return JsonResponse({"error": "not_found"}, status=404)

        data = self.json_body(request)
        fields = []
        if "plan" in data and isinstance(data["plan"], str) and data["plan"].strip():
            org.plan = data["plan"].strip()
            fields.append("plan")
        if "is_active" in data and isinstance(data["is_active"], bool):
            org.is_active = data["is_active"]
            fields.append("is_active")
        if "member_limit" in data and (data["member_limit"] is None or isinstance(data["member_limit"], int)):
            org.member_limit = data["member_limit"]
            fields.append("member_limit")
        if "billing_reference" in data and isinstance(data["billing_reference"], str):
            org.billing_reference = data["billing_reference"].strip()
            fields.append("billing_reference")

        # Parteien: Liste von Namen — get_or_create je Name
        parties_updated = False
        if "parties" in data and isinstance(data["parties"], list) and all(isinstance(p, str) for p in data["parties"]):
            from apps.tenants.models import PartyGroup

            names = [p.strip() for p in data["parties"] if p.strip()]
            parties = []
            for name in names:
                party = PartyGroup.objects.filter(name__iexact=name).first()
                if party is None:
                    party = PartyGroup.objects.create(name=name)
                if party not in parties:
                    parties.append(party)
            # Primäre Parteigruppe (FK) bleibt immer verknüpft
            if org.party_group and org.party_group not in parties:
                parties.append(org.party_group)
            org.parties.set(parties)
            parties_updated = True

        if not fields and not parties_updated:
            return JsonResponse({"error": "no_valid_fields"}, status=400)

        if fields:
            org.save(update_fields=fields + ["updated_at"])
        logger.info(
            f"[Provisioning] Organisation '{slug}' aktualisiert: {fields + (['parties'] if parties_updated else [])}"
        )
        return JsonResponse(_org_payload(org))
