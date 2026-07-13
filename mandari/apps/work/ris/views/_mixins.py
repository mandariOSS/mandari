# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""


class RISBodiesMixin:
    """
    Multi-Kommune-Unterstützung für RIS-Views.

    Eine Organisation kann mit mehreren OParl-Bodies verknüpft sein
    (Organization.bodies M2M + primärer FK Organization.body).
    """

    def get_bodies(self):
        """Alle verknüpften Kommunen als QuerySet (für body__in-Filter)."""
        return self.organization.get_all_bodies()

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
        context["body"] = self.organization.get_primary_body()
        return bodies
