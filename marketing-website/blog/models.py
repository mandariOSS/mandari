"""
Wagtail Page Models for Blog and Releases.
"""

from django.db import models

from wagtail.admin.panels import FieldPanel
from wagtail.fields import RichTextField, StreamField
from wagtail.models import Page
from wagtail.search import index

from marketing.blocks import MarketingStreamBlock


class BlogIndexPage(Page):
    """Blog-Übersichtsseite."""

    intro = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("intro"),
    ]

    template = "blog/blog_index.html"
    max_count = 1
    parent_page_types = ["marketing.HomePage"]
    subpage_types = ["BlogPostPage", "ReleasePage"]

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        blog_posts = BlogPostPage.objects.live().descendant_of(self).order_by("-date")
        releases = ReleasePage.objects.live().descendant_of(self).order_by("-release_date")
        context["blog_posts"] = blog_posts
        context["releases"] = releases
        return context

    class Meta:
        verbose_name = "Blog-Index"


class BlogPostPage(Page):
    """Einzelner Blog-Post."""

    date = models.DateField("Datum")
    excerpt = models.TextField("Kurzfassung", max_length=500, blank=True)
    body = StreamField(MarketingStreamBlock(), blank=True, use_json_field=True)
    category = models.CharField(
        "Kategorie",
        max_length=50,
        choices=[
            ("news", "Neuigkeiten"),
            ("tutorial", "Tutorial"),
            ("community", "Community"),
            ("tech", "Technologie"),
        ],
        default="news",
    )
    featured_image = models.ForeignKey(
        "wagtailimages.Image",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    search_fields = Page.search_fields + [
        index.SearchField("excerpt"),
        index.SearchField("body"),
    ]

    content_panels = Page.content_panels + [
        FieldPanel("date"),
        FieldPanel("category"),
        FieldPanel("excerpt"),
        FieldPanel("featured_image"),
        FieldPanel("body"),
    ]

    template = "blog/blog_post.html"
    parent_page_types = ["BlogIndexPage"]
    subpage_types = []

    class Meta:
        verbose_name = "Blog-Beitrag"
        verbose_name_plural = "Blog-Beiträge"
        ordering = ["-date"]


class ReleasePage(Page):
    """Release-Notes."""

    version = models.CharField("Version", max_length=20)
    release_type = models.CharField(
        "Typ",
        max_length=20,
        choices=[
            ("major", "Major Release"),
            ("minor", "Minor Release"),
            ("patch", "Patch/Bugfix"),
            ("beta", "Beta"),
        ],
        default="minor",
    )
    release_date = models.DateField("Release-Datum")
    body = StreamField(MarketingStreamBlock(), blank=True, use_json_field=True)
    github_url = models.URLField("GitHub URL", blank=True)
    breaking_changes = models.BooleanField("Breaking Changes", default=False)

    content_panels = Page.content_panels + [
        FieldPanel("version"),
        FieldPanel("release_type"),
        FieldPanel("release_date"),
        FieldPanel("github_url"),
        FieldPanel("breaking_changes"),
        FieldPanel("body"),
    ]

    template = "blog/release.html"
    parent_page_types = ["BlogIndexPage"]
    subpage_types = []

    class Meta:
        verbose_name = "Release"
        verbose_name_plural = "Releases"
        ordering = ["-release_date"]
