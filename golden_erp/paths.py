"""Filesystem locations of the assets golden ships inside this package.

Settings point at these instead of a project ``BASE_DIR``, so they resolve to
the same files in a source checkout and in an installed wheel.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

#: Project-level templates (base.html, includes/, erp/, registration/, …).
TEMPLATES_DIR = PACKAGE_DIR / "templates"

#: Project-level static assets (assets/css, assets/fonts, themes, …).
STATIC_DIR = PACKAGE_DIR / "static"
