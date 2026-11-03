"""Gmail timeline adapter (docs/crm/phase6-adapters.md 3.3, step 4).

Reads a staff user's own mailbox through a per-user, read-only OAuth grant
and logs the mail that involves a **known external party** as ``email``
communications. Install with::

    GOLDEN_PLUGINS = [..., "golden_erp.addons.gmail.plugin.GmailPlugin"]

Golden never sends through Gmail and writes nothing into it.
"""
