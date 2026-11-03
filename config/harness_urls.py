"""The release harness's root URLconf: ``golden_erp.urls`` plus the harness-only routes.

Only ``config.settings.harness`` points ROOT_URLCONF here. Prod and dev
settings resolve ``config.urls``, which mounts none of ``/harness/``
(``core/tests/test_harness_settings.py`` keeps it so).
"""

from django.urls import path

import golden_erp.urls as _golden
from config import harness_views as v

urlpatterns = [
    # The dev stub identity provider (Supabase-shaped; see harness settings).
    path("harness/idp/.well-known/jwks.json", v.idp_jwks, name="harness-idp-jwks"),
    path("harness/idp/auth/v1/admin/users/<str:subject>", v.idp_admin_user, name="harness-idp-admin-user"),
    path("harness/idp/token/", v.idp_token, name="harness-idp-token"),
    # Server-error capture fixture: a redirect hides the 500 from the page.
    path("harness/boom/", v.boom, name="harness-boom"),
    path("harness/boom/raise/", v.boom_raise, name="harness-boom-raise"),
    # Local webhook receiver (dedupes by X-Golden-Delivery-Id).
    path("harness/webhooks/", v.webhooks, name="harness-webhooks"),
    # Read-only database facts the journeys assert (gl/ before the generic route).
    path("harness/state/gl/", v.state_gl, name="harness-state-gl"),
    path("harness/state/<str:label>/", v.state, name="harness-state"),
    *_golden.urlpatterns,
]

# Styled error handlers, when the packaged URLconf declares them (they must be
# named in the ROOT_URLCONF module to take effect).
for _name in ("handler400", "handler403", "handler404", "handler500"):
    if hasattr(_golden, _name):
        globals()[_name] = getattr(_golden, _name)
