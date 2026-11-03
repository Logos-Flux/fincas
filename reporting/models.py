from django.db import models

from core.models import BaseModel


class ReportDefinition(BaseModel):
    """Saved report / dashboard query definition."""

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    query = models.TextField()
    chart_type = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
