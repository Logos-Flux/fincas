from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "communications"

    def ready(self):
        # Validate GOLDEN_COMMUNICATION_KINDS and every plugin's kinds at boot
        # (phase3-engagement.md 2.3, 2.6): bad config or a kind claimed twice
        # fails `manage.py check` instead of the first write.
        from django.db.models.signals import post_save

        from core.provenance import track
        from parties.models import PartyEmail, PartyPhone

        from . import kinds
        from .email_link import email_log_saved
        from .models import Communication, EmailLog
        from .participants import party_email_saved, party_phone_saved

        kinds.communication_kinds()

        # Provider ids are ExternalRefs on the communication (2.2).
        track(Communication)

        # A delivery update (bounced) flips the thread's communication (2.5).
        post_save.connect(email_log_saved, sender=EmailLog, dispatch_uid="communications.email_log_status")
        # Participants waiting on an address link once exactly one party holds it.
        post_save.connect(party_email_saved, sender=PartyEmail,
                          dispatch_uid="communications.resolve_on_party_email")
        post_save.connect(party_phone_saved, sender=PartyPhone,
                          dispatch_uid="communications.resolve_on_party_phone")
