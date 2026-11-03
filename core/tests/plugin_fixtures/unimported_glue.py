"""Read (never imported) by the settings-time classifier test.

The classifier must spot a BasePlugin subclass from source alone, including
one that inherits through a local base class.
"""

from core.plugins.base import BasePlugin


class LocalBase(BasePlugin):
    name = "local_base_fixture"


class IndirectPlugin(LocalBase):
    name = "indirect_fixture"


class NotAPlugin:
    pass
