"""Session-login signals — link Django users to UserProfile (AD-12, F-026).

``scope_to_company`` resolves the company via ``user.userprofile.company``, but
``UserProfile`` historically linked Supabase uid only — so every ``/erp/``
session login failed closed to zero rows. On a staff login we link (or create) the
profile and, when exactly one Company exists, attach it. With two or more
companies we never guess: the profile stays company-less (fail closed) until an
admin assigns one.
"""

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver


def resolve_session_company(user):
    """Link ``user`` to a UserProfile; auto-attach the company iff unique."""
    from .models import Company, UserProfile

    # No placeholder uid (it used to get "session:<pk>"): the legacy column
    # is gone since identity step 8.
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.company_id is None and Company.objects.count() == 1:
        profile.company = Company.objects.first()
        profile.save(update_fields=["company", "updated_at"])
    return profile


@receiver(user_logged_in)
def link_profile_on_login(sender, request, user, **kwargs):
    # Staff only (identity-unification §4): a UserProfile is the back-office
    # profile and company-scoping anchor. A non-staff session (a future
    # customer login) must never mint one.
    from .permissions import is_staff_principal

    if is_staff_principal(user):
        resolve_session_company(user)
