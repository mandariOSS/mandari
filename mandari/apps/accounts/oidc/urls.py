# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Routen des OpenID-Connect-Anbieters (Issue #1206), eingebunden an der Wurzel (``mandari/urls.py``).

Namensraum ``oauth2_provider``: Die Discovery der Bibliothek baut die Adressen der Endpunkte über diese Namen.
Alles liegt unter ``/oauth/``, nur die Discovery steht, wie OpenID Connect es verlangt, unter
``/.well-known/openid-configuration`` des Ausstellers (``SITE_URL``). Nicht unter ``/accounts/``: Dort gilt die
Durchsetzung des zweiten Faktors nicht (``apps.accounts.middleware``).
"""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "oauth2_provider"

urlpatterns = [
    path(".well-known/openid-configuration", views.discovery, name="oidc-connect-discovery-info"),
    path("oauth/authorize/", views.authorize, name="authorize"),
    path("oauth/token/", views.token, name="token"),
    path("oauth/revoke/", views.revoke, name="revoke-token"),
    path("oauth/introspect/", views.introspect, name="introspect"),
    path("oauth/userinfo/", views.userinfo, name="user-info"),
    path("oauth/jwks/", views.jwks, name="jwks-info"),
]
