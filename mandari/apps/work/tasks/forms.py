# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Forms for the Tasks module.
"""

from django import forms
from django.core.exceptions import ValidationError

from .models import Task, TaskAttachment, TaskChecklistItem, TaskLabel


class TaskForm(forms.ModelForm):
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
        self.fields["assigned_to"].required = False
        self.fields["due_date"].required = False


class TaskPanelForm(forms.ModelForm):
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
                    "class": "w-full text-lg font-semibold bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-none ring-0 focus:ring-0 text-gray-900 dark:text-white placeholder-gray-400",
                    "placeholder": "Aufgabentitel",
                    "maxlength": "200",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "w-full text-sm bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-none ring-0 focus:ring-0 text-gray-600 dark:text-gray-300 placeholder-gray-400 dark:placeholder-gray-500 resize-none leading-relaxed",
                    "rows": 2,
                    "placeholder": "Beschreibung hinzufügen...",
                    "maxlength": "2000",
                }
            ),
            "status": forms.Select(
                attrs={
                    "class": "text-[11px] font-semibold bg-transparent border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 rounded-md px-2 py-1 outline-none cursor-pointer appearance-none",
                }
            ),
            "priority": forms.Select(
                attrs={
                    "class": "text-[11px] font-semibold bg-transparent border border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 rounded-md px-2 py-1 outline-none cursor-pointer appearance-none",
                }
            ),
            "due_date": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": "text-xs bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-none ring-0 focus:ring-0 text-gray-900 dark:text-white cursor-pointer",
                }
            ),
            "assigned_to": forms.Select(
                attrs={
                    "class": "w-full text-xs bg-transparent border-0 border-b border-transparent hover:border-gray-200 dark:hover:border-gray-700 focus:border-primary-400 rounded-none px-0 py-1 outline-none ring-0 focus:ring-0 cursor-pointer text-gray-900 dark:text-white",
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


class TaskStatusForm(forms.Form):
    """Form for updating task status via drag & drop."""

    task_id = forms.UUIDField()
    status = forms.ChoiceField(choices=Task.STATUS_CHOICES)
    position = forms.IntegerField(min_value=0)


class TaskAttachmentForm(forms.ModelForm):
    """Form for uploading attachments to a task."""

    class Meta:
        model = TaskAttachment
        fields = ["file"]

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f:
            max_size = 20 * 1024 * 1024  # 20 MB
            if f.size > max_size:
                raise ValidationError("Datei darf maximal 20 MB groß sein.")
        return f


class TaskChecklistItemForm(forms.ModelForm):
    """Form for adding a checklist item."""

    class Meta:
        model = TaskChecklistItem
        fields = ["title"]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "flex-1 min-w-0 px-2 py-1 text-sm bg-transparent border-0 border-b border-gray-200 dark:border-gray-700 focus:border-primary-400 rounded-none outline-none ring-0 focus:ring-0 text-gray-900 dark:text-white placeholder-gray-400",
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
                    "class": "w-full px-3 py-1.5 text-sm bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 outline-none",
                    "placeholder": "Label-Name",
                    "maxlength": "50",
                }
            ),
            "color": forms.Select(
                attrs={
                    "class": "px-3 py-1.5 text-sm bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:border-primary-400 focus:ring-1 focus:ring-primary-500/20 outline-none",
                }
            ),
        }
