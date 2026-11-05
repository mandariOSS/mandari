# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Scopes je Anwendung für django-oauth-toolkit (``SCOPES_BACKEND_CLASS``, Issue #1206).

Verfügbar sind für eine Anwendung nur ihre erlaubten Scopes aus dem Katalog (:mod:`.catalog`), soweit sie zu
ihrem Ablauf passen. Ohne Anwendung (Discovery) nennt das Backend den ganzen Katalog.

Fordert ein Dienst (Client Credentials) keinen Scope an, bekommt er seine erlaubten Scopes; die hat die
Plattform-Administration für ihn freigegeben. Im Namen einer Person gibt es ohne Anforderung keinen Scope:
Die Person stimmt nur dem zu, was die Anwendung ausdrücklich verlangt.
"""

from __future__ import annotations

from typing import Any

from oauth2_provider.scopes import BaseScopes

from . import catalog


class CatalogScopes(BaseScopes):  # type: ignore[misc]  # Bibliothek ohne Typen
    def get_all_scopes(self) -> dict[str, str]:
        return dict(catalog.DESCRIPTIONS)

    def get_available_scopes(
        self, application: Any = None, request: Any = None, *args: Any, **kwargs: Any
    ) -> list[str]:
        if application is None:
            return [scope.name for scope in catalog.CATALOG]
        return list(application.scope_list)

    def get_default_scopes(self, application: Any = None, request: Any = None, *args: Any, **kwargs: Any) -> list[str]:
        if application is None or application.acts_for_person:
            return []
        return list(application.scope_list)
