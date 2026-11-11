# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prüfung von Adressen aus der Konfiguration (ohne Django-Modelle, von ``settings.py`` aus nutzbar)."""

from __future__ import annotations

from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def basis_adresse(wert: str, name: str) -> str:
    """
    Vollständige Adresse eines Verzeichnisses, an die ein Pfad angehängt werden darf: ``https://host/pfad/``.

    Leer bleibt leer (Einstellung nicht gesetzt). Sonst muss die Adresse mit ``https://`` oder ``http://`` beginnen und
    einen Host nennen; Abfrage (``?…``) und Sprungmarke (``#…``) sind nicht erlaubt, weil ein angehängter Pfad sonst in
    der Abfrage landete. Zugangsdaten (``nutzer:passwort@``) sind nicht erlaubt, weil die Adresse in Weiterleitungen und
    Links öffentlich wird. Ein fehlender Schrägstrich am Ende wird ergänzt.
    """
    adresse = (wert or "").strip()
    if not adresse:
        return ""
    if any(zeichen.isspace() or not zeichen.isprintable() for zeichen in adresse):
        raise ImproperlyConfigured(f"{name} darf keine Leer- oder Steuerzeichen enthalten")
    try:
        teile = urlsplit(adresse)
        host = teile.hostname
        _ = teile.port  # ein Port, der keine Zahl ist, fällt erst hier auf
    except ValueError as fehler:
        raise ImproperlyConfigured(f"{name} ist keine gültige Adresse") from fehler
    if teile.scheme not in ("https", "http") or not host:
        raise ImproperlyConfigured(f"{name} muss eine vollständige Adresse sein (https://host/pfad/)")
    if teile.username is not None or teile.password is not None:
        # Die Adresse steht in Weiterleitungen und Links: Zugangsdaten darin wären öffentlich
        raise ImproperlyConfigured(f"{name} darf keine Zugangsdaten enthalten (nutzer:passwort@)")
    if teile.query or teile.fragment or "?" in adresse or "#" in adresse:
        raise ImproperlyConfigured(f"{name} darf weder Abfrage (?) noch Sprungmarke (#) enthalten")
    return adresse if adresse.endswith("/") else adresse + "/"
