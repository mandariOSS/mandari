# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Forms for motion/document management.
"""

from django import forms

from .models import (
    Motion,
    MotionComment,
    MotionShare,
    MotionTemplate,
    MotionType,
    OrganizationLetterhead,
)


class MotionShareForm(forms.ModelForm):
    """Form for sharing a motion."""

    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                "placeholder": "E-Mail-Adresse eingeben",
            }
        ),
    )

    class Meta:
        model = MotionShare
        fields = ["scope", "level", "message"]
        widgets = {
            "scope": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500"
                }
            ),
            "level": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500"
                }
            ),
            "message": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 3,
                    "placeholder": "Optionale Nachricht an den Empfänger",
                }
            ),
        }


class MotionCommentForm(forms.ModelForm):
    """Form for adding comments to a motion."""

    class Meta:
        model = MotionComment
        fields = ["content", "selection_start", "selection_end", "selected_text", "parent"]
        widgets = {
            "content": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 3,
                    "placeholder": "Kommentar hinzufügen...",
                }
            ),
            "selection_start": forms.HiddenInput(),
            "selection_end": forms.HiddenInput(),
            "selected_text": forms.HiddenInput(),
            "parent": forms.HiddenInput(),
        }

    def __init__(self, *args, motion=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Antworten nur auf Kommentare desselben Dokuments
        self.fields["parent"].queryset = (
            MotionComment.objects.filter(motion=motion) if motion is not None else MotionComment.objects.none()
        )


class MotionStatusForm(forms.Form):
    """Form for changing motion status."""

    status = forms.ChoiceField(
        choices=Motion.STATUS_CHOICES,
        widget=forms.Select(
            attrs={
                "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500"
            }
        ),
    )


class MotionTemplateForm(forms.ModelForm):
    """Form for creating/editing motion templates."""

    class Meta:
        model = MotionTemplate
        fields = [
            "name",
            "description",
            "motion_type",
            "letterhead",
            "content_template",
            "structure_hints",
            "signature_block",
            "is_shared_party",
            "is_default",
            "is_active",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 2,
                }
            ),
            "motion_type": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "letterhead": forms.Select(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                }
            ),
            "content_template": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500 "
                    "font-mono text-sm",
                    "rows": 10,
                    "placeholder": "Vorausgefüllter Inhalt für neue Dokumente (HTML/Markdown)",
                }
            ),
            "structure_hints": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 4,
                    "placeholder": "Hinweise für die KI-Unterstützung (Abschnitte, Formatierung...)",
                }
            ),
            "signature_block": forms.Textarea(
                attrs={
                    "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                    "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                    "rows": 4,
                }
            ),
        }

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization

        # Limit motion_type and letterhead choices to organization
        if organization:
            self.fields["motion_type"].queryset = MotionType.objects.filter(
                organization=organization, is_active=True
            ).order_by("sort_order", "name")
            self.fields["letterhead"].queryset = OrganizationLetterhead.objects.filter(
                organization=organization, is_active=True
            ).order_by("-is_default", "name")


class AIAssistantForm(forms.Form):
    """Form for AI assistant actions."""

    ACTION_CHOICES = [
        ("improve", "Text verbessern"),
        ("check", "Formale Prüfung"),
        ("suggest", "Vorschläge generieren"),
        ("title", "Titel generieren"),
        ("expand", "Stichpunkte ausformulieren"),
        ("summary", "Zusammenfassung erstellen"),
        ("chat", "Dokumenten-Chat"),
    ]

    action = forms.ChoiceField(choices=ACTION_CHOICES, widget=forms.HiddenInput())
    text = forms.CharField(required=False, widget=forms.HiddenInput())
    instruction = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "block w-full rounded-lg border-gray-300 dark:border-gray-600 "
                "bg-white dark:bg-gray-800 shadow-sm focus:ring-primary-500",
                "placeholder": "Anweisung für die KI, z.B. 'kürzer formulieren'",
            }
        ),
    )
    motion_type = forms.ChoiceField(choices=Motion.LEGACY_TYPE_CHOICES, required=False, widget=forms.HiddenInput())
    selected_text = forms.CharField(required=False, widget=forms.HiddenInput())
    history = forms.JSONField(required=False, widget=forms.HiddenInput())
