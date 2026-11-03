"""The API's exception handler (``REST_FRAMEWORK["EXCEPTION_HANDLER"]``).

DRF's default handler translates its own ``APIException`` plus Django's
``Http404`` and ``PermissionDenied``; everything else is a 500. A ``DELETE``
of a row that another row references through ``on_delete=PROTECT`` (or
``RESTRICT``) raises the ORM's ``ProtectedError`` inside ``perform_destroy``,
which the access matrix saw as a 500 on Company, Customer, Order, Product,
ProductVariant and Supplier. Here it is a 409 with a stable ``code`` and the
protecting relations, for every viewset at once. The envelope for
everything else is unchanged (``core/tests/test_api_error_envelope.py``).
"""

from __future__ import annotations

from django.db.models.deletion import ProtectedError, RestrictedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

PROTECTED_CODE = "protected"


def protecting_relations(exc) -> list[str]:
    """The model labels (``app.Model``) that hold the referencing rows."""
    objects = getattr(exc, "protected_objects", None) or getattr(exc, "restricted_objects", None) or ()
    return sorted({obj._meta.label for obj in objects})


def exception_handler(exc, context):
    if isinstance(exc, (ProtectedError, RestrictedError)):
        relations = protecting_relations(exc)
        names = ", ".join(relations) or "other records"
        return Response(
            {
                "code": PROTECTED_CODE,
                "detail": f"Cannot delete: referenced by {names}.",
                "protected_by": relations,
            },
            status=status.HTTP_409_CONFLICT,
        )
    return drf_exception_handler(exc, context)
