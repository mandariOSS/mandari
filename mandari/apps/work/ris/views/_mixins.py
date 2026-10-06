# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from apps.work.rahmen import neues_design

from .. import selectors


class RISBodiesMixin:
    """
    Multi-Kommune-Unterstützung für RIS-Views.

    Eine Organisation kann mit mehreren OParl-Bodies verknüpft sein
    (Organization.bodies M2M + primärer FK Organization.body).

    Im neuen Erscheinungsbild (Schalter je Organisation, Issues #852/#853) nimmt eine View mit ``neue_vorlage``
    diese Vorlage aus ``work/ris/neu/``; ``neu`` sagt der View, ob sie den Kontext dafür ergänzen soll.
    """

    #: Vorlage im neuen Erscheinungsbild; leer = dieselbe Vorlage wie bisher
    neue_vorlage = ""

    @property
    def neu(self):
        return bool(self.neue_vorlage) and neues_design(self.organization)

    def get_template_names(self):
        if self.neu:
            return [self.neue_vorlage]
        return super().get_template_names()

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
