"""Template helpers for the CRM back office."""

from django import template

register = template.Library()


@register.filter
def profile_name(profile, empty="Unassigned"):
    """A staff profile's display name, else its login; ``empty`` for None;
    "(archived)" after an archived person's name (phase4-fields.md 2.5).

    Seeded and imported profiles may have no login, so this never reaches
    through ``profile.user`` blindly (a filter argument that fails to resolve
    is an error, not an empty string).
    """
    from core.staff import profile_display

    return profile_display(profile, empty)
