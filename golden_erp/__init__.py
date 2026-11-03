"""Fincas as an installable package.

What a downstream Django project imports from golden:

- ``golden_erp.settings.base`` — the base settings module (``from … import *``);
- ``golden_erp.urls``          — the URL patterns to ``include()``;
- ``golden_erp.paths``         — where the packaged templates and static live.

The app packages (``core``, ``orders``, …) ship next to it as top-level
packages. golden's own runnable project shell (``config/``, ``manage.py``) is
never shipped. See docs/packaging.md.
"""

from importlib.metadata import PackageNotFoundError, version


def _read_version() -> str:
    try:
        return version("fincas")
    except PackageNotFoundError:
        pass
    # Running from a source checkout that was never installed: read pyproject.
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0+unknown"


__version__ = _read_version()
