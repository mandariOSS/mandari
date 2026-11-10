# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from .. import selectors


def erlaubter_reiter(wert: str | None, erlaubt: tuple[str, ...]) -> str:
    """
    Reiter aus der Adresse (``?tab=``) nur, wenn die Seite ihn kennt, sonst der erste der Seite.

    Der Wert steht in der Vorlage in einem Alpine-Ausdruck; ein beliebiger Text aus der Adresse würde dort als
    Skript ausgewertet.
    """
    return wert if wert in erlaubt else erlaubt[0]


class RISBodiesMixin:
    """
    Multi-Kommune-Unterstützung für RIS-Views.

    Eine Organisation kann mit mehreren OParl-Bodies verknüpft sein
    (Organization.bodies M2M + primärer FK Organization.body).
    """

    def get_bodies(self):
        """Alle verknüpften Kommunen als QuerySet (für body__in-Filter)."""
        return selectors.bodies_for_organization(self.organization)

    def setup_body_context(self, context):
        """
        Setzt bodies/body/no_body_linked in den Context.

        Returns:
            QuerySet der Bodies oder None, wenn keine Kommune verknüpft ist.
        """
        bodies = self.get_bodies()
        if not bodies.exists():
            context["no_body_linked"] = True
            return None
        context["bodies"] = bodies
        context["has_multiple_bodies"] = bodies.count() > 1
        # Primäre Kommune für Anzeige (Subtitle, Karte etc.)
        context["body"] = selectors.primary_body(self.organization)
        return bodies
