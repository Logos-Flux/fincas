"""Default pipeline seeding (docs/crm/phase3-engagement.md 1.2).

Every Company gets the configured default pipelines
(``crm.registries.default_pipelines()``: golden's ``sales`` pipeline unless
``GOLDEN_DEFAULT_PIPELINES`` replaces it). Seeding runs from the data
migration ``crm.0008`` for existing companies and from a ``post_save`` on
Company creation (``CrmConfig.ready``). After seeding, pipelines are tenant
data: seeding never edits, re-adds or re-orders anything that exists.

The functions take the model classes as arguments so the data migration can
pass its historical models.
"""

from __future__ import annotations

from django.db import transaction

from .registries import default_pipelines


def seed_default_pipelines(company, *, pipeline_model=None, stage_model=None, seeds=None) -> int:
    """Create the missing default pipelines for ``company``. Idempotent.

    A pipeline whose ``key`` already exists for the company is left alone
    (its stages too). The first seed becomes the default only when the
    company has no default yet. Returns the number of pipelines created.
    """
    if pipeline_model is None or stage_model is None:
        from .models import Pipeline, Stage

        pipeline_model = pipeline_model or Pipeline
        stage_model = stage_model or Stage
    seeds = default_pipelines() if seeds is None else seeds
    created = 0
    with transaction.atomic():
        existing = set(
            pipeline_model._base_manager.filter(company_id=company.pk).values_list("key", flat=True)
        )
        has_default = pipeline_model._base_manager.filter(
            company_id=company.pk, is_default=True
        ).exists()
        for order, seed in enumerate(seeds):
            if seed.key in existing:
                continue
            pipeline = pipeline_model._base_manager.create(
                company_id=company.pk, key=seed.key, name=seed.name,
                is_default=not has_default, sort_order=order,
            )
            has_default = True
            stage_model._base_manager.bulk_create(
                stage_model(
                    pipeline_id=pipeline.pk, company_id=company.pk, key=stage.key,
                    name=stage.name, sort_order=(i + 1) * 10,
                    default_probability=stage.default_probability,
                    outcome_hint=stage.outcome_hint,
                )
                for i, stage in enumerate(seed.stages)
            )
            created += 1
    return created


def seed_all_companies(*, company_model=None, pipeline_model=None, stage_model=None) -> int:
    """Seed every company (the data migration's forward step)."""
    if company_model is None:
        from core.models import Company

        company_model = Company
    return sum(
        seed_default_pipelines(company, pipeline_model=pipeline_model, stage_model=stage_model)
        for company in company_model._base_manager.order_by("pk")
    )


def append_stage(seed_key, stage, *, pipeline_model=None, stage_model=None) -> int:
    """Add ``stage`` (a ``StageSeed``) at the end of every pipeline keyed
    ``seed_key`` that lacks its key. The one exception to "seeding never
    edits a pipeline": a data migration shipping a new default stage
    (``crm.0019``). A pipeline that already ends in a stage of the same
    outcome (a tenant's own closing stage, whatever its key) is left alone.
    Returns the number of stages created."""
    from django.db.models import Max

    if pipeline_model is None or stage_model is None:
        from .models import Pipeline, Stage

        pipeline_model = pipeline_model or Pipeline
        stage_model = stage_model or Stage
    created = 0
    with transaction.atomic():
        pipelines = pipeline_model._base_manager.filter(key=seed_key).exclude(
            stages__key=stage.key,
        )
        if stage.outcome_hint:
            pipelines = pipeline_models_without_outcome(pipelines, stage.outcome_hint)
        pipelines = pipelines.annotate(last=Max("stages__sort_order")).values_list("pk", "company_id", "last")
        for pipeline_id, company_id, last in pipelines:
            stage_model._base_manager.create(
                pipeline_id=pipeline_id, company_id=company_id, key=stage.key, name=stage.name,
                sort_order=min((last or 0) + 10, SORT_ORDER_MAX), default_probability=stage.default_probability,
                outcome_hint=stage.outcome_hint,
            )
            created += 1
    return created


#: ``Stage.sort_order`` is a PositiveSmallIntegerField.
SORT_ORDER_MAX = 32767


def pipeline_models_without_outcome(pipelines, outcome_hint):
    """``pipelines`` that have no active stage proposing ``outcome_hint``."""
    return pipelines.exclude(stages__outcome_hint=outcome_hint, stages__is_active=True)


def company_created(sender, instance, created=False, raw=False, **kwargs):
    """``post_save`` on Company: a new tenant starts with its default pipeline."""
    if created and not raw:
        seed_default_pipelines(instance)


def default_pipeline(company):
    """The company's default pipeline, or ``None``."""
    from .models import Pipeline

    return Pipeline.objects.for_company(company).filter(is_default=True).first()
