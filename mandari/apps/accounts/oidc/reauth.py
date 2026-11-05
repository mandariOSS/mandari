# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erneute Anmeldung auf Wunsch einer Anwendung durchsetzen (``prompt=login`` und ``max_age``, Issue #1206).

Verlangt eine Anwendung eine erneute Anmeldung, leitet der Autorisierungs-Endpunkt zur Anmeldeseite weiter. Ziel
danach ist dieselbe Anfrage ohne ``prompt`` und ``max_age``. Damit dieses Ziel nicht ohne Anmeldung zum Code
führt (etwa an einem unbeaufsichtigten, noch angemeldeten Browser), merkt sich die Sitzung den Wunsch, gebunden
an Client-ID und ``state``. Erfüllt ist er erst mit der nächsten Anmeldung in dieser Sitzung (Signal
``user_logged_in``); bis dahin führt die Anfrage wieder zur Anmeldung, mit ``prompt=none`` zu ``login_required``.

Gemerkt werden höchstens :data:`MAX_PENDING` Wünsche. Kommen mehr zusammen, verdrängt keiner einen anderen:
Dann gilt bis zur nächsten Anmeldung jede Anfrage als offen.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.contrib.auth.signals import user_logged_in

SESSION_KEY = "oidc_erneute_anmeldung"
#: Höchstzahl gemerkter Wünsche je Sitzung
MAX_PENDING = 20
#: Steht für „jede Anfrage“, sobald mehr als :data:`MAX_PENDING` Wünsche offen sind
ALL = "*"


def _key(client_id: str, state: str) -> str:
    """Kurzer, eindeutiger Schlüssel; ``state`` kann lang sein und gehört nicht im Klartext in die Sitzung."""
    return hashlib.sha256(json.dumps([client_id, state]).encode()).hexdigest()[:32]


def _pending(request: Any) -> list[str]:
    session = getattr(request, "session", None)
    value = session.get(SESSION_KEY) if session is not None else None
    return [str(item) for item in value] if isinstance(value, list) else []


def require(request: Any, client_id: str, state: str) -> None:
    """Für diese Anfrage gibt es erst nach der nächsten Anmeldung in der Sitzung einen Code."""
    pending = _pending(request)
    key = _key(client_id, state)
    if ALL in pending or key in pending:
        return
    request.session[SESSION_KEY] = [ALL] if len(pending) >= MAX_PENDING else [*pending, key]


def pending(request: Any, client_id: str, state: str) -> bool:
    """Wartet diese Anfrage noch auf die erneute Anmeldung?"""
    waiting = _pending(request)
    return ALL in waiting or _key(client_id, state) in waiting


def on_user_logged_in(sender: Any, request: Any = None, user: Any = None, **kwargs: Any) -> None:
    """Signal ``user_logged_in``: Die Anmeldung erfüllt alle offenen Wünsche dieser Sitzung."""
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(SESSION_KEY, None)


def connect() -> None:
    """Signal-Empfänger verbinden (idempotent über ``dispatch_uid``)."""
    user_logged_in.connect(on_user_logged_in, dispatch_uid="oidc_reauth_logged_in")
