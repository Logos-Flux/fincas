"""Cache invalidation + the audit backstop for writes that bypass the views."""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from . import audit, conf
from .models import LLMModel, LLMSettings, LLMSettingsAudit

# Bookkeeping columns the page itself writes on every test / models refresh.
_QUIET_FIELDS = {"last_test", "models_cache", "models_cache_at", "updated_at", "created_at", "id"}
_SECRET_FIELDS = {"gateway_api_key_enc"}


def _invalidate_now_and_on_commit() -> None:
    # Now, for this thread; and again after commit, because another thread
    # rebuilding between the save and the commit would cache the old rows for
    # a whole TTL.
    from django.db import transaction

    conf.invalidate()
    transaction.on_commit(conf.invalidate)


def _values(instance) -> dict:
    return {f.attname: getattr(instance, f.attname) for f in instance._meta.concrete_fields}


@receiver(pre_save, sender=LLMSettings)
@receiver(pre_save, sender=LLMModel)
def _stash_old(sender, instance, **kwargs):
    old = sender.objects.filter(pk=instance.pk).first() if instance.pk else None
    instance._llmhub_old = _values(old) if old is not None else None


@receiver(post_save, sender=LLMSettings)
@receiver(post_save, sender=LLMModel)
def _backstop(sender, instance, created, raw=False, **kwargs):
    _invalidate_now_and_on_commit()
    if raw or audit.in_view_write():
        return
    old = getattr(instance, "_llmhub_old", None)
    if old is None:
        # A new row is audited against the defaults it would have had, so
        # creating the settings row with defaults records nothing.
        old = _values(sender())
    new = _values(instance)
    label = f"{sender.__name__}"
    if sender is LLMModel:
        label += f"[{instance.key}]"
    if created and sender is LLMModel:
        audit.record(LLMSettingsAudit.MODEL_ADDED, field=label, new=instance.route, detail="created outside the settings page")
        return
    for name, value in new.items():
        if name in _QUIET_FIELDS or old.get(name) == value:
            continue
        action = LLMSettingsAudit.FIELD_CHANGED
        if name == "fallback_to_env":
            action = LLMSettingsAudit.FALLBACK_TOGGLED
        audit.record(
            action,
            field=f"{label}.{name}",
            old=old.get(name),
            new=value,
            secret=name in _SECRET_FIELDS,
            detail="changed outside the settings page (admin / shell / fixture)",
        )


@receiver(post_delete, sender=LLMModel)
def _deleted(sender, instance, **kwargs):
    _invalidate_now_and_on_commit()
    if audit.in_view_write():
        return
    audit.record(
        LLMSettingsAudit.MODEL_DELETED,
        field=f"LLMModel[{instance.key}]",
        old=instance.route,
        detail="deleted outside the settings page",
    )
