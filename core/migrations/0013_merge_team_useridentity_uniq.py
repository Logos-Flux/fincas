"""Merge the two core 0012 leaves.

#79 (``0012_team``) and #81 (``0012_useridentity_user_provider_uniq``) both
branched from ``0011_remove_userprofile_supabase_uid`` and landed the same
day, leaving two leaf nodes: ``migrate`` and ``makemigrations --check`` then
refuse ("Conflicting migrations detected"). No operations.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_team"),
        ("core", "0012_useridentity_user_provider_uniq"),
    ]

    operations = []
