"""Exit codes shared by the release commands (docs/deployment.md, "Release
command exit codes").

The downstream's runbook calls these commands as ``cmd || rc=$?`` and
branches on the number, so the table is defined once, here, and every
release command imports it rather than picking its own:

====  ==========  ==================================================
code  name        meaning
====  ==========  ==================================================
0     OK          every check passed; keep going
2     USAGE       bad arguments or an environment the command refuses
                  (a shallow clone, an unknown ref); nothing was checked
10    DEGRADED    something needs a person but the release can stay up
                  (dead outbox rows, drift a ``--fix`` repairs)
20    ROLL_BACK   the release must not stay up (two companies, unapplied
                  migrations, no staff login); see the cutover runbook §12
====  ==========  ==================================================

``smoke_cutover`` exits with the worst status of its checks and prints,
then flushes, its guidance before exiting, so a wrapper that captures stdout
sees the reason even when it only reads ``$?``.
"""

from __future__ import annotations

OK = 0
USAGE = 2
DEGRADED = 10
ROLL_BACK = 20

#: Status word printed per check, in severity order.
STATUS_OK = "OK"
STATUS_DEGRADED = "DEGRADED"
STATUS_ROLL_BACK = "ROLL BACK"

EXIT_FOR_STATUS = {
    STATUS_OK: OK,
    STATUS_DEGRADED: DEGRADED,
    STATUS_ROLL_BACK: ROLL_BACK,
}


def worst(statuses) -> str:
    """The most severe of the status words (``OK`` when there are none)."""
    order = [STATUS_OK, STATUS_DEGRADED, STATUS_ROLL_BACK]
    worst_seen = STATUS_OK
    for status in statuses:
        if order.index(status) > order.index(worst_seen):
            worst_seen = status
    return worst_seen
