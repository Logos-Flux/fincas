"""Brand constants for document templates.

Used by base_document.html and all document-type templates.
Reads from settings.ERP_BRAND if configured, otherwise falls back to defaults.
"""

from django.conf import settings

_DEFAULT_BRAND = {
    "company_name": "Acme Commerce",
    "address_line1": "100 Main Street",
    "address_line2": "Suite 100, City, ST 00000",
    "email": "info@example.com",
    "phone": "(555) 000-0000",
    "website": "example.com",
    "logo_path": "images/logo-80.webp",
    "colors": {
        "forest": "#042e0d",
        "green": "#278040",
        "blue": "#4d65b2",
        "bronze": "#c3a859",
        "bronze_dark": "#7a6223",
    },
    "fonts": {
        "heading": "Barlow, sans-serif",
        "body": "'Source Sans 3', sans-serif",
        "mono": "'Roboto Mono', monospace",
    },
}

BRAND = getattr(settings, "ERP_BRAND", _DEFAULT_BRAND) or _DEFAULT_BRAND
