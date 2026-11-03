from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, DetailView, UpdateView

from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin

from .forms import ArticleForm
from .models import Article

# Sections shared by create + edit — mirrors the discounts app pattern.
ARTICLE_FORM_SECTIONS = [
    {"title": "Article", "fields": ["title", "slug", "status", "published_at"]},
    {"title": "Content", "fields": ["excerpt", "content"]},
    {"title": "Byline & Image", "fields": ["author_name", "featured_image_url", "tags"]},
    {"title": "SEO", "fields": ["seo_title", "seo_meta_description", "meta_keywords"]},
]


class ArticleListView(ERPListView):
    model = Article
    current_section = "content"
    page_title = "Articles"
    entity_name = "Article"
    create_url_name = "erp-article-create"
    ordering = ["-published_at", "-created_at"]
    search_fields = ["title", "slug", "author_name"]
    list_columns = [
        {"field": "title", "label": "Title", "link": True, "link_url_name": "erp-article-detail"},
        {"field": "author_name", "label": "Author"},
        {"field": "status", "label": "Status", "badge": True},
        {"field": "published_at", "label": "Published"},
    ]
    list_filters = [
        {"field": "status", "label": "Status", "choices": Article.Status.choices},
    ]


class ArticleCreateView(ERPViewMixin, CreateView):
    model = Article
    form_class = ArticleForm
    template_name = "erp/form.html"
    current_section = "content"
    page_title = "New Article"
    entity_name = "Article"
    form_sections = ARTICLE_FORM_SECTIONS

    def get_success_url(self):
        return reverse("erp-article-detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_name"] = self.entity_name
        context["form_sections"] = self.form_sections
        context["is_edit"] = False
        return context


class ArticleUpdateView(ERPViewMixin, UpdateView):
    model = Article
    form_class = ArticleForm
    template_name = "erp/form.html"
    current_section = "content"
    page_title = "Edit Article"
    entity_name = "Article"
    form_sections = ARTICLE_FORM_SECTIONS

    def get_success_url(self):
        return reverse("erp-article-detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["entity_name"] = self.entity_name
        context["form_sections"] = self.form_sections
        context["is_edit"] = True
        return context


class ArticleDetailView(ERPViewMixin, DetailView):
    model = Article
    template_name = "content/detail.html"
    current_section = "content"
    page_title = "Article"


class ArticleDeleteView(ERPViewMixin, View):
    """POST-only delete — mirrors the customers app's address-delete pattern.

    storefront.blog_posts is a plain live SQL view over content_article, so
    the delete propagates to the storefront automatically (no MV refresh).
    """

    def post(self, request, pk):
        article = get_object_or_404(Article, pk=pk)
        title = article.title
        article.delete()
        messages.success(request, f'Article "{title}" deleted.')
        return redirect(reverse("erp-article-list"))
