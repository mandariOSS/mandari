# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Forms for meeting preparation.
"""

from django import forms

from .models import (
    MeetingPreparation,
    AgendaItemPosition,
    AgendaItemNote,
    AgendaSpeechNote,
    AgendaDocumentLink,
)


class MeetingPreparationForm(forms.ModelForm):
    """Form for overall meeting preparation."""

    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "rows": 4,
            "placeholder": "Allgemeine Notizen zur Sitzung...",
            "class": "w-full px-4 py-2.5 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
        })
    )

    class Meta:
        model = MeetingPreparation
        fields = ["is_prepared"]
        widgets = {
            "is_prepared": forms.CheckboxInput(attrs={
                "class": "w-5 h-5 text-primary-600 border-gray-300 rounded focus:ring-primary-500"
            })
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["notes"].initial = self.instance.notes_encrypted

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.notes_encrypted = self.cleaned_data.get("notes", "")
        if commit:
            instance.save()
        return instance


class AgendaItemPositionForm(forms.ModelForm):
    """Form for a member's position on an agenda item."""

    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "rows": 2,
            "placeholder": "Private Notizen (nur für Sie sichtbar)...",
            "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
        })
    )

    class Meta:
        model = AgendaItemPosition
        fields = ["position", "discussion_note", "is_final"]
        widgets = {
            "position": forms.RadioSelect(attrs={
                "class": "hidden peer"
            }),
            "discussion_note": forms.Textarea(attrs={
                "rows": 2,
                "placeholder": "Notiz für die Fraktionsdiskussion (für alle sichtbar)...",
                "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
            }),
            "is_final": forms.CheckboxInput(attrs={
                "class": "w-4 h-4 text-primary-600 border-gray-300 rounded focus:ring-primary-500"
            })
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["notes"].initial = self.instance.notes_encrypted

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.notes_encrypted = self.cleaned_data.get("notes", "")
        if commit:
            instance.save()
        return instance


class AgendaSpeechNoteForm(forms.ModelForm):
    """Form for speech notes / teleprompter content."""

    class Meta:
        model = AgendaSpeechNote
        fields = ["title", "content", "estimated_duration", "is_shared"]
        widgets = {
            "title": forms.TextInput(attrs={
                "placeholder": "Titel der Rede (optional)...",
                "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
            }),
            "content": forms.Textarea(attrs={
                "rows": 6,
                "placeholder": "Redetext für den Teleprompter...",
                "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500 font-mono"
            }),
            "estimated_duration": forms.NumberInput(attrs={
                "min": 0,
                "max": 3600,
                "placeholder": "Sekunden",
                "class": "w-24 px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg"
            }),
            "is_shared": forms.CheckboxInput(attrs={
                "class": "w-4 h-4 text-primary-600 border-gray-300 rounded focus:ring-primary-500"
            })
        }


class AgendaDocumentLinkForm(forms.ModelForm):
    """Form for adding custom document links to agenda items."""

    class Meta:
        model = AgendaDocumentLink
        fields = ["title", "url", "description"]
        widgets = {
            "title": forms.TextInput(attrs={
                "placeholder": "Dokumenttitel...",
                "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
            }),
            "url": forms.URLInput(attrs={
                "placeholder": "https://...",
                "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
            }),
            "description": forms.Textarea(attrs={
                "rows": 2,
                "placeholder": "Kurze Beschreibung (optional)...",
                "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
            })
        }


class AgendaItemNoteForm(forms.ModelForm):
    """Form for collaborative notes on an agenda item."""

    content = forms.CharField(
        widget=forms.Textarea(attrs={
            "rows": 3,
            "placeholder": "Kommentar hinzufügen...",
            "class": "w-full px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg focus:ring-2 focus:ring-primary-500"
        })
    )

    class Meta:
        model = AgendaItemNote
        fields = ["visibility", "is_decision", "is_pinned"]
        widgets = {
            "visibility": forms.Select(attrs={
                "class": "px-3 py-2 text-sm bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg"
            }),
            "is_decision": forms.CheckboxInput(attrs={
                "class": "w-4 h-4 text-primary-600 border-gray-300 rounded focus:ring-primary-500"
            }),
            "is_pinned": forms.CheckboxInput(attrs={
                "class": "w-4 h-4 text-primary-600 border-gray-300 rounded focus:ring-primary-500"
            })
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["content"].initial = self.instance.get_content_decrypted()

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.set_content_encrypted(self.cleaned_data.get("content", ""))
        if commit:
            instance.save()
        return instance


class QuickNoteForm(forms.Form):
    """Simplified form for quick note submission."""

    content = forms.CharField(
        widget=forms.Textarea(attrs={
            "rows": 2,
            "placeholder": "Schnelle Notiz...",
        })
    )
    visibility = forms.ChoiceField(
        choices=AgendaItemNote.VISIBILITY_CHOICES,
        initial="organization"
    )
