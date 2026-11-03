from django.db import models

from core.models import BaseModel


class ProductDocument(BaseModel):
    """Manufacturer document (spec sheet, manual, etc.) stored in R2."""

    class DocumentType(models.TextChoices):
        SPEC_SHEET = "spec_sheet", "Spec Sheet"
        MANUAL = "manual", "Manual"
        COMPLIANCE = "compliance", "Compliance"
        DATASHEET = "datasheet", "Datasheet"
        INSTALLATION = "installation", "Installation Guide"
        WARRANTY = "warranty", "Warranty"

    class MatchMethod(models.TextChoices):
        MANUAL = "manual", "Manual"
        AI_VECTOR = "ai_vector", "AI Vector"
        SCRAPER = "scraper", "Scraper"
        AUTO_MATCH = "auto_match", "Auto Token Match"

    product = models.ForeignKey("products.Product", on_delete=models.CASCADE, related_name="documents")
    document_type = models.CharField(max_length=20, choices=DocumentType.choices)
    title = models.CharField(max_length=255)
    r2_key = models.CharField(max_length=500)
    r2_url = models.URLField()
    file_size = models.IntegerField(null=True, blank=True)
    match_confidence = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    match_method = models.CharField(max_length=15, choices=MatchMethod.choices, blank=True)
    is_verified = models.BooleanField(default=False)

    # Auto-match audit — populated when match_method == AUTO_MATCH. The
    # confidence tier comes from *which* identifier matched (SKU > MPN >
    # brand+family), not from an LLM score, so we keep the human-readable
    # reason alongside the numeric score.
    matched_token = models.CharField(
        max_length=200, blank=True,
        help_text="Token that produced the winning match (e.g. SKU or MPN).",
    )
    match_details = models.JSONField(
        default=dict, blank=True,
        help_text="Structured auto-match audit: match_type, sources, reason.",
    )

    # Extraction pipeline fields
    extracted_specs = models.JSONField(default=dict, blank=True, help_text="Structured spec data extracted from this document.")
    extracted_text = models.TextField(blank=True, help_text="Full text content extracted from document.")
    extraction_status = models.CharField(
        max_length=20,
        default="pending",
        choices=[
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
    )

    class Meta:
        ordering = ["product", "document_type"]

    def __str__(self):
        return f"{self.product} — {self.title}"
