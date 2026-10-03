# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollenzuweisungen: zuweisen, aufheben, Spiegel von ``SessionUser.roles`` (Issue #772).

Im Übergang bleibt ``SessionUser.roles`` maßgeblich für mandantenweite, unbefristete Rollen. Zu jedem Paar aus
Konto und Rolle gibt es genau eine aktive **Spiegelzuweisung**:

- jede Änderung von ``SessionUser.roles`` (Oberfläche, Einladung, Admin, Skripte) führt den Spiegel über das Signal
  ``m2m_changed`` nach (:func:`rollen_geaendert`);
- jeder ``migrate``-Lauf gleicht ab (:func:`abgleichen`) – für Änderungen eines älteren Images, das die Tabelle nicht
  kennt, und für Massenänderungen ohne Signal.

Die Spiegelung schreibt keine eigenen Einträge ins Prüfprotokoll; die Rollenänderung protokolliert wie bisher die
Oberfläche. Zuweisungen sind bis auf die Aufhebung unveränderlich (:func:`aufheben`); aufgehobene bleiben als
Nachweis.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date
from typing import Any

from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .bereiche import AMT, GREMIUM, KOERPERSCHAFT, bereich, bereichsbaum

logger = logging.getLogger(__name__)

#: Vermerk der Spiegelzuweisungen, die der Abgleich nach ``migrate`` anlegt
VERMERK_ABGLEICH = "Abgleich mit den Rollen des Kontos"


def spiegel_q(prefix: str = "") -> Q:
    """Bedingung „aktive Spiegelzuweisung“: mandantenweit, unbefristet, nicht aufgehoben."""
    return Q(
        **{
            f"{prefix}revoked_at__isnull": True,
            f"{prefix}scope_type": "mandant",
            f"{prefix}valid_from__isnull": True,
            f"{prefix}valid_until__isnull": True,
        }
    )


def _spiegel_anlegen(
    modell: Any, konto_modell: Any, paare: Iterable[tuple[Any, Any]], *, quelle: str, vermerk: str = ""
) -> int:
    """Fehlende Spiegelzuweisungen für (Konto, Rolle) anlegen; vorhandene bleiben unverändert."""
    paare = set(paare)
    if not paare:
        return 0
    konten = {k for k, _ in paare}
    vorhanden = set(modell.objects.filter(spiegel_q(), user_id__in=konten).values_list("user_id", "role_id"))
    fehlend = paare - vorhanden
    if not fehlend:
        return 0
    mandanten = dict(konto_modell.objects.filter(pk__in={k for k, _ in fehlend}).values_list("pk", "tenant_id"))
    modell.objects.bulk_create(
        [
            modell(tenant_id=mandanten[konto], user_id=konto, role_id=rolle, source=quelle, note=vermerk)
            for konto, rolle in fehlend
            if konto in mandanten
        ],
        ignore_conflicts=True,
    )
    return len(fehlend)


def _spiegel_aufheben(modell: Any, paare: Iterable[tuple[Any, Any]]) -> int:
    """Aktive Spiegelzuweisungen für (Konto, Rolle) aufheben; sie bleiben als Nachweis (wenige Paare je Änderung)."""
    paare = set(paare)
    if not paare:
        return 0
    bedingung = Q()
    for konto, rolle in paare:
        bedingung |= Q(user_id=konto, role_id=rolle)
    return int(modell.objects.filter(spiegel_q()).filter(bedingung).update(revoked_at=timezone.now()))


def rollen_geaendert(sender: Any, instance: Any, action: str, reverse: bool, pk_set: Any, **kwargs: Any) -> None:
    """Signal ``m2m_changed`` von ``SessionUser.roles``: Spiegelzuweisungen nachführen."""
    from apps.session.models import SessionRoleAssignment, SessionUser

    if action == "pre_clear":
        # Vor dem Leeren merken, welche Paare wegfallen (post_clear kennt sie nicht mehr)
        if reverse:
            instance._rechte_geleert = [(k, instance.pk) for k in instance.users.values_list("pk", flat=True)]
        else:
            instance._rechte_geleert = [(instance.pk, r) for r in instance.roles.values_list("pk", flat=True)]
        return
    if action == "post_clear":
        _spiegel_aufheben(SessionRoleAssignment, getattr(instance, "_rechte_geleert", []))
        instance._rechte_geleert = []
        return
    if action not in ("post_add", "post_remove") or not pk_set:
        return
    # reverse: ``rolle.users.add(…)`` – instance ist die Rolle, pk_set die Konten
    paare = [(konto, instance.pk) for konto in pk_set] if reverse else [(instance.pk, rolle) for rolle in pk_set]
    if action == "post_add":
        _spiegel_anlegen(SessionRoleAssignment, SessionUser, paare, quelle=SessionRoleAssignment.SOURCE_MANUAL)
    else:
        _spiegel_aufheben(SessionRoleAssignment, paare)


def abgleichen(registry: Any = None) -> dict[str, int]:
    """
    Spiegel mit ``SessionUser.roles`` abgleichen: fehlende Spiegelzuweisungen anlegen, verwaiste aufheben.

    Idempotent. ``registry`` ist die App-Registry des Migrationsstands (``post_migrate``) oder ``None``.

    Beide Richtungen vergleichen Rollen und Spiegel in **einer** Abfrage (``NOT EXISTS``), nie zwei getrennt gelesene
    Stände: Eine Rollenänderung zwischen zwei Lesezugriffen ließe sonst ihren frischen Spiegel als verwaist aufheben
    bzw. einen Spiegel für eine eben entzogene Rolle anlegen. In PostgreSQL warten Rollenänderungen zudem, bis der
    Abgleich fertig ist (Sperre ``SHARE`` auf der Verknüpfungstabelle, nur Schreiben wartet): Signal und Abgleich
    sehen damit stets denselben Stand.
    """
    from django.apps import apps as global_apps

    registry = registry or global_apps
    try:
        modell = registry.get_model("session", "SessionRoleAssignment")
        konto_modell = registry.get_model("session", "SessionUser")
    except LookupError:
        return {}
    through = konto_modell.roles.through
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(f"LOCK TABLE {connection.ops.quote_name(through._meta.db_table)} IN SHARE MODE")
        aktiv = modell.objects.filter(spiegel_q())
        fehlend = list(
            through.objects.filter(
                ~Exists(aktiv.filter(user_id=OuterRef("sessionuser_id"), role_id=OuterRef("sessionrole_id")))
            ).values_list("sessionuser_id", "sessionrole_id", "sessionuser__tenant_id")
        )
        if fehlend:
            modell.objects.bulk_create(
                [
                    modell(tenant_id=mandant, user_id=konto, role_id=rolle, source="migration", note=VERMERK_ABGLEICH)
                    for konto, rolle, mandant in fehlend
                ],
                ignore_conflicts=True,
            )
        aufgehoben = aktiv.filter(
            ~Exists(through.objects.filter(sessionuser_id=OuterRef("user_id"), sessionrole_id=OuterRef("role_id")))
        ).update(revoked_at=timezone.now())
    return {"angelegt": len(fehlend), "aufgehoben": int(aufgehoben)}


def post_migrate_abgleichen(sender: Any, apps: Any = None, **kwargs: Any) -> None:
    """Nach jedem ``migrate``: Änderungen eines älteren Images an ``SessionUser.roles`` in den Spiegel übernehmen."""
    try:
        zahlen = abgleichen(apps)
    except Exception:
        # Ein Fehler hier darf den Deploy nicht abbrechen; der nächste Lauf versucht es erneut
        logger.exception("Rollenzuweisungen: Abgleich nach migrate fehlgeschlagen.")
        return
    if any(zahlen.values()):
        logger.info(
            "Rollenzuweisungen abgeglichen: %s angelegt, %s aufgehoben.", zahlen["angelegt"], zahlen["aufgehoben"]
        )


def _bereich_pruefen(session_user: Any, art: str, kennung: Any) -> None:
    from apps.session.models import SessionRoleAssignment

    if art == SessionRoleAssignment.SCOPE_TENANT:
        if kennung is not None:
            raise ValueError("Für den ganzen Mandanten gibt es keine Kennung des Geltungsbereichs.")
        return
    if art not in (KOERPERSCHAFT, GREMIUM, AMT) or kennung is None:
        raise ValueError("Unbekannter Geltungsbereich.")
    if not bereichsbaum(session_user.tenant).kennt(bereich(art, kennung)):
        raise ValueError("Der Geltungsbereich gehört nicht zu diesem Mandanten.")


@transaction.atomic
def zuweisen(
    session_user: Any,
    rolle: Any,
    *,
    bereich_art: str = "mandant",
    bereich_kennung: Any = None,
    gueltig_von: date | None = None,
    gueltig_bis: date | None = None,
    quelle: str = "manuell",
    vermerk: str = "",
    von: Any = None,
) -> Any:
    """
    Eine Rolle zuweisen. Mandantenweit und unbefristet heißt: Eintrag in ``SessionUser.roles`` mit Spiegel.

    Administrator-Rollen nur mandantenweit und unbefristet; Rolle, Konto und Geltungsbereich aus demselben Mandanten.
    Rechteprüfung (nur innerhalb der eigenen Rechte) und Prüfprotokoll liegen beim Aufrufer.
    """
    from apps.session.models import SessionRoleAssignment

    if rolle.tenant_id != session_user.tenant_id:
        raise ValueError("Die Rolle gehört nicht zu diesem Mandanten.")
    if gueltig_von and gueltig_bis and gueltig_bis < gueltig_von:
        raise ValueError("Das Ende liegt vor dem Beginn.")
    _bereich_pruefen(session_user, bereich_art, bereich_kennung)
    spiegel = bereich_art == SessionRoleAssignment.SCOPE_TENANT and gueltig_von is None and gueltig_bis is None
    if rolle.is_admin and not spiegel:
        raise ValueError("Administrator-Rollen werden nur mandantenweit und unbefristet zugewiesen.")
    if spiegel:
        vorhanden = SessionRoleAssignment.objects.filter(spiegel_q(), user=session_user, role=rolle).first()
        if vorhanden is not None:
            session_user.roles.add(rolle)
            return vorhanden
    zuweisung = SessionRoleAssignment.objects.create(
        tenant_id=session_user.tenant_id,
        user=session_user,
        role=rolle,
        scope_type=bereich_art,
        scope_id=bereich_kennung,
        valid_from=gueltig_von,
        valid_until=gueltig_bis,
        source=quelle,
        note=vermerk,
        created_by=von,
    )
    if spiegel:
        session_user.roles.add(rolle)
    return zuweisung


@transaction.atomic
def aufheben(zuweisung: Any, *, von: Any = None) -> bool:
    """
    Eine Zuweisung aufheben (bleibt als Nachweis); beim Spiegel auch die Rolle aus ``SessionUser.roles``.

    Bedingt in einer Abfrage (nur solange nicht aufgehoben): Heben zwei Aufrufe gleichzeitig auf, gilt der erste;
    Zeitpunkt und Person der Aufhebung überschreibt keiner. ``True``, wenn dieser Aufruf aufgehoben hat.
    """
    from apps.session.models import SessionRoleAssignment

    jetzt = timezone.now()
    getroffen = SessionRoleAssignment.objects.filter(pk=zuweisung.pk, revoked_at__isnull=True).update(
        revoked_at=jetzt, revoked_by=von
    )
    if not getroffen:
        zuweisung.refresh_from_db(fields=["revoked_at", "revoked_by"])
        return False
    zuweisung.revoked_at, zuweisung.revoked_by = jetzt, von
    if zuweisung.is_mirror:
        zuweisung.user.roles.remove(zuweisung.role)
    return True
