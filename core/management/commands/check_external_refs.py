"""Report ExternalRefs whose target row no longer exists (phase2-party.md 1.7).

``ExternalRef`` has a generic target, so the database cannot stop a ref from
outliving its row. ``core.provenance.track`` deletes refs on a hard delete
through the ORM; this command finds the ones that slipped past it (raw SQL,
an untracked model, a model whose app was uninstalled). CI runs it on the
seeded database.

Read-only. Exit 0 when every ref resolves, 1 when any dangles.
"""

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError

from core.models import ExternalRef

_SAMPLE = 10


class Command(BaseCommand):
    help = "Report ExternalRefs whose target row is gone (exit 1 if any)."

    def handle(self, *args, **options):
        total = ExternalRef.objects.count()
        dangling_total = 0
        content_type_ids = (
            ExternalRef.objects.order_by().values_list("content_type_id", flat=True).distinct()
        )
        for content_type in ContentType.objects.filter(pk__in=list(content_type_ids)).order_by(
            "app_label", "model"
        ):
            refs = ExternalRef.objects.filter(content_type=content_type)
            model = content_type.model_class()
            label = f"{content_type.app_label}.{content_type.model}"
            if model is None:
                reason = "model is not installed"
                dangling = refs
            elif model._meta.pk.get_internal_type() != "UUIDField":
                reason = "model has no UUID primary key"
                dangling = refs
            else:
                reason = "target row missing"
                dangling = refs.exclude(object_id__in=model._base_manager.values("pk"))
            count = dangling.count()
            if not count:
                continue
            dangling_total += count
            self.stdout.write(f"{label}: {count} dangling ref(s) ({reason})")
            for ref in dangling.order_by("source", "object_type", "source_id")[:_SAMPLE]:
                self.stdout.write(
                    f"  {ref.source}:{ref.object_type}:{ref.source_id} -> {ref.object_id} "
                    f"(company {ref.company_id})"
                )
            if count > _SAMPLE:
                self.stdout.write(f"  … and {count - _SAMPLE} more")

        self.stdout.write(f"{total} external ref(s) checked, {dangling_total} dangling.")
        if dangling_total:
            raise CommandError(
                f"{dangling_total} dangling external ref(s). Register the target model with "
                "core.provenance.track, or delete the refs on purpose.",
                returncode=1,
            )
