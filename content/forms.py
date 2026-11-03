from django import forms

from .models import Article


class ArticleForm(forms.ModelForm):
    # tags is a JSONField (list of strings). Present it as a comma-separated
    # text input and (de)serialize in the form, same idea as discounts'
    # allowed_skus textarea.
    tags = forms.CharField(
        required=False,
        help_text="Comma-separated, e.g. solar, batteries, how-to",
    )

    class Meta:
        model = Article
        fields = [
            "title", "slug", "content", "excerpt", "author_name",
            "featured_image_url", "tags", "status", "published_at",
            "seo_title", "seo_meta_description", "meta_keywords",
        ]
        widgets = {
            "content": forms.Textarea(attrs={"rows": 16}),
            "excerpt": forms.Textarea(attrs={"rows": 3}),
            "seo_meta_description": forms.Textarea(attrs={"rows": 2}),
            "published_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Show the stored tags list as comma-separated text.
        if self.instance and self.instance.pk and self.instance.tags:
            self.initial["tags"] = ", ".join(self.instance.tags)

    def clean_tags(self):
        raw = self.cleaned_data.get("tags", "")
        if not raw.strip():
            return []
        return [t.strip() for t in raw.split(",") if t.strip()]
