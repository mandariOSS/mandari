# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Forms for the Tasks module.
"""

from django import forms

from .models import Task, TaskComment


class TaskForm(forms.ModelForm):
    """Form for creating and editing tasks."""

    class Meta:
        model = Task
        fields = [
            "title",
            "description",
            "priority",
            "status",
            "due_date",
            "assigned_to",
            "tags",
        ]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                "placeholder": "Aufgabentitel",
                "maxlength": "200",
            }),
            "description": forms.Textarea(attrs={
                "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                "rows": 4,
                "placeholder": "Optionale Beschreibung...",
                "maxlength": "2000",
            }),
            "priority": forms.Select(attrs={
                "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
            }),
            "status": forms.Select(attrs={
                "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
            }),
            "due_date": forms.DateInput(attrs={
                "type": "date",
                "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
            }),
            "assigned_to": forms.Select(attrs={
                "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
            }),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        if organization:
            self.fields["assigned_to"].queryset = organization.memberships.filter(
                is_active=True
            ).select_related("user")
            self.fields["assigned_to"].label_from_instance = lambda obj: obj.user.get_display_name()
        self.fields["assigned_to"].required = False
        self.fields["due_date"].required = False


class QuickTaskForm(forms.Form):
    """Quick task creation form for Kanban board."""

    title = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={
            "class": "flex-1 px-3 py-2 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
            "placeholder": "Neue Aufgabe...",
            "maxlength": "200",
        })
    )
    status = forms.ChoiceField(
        choices=Task.STATUS_CHOICES,
        widget=forms.HiddenInput()
    )
    priority = forms.ChoiceField(
        choices=Task.PRIORITY_CHOICES,
        initial="medium",
        required=False,
        widget=forms.HiddenInput()
    )


class TaskCommentForm(forms.ModelForm):
    """Form for adding comments to tasks."""

    class Meta:
        model = TaskComment
        fields = ["content"]
        widgets = {
            "content": forms.Textarea(attrs={
                "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500",
                "rows": 2,
                "placeholder": "Kommentar hinzufügen...",
            })
        }


class TaskStatusForm(forms.Form):
    """Form for updating task status via drag & drop."""

    task_id = forms.UUIDField()
    status = forms.ChoiceField(choices=Task.STATUS_CHOICES)
    position = forms.IntegerField(min_value=0)
