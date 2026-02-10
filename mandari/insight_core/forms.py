"""
Forms für Mandari Insight Core.

Enthält Formulare für öffentliche Fragen (Ratsfragen).
"""

from django import forms

from .models import PublicQuestion


class PublicQuestionForm(forms.ModelForm):
    """Formular für öffentliche Fragen an Ratsmitglieder."""

    privacy_accepted = forms.BooleanField(
        required=True,
        label="Ich stimme zu, dass mein Name und meine Frage nach Prüfung öffentlich angezeigt werden.",
        error_messages={"required": "Sie müssen der Veröffentlichung zustimmen."},
    )

    # Honeypot-Feld (Spam-Schutz)
    website = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = PublicQuestion
        fields = [
            "questioner_name",
            "questioner_email",
            "questioner_city",
            "subject",
            "question_text",
            "privacy_accepted",
        ]
        labels = {
            "questioner_name": "Ihr Name",
            "questioner_email": "Ihre E-Mail-Adresse",
            "questioner_city": "Wohnort (optional)",
            "subject": "Betreff",
            "question_text": "Ihre Frage",
        }
        widgets = {
            "questioner_name": forms.TextInput(attrs={
                "placeholder": "Vor- und Nachname",
                "autocomplete": "name",
            }),
            "questioner_email": forms.EmailInput(attrs={
                "placeholder": "ihre@email.de",
                "autocomplete": "email",
            }),
            "questioner_city": forms.TextInput(attrs={
                "placeholder": "z.B. Münster",
                "autocomplete": "address-level2",
            }),
            "subject": forms.TextInput(attrs={
                "placeholder": "Betreff Ihrer Frage",
                "maxlength": 300,
            }),
            "question_text": forms.Textarea(attrs={
                "rows": 6,
                "maxlength": 2000,
                "placeholder": "Formulieren Sie Ihre Frage...",
            }),
        }
        help_texts = {
            "questioner_email": "Wird nicht öffentlich angezeigt. Dient nur zur Verifizierung.",
            "questioner_city": "Wird öffentlich angezeigt, wenn angegeben.",
        }

    def clean_question_text(self):
        text = self.cleaned_data["question_text"]
        if len(text) < 50:
            raise forms.ValidationError(
                "Die Frage muss mindestens 50 Zeichen lang sein."
            )
        return text

    def clean(self):
        cleaned_data = super().clean()
        # Honeypot: Wenn das Website-Feld ausgefüllt ist, ist es ein Bot
        if cleaned_data.get("website"):
            raise forms.ValidationError("Ungültige Anfrage.")
        return cleaned_data


class PublicAnswerForm(forms.Form):
    """Formular für die Antwort eines Ratsmitglieds."""

    answer_text = forms.CharField(
        label="Ihre Antwort",
        widget=forms.Textarea(attrs={
            "rows": 8,
            "maxlength": 5000,
            "placeholder": "Verfassen Sie Ihre Antwort...",
        }),
        min_length=20,
        error_messages={
            "min_length": "Die Antwort muss mindestens 20 Zeichen lang sein.",
        },
    )
