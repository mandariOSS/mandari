"""
RSS-Feed für den Mandari Blog.
"""

from django.contrib.syndication.views import Feed
from django.utils.html import strip_tags

from .models import BlogPostPage


class BlogFeed(Feed):
    title = "Mandari Blog"
    link = "/blog/"
    description = "Neuigkeiten, Tutorials und Updates rund um Mandari – die Open-Source-Plattform für kommunalpolitische Transparenz."

    def items(self):
        return BlogPostPage.objects.live().order_by("-date")[:20]

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        if item.excerpt:
            return item.excerpt
        # Fallback: Ersten Text aus StreamField extrahieren
        for block in item.body:
            text = strip_tags(str(block.value))
            if text.strip():
                return text[:300]
        return ""

    def item_link(self, item):
        return item.url

    def item_pubdate(self, item):
        # Feed erwartet datetime, date reicht aber für RSS 2.0
        from datetime import datetime, time

        from django.utils.timezone import make_aware

        return make_aware(datetime.combine(item.date, time.min))

    def item_categories(self, item):
        return [item.get_category_display()]
