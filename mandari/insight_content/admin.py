"""
Admin-Konfiguration für Blog und Releases mit Markdown-Editor.

Verwendet Django Unfold für modernes Admin-Interface.
"""

from django import forms
from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin

from .models import BlogPost, Release


class MarkdownEditorWidget(forms.Textarea):
    """Custom Widget das EasyMDE Markdown Editor lädt"""

    def __init__(self, attrs=None):
        default_attrs = {
            "class": "markdown-editor",
            "rows": 20,
        }
        if attrs:
            default_attrs.update(attrs)
        super().__init__(attrs=default_attrs)

    class Media:
        css = {
            "all": [
                "vendor/easymde/easymde.min.css",
            ]
        }
        js = [
            "vendor/easymde/easymde.min.js",
        ]


class BlogPostAdminForm(forms.ModelForm):
    """Custom Form mit Markdown-Editor für Blog-Inhalte"""

    class Meta:
        model = BlogPost
        fields = "__all__"
        widgets = {
            "content": MarkdownEditorWidget(),
            "excerpt": forms.Textarea(attrs={"rows": 3}),
        }


@admin.register(BlogPost)
class BlogPostAdmin(ModelAdmin):
    form = BlogPostAdminForm

    list_display = [
        "title",
        "category_badge",
        "status_badge",
        "author",
        "published_at",
        "reading_time_display",
    ]
    list_filter = ["status", "category", "author", "created_at"]
    search_fields = ["title", "excerpt", "content"]
    prepopulated_fields = {"slug": ("title",)}
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    fieldsets = (
        (None, {"fields": ("title", "slug", "excerpt", "content")}),
        (
            "Kategorisierung",
            {
                "fields": ("category", "status", "author"),
                "classes": ("collapse",),
            },
        ),
        (
            "Bild",
            {
                "fields": ("featured_image", "image_alt"),
                "classes": ("collapse",),
            },
        ),
        (
            "SEO",
            {
                "fields": ("meta_description",),
                "classes": ("collapse",),
            },
        ),
        (
            "Zeitstempel",
            {
                "fields": ("published_at",),
                "classes": ("collapse",),
            },
        ),
    )

    readonly_fields = ["created_at", "updated_at"]

    def category_badge(self, obj):
        colors = {
            "news": "bg-blue-100 text-blue-800",
            "tutorial": "bg-green-100 text-green-800",
            "update": "bg-purple-100 text-purple-800",
            "community": "bg-yellow-100 text-yellow-800",
            "tech": "bg-gray-100 text-gray-800",
        }
        color = colors.get(obj.category, "bg-gray-100 text-gray-800")
        return format_html(
            '<span style="padding: 2px 8px; border-radius: 4px; font-size: 12px;" class="{}">{}</span>',
            color,
            obj.get_category_display(),
        )

    category_badge.short_description = "Kategorie"

    def status_badge(self, obj):
        if obj.status == BlogPost.Status.PUBLISHED:
            return mark_safe('<span style="color: green;">&#x2713; Veröffentlicht</span>')
        return mark_safe('<span style="color: orange;">&#x270E; Entwurf</span>')

    status_badge.short_description = "Status"

    def reading_time_display(self, obj):
        return f"{obj.reading_time} Min."

    reading_time_display.short_description = "Lesezeit"

    class Media:
        js = ("admin/js/markdown_editor_init.js",)


class ReleaseAdminForm(forms.ModelForm):
    """Custom Form mit Markdown-Editor für Release-Changelog"""

    class Meta:
        model = Release
        fields = "__all__"
        widgets = {
            "content": MarkdownEditorWidget(),
        }


@admin.register(Release)
class ReleaseAdmin(ModelAdmin):
    form = ReleaseAdminForm

    list_display = [
        "version_display",
        "title",
        "release_type_badge",
        "release_date",
        "is_published",
        "breaking_changes_badge",
    ]
    list_filter = ["release_type", "is_published", "breaking_changes", "release_date"]
    search_fields = ["version", "title", "content"]
    ordering = ["-release_date", "-version"]
    date_hierarchy = "release_date"

    fieldsets = (
        (None, {"fields": ("version", "title", "content")}),
        (
            "Release-Info",
            {
                "fields": ("release_type", "release_date", "is_published", "breaking_changes"),
            },
        ),
        (
            "Links",
            {
                "fields": ("github_url",),
                "classes": ("collapse",),
            },
        ),
    )

    readonly_fields = ["created_at", "updated_at"]

    def version_display(self, obj):
        return format_html(
            '<code style="background: #f3f4f6; padding: 2px 6px; border-radius: 4px;">v{}</code>',
            obj.version,
        )

    version_display.short_description = "Version"

    def release_type_badge(self, obj):
        colors = {
            "major": ("#7c3aed", "#ede9fe"),  # purple
            "minor": ("#2563eb", "#dbeafe"),  # blue
            "patch": ("#6b7280", "#f3f4f6"),  # gray
        }
        text_color, bg_color = colors.get(obj.release_type, colors["patch"])
        return format_html(
            '<span style="background: {}; color: {}; padding: 2px 8px; border-radius: 4px; font-size: 12px;">{}</span>',
            bg_color,
            text_color,
            obj.get_release_type_display(),
        )

    release_type_badge.short_description = "Typ"

    def breaking_changes_badge(self, obj):
        if obj.breaking_changes:
            return mark_safe('<span style="color: #dc2626;">&#x26A0; Breaking</span>')
        return mark_safe('<span style="color: #16a34a;">&#x2713;</span>')

    breaking_changes_badge.short_description = "Breaking"

    class Media:
        js = ("admin/js/markdown_editor_init.js",)
