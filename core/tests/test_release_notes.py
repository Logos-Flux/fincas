"""``manage.py release_notes`` against a throwaway git repository.

The repository is built in a temporary directory with two commits: the
first holds a settings file and a ``core/checks.py`` without
``DEPLOY_SECRETS``; the second adds a schema migration, a one-way
``RunPython`` migration, a reversible one, the ``DEPLOY_SECRETS`` dict, a
settings assignment and an environment read. ``file://`` clones stay on
disk, so the no-network runner is happy.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase

from core import release_notes
from core.management import release_exit_codes as rc

SCHEMA_MIGRATION = '''
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0007_x")]
    operations = [
        migrations.CreateModel(
            name="DocumentContact",
            fields=[("id", models.UUIDField(primary_key=True))],
        ),
    ]
'''

ONE_WAY_MIGRATION = '''
from decimal import Decimal

from django.db import migrations


def add_stage(apps, schema_editor):
    Stage = apps.get_model("crm", "Stage")
    Stage.objects.create(name="Closed", probability=Decimal("1"))


class Migration(migrations.Migration):
    dependencies = [("crm", "0018_x")]
    operations = [migrations.RunPython(add_stage, migrations.RunPython.noop)]
'''

REVERSIBLE_MIGRATION = '''
from django.db import migrations, models


def forward(apps, schema_editor):
    pass


def backward(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("crm", "0019_x")]
    operations = [
        migrations.AlterField(model_name="lead", name="source", field=models.CharField(max_length=20)),
        migrations.RunPython(forward, reverse_code=backward),
    ]
'''

ALTER_ONLY_MIGRATION = '''
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("parties", "0007_x")]
    operations = [
        migrations.AlterField(model_name="party", name="kind", field=models.CharField(max_length=20)),
    ]
'''

CHECKS_BEFORE = '''
from django.core.checks import Error, register


@register()
def nothing(app_configs, **kwargs):
    return []
'''

CHECKS_AFTER = '''
from django.core.checks import Error, register

DEPLOY_SECRETS = {
    "API_JWT_SIGNING_KEY": "signs API bearer tokens",
    "GOLDEN_SECRET_KEYS": "encrypts connected-account credentials",
}


@register()
def nothing(app_configs, **kwargs):
    return []
'''

SETTINGS_BEFORE = '''
import os

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "x")
DISABLE_SERVER_SIDE_CURSORS = True
'''

SETTINGS_AFTER = '''
import os

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "x")
GOLDEN_REQUIRE_DEPLOY_SECRETS = os.environ.get("GOLDEN_REQUIRE_DEPLOY_SECRETS") == "1"
'''

APP_AFTER = '''
import os

from django.conf import settings

EXCLUDED = getattr(settings, "GOLDEN_DEMO_ROLL_EXCLUDE", ())
LEGACY = os.getenv("GOLDEN_LEGACY_FLAG")
'''


def _git(repo, *args):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def _write(repo, path, text):
    p = Path(repo) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "core/checks.py", CHECKS_BEFORE)
    _write(repo, "golden_erp/settings/base.py", SETTINGS_BEFORE)
    _write(repo, "orders/migrations/0007_x.py", ALTER_ONLY_MIGRATION.replace("parties", "orders"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "chore: initial")
    _git(repo, "tag", "v0.4.0-rc.4")
    _write(repo, "core/checks.py", CHECKS_AFTER)
    _write(repo, "golden_erp/settings/base.py", SETTINGS_AFTER)
    _write(repo, "core/demo_clock.py", APP_AFTER)
    _write(repo, "core/tests/test_demo_clock.py", 'X = getattr(settings, "ONLY_IN_TESTS", 1)\n')
    _write(repo, "orders/migrations/0008_document_contact.py", SCHEMA_MIGRATION)
    _write(repo, "crm/migrations/0019_closed_signed_stage.py", ONE_WAY_MIGRATION)
    _write(repo, "crm/migrations/0020_reversible.py", REVERSIBLE_MIGRATION)
    _write(repo, "parties/migrations/0008_labels.py", ALTER_ONLY_MIGRATION)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat(deploy): production refuses to migrate without its own keys")
    _write(repo, "README.md", "x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "fix(money): show USD as $1,234.56")
    _write(repo, "README.md", "x\ny\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "perf(list): fewer queries on the order list (a-downstream OP #3221) (#7)")
    # A tripwire pattern file (as the dev repo keeps one) withholds subjects
    # that name private infrastructure.
    _write(repo, "scripts/release/tripwire-private.txt", "# test\n[all]\nacme-secret\n[shipping]\ndb\\.internal\\.example\n")
    _write(repo, "README.md", "x\ny\nz\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs: the test database moved to db.internal.example")
    return repo


class ReleaseNotesTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build_repo(Path(cls._tmp.name))
        cls.notes = release_notes.collect(cls.repo, "v0.4.0-rc.4", "HEAD")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def test_migrations_added_and_classified(self):
        by_label = {m.label: m for m in self.notes.migrations_added}
        self.assertEqual(
            sorted(by_label),
            ["crm.0019_closed_signed_stage", "crm.0020_reversible",
             "orders.0008_document_contact", "parties.0008_labels"],
        )
        self.assertEqual(by_label["orders.0008_document_contact"].kind, "schema")
        self.assertEqual(by_label["orders.0008_document_contact"].reverse, "-")
        self.assertEqual(by_label["parties.0008_labels"].kind, "state (AlterField only)")
        self.assertEqual(by_label["crm.0019_closed_signed_stage"].kind, "data")
        self.assertEqual(by_label["crm.0019_closed_signed_stage"].reverse, "one-way (RunPython reverse is noop)")
        self.assertEqual(by_label["crm.0020_reversible"].kind, "data + schema")
        self.assertEqual(by_label["crm.0020_reversible"].reverse, "reversible")
        # Decimal(...) inside the helper is not an operation.
        self.assertEqual(by_label["crm.0019_closed_signed_stage"].operations, ["RunPython"])
        self.assertEqual([m.label for m in self.notes.one_way], ["crm.0019_closed_signed_stage"])

    def test_run_python_without_reverse_is_one_way(self):
        ops, one_way = release_notes.classify_migration(
            "from django.db import migrations\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.RunPython(lambda a, s: None), migrations.RunSQL('x')]\n"
        )
        self.assertEqual(ops, ["RunPython", "RunSQL"])
        self.assertEqual(one_way, ["RunPython without a reverse", "RunSQL without reverse_sql"])

    def test_deploy_secrets_new_keys(self):
        self.assertEqual(self.notes.secrets_new, ["API_JWT_SIGNING_KEY", "GOLDEN_SECRET_KEYS"])
        self.assertEqual(self.notes.secrets_to["API_JWT_SIGNING_KEY"], "signs API bearer tokens")
        self.assertEqual(release_notes.deploy_secrets(self.repo, "v0.4.0-rc.4"), {})

    def test_settings_and_env_reads(self):
        self.assertEqual(self.notes.settings_new, ["GOLDEN_REQUIRE_DEPLOY_SECRETS"])
        self.assertEqual(self.notes.settings_removed, ["DISABLE_SERVER_SIDE_CURSORS"])
        # DJANGO_SECRET_KEY was read before; ONLY_IN_TESTS is under tests/.
        self.assertEqual(
            self.notes.env_reads_new,
            ["GOLDEN_DEMO_ROLL_EXCLUDE", "GOLDEN_LEGACY_FLAG", "GOLDEN_REQUIRE_DEPLOY_SECRETS"],
        )

    def test_commits_grouped(self):
        self.assertEqual(self.notes.commits["feat"], ["feat(deploy): production refuses to migrate without its own keys"])
        self.assertEqual(self.notes.commits["fix"], ["fix(money): show USD as $1,234.56"])
        # A downstream's tracker reference is dropped; the PR suffix stays.
        self.assertEqual(self.notes.commits["perf"], ["perf(list): fewer queries on the order list (#7)"])
        self.assertNotIn("chore", self.notes.commits)
        # A subject matching a tripwire pattern is counted, never listed.
        self.assertNotIn("docs", self.notes.commits)
        self.assertEqual(self.notes.commits_withheld, 1)

    def test_withheld_subjects_are_only_counted_in_the_render(self):
        text = release_notes.render(self.notes)
        self.assertNotIn("internal.example", text)
        self.assertIn("_1 subject(s) withheld: they name a deployment or private infrastructure._", text)

    def test_no_pattern_files_withholds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(Path(tmp))
            (repo / "scripts/release/tripwire-private.txt").unlink()
            grouped, withheld = release_notes.commits(
                repo, "v0.4.0-rc.4", "HEAD", withhold=release_notes.withheld_pattern(repo),
            )
        self.assertEqual(withheld, 0)
        self.assertEqual(grouped["docs"], ["docs: the test database moved to db.internal.example"])

    def test_render_sections(self):
        text = release_notes.render(self.notes)
        self.assertIn("## 1. Before you upgrade", text)
        self.assertIn("- `API_JWT_SIGNING_KEY`: signs API bearer tokens **(new in this range)**", text)
        self.assertIn("Migrations added: 4", text)
        self.assertIn("| `crm.0019_closed_signed_stage` | data | one-way (RunPython reverse is noop) | _ |", text)
        self.assertIn("- `crm.0019_closed_signed_stage`: RunPython reverse is noop", text)
        self.assertIn("**Fixes**\n\n- fix(money): show USD as $1,234.56", text)
        for n in release_notes.GENERATED_SECTIONS:
            self.assertIn(release_notes.MARKER_BEGIN.format(n=n), text)
            self.assertIn(release_notes.MARKER_END.format(n=n), text)

    def test_write_creates_outline_then_keeps_hand_written_sections(self):
        with tempfile.TemporaryDirectory() as out:
            path = Path(out) / "releases" / "v0.4.0.md"
            self.assertEqual(release_notes.write(path, self.notes), "created")
            text = path.read_text()
            self.assertTrue(text.startswith("# Fincas v0.4.0\n"))
            for heading in ("## 0. About", "## 1. Before you upgrade", "## 2. Behavior changes",
                            "## 3. Known limitations", "## 4. Migrations", "## 5. Everything else"):
                self.assertIn(heading, text)
            self.assertIn("_Written by hand before the tag._", text)
            # A person edits sections 2 and 3 and the top of 1.
            text = text.replace("## 2. Behavior changes staff will notice\n\n_Written by hand before the tag._",
                                "## 2. Behavior changes staff will notice\n\n- Money shows as $1,234.56.")
            text = text.replace("## 1. Before you upgrade\n\n", "## 1. Before you upgrade\n\n```bash\nmigrate\n```\n\n")
            path.write_text(text)
            self.assertEqual(release_notes.write(path, self.notes), "updated")
            after = path.read_text()
            self.assertIn("- Money shows as $1,234.56.", after)
            self.assertIn("```bash\nmigrate\n```", after)
            self.assertEqual(after.count(release_notes.MARKER_BEGIN.format(n=4)), 1)
            self.assertIn("Migrations added: 4", after)

    def test_write_appends_missing_block(self):
        with tempfile.TemporaryDirectory() as out:
            path = Path(out) / "v9.md"
            path.write_text("# hand\n\n## 4. Migrations\n\n<!-- release_notes:begin:4 -->\nold\n<!-- release_notes:end:4 -->\n")
            release_notes.write(path, self.notes)
            text = path.read_text()
            self.assertNotIn("\nold\n", text)
            self.assertIn("## 1. (generated)", text)
            self.assertIn("## 5. (generated)", text)

    def test_command_prints_and_writes(self):
        out = io.StringIO()
        call_command("release_notes", "v0.4.0-rc.4", "HEAD", repo=str(self.repo), stdout=out)
        self.assertIn("Migrations added: 4", out.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "v0.4.0.md"
            out = io.StringIO()
            call_command("release_notes", "v0.4.0-rc.4", "HEAD", repo=str(self.repo), write=str(target), stdout=out)
            self.assertIn("created", out.getvalue())
            self.assertIn("4 migration(s), 1 one-way, 2 new secret(s), 1 new setting(s), 3 new env read(s)",
                          out.getvalue())
            self.assertTrue(target.exists())

    def test_unknown_ref_exits_usage(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            call_command("release_notes", "v0.4.0-rc.4", "no-such-ref", repo=str(self.repo), stderr=err)
        self.assertEqual(caught.exception.code, rc.USAGE)
        self.assertIn("unknown ref 'no-such-ref'", err.getvalue())

    def test_shallow_clone_exits_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            shallow = Path(tmp) / "shallow"
            _git(Path(tmp), "clone", "-q", "--depth", "1", f"file://{self.repo}", str(shallow))
            err = io.StringIO()
            with self.assertRaises(SystemExit) as caught:
                call_command("release_notes", "HEAD~1", "HEAD", repo=str(shallow), stderr=err)
            self.assertEqual(caught.exception.code, rc.USAGE)
            self.assertIn("run git fetch --unshallow", err.getvalue())

    def test_not_a_repo_exits_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with self.assertRaises(SystemExit) as caught:
                call_command("release_notes", "a", "b", repo=tmp, stderr=err)
            self.assertEqual(caught.exception.code, rc.USAGE)
            self.assertIn("not a git checkout", err.getvalue())
