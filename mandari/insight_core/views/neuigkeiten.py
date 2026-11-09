# SPDX-License-Identifier: AGPL-3.0-or-later
"""„Was ist neu?“: kuratierte Neuigkeiten des Bürgerportals, verlinkt von der Versionsnummer im Fuß (Issue #1261)."""

from __future__ import annotations

from typing import Any

from django.views.generic import TemplateView

from ..seo import get_page_seo
from ..services import neuigkeiten


class NeuigkeitenView(TemplateView):
    template_name = "pages/neuigkeiten.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["gruppen"] = neuigkeiten.gruppen()
        context["seo"] = get_page_seo(
            self.request,
            title="Was ist neu?",
            description="Neue Funktionen im Bürgerportal von mandari, kurz erklärt: das Neueste zuerst.",
            keywords=["Neuigkeiten", "Bürgerportal", "Ratsinformationen"],
        ).to_dict()
        return context
