"""The isolation tripwire (scripts/tripwire.sh) and the release-tree build gate.

Each test builds a throwaway git repository holding copies of the real scripts,
so the shell logic is exercised exactly as CI and scripts/release/public_tree.sh
run it.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from core import tripwire_patterns

REPO = Path(settings.BASE_DIR)
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.hooksPath=/dev/null"]


class TripwireRepo:
    def __init__(self, root: Path):
        self.root = root
        (root / "scripts" / "release").mkdir(parents=True)
        shutil.copy(REPO / "scripts" / "tripwire.sh", root / "scripts" / "tripwire.sh")
        shutil.copy(
            REPO / "scripts" / "release" / "public_tree.sh",
            root / "scripts" / "release" / "public_tree.sh",
        )
        (root / "scripts" / "release" / "CLAUDE.public.md").write_text("# public\n")
        (root / "scripts" / "release" / "dev-only.txt").write_text(
            "HANDOFF.md\nscripts/release/tripwire-private.txt\n"
        )
        subprocess.run([*GIT, "init", "-q", "-b", "main"], cwd=root, check=True)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self) -> None:
        subprocess.run([*GIT, "add", "-A"], cwd=self.root, check=True)
        subprocess.run([*GIT, "commit", "-q", "-m", "c"], cwd=self.root, check=True)

    def run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", *args], cwd=self.root, capture_output=True, text=True,
            env=None if env is None else {**_base_env(), **env},
        )


def _base_env() -> dict:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("TRIPWIRE_")}


class TripwireScriptTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = TripwireRepo(self.tmp / "repo")

    def test_no_pattern_files_passes(self):
        self.repo.write("app.py", "x = 1\n")
        self.repo.commit()
        result = self.repo.run("scripts/tripwire.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_trailing_whitespace_on_a_pattern_still_matches(self):
        self.repo.write("scripts/release/tripwire-private.txt", "[shipping]\nacme-corp \n")
        self.repo.write("app.py", "see acme-corp.com here\n")
        self.repo.commit()
        result = self.repo.run("scripts/tripwire.sh")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("app.py:1", result.stdout)

    def test_shipping_tier_ignores_dev_only_files_and_all_tier_does_not(self):
        self.repo.write(
            "scripts/release/tripwire-private.txt", "[all]\nsecret-id\n[shipping]\nhostname-x\n"
        )
        self.repo.write("HANDOFF.md", "ran on hostname-x\n")
        self.repo.commit()
        self.assertEqual(self.repo.run("scripts/tripwire.sh").returncode, 0)
        self.repo.write("HANDOFF.md", "ran on hostname-x with secret-id\n")
        self.repo.commit()
        self.assertEqual(self.repo.run("scripts/tripwire.sh").returncode, 1)

    def test_pattern_file_outside_the_checkout(self):
        outside = self.tmp / "patterns.txt"
        outside.write_text("[shipping]\nleak-name\n")
        self.repo.write("app.py", "leak-name\n")
        self.repo.commit()
        result = self.repo.run("scripts/tripwire.sh", env={"TRIPWIRE_PATTERN_FILES": str(outside)})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_double_dash_pattern_is_refused(self):
        self.repo.write("scripts/release/tripwire-private.txt", "[shipping]\n--\nleak-name\n")
        self.repo.commit()
        self.assertEqual(self.repo.run("scripts/tripwire.sh").returncode, 2)

    def test_release_build_still_checks_the_dev_only_patterns(self):
        """The build removes tripwire-private.txt before its tripwire run."""
        self.repo.write("scripts/release/tripwire-private.txt", "[shipping]\nleak-name\n")
        self.repo.write("core/probe.txt", "host leak-name\n")
        self.repo.commit()
        result = self.repo.run("scripts/release/public_tree.sh", "--build", "9.9.9", "--from", "HEAD")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("core/probe.txt", result.stdout)


class PatternLoaderTests(SimpleTestCase):
    def test_loader_strips_like_the_shell(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / tripwire_patterns.PATTERN_GLOB.replace("*", "x")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(" [shipping] \n  acme-corp  \n")
            self.assertEqual(tripwire_patterns.load(root)["shipping"], ["acme-corp"])
