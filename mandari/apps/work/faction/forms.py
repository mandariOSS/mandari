# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Forms for faction meeting management.
"""

from django import forms

from .models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionMeetingSchedule,
    FactionProtocolEntry,
)


class FactionMeetingForm(forms.ModelForm):
    """Form for creating and editing faction meetings."""

    class Meta:
        model = FactionMeeting
        fields = [
            "title",
            "description",
            "schedule",
            "start",
            "end",
            "location",
            "is_virtual",
            "video_link",
            "related_meeting",
        ]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "placeholder": "Titel der Sitzung",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 3,
                    "placeholder": "Optionale Beschreibung",
                }
            ),
            "schedule": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "start": forms.DateTimeInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "type": "datetime-local",
                },
                format="%Y-%m-%dT%H:%M",
            ),
            "end": forms.DateTimeInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "type": "datetime-local",
                },
                format="%Y-%m-%dT%H:%M",
            ),
            "location": forms.TextInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "placeholder": "Ort der Sitzung",
                }
            ),
            "is_virtual": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-gray-300 text-primary-600 focus:ring-primary-500",
                }
            ),
            "video_link": forms.URLInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "placeholder": "https://...",
                }
            ),
            "related_meeting": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization

        if organization:
            self.fields["schedule"].queryset = FactionMeetingSchedule.objects.filter(
                organization=organization, is_active=True
            )


class FactionScheduleForm(forms.ModelForm):
    """Form for creating/editing meeting schedules."""

    class Meta:
        model = FactionMeetingSchedule
        fields = [
            "name",
            "recurrence",
            "weekday",
            "time",
            "duration_minutes",
            "default_location",
            "default_video_link",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "placeholder": "z.B. Wöchentliche Fraktionssitzung",
                }
            ),
            "recurrence": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "weekday": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "time": forms.TimeInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "type": "time",
                }
            ),
            "duration_minutes": forms.NumberInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "min": 15,
                    "max": 480,
                }
            ),
            "default_location": forms.TextInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "placeholder": "Standard-Ort",
                }
            ),
            "default_video_link": forms.URLInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "placeholder": "https://...",
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-gray-300 text-primary-600 focus:ring-primary-500",
                }
            ),
        }


class FactionAgendaItemForm(forms.ModelForm):
    """Form for agenda items."""

    class Meta:
        model = FactionAgendaItem
        fields = [
            "number",
            "title",
            "description_encrypted",
            "related_agenda_item",
        ]
        widgets = {
            "number": forms.TextInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "placeholder": "TOP 1",
                }
            ),
            "title": forms.TextInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "description_encrypted": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 3,
                }
            ),
            "related_agenda_item": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
        }


class FactionProtocolEntryForm(forms.ModelForm):
    """Form for protocol entries."""

    class Meta:
        model = FactionProtocolEntry
        fields = [
            "agenda_item",
            "entry_type",
            "content_encrypted",
            "speaker",
            "action_assignee",
            "action_due_date",
        ]
        widgets = {
            "agenda_item": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "entry_type": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "content_encrypted": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 3,
                    "placeholder": "Protokolleintrag...",
                }
            ),
            "speaker": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "action_assignee": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "action_due_date": forms.DateInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "type": "date",
                }
            ),
        }


class FactionDecisionForm(forms.ModelForm):
    """Form for recording voting results."""

    class Meta:
        model = FactionDecision
        fields = [
            "votes_yes",
            "votes_no",
            "votes_abstain",
            "result",
            "decision_text",
            "notes",
        ]
        widgets = {
            "votes_yes": forms.NumberInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "min": 0,
                }
            ),
            "votes_no": forms.NumberInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "min": 0,
                }
            ),
            "votes_abstain": forms.NumberInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "min": 0,
                }
            ),
            "result": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "decision_text": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 3,
                    "placeholder": "Abweichender Beschlusstext (optional)",
                }
            ),
            "notes": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 2,
                    "placeholder": "Anmerkungen zur Abstimmung",
                }
            ),
        }


class FactionAttendanceResponseForm(forms.ModelForm):
    """Form for attendance response (RSVP)."""

    class Meta:
        model = FactionAttendance
        fields = ["status", "response_message"]
        widgets = {
            "status": forms.RadioSelect(
                attrs={
                    "class": "text-primary-600 focus:ring-primary-500",
                }
            ),
            "response_message": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 2,
                    "placeholder": "Begründung (bei Absage)",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Limit status choices for response form
        self.fields["status"].choices = [
            ("confirmed", "Zusagen"),
            ("declined", "Absagen"),
            ("tentative", "Vielleicht"),
        ]
