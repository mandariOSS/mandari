"""
Views für Blog und Releases
"""

from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.views.generic import DetailView, ListView

from .models import BlogPost, Release


class BlogListView(ListView):
    """Liste aller veröffentlichten Blog-Artikel"""

    model = BlogPost
    template_name = "pages/about/blog.html"
    context_object_name = "posts"
    paginate_by = 10

    def get_queryset(self):
        queryset = BlogPost.objects.filter(status=BlogPost.Status.PUBLISHED)

        # Filter nach Kategorie
        category = self.request.GET.get("category")
        if category:
            queryset = queryset.filter(category=category)

        # Suche
        search = self.request.GET.get("q")
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(excerpt__icontains=search) | Q(content__icontains=search)
            )

        return queryset.select_related("author")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["categories"] = BlogPost.Category.choices
        context["current_category"] = self.request.GET.get("category", "")
        context["search_query"] = self.request.GET.get("q", "")

        # Featured Post (neuester)
        if not context["current_category"] and not context["search_query"]:
            featured = BlogPost.objects.filter(status=BlogPost.Status.PUBLISHED).first()
            context["featured_post"] = featured

        return context


class BlogDetailView(DetailView):
    """Einzelner Blog-Artikel"""

    model = BlogPost
    template_name = "pages/about/blog_detail.html"
    context_object_name = "post"
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        # Nur veröffentlichte Artikel (außer für Staff)
        if self.request.user.is_staff:
            return BlogPost.objects.all()
        return BlogPost.objects.filter(status=BlogPost.Status.PUBLISHED)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Verwandte Artikel (gleiche Kategorie)
        context["related_posts"] = BlogPost.objects.filter(
            status=BlogPost.Status.PUBLISHED, category=self.object.category
        ).exclude(pk=self.object.pk)[:3]

        # Navigation (vorheriger/nächster Artikel)
        context["previous_post"] = BlogPost.objects.filter(
            status=BlogPost.Status.PUBLISHED, published_at__lt=self.object.published_at
        ).first()

        context["next_post"] = BlogPost.objects.filter(
            status=BlogPost.Status.PUBLISHED, published_at__gt=self.object.published_at
        ).last()

        return context


class ReleaseListView(ListView):
    """Changelog mit allen Releases"""

    model = Release
    template_name = "pages/about/releases.html"
    context_object_name = "releases"

    def get_queryset(self):
        return Release.objects.filter(is_published=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Gruppiere nach Major Version
        releases = self.get_queryset()
        grouped = {}
        for release in releases:
            major = release.version.split(".")[0]
            if major not in grouped:
                grouped[major] = []
            grouped[major].append(release)

        context["grouped_releases"] = grouped

        # Neuestes Release
        context["latest_release"] = releases.first()

        return context


# Einfache Function-Based Views als Alternative


def blog_list(request):
    """Blog-Übersicht"""
    # Prüfe ob überhaupt Posts existieren (für Coming Soon vs. Filter-Ergebnisse)
    all_posts = BlogPost.objects.filter(status=BlogPost.Status.PUBLISHED)
    has_any_posts = all_posts.exists()

    posts = all_posts
    category = request.GET.get("category")
    if category:
        posts = posts.filter(category=category)

    search = request.GET.get("q")
    if search:
        posts = posts.filter(Q(title__icontains=search) | Q(excerpt__icontains=search))

    # Zeige Filter nur wenn Posts existieren ODER ein Filter/Suche aktiv ist
    is_filtering = bool(category or search)

    context = {
        "posts": posts.select_related("author"),
        "categories": BlogPost.Category.choices,
        "current_category": category or "",
        "search_query": search or "",
        "has_any_posts": has_any_posts,
        "is_filtering": is_filtering,
    }

    # Featured Post
    if not category and not search:
        context["featured_post"] = posts.first()

    return render(request, "pages/about/blog.html", context)


def blog_detail(request, slug):
    """Blog-Artikel Detail"""
    if request.user.is_staff:
        post = get_object_or_404(BlogPost, slug=slug)
    else:
        post = get_object_or_404(BlogPost, slug=slug, status=BlogPost.Status.PUBLISHED)

    related_posts = BlogPost.objects.filter(status=BlogPost.Status.PUBLISHED, category=post.category).exclude(
        pk=post.pk
    )[:3]

    context = {
        "post": post,
        "related_posts": related_posts,
    }

    return render(request, "pages/about/blog_detail.html", context)


def releases_list(request):
    """Releases/Changelog"""
    releases = Release.objects.filter(is_published=True)

    # Gruppiere nach Major Version
    grouped = {}
    for release in releases:
        major = release.version.split(".")[0]
        if major not in grouped:
            grouped[major] = []
        grouped[major].append(release)

    context = {
        "releases": releases,
        "grouped_releases": grouped,
        "latest_release": releases.first(),
    }

    return render(request, "pages/about/releases.html", context)
