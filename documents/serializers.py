from django.conf import settings
from rest_framework import serializers

from .models import ProductDocument

# Extensions the document pipeline knows how to store/index. Anything else
# (executables, scripts, extensionless blobs) is rejected at the API.
DOCUMENT_ALLOWED_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "csv", "txt",
    "png", "jpg", "jpeg", "zip",
})

DEFAULT_DOCUMENT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class ProductDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductDocument
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_file_size(self, value):
        max_bytes = getattr(
            settings, "DOCUMENT_MAX_UPLOAD_BYTES", DEFAULT_DOCUMENT_MAX_UPLOAD_BYTES
        )
        if value is not None and value > max_bytes:
            raise serializers.ValidationError(
                f"File size {value} bytes exceeds the "
                f"{max_bytes}-byte upload cap."
            )
        return value

    def validate_r2_key(self, value):
        ext = value.rsplit(".", 1)[-1].lower() if "." in value else ""
        if ext not in DOCUMENT_ALLOWED_EXTENSIONS:
            raise serializers.ValidationError(
                f"Extension '.{ext}' is not allowed "
                f"(allowed: {', '.join(sorted(DOCUMENT_ALLOWED_EXTENSIONS))})."
            )
        return value
