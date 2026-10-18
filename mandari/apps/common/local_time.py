# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ortszeit an den Tagen der Zeitumstellung (Issue #974).

Formulare nehmen Datum und Uhrzeit als Ortszeit entgegen (``TIME_ZONE``, Europe/Berlin). An zwei Tagen im Jahr passt
eine Uhrzeit nicht zu genau einem Zeitpunkt:

- **Doppelte Stunde** (Ende der Sommerzeit, letzter Sonntag im Oktober): 02:00 bis 02:59 Uhr gibt es zweimal, zuerst
  in Sommerzeit, eine Stunde später noch einmal in Normalzeit. Wir nehmen die erste. Wer um diese Zeit eine Sitzung
  beginnen oder enden lässt, schließt an den Abend an, und die erste Uhrzeit folgt ihm unmittelbar. Dieselbe Wahl
  treffen Python (``fold=0``), ``timezone.make_aware`` und die Zeitpläne (``apps/events/schedule.py``); ein Termin aus
  dem Formular passt so zu den übrigen Wegen. Statt abzulehnen, speichern wir und weisen auf die Wahl hin
  (:func:`ambiguous_notice`, :func:`ambiguous_notices`).
- **Fehlende Stunde** (Beginn der Sommerzeit, letzter Sonntag im März): 02:00 bis 02:59 Uhr gibt es nicht. Die Eingabe
  wird mit einer verständlichen Meldung abgelehnt (:class:`NonexistentLocalTimeError`), statt still einen anderen
  Zeitpunkt zu speichern.

Djangos ``forms.DateTimeField`` lehnt beide Fälle mit derselben allgemeinen Meldung ab; :class:`LocalDateTimeField`
tritt in Formularen an seine Stelle. Für Eingaben außerhalb von Django-Formularen gibt es :func:`aware` und
:func:`keep_stored`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class NonexistentLocalTimeError(ValueError):
    """Die Uhrzeit fällt in die übersprungene Stunde; ``str()`` ist die Meldung für die Nutzerin oder den Nutzer."""


def _zone(tz: dt.tzinfo | None) -> dt.tzinfo:
    return tz or timezone.get_current_timezone()


def _wall(value: dt.datetime) -> dt.datetime:
    """Uhrzeit ohne Zeitzone und ohne Kennzeichen der Wiederholung (``fold``)."""
    return value.replace(tzinfo=None, fold=0)


def is_nonexistent(value: dt.datetime, tz: dt.tzinfo | None = None) -> bool:
    """Fällt die Ortszeit ``value`` (ohne Zeitzone) in die Stunde, die beim Vorstellen der Uhr entfällt?"""
    zone = _zone(tz)
    local = _wall(value).replace(tzinfo=zone)
    return _wall(local.astimezone(dt.UTC).astimezone(zone)) != _wall(value)


def is_ambiguous(value: dt.datetime, tz: dt.tzinfo | None = None) -> bool:
    """Gibt es die Ortszeit ``value`` (ohne Zeitzone) an diesem Tag zweimal, weil die Uhr zurückgestellt wird?"""
    zone = _zone(tz)
    first, second = _wall(value).replace(tzinfo=zone), _wall(value).replace(tzinfo=zone, fold=1)
    return first.utcoffset() != second.utcoffset() and not is_nonexistent(value, zone)


def nonexistent_message(value: dt.datetime, tz: dt.tzinfo | None = None) -> str:
    """Meldung für eine Uhrzeit in der übersprungenen Stunde, mit der Uhrzeit, die demselben Zeitpunkt entspricht."""
    zone = _zone(tz)
    shifted = _wall(value).replace(tzinfo=zone).astimezone(dt.UTC).astimezone(zone)
    return (
        f"{value:%H:%M} Uhr gibt es am {value:%d.%m.%Y} nicht, weil die Uhr in dieser Nacht vorgestellt wird "
        f"(Zeitumstellung). Bitte eine andere Uhrzeit angeben, etwa {shifted:%H:%M} Uhr."
    )


def aware(value: dt.datetime, tz: dt.tzinfo | None = None) -> dt.datetime:
    """
    Ortszeit ohne Zeitzone als Zeitpunkt; ein Wert mit Zeitzone bleibt unverändert.

    In der doppelten Stunde gilt die erste Uhrzeit (Sommerzeit, Begründung im Modulkopf). In der fehlenden Stunde
    wirft die Funktion :class:`NonexistentLocalTimeError` mit einer Meldung für die Oberfläche.
    """
    if timezone.is_aware(value):
        return value
    zone = _zone(tz)
    if is_nonexistent(value, zone):
        raise NonexistentLocalTimeError(nonexistent_message(value, zone))
    return _wall(value).replace(tzinfo=zone)


def keep_stored(value: dt.datetime | None, stored: Any) -> dt.datetime | None:
    """
    Gespeicherten Zeitpunkt behalten, wenn die Eingabe dieselbe Ortszeit zeigt.

    Das Formular kann in der doppelten Stunde nur die erste Uhrzeit ausdrücken. Steht ein Termin (etwa aus einer
    Schnittstelle) auf der zweiten, verschöbe ihn sonst jedes Speichern des Formulars um eine Stunde, obwohl niemand
    die Uhrzeit geändert hat. Außerhalb der doppelten Stunde ist der Wert ohnehin derselbe.
    """
    if (
        isinstance(value, dt.datetime)
        and isinstance(stored, dt.datetime)
        and timezone.is_aware(value)
        and timezone.is_aware(stored)
        and value != stored
        and _wall(timezone.localtime(value)) == _wall(timezone.localtime(stored))
    ):
        return stored
    return value


def ambiguous_notice(value: dt.datetime | None, label: str = "") -> str:
    """
    Hinweis, wenn es die Ortszeit des Zeitpunkts ``value`` am Tag der Zeitumstellung zweimal gibt, sonst leer.

    Nennt, welche der beiden gespeichert ist (die erste in Sommerzeit oder, aus :func:`keep_stored`, die zweite).
    """
    if value is None or timezone.is_naive(value):
        return ""
    local = timezone.localtime(value)
    if not is_ambiguous(_wall(local), local.tzinfo):
        return ""
    which = "die erste (noch Sommerzeit)" if local.fold == 0 else "die zweite (schon Normalzeit)"
    prefix = f"{label}: " if label else ""
    return (
        f"{prefix}{local:%H:%M} Uhr gibt es am {local:%d.%m.%Y} zweimal, weil die Uhr in dieser Nacht zurückgestellt "
        f"wird (Zeitumstellung). Gespeichert ist {which}."
    )


def ambiguous_notices(form: forms.BaseForm) -> list[str]:
    """
    Hinweise für die geänderten Felder mit Datum und Uhrzeit eines gültigen Formulars, deren Uhrzeit es zweimal gibt.

    Für ``messages.warning`` nach dem Speichern; unveränderte Felder melden nichts erneut.
    """
    notices = []
    for name, field in form.fields.items():
        value = form.cleaned_data.get(name)
        if isinstance(field, forms.DateTimeField) and isinstance(value, dt.datetime) and name in form.changed_data:
            notice = ambiguous_notice(value, label=str(field.label or name))
            if notice:
                notices.append(notice)
    return notices


class LocalDateTimeField(forms.DateTimeField):
    """
    Datum mit Uhrzeit als Ortszeit, auch an den Tagen der Zeitumstellung (Issue #974).

    Wie ``forms.DateTimeField``, aber in der doppelten Stunde gilt die erste Uhrzeit (Hinweis über
    :func:`ambiguous_notices`), und für die fehlende Stunde gibt es eine verständliche Meldung. Zeigt die Eingabe die
    Ortszeit des gespeicherten Werts, bleibt dieser erhalten (:func:`keep_stored`) und das Feld gilt als unverändert.
    """

    def to_python(self, value: Any) -> dt.datetime | None:
        if value in self.empty_values:
            return None
        if isinstance(value, dt.datetime):
            result: dt.datetime | None = value
        elif isinstance(value, dt.date):
            result = dt.datetime(value.year, value.month, value.day)
        else:
            try:
                result = parse_datetime(str(value).strip())
            except ValueError:
                raise ValidationError(self.error_messages["invalid"], code="invalid") from None
            if not result:
                # Weitere Eingabeformate wie in forms.DateTimeField (DATETIME_INPUT_FORMATS, DATE_INPUT_FORMATS)
                result = super(forms.DateTimeField, self).to_python(value)
        if result is None or not settings.USE_TZ or timezone.is_aware(result):
            return result
        try:
            return aware(result)
        except NonexistentLocalTimeError as exc:
            raise ValidationError(str(exc), code="nonexistent_local_time") from exc
        except OverflowError:
            # Randdaten (Jahr 1 bzw. 9999), deren Zeitpunkt sich nicht darstellen lässt
            raise ValidationError(self.error_messages["invalid"], code="invalid") from None

    def _clean_bound_field(self, bf: forms.BoundField) -> Any:
        # Djangos Haken für Felder, die den gespeicherten Wert (bf.initial) brauchen, wie forms.FileField. django-stubs
        # kennt ihn nicht; test_local_time.py (gespeicherte zweite Uhrzeit) fällt auf, wenn Django ihn ändert
        return keep_stored(super()._clean_bound_field(bf), bf.initial)  # type: ignore[misc]

    def has_changed(self, initial: Any, data: Any) -> bool:
        if self.disabled:
            return False
        try:
            value = keep_stored(self.to_python(data), initial)
        except ValidationError:
            return True
        return (initial if initial is not None else "") != (value if value is not None else "")
