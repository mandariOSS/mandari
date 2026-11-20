# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Forms for the Tasks module.
"""

import json
from typing import Any

from django import forms

from apps.common.uploads import DOCUMENTS, MB, validate_upload

from .models import Task, TaskAttachment, TaskChecklistItem, TaskLabel

#: Verstecktes Feld neben der Auswahl der weiteren Zuständigen: Nur wenn es mitkommt, gilt die Auswahl. Eine leere
#: Auswahl sendet der Browser gar nicht; ohne die Marke wäre sie von „Feld gar nicht im Formular“ nicht zu
#: unterscheiden, und ein vor dem Update geöffnetes Panel löschte beim Speichern alle weiteren Zuständigen.
WEITERE_GESENDET = "weitere_zustaendige_gesendet"
#: Auswahl der weiteren Zuständigen, die das Panel zuletzt kannte (Kennungen, durch Komma getrennt). Das Panel sendet
#: bei jeder Änderung alle Felder. Mit diesem Stand wendet der Server nur den Unterschied an: Ein Panel, das vor einer
#: fremden Änderung geöffnet wurde und etwa nur den Termin speichert, nimmt niemanden heraus, den es noch nicht kannte.
WEITERE_STAND = "weitere_zustaendige_stand"


class WeitereZustaendigeMixin:
    """Auswahl der weiteren Zuständigen (Issue #1462) für das Formular „Neue Aufgabe“ und das Panel."""

    def weitere_einrichten(self: Any, mitglieder: Any) -> None:
        feld = forms.ModelMultipleChoiceField(
            queryset=mitglieder.filter(is_guest=False),
            required=False,
            label="Weitere Zuständige",
            widget=forms.CheckboxSelectMultiple(
                attrs={
                    "class": "w-3.5 h-3.5 shrink-0 rounded-sm border-gray-300 dark:border-gray-600 text-primary-600 "
                    "focus:ring-primary-500 dark:bg-gray-700"
                }
            ),
        )
        feld.label_from_instance = lambda obj: obj.user.get_display_name()
        self.fields["weitere_zustaendige"] = feld

    @property
    def weitere_gesendet(self: Any) -> bool:
        return WEITERE_GESENDET in self.data

    def weitere_auswahl(self: Any) -> list[Any] | None:
        """Die gewählten weiteren Zuständigen, oder ``None``, wenn das Formular die Auswahl nicht enthielt."""
        if not self.weitere_gesendet:
            return None
        return list(self.cleaned_data.get("weitere_zustaendige") or [])

    def weitere_stand(self: Any) -> set[str] | None:
        """Kennungen der Auswahl, die das Formular zuletzt kannte; ``None`` ohne Angabe (Formular „Neue Aufgabe“)."""
        if WEITERE_STAND not in self.data:
            return None
        return {teil.strip() for teil in str(self.data.get(WEITERE_STAND) or "").split(",") if teil.strip()}

    @property
    def weitere_stand_wert(self: Any) -> str:
        """
        Wert des versteckten Felds: die Auswahl, die das gezeichnete Formular zeigt. Ein abgelehntes Formular zeigt die
        gesendete Auswahl und behält deshalb den gesendeten Stand, sonst gilt die Auswahl der Aufgabe.
        """
        if self.is_bound:
            return ",".join(sorted(self.weitere_stand() or ()))
        return ",".join(str(pk) for pk in self.initial.get("weitere_zustaendige") or [])


class TaskForm(WeitereZustaendigeMixin, forms.ModelForm):
    """Form for creating and editing tasks."""

    class Meta:
        model = Task
        fields = [
            "title",
            "description",
            "visibility",
            "priority",
            "status",
            "due_date",
            "assigned_to",
            "tags",
        ]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                    "placeholder": "Aufgabentitel",
                    "maxlength": "200",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                    "rows": 4,
                    "placeholder": "Optionale Beschreibung...",
                    "maxlength": "2000",
                }
            ),
            "priority": forms.Select(
                attrs={
                    "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                }
            ),
            "status": forms.Select(
                attrs={
                    "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                }
            ),
            "due_date": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                }
            ),
            "assigned_to": forms.Select(
                attrs={
                    "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                }
            ),
            "visibility": forms.RadioSelect(
                attrs={
                    "class": "visibility-radio",
                }
            ),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields["assigned_to"].queryset = organization.memberships.filter(is_active=True).select_related("user")
            self.fields["assigned_to"].label_from_instance = lambda obj: obj.user.get_display_name()
            self.weitere_einrichten(self.fields["assigned_to"].queryset)
        self.fields["assigned_to"].required = False
        self.fields["due_date"].required = False

    @property
    def tags_list(self) -> list[str]:
        """Tags als Liste für ``json_script`` – nach einem Fehler steht der gepostete JSON-Text im Feld."""
        wert = self["tags"].value()
        if isinstance(wert, str):
            try:
                wert = json.loads(wert or "[]")
            except ValueError:
                wert = []
        return [str(t) for t in wert or [] if str(t).strip()]


class TaskPanelForm(WeitereZustaendigeMixin, forms.ModelForm):
    """Form for inline editing in the slide-over panel. Content-first styling."""

    class Meta:
        model = Task
        fields = [
            "title",
            "description",
            "status",
            "priority",
            "due_date",
            "assigned_to",
            "visibility",
        ]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "w-full text-lg font-semibold bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-hidden ring-0 focus:ring-0 text-gray-900 dark:text-white placeholder-gray-400",
                    "placeholder": "Aufgabentitel",
                    "maxlength": "200",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "w-full text-sm bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-hidden ring-0 focus:ring-0 text-gray-600 dark:text-gray-300 placeholder-gray-400 dark:placeholder-gray-500 resize-none leading-relaxed",
                    "rows": 2,
                    "placeholder": "Beschreibung hinzufügen...",
                    "maxlength": "2000",
                }
            ),
            "status": forms.Select(
                attrs={
                    "class": "text-[11px] font-semibold bg-transparent border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 rounded-md px-2 py-1 outline-hidden cursor-pointer appearance-none",
                }
            ),
            "priority": forms.Select(
                attrs={
                    "class": "text-[11px] font-semibold bg-transparent border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 rounded-md px-2 py-1 outline-hidden cursor-pointer appearance-none",
                }
            ),
            "due_date": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": "text-xs bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-hidden ring-0 focus:ring-0 text-gray-900 dark:text-white cursor-pointer",
                }
            ),
            "assigned_to": forms.Select(
                attrs={
                    "class": "w-full text-xs bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-hidden ring-0 focus:ring-0 cursor-pointer text-gray-900 dark:text-white",
                }
            ),
            "visibility": forms.RadioSelect(
                attrs={
                    "class": "visibility-radio",
                }
            ),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            mitglieder = organization.memberships.filter(is_active=True).select_related("user")
            if self.instance.pk and self.instance.noe_top:
                # Aufgabe aus einem nichtöffentlichen TOP: zuständig nur, wer sie auch öffnen darf. Sonst ginge der
                # Titel mit der Benachrichtigung an eine Person, für die die Aufgabe verschlossen bleibt.
                from apps.work.faction.services import ZUSTAENDIG_NUR_VEREIDIGTE

                from . import selectors

                mitglieder = mitglieder.filter(
                    pk__in=selectors.zustaendige_fuer_noe_aufgabe(organization, self.instance)
                )
                # Ein vor der Änderung geöffnetes Panel bietet noch alle an: Die Meldung nennt den Grund
                self.fields["assigned_to"].error_messages["invalid_choice"] = ZUSTAENDIG_NUR_VEREIDIGTE
            self.fields["assigned_to"].queryset = mitglieder
            self.fields["assigned_to"].label_from_instance = lambda obj: obj.user.get_display_name()
            # Dieselbe Grenze für die weiteren Zuständigen (bei nichtöffentlichen TOPs nur Vereidigte)
            self.weitere_einrichten(mitglieder)
            if self.instance.pk and not self.is_bound:
                self.initial["weitere_zustaendige"] = [
                    m.pk for m in self.instance.weitere_zustaendige.all() if m.pk != self.instance.assigned_to_id
                ]
        self.fields["assigned_to"].required = False
        self.fields["due_date"].required = False


class QuickTaskForm(forms.Form):
    """Quick task creation form for Kanban board."""

    title = forms.CharField(
        max_length=200,
        widget=forms.TextInput(
            attrs={
                "class": "flex-1 px-3 py-2 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                "placeholder": "Neue Aufgabe...",
                "maxlength": "200",
            }
        ),
    )
    status = forms.ChoiceField(choices=Task.STATUS_CHOICES, widget=forms.HiddenInput())
    priority = forms.ChoiceField(
        choices=Task.PRIORITY_CHOICES, initial="medium", required=False, widget=forms.HiddenInput()
    )


class TaskAttachmentForm(forms.ModelForm):
    """Form for uploading attachments to a task."""

    class Meta:
        model = TaskAttachment
        fields = ["file"]

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f:
            validate_upload(f, allowed=DOCUMENTS, max_bytes=20 * MB, bezeichnung="Anlage")
        return f


class TaskChecklistItemForm(forms.ModelForm):
    """Form for adding a checklist item."""

    class Meta:
        model = TaskChecklistItem
        fields = ["title"]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "flex-1 min-w-0 px-2 py-1 text-sm bg-transparent border-0 border-b border-gray-200 dark:border-gray-700 focus:border-primary-400 rounded-none outline-hidden ring-0 focus:ring-0 text-gray-900 dark:text-white placeholder-gray-400",
                    "placeholder": "Neuen Punkt hinzufügen...",
                    "maxlength": "300",
                }
            ),
        }


class TaskLabelForm(forms.ModelForm):
    """Form for creating/editing labels."""

    class Meta:
        model = TaskLabel
        fields = ["name", "color"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full px-3 py-1.5 text-sm bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 outline-hidden",
                    "placeholder": "Label-Name",
                    "maxlength": "50",
                }
            ),
            "color": forms.Select(
                attrs={
                    "class": "px-3 py-1.5 text-sm bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 outline-hidden",
                }
            ),
        }
