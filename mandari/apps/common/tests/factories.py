# SPDX-License-Identifier: AGPL-3.0-or-later
"""
factory_boy-Fabriken für Tests: Nutzer, Organisationen, Rollen, Berechtigungen, Mitgliedschaften.

Verwendung:
    org = OrganizationFactory()
    member = MembershipFactory(organization=org, roles=[RoleFactory(organization=org, permissions=["motions.view"])])
"""

import factory
from django.utils.text import slugify

from apps.accounts.models import User
from apps.common.encryption import TenantEncryption
from apps.tenants.models import Membership, Organization, Permission, Role

DEFAULT_PASSWORD = "test-passwort-1234!"


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("email",)

    email = factory.Sequence(lambda n: f"person-{n}@example.org")
    password = factory.PostGenerationMethodCall("set_password", DEFAULT_PASSWORD)
    is_active = True


class OrganizationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Organization
        django_get_or_create = ("slug",)

    name = factory.Sequence(lambda n: f"Fraktion {n}")
    slug = factory.LazyAttribute(lambda o: slugify(o.name))

    @factory.post_generation
    def encryption(self, create, extracted, **kwargs):
        # Mandantenschlüssel anlegen, damit verschlüsselte Felder sofort nutzbar sind
        if create:
            _ = TenantEncryption(self).key


class PermissionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Permission
        django_get_or_create = ("codename",)

    codename = factory.Sequence(lambda n: f"test.permission_{n}")
    name = factory.LazyAttribute(lambda o: o.codename)
    category = factory.LazyAttribute(lambda o: o.codename.split(".")[0])


class RoleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Role

    organization = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Rolle {n}")
    is_admin = False

    @factory.post_generation
    def permissions(self, create, extracted, **kwargs):
        """Berechtigungen per Codename-Liste: RoleFactory(permissions=["motions.view", "motions.edit"])."""
        if not create or not extracted:
            return
        for item in extracted:
            perm = item if isinstance(item, Permission) else PermissionFactory(codename=item)
            self.permissions.add(perm)


class MembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Membership

    user = factory.SubFactory(UserFactory)
    organization = factory.SubFactory(OrganizationFactory)

    @factory.post_generation
    def roles(self, create, extracted, **kwargs):
        if create and extracted:
            self.roles.add(*extracted)
