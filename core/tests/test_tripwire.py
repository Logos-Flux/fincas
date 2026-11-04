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

    def commit(self, message: str = "c") -> None:
        subprocess.run([*GIT, "add", "-A"], cwd=self.root, check=True)
        subprocess.run([*GIT, "commit", "-q", "-m", message], cwd=self.root, check=True)

    def git(self, *args: str) -> str:
        return subprocess.run(
            [*GIT, *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

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


class ParentedReleaseBuildTests(SimpleTestCase):
    """--build --parent: a release commit on top of public main, with the revert guard."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = TripwireRepo(self.tmp / "repo")
        self.repo.write("app.py", "x = 1\n")
        self.repo.write("docs/guide.md", "guide v1\nb\nc\nd\ne\n")
        self.repo.write("HANDOFF.md", "dev only\n")
        self.repo.commit()
        first = self.build("1.0.0")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.first_release = self.repo.git("rev-parse", "release-v1.0.0")
        # Public main: the first release, then a fix merged on the public repo.
        # Public PRs are authored as the public identity too, so the guard must
        # not mistake them for releases.
        self.repo.git("checkout", "-q", "-b", "public", "release-v1.0.0")

    def build(self, version: str, *extra: str) -> subprocess.CompletedProcess:
        return self.repo.run(
            "scripts/release/public_tree.sh", "--build", version, "--from", "main", *extra
        )

    def public_commit(self, message: str) -> None:
        subprocess.run(
            [*GIT, "-c", "user.name=Logos Flux", "-c", "user.email=lf@logosflux.io", "add", "-A"],
            cwd=self.repo.root, check=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=Logos Flux", "-c", "user.email=lf@logosflux.io",
             "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", message],
            cwd=self.repo.root, check=True,
        )

    def dev(self) -> None:
        self.repo.git("checkout", "-q", "main")

    def test_parented_commit_has_the_parent_tree_and_identity(self):
        self.repo.write("README.md", "public readme\n")
        self.public_commit("docs: readme")
        public_head = self.repo.git("rev-parse", "public")
        self.dev()
        self.repo.write("README.md", "public readme\n")
        self.repo.write("app.py", "x = 2\n")
        self.repo.commit()
        main_sha = self.repo.git("rev-parse", "main")
        result = self.build("1.0.1", "--parent", "public")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        branch = "release-v1.0.1"
        self.assertEqual(self.repo.git("rev-list", "--parents", "-n1", branch).split()[1:], [public_head])
        self.assertEqual(
            self.repo.git("log", "-1", "--format=%an <%ae>|%cn <%ce>|%s", branch),
            "Logos Flux <lf@logosflux.io>|Logos Flux <lf@logosflux.io>|Release v1.0.1",
        )
        self.assertIn(f"development history at {main_sha} (main)", self.repo.git("log", "-1", "--format=%b", branch))
        files = set(self.repo.git("ls-tree", "-r", "--name-only", branch).split())
        self.assertEqual(
            files,
            {"app.py", "docs/guide.md", "README.md", "CLAUDE.md", "scripts/tripwire.sh",
             "scripts/release/public_tree.sh", "scripts/release/CLAUDE.public.md",
             "scripts/release/dev-only.txt"},
        )
        self.assertEqual(self.repo.git("show", f"{branch}:app.py"), "x = 2")
        self.assertEqual(self.repo.git("show", f"{branch}:CLAUDE.md"), "# public")
        self.assertEqual(self.repo.git("symbolic-ref", "HEAD"), f"refs/heads/{branch}")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")
        self.assertIn(f"last release {self.first_release[:7]}", result.stdout)
        self.assertIn("public-only paths checked: 1", result.stdout)
        self.assertIn("app.py", result.stdout)  # the diff --stat against public main

        # The next release finds this one as its base, not the first.
        self.repo.git("checkout", "-q", "-B", "public", branch)
        self.repo.write("docs/guide.md", "guide public\n")
        self.public_commit("docs: guide")
        self.dev()
        self.repo.write("docs/guide.md", "guide public\n")
        self.repo.commit()
        again = self.build("1.0.2", "--parent", "public")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn(f"last release {self.repo.git('rev-parse', '--short=7', branch)}", again.stdout)

    def test_public_only_change_missing_from_dev_fails_and_names_it(self):
        self.repo.write("docs/guide.md", "guide fixed on public\n")
        self.repo.write("README.md", "public readme\n")
        self.public_commit("Release v9.9.9 lookalike without the squash body")
        self.dev()
        result = self.build("1.0.1", "--parent", "public")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("docs/guide.md  (changed on public main; the release has a different version)", result.stdout)
        self.assertIn("README.md  (added on public main; the release lacks it)", result.stdout)
        self.assertIn("--allow-revert", result.stdout)
        # The lookalike subject did not count as a release.
        self.assertIn(f"last release {self.first_release[:7]}", result.stdout)
        self.assertFalse(self.repo.git("branch", "--list", "release-v1.0.1"))

    def test_allow_revert_lets_the_release_supersede_a_public_change(self):
        self.repo.write("docs/guide.md", "guide fixed on public\n")
        self.public_commit("docs: guide")
        self.dev()
        result = self.build("1.0.1", "--parent", "public", "--allow-revert", "docs/guide.md")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("allowed revert: docs/guide.md", result.stdout)
        self.assertEqual(self.repo.git("show", "release-v1.0.1:docs/guide.md"), "guide v1\nb\nc\nd\ne")

    def test_public_change_ported_to_dev_passes(self):
        self.repo.write("docs/guide.md", "guide fixed on public\n")
        self.public_commit("docs: guide")
        self.dev()
        self.repo.write("docs/guide.md", "guide fixed on public\n")
        self.repo.commit()
        result = self.build("1.0.1", "--parent", "public")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("allowed revert", result.stdout)

    def test_public_deletion_must_stay_deleted(self):
        self.repo.git("rm", "-q", "docs/guide.md")
        self.public_commit("docs: drop the guide")
        self.dev()
        result = self.build("1.0.1", "--parent", "public")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("docs/guide.md  (deleted on public main; the release would bring it back)", result.stdout)
        self.repo.git("checkout", "-q", "-f", "main")
        self.repo.git("rm", "-q", "docs/guide.md")
        self.repo.commit()
        ported = self.build("1.0.1", "--parent", "public")
        self.assertEqual(ported.returncode, 0, ported.stdout + ported.stderr)
        self.assertNotIn("docs/guide.md", self.repo.git("ls-tree", "-r", "--name-only", "release-v1.0.1"))

    def test_ported_change_with_further_dev_edits_is_flagged_with_a_hint(self):
        self.repo.write("docs/guide.md", "Guide\nb\nc\nd\ne\n")
        self.public_commit("docs: guide")
        self.dev()
        self.repo.write("docs/guide.md", "Guide\nb\nc\nd\nmore in dev\n")
        self.repo.write("app.py", "x = 2\n")  # dev-only change: not public's business
        self.repo.commit()
        result = self.build("1.0.1", "--parent", "public")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("docs/guide.md  (changed on public main; the release's version contains that change", result.stdout)
        self.assertNotIn("app.py", result.stdout.split("FAIL")[1])  # public never touched it

    def test_public_change_deleted_in_dev_is_flagged(self):
        self.repo.write("docs/guide.md", "guide fixed on public\n")
        self.public_commit("docs: guide")
        self.dev()
        self.repo.git("rm", "-q", "docs/guide.md")
        self.repo.commit()
        result = self.build("1.0.1", "--parent", "public")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("docs/guide.md  (changed on public main; the release would delete it)", result.stdout)

    def test_mode_change_on_public_is_guarded(self):
        (self.repo.root / "app.py").chmod(0o755)
        self.public_commit("chore: executable")
        self.dev()
        result = self.build("1.0.1", "--parent", "public")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("app.py", result.stdout)

    def test_published_docs_images_survive_the_build_sweep(self):
        """docs/images/ is public on purpose: --check allows it, so the build must too."""
        self.dev()
        self.repo.write("docs/images/screenshots/shot.png", "png")
        self.repo.commit()
        result = self.build("1.0.1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("docs/images/screenshots/shot.png", self.repo.git("ls-tree", "-r", "--name-only", "release-v1.0.1"))

    def test_orphan_mode_is_unchanged(self):
        self.dev()
        self.repo.write("app.py", "x = 3\n")
        self.repo.commit()
        result = self.build("1.0.1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.repo.git("rev-list", "--count", "release-v1.0.1"), "1")
        self.assertEqual(self.repo.git("log", "-1", "--format=%an <%ae>|%s", "release-v1.0.1"),
                         "Logos Flux <lf@logosflux.io>|Release v1.0.1")
        self.assertNotIn("HANDOFF.md", self.repo.git("ls-tree", "-r", "--name-only", "release-v1.0.1"))
        self.assertIn("(orphan)", result.stdout)

    def test_allow_revert_without_parent_is_a_usage_error(self):
        self.dev()
        result = self.build("1.0.1", "--allow-revert", "docs/guide.md")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--allow-revert needs --parent", result.stdout)

    def test_parent_without_a_release_commit_is_refused_before_checkout(self):
        self.dev()
        self.repo.git("checkout", "-q", "--orphan", "stranger")
        self.repo.commit("unrelated")
        self.dev()
        result = self.build("1.0.1", "--parent", "stranger")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.repo.git("symbolic-ref", "HEAD"), "refs/heads/main")


class PatternLoaderTests(SimpleTestCase):
    def test_loader_strips_like_the_shell(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / tripwire_patterns.PATTERN_GLOB.replace("*", "x")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(" [shipping] \n  acme-corp  \n")
            self.assertEqual(tripwire_patterns.load(root)["shipping"], ["acme-corp"])
