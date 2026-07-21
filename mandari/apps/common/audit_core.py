# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsamer Audit-Baustein (Issue #66).

Generalisiert die Session-Audit-Infrastruktur (apps/session/audit.py,
Issue #23) zu wiederverwendbaren Grundfunktionen, die von mehreren
Portalen genutzt werden:

- Thread-Local-Request für die Nutzer-Attribution (Middleware setzt/räumt)
- Feld-Diff zwischen altem und neuem Zustand (verschlüsselte Felder maskiert)
- Kaskadenlösch-Schutz: Während der Kaskadenlöschung eines Wurzelobjekts
  (SessionTenant, Organization) dürfen die post_delete-Receiver keine
  neuen Log-Zeilen für das verschwindende Wurzelobjekt anlegen — sonst
  bleiben hängende Fremdschlüssel zurück und das abschließende DELETE
  schlägt mit IntegrityError fehl (Muster aus Session-Issue #56).

Sicherheit:
- Verschlüsselte Felder (BinaryField/EncryptedTextField) werden niemals
  im Klartext protokolliert — nur die Tatsache, DASS sie geändert wurden.
"""

import threading

from django.db import models

_thread_state = threading.local()

# Felder, die nie in den Änderungs-Diff aufgenommen werden
DEFAULT_SKIP_FIELDS = {"id", "created_at", "updated_at", "last_access", "joined_at", "submitted_at"}

# Maximale Länge protokollierter Werte
MAX_VALUE_LENGTH = 300

# Platzhalter für verschlüsselte/binäre Felder
MASKED = "[verschlüsselt geändert]"


# =============================================================================
# Thread-Local-Request (Attribution)
# =============================================================================


def set_current_request(request):
    """Aktuellen Request für die Audit-Attribution merken (Middleware)."""
    _thread_state.request = request


def clear_current_request():
    """Thread-Local-Request wieder entfernen (Middleware, Response/Exception)."""
    _thread_state.request = None


def get_current_request():
    """Aktuellen Request abrufen (oder None außerhalb eines Requests)."""
    return getattr(_thread_state, "request", None)


def get_client_meta(request):
    """IP-Adresse und User-Agent aus dem Request extrahieren."""
    ip_address = None
    user_agent = ""
    if request is not None:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(",")[0].strip()
        else:
            ip_address = request.META.get("REMOTE_ADDR") or None
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
    return ip_address, user_agent


# =============================================================================
# Kaskadenlösch-Schutz (Muster aus Session-Issue #56)
# =============================================================================


def _deleting_roots():
    roots = getattr(_thread_state, "deleting_root_pks", None)
    if roots is None:
        roots = {}
        _thread_state.deleting_root_pks = roots
    return roots


def mark_root_deleting(scope, pk):
    """Wurzelobjekt-PK als 'wird gerade kaskadengelöscht' markieren (pre_delete)."""
    _deleting_roots().setdefault(scope, set()).add(pk)


def unmark_root_deleting(scope, pk):
    """Markierung nach Abschluss der Kaskadenlöschung entfernen (post_delete)."""
    roots = getattr(_thread_state, "deleting_root_pks", None)
    if roots is not None:
        roots.get(scope, set()).discard(pk)


def is_root_deleting(scope, pk) -> bool:
    """Läuft für dieses Wurzelobjekt gerade eine Kaskadenlöschung?"""
    roots = getattr(_thread_state, "deleting_root_pks", None)
    if not roots:
        return False
    return pk in roots.get(scope, set())


# =============================================================================
# Feld-Diff (verschlüsselte Werte maskiert)
# =============================================================================


def serialize_value(field, value):
    """Feldwert für das Änderungsprotokoll aufbereiten (nie Klartext-Sensitives)."""
    if isinstance(field, models.BinaryField):
        # Deckt EncryptedTextField ab: niemals Inhalte protokollieren
        return MASKED if value else ""
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if len(text) > MAX_VALUE_LENGTH:
        text = text[:MAX_VALUE_LENGTH] + "…"
    return text


def build_changes(old_instance, new_instance, skip_fields=None) -> dict:
    """Feld-Diff zwischen altem und neuem Zustand (verschlüsselte Werte maskiert)."""
    if skip_fields is None:
        skip_fields = DEFAULT_SKIP_FIELDS
    changes = {}
    for field in new_instance._meta.concrete_fields:
        if field.name in skip_fields:
            continue
        old_value = getattr(old_instance, field.attname, None)
        new_value = getattr(new_instance, field.attname, None)
        if old_value == new_value:
            continue
        if isinstance(field, models.BinaryField):
            # Nur die Tatsache der Änderung festhalten
            changes[field.name] = {"alt": MASKED, "neu": MASKED}
        else:
            changes[field.name] = {
                "alt": serialize_value(field, old_value),
                "neu": serialize_value(field, new_value),
            }
    return changes


def capture_old_state(sender, instance):
    """Alten Zustand für den Diff laden (pre_save-Baustein)."""
    if instance.pk:
        try:
            instance._audit_old = sender.objects.get(pk=instance.pk)
        except sender.DoesNotExist:
            instance._audit_old = None
    else:
        instance._audit_old = None
