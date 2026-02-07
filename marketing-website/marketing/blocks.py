"""
StreamField Blocks for Mandari Marketing Website.

Tailwind-based blocks for building marketing pages via Wagtail CMS.
"""

from wagtail.blocks import (
    CharBlock,
    ChoiceBlock,
    ListBlock,
    RichTextBlock,
    StreamBlock,
    StructBlock,
    TextBlock,
    URLBlock,
)
from wagtail.images.blocks import ImageChooserBlock


class HeroBlock(StructBlock):
    """Hero-Sektion mit Titel, Subtitle und CTA-Buttons."""

    badge_text = CharBlock(required=False, help_text="Badge-Text über dem Titel")
    badge_icon = CharBlock(required=False, help_text="Lucide Icon Name (z.B. 'unlock')")
    title = CharBlock(required=True, help_text="Hauptüberschrift")
    subtitle = TextBlock(required=False, help_text="Unterüberschrift")
    primary_cta_text = CharBlock(required=False, help_text="Text für primären Button")
    primary_cta_url = URLBlock(required=False, help_text="URL für primären Button")
    secondary_cta_text = CharBlock(required=False, help_text="Text für sekundären Button")
    secondary_cta_url = URLBlock(required=False, help_text="URL für sekundären Button")
    gradient_from = ChoiceBlock(
        choices=[
            ("primary", "Primary (Indigo)"),
            ("green", "Grün"),
            ("amber", "Amber"),
            ("blue", "Blau"),
            ("rose", "Rose"),
        ],
        default="primary",
        help_text="Hintergrund-Gradient",
    )

    class Meta:
        template = "marketing/blocks/hero.html"
        icon = "image"
        label = "Hero-Sektion"


class FeatureCardBlock(StructBlock):
    """Einzelne Feature-Karte."""

    icon = CharBlock(required=False, help_text="Lucide Icon Name")
    title = CharBlock(required=True)
    description = TextBlock(required=True)


class FeatureGridBlock(StructBlock):
    """Grid von Feature-Karten."""

    title = CharBlock(required=False, help_text="Abschnitts-Überschrift")
    subtitle = TextBlock(required=False)
    features = ListBlock(FeatureCardBlock())
    columns = ChoiceBlock(
        choices=[("2", "2 Spalten"), ("3", "3 Spalten"), ("4", "4 Spalten")],
        default="3",
    )

    class Meta:
        template = "marketing/blocks/feature_grid.html"
        icon = "grip"
        label = "Feature-Grid"


class CTABlock(StructBlock):
    """Call-to-Action Banner."""

    title = CharBlock(required=True)
    description = TextBlock(required=False)
    button_text = CharBlock(required=True)
    button_url = URLBlock(required=True)
    style = ChoiceBlock(
        choices=[("primary", "Primary"), ("dark", "Dunkel"), ("gradient", "Gradient")],
        default="primary",
    )

    class Meta:
        template = "marketing/blocks/cta.html"
        icon = "pick"
        label = "Call-to-Action"


class FAQItemBlock(StructBlock):
    """Einzelne FAQ."""

    question = CharBlock(required=True)
    answer = RichTextBlock(required=True)


class FAQBlock(StructBlock):
    """Akkordeon-FAQ."""

    title = CharBlock(required=False, default="Häufig gestellte Fragen")
    items = ListBlock(FAQItemBlock())

    class Meta:
        template = "marketing/blocks/faq.html"
        icon = "help"
        label = "FAQ-Sektion"


class PricingTierBlock(StructBlock):
    """Einzelne Preisstufe."""

    name = CharBlock(required=True)
    price = CharBlock(required=True, help_text="z.B. '0€', '49€/Monat'")
    description = TextBlock(required=False)
    features = ListBlock(CharBlock())
    is_highlighted = ChoiceBlock(
        choices=[("no", "Normal"), ("yes", "Hervorgehoben")],
        default="no",
    )
    cta_text = CharBlock(required=False, default="Kontakt aufnehmen")
    cta_url = URLBlock(required=False)


class PricingBlock(StructBlock):
    """Preistabelle."""

    title = CharBlock(required=False, default="Transparente Preise")
    subtitle = TextBlock(required=False)
    tiers = ListBlock(PricingTierBlock())

    class Meta:
        template = "marketing/blocks/pricing.html"
        icon = "tag"
        label = "Preistabelle"


class TeamMemberBlock(StructBlock):
    """Einzelnes Team-Mitglied."""

    name = CharBlock(required=True)
    role = CharBlock(required=True)
    image = ImageChooserBlock(required=False)
    bio = TextBlock(required=False)


class TeamGridBlock(StructBlock):
    """Team-Mitglieder-Raster."""

    title = CharBlock(required=False, default="Das Team")
    members = ListBlock(TeamMemberBlock())

    class Meta:
        template = "marketing/blocks/team_grid.html"
        icon = "group"
        label = "Team-Grid"


class StatsBlock(StructBlock):
    """Statistiken (via API von Mandari)."""

    title = CharBlock(required=False, default="Mandari in Zahlen")
    show_bodies = ChoiceBlock(
        choices=[("yes", "Ja"), ("no", "Nein")],
        default="yes",
        help_text="Kommunen-Statistik anzeigen",
    )

    class Meta:
        template = "marketing/blocks/stats.html"
        icon = "chart-bar"
        label = "Statistiken (API)"


class RichTextSectionBlock(StructBlock):
    """Rich-Text-Sektion mit optionaler Überschrift."""

    title = CharBlock(required=False)
    body = RichTextBlock(required=True)

    class Meta:
        template = "marketing/blocks/richtext_section.html"
        icon = "doc-full"
        label = "Richtext-Sektion"


class MarketingStreamBlock(StreamBlock):
    """Hauptblock für Marketing-Seiten."""

    hero = HeroBlock()
    features = FeatureGridBlock()
    cta = CTABlock()
    faq = FAQBlock()
    pricing = PricingBlock()
    team = TeamGridBlock()
    stats = StatsBlock()
    richtext = RichTextSectionBlock()
