"""
Wagtail Page Models for Blog and Releases.
"""

import math
import re

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import models

from modelcluster.contrib.taggit import ClusterTaggableManager
from modelcluster.fields import ParentalKey
from taggit.models import TaggedItemBase
from wagtail.admin.panels import FieldPanel
from wagtail.fields import RichTextField, StreamField
from wagtail.models import Page
from wagtail.search import index
from wagtail.snippets.models import register_snippet

from marketing.blocks import MarketingStreamBlock


# ── Snippets ──────────────────────────────────────────────────────────────


@register_snippet
class BlogAuthor(models.Model):
    """Wiederverwendbares Autoren-Snippet."""

    name = models.CharField("Name", max_length=100)
    role = models.CharField("Rolle", max_length=100, blank=True, help_text="z.B. Gründer, Entwickler")
    image = models.ForeignKey(
        "wagtailimages.Image",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Bild",
    )
    bio = models.TextField("Bio", max_length=300, blank=True)

    panels = [
        FieldPanel("name"),
        FieldPanel("role"),
        FieldPanel("image"),
        FieldPanel("bio"),
    ]

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Autor"
        verbose_name_plural = "Autoren"
        ordering = ["name"]


# ── Tags ──────────────────────────────────────────────────────────────────


class BlogPostTag(TaggedItemBase):
    content_object = ParentalKey("BlogPostPage", related_name="tagged_items", on_delete=models.CASCADE)


# ── Blog ──────────────────────────────────────────────────────────────────


class BlogIndexPage(Page):
    """Blog-Übersichtsseite."""

    intro = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("intro"),
    ]

    template = "blog/blog_index.html"
    max_count = 1
    parent_page_types = ["marketing.HomePage"]
    subpage_types = ["BlogPostPage"]

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)

        posts = BlogPostPage.objects.live().descendant_of(self).order_by("-date")

        # Kategorie-Filter
        category = request.GET.get("category")
        if category and category in dict(BlogPostPage.CATEGORY_CHOICES):
            posts = posts.filter(category=category)
            context["active_category"] = category
        else:
            context["active_category"] = ""

        context["categories"] = BlogPostPage.CATEGORY_CHOICES

        # Pagination (12 pro Seite)
        paginator = Paginator(posts, 12)
        page_number = request.GET.get("page")
        try:
            posts_page = paginator.page(page_number)
        except PageNotAnInteger:
            posts_page = paginator.page(1)
        except EmptyPage:
            posts_page = paginator.page(paginator.num_pages)

        context["blog_posts"] = posts_page

        return context

    class Meta:
        verbose_name = "Blog-Index"


class BlogPostPage(Page):
    """Einzelner Blog-Post."""

    CATEGORY_CHOICES = [
        ("news", "Neuigkeiten"),
        ("tutorial", "Tutorial"),
        ("community", "Community"),
        ("tech", "Technologie"),
    ]

    date = models.DateField("Datum")
    excerpt = models.TextField("Kurzfassung", max_length=500, blank=True)
    body = StreamField(MarketingStreamBlock(), blank=True, use_json_field=True)
    category = models.CharField("Kategorie", max_length=50, choices=CATEGORY_CHOICES, default="news")
    featured_image = models.ForeignKey(
        "wagtailimages.Image",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    author = models.ForeignKey(
        BlogAuthor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="blog_posts",
        verbose_name="Autor",
    )
    tags = ClusterTaggableManager(through=BlogPostTag, blank=True)

    search_fields = Page.search_fields + [
        index.SearchField("excerpt"),
        index.SearchField("body"),
    ]

    content_panels = Page.content_panels + [
        FieldPanel("date"),
        FieldPanel("author"),
        FieldPanel("category"),
        FieldPanel("tags"),
        FieldPanel("excerpt"),
        FieldPanel("featured_image"),
        FieldPanel("body"),
    ]

    template = "blog/blog_post.html"
    parent_page_types = ["BlogIndexPage"]
    subpage_types = []

    @property
    def reading_time(self):
        """Geschätzte Lesezeit in Minuten basierend auf StreamField-Inhalt."""
        text = ""
        for block in self.body:
            text += str(block.value) + " "
        word_count = len(re.findall(r"\w+", text))
        minutes = max(1, math.ceil(word_count / 200))
        return minutes

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        # Verwandte Posts (gleiche Kategorie, max 3)
        related = (
            BlogPostPage.objects.live()
            .sibling_of(self)
            .filter(category=self.category)
            .exclude(pk=self.pk)
            .order_by("-date")[:3]
        )
        context["related_posts"] = related
        return context

    class Meta:
        verbose_name = "Blog-Beitrag"
        verbose_name_plural = "Blog-Beiträge"
        ordering = ["-date"]


# ── Releases ──────────────────────────────────────────────────────────────


class ReleaseIndexPage(Page):
    """Release-Übersichtsseite."""

    intro = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel("intro"),
    ]

    template = "blog/release_index.html"
    max_count = 1
    parent_page_types = ["marketing.HomePage"]
    subpage_types = ["ReleasePage"]

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        releases = ReleasePage.objects.live().descendant_of(self).order_by("-release_date")
        context["releases"] = releases
        return context

    class Meta:
        verbose_name = "Release-Index"


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
    parent_page_types = ["ReleaseIndexPage"]
    subpage_types = []

    def get_context(self, request, *args, **kwargs):
        context = super().get_context(request, *args, **kwargs)
        siblings = ReleasePage.objects.live().sibling_of(self).order_by("-release_date")
        # Prev/Next Navigation
        prev_release = siblings.filter(release_date__gt=self.release_date).order_by("release_date").first()
        next_release = siblings.filter(release_date__lt=self.release_date).first()
        context["prev_release"] = prev_release
        context["next_release"] = next_release
        return context

    class Meta:
        verbose_name = "Release"
        verbose_name_plural = "Releases"
        ordering = ["-release_date"]
