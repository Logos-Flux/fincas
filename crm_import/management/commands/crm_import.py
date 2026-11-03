"""The CRM importer's command (docs/crm/phase5-importer.md 5.5, docs/crm/importing.md).

    manage.py crm_import stage --company <id> --dir <canonical> [--previous-job <id>]
                               [--mapping mapping.toml] [--include-archived]
                               [--tickets-as-engagements] [--json]
    manage.py crm_import stage --resume <job id> --dir <canonical>
    manage.py crm_import run    --job <id> [--dry-run] [--through <step>] [--report-dir <dir>]
    manage.py crm_import retry  --job <id> [--code <error code> ...] [--dry-run] [--report-dir <dir>]
    manage.py crm_import status --job <id> [--json]
    manage.py crm_import undo   --job <id> [--batch <step>]
    manage.py crm_import cancel --job <id>
    manage.py crm_import ai     --job <id> [--enable] [--force] [--json]

``stage`` validates the export, stages every record, matches it (section 3)
and auto-approves the unflagged rows at or above 0.95. It writes nothing
outside ``unify`` staging and the job.

``run`` applies the approved rows step by step in the 4.2 order behind the
step-order gate, resuming after the last complete step (``crm_import.runner``);
``--dry-run`` does the same inside one transaction that is rolled back.
Both write ``import-report.json`` (``import-report.dry-run.json``) into
``--report-dir`` (default: the current directory; ``--no-report-file`` to
skip). ``retry`` re-applies failed rows only. ``undo`` reverses the whole job
newest first, or one step (``--batch <object type>``) once no later step
depends on it. ``cancel`` stops a job nothing was applied from.

With ``stage --ai-hints`` staging then asks the deployment's ``llmhub`` route
for suggestions (spec 3.5): they never decide a row. ``ai`` runs that pass
again on a job in review (``--enable`` opts in a job staged without it).

Exit status: non-zero when a run stops at a closed gate, when an undo
refuses rows, and on every refusal to start.
"""

import getpass
import json
import tomllib
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from crm_import.canonical import CanonicalFormatError


class Command(BaseCommand):
    help = "Stage a canonical CRM export (docs/crm/importing.md)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        st = sub.add_parser("stage", help="validate, stage, match and auto-approve an export")
        st.add_argument("--company", help="the tenant (core.Company id)")
        st.add_argument("--dir", required=True, help="the canonical export directory")
        st.add_argument("--previous-job", help="the job this export re-imports over (3.4)")
        st.add_argument("--resume", help="resume an interrupted staging of this job")
        st.add_argument("--mapping", help="the mapping file (TOML, spec 2.12), frozen onto the job")
        st.add_argument("--include-archived", action="store_true", help="import records archived in the source")
        st.add_argument("--tickets-as-engagements", action="store_true",
                        help="match ticket pipelines against engagement pipelines (2.8)")
        st.add_argument("--ai-hints", action="store_true",
                        help="ask the llmhub crm_match_propose / crm_dedupe_hint routes for suggestions (3.5)")
        st.add_argument("--json", action="store_true", help="print the job report as JSON")
        ai = sub.add_parser("ai", help="AI suggestions for a job in review (suggestions only, spec 3.5)")
        ai.add_argument("--job", required=True, help="the import job")
        ai.add_argument("--enable", action="store_true", help="opt the job in (as stage --ai-hints would)")
        ai.add_argument("--force", action="store_true", help="ask again for rows that have a suggestion")
        ai.add_argument("--json", action="store_true", help="print the AI summary as JSON")

        from crm_import.models import STEPS

        steps = sorted(STEPS, key=STEPS.get)
        for name, helptext in (("run", "apply the approved rows, step by step (or --dry-run)"),
                               ("retry", "re-apply the failed rows only")):
            sp = sub.add_parser(name, help=helptext)
            sp.add_argument("--job", required=True)
            sp.add_argument("--dry-run", action="store_true",
                            help="do everything inside one transaction and roll it back")
            sp.add_argument("--through", choices=steps, help="stop after this step")
            sp.add_argument("--report-dir", default=".", help="where import-report.json goes (default: .)")
            sp.add_argument("--no-report-file", action="store_true", help="write no report file")
            sp.add_argument("--chunk-size", type=int, default=200)
            sp.add_argument("--actor", help="the decider recorded on the runs (default cli:<os user>)")
            sp.add_argument("--json", action="store_true", help="print the report as JSON")
            if name == "retry":
                sp.add_argument("--code", action="append", default=[],
                                help="only rows failed with this error code (repeatable)")
        sp = sub.add_parser("status", help="the job's state, step by step")
        sp.add_argument("--job", required=True)
        sp.add_argument("--json", action="store_true", help="print the full report as JSON")
        sp = sub.add_parser("undo", help="undo the whole job, or one step")
        sp.add_argument("--job", required=True)
        sp.add_argument("--batch", choices=steps, help="undo only this step (an object type)")
        sp.add_argument("--actor", help="the decider recorded on the undo (default cli:<os user>)")
        sp = sub.add_parser("cancel", help="stop a job nothing was applied from")
        sp.add_argument("--job", required=True)

    def handle(self, *args, **options):
        action = options["action"]
        if action == "stage":
            return self.stage(options)
        if action == "ai":
            return self.ai(options)
        if action in ("run", "retry", "status", "undo", "cancel"):
            from crm_import.runner import RunnerError

            job = self.job(options["job"])
            try:
                return getattr(self, f"do_{action}")(job, options)
            except RunnerError as exc:
                raise CommandError(str(exc)) from exc
        raise CommandError(f"Unknown action {action!r}.")

    # --- run / retry / status / undo / cancel ----------------------------------------

    def job(self, pk):
        from django.core.exceptions import ValidationError

        from crm_import.models import ImportJob

        try:
            job = ImportJob.objects.select_related("company").filter(pk=pk).first()
        except (ValueError, ValidationError):
            job = None
        if job is None:
            raise CommandError(f"No import job {pk}.")
        return job

    def actor(self, options) -> str:
        if options.get("actor"):
            return options["actor"]
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            user = "unknown"
        return f"cli:{user}"

    def do_run(self, job, options, *, retry=False):
        from crm_import import report, runner

        result = runner.run_job(
            job, actor=self.actor(options), dry_run=options["dry_run"], through=options["through"],
            retry=retry, codes=options.get("code") or None, chunk_size=options["chunk_size"],
            progress=None if options["json"] else (lambda msg: self.stdout.write(f"  {msg}")),
        )
        if not options["no_report_file"]:
            name = "import-report.dry-run.json" if options["dry_run"] else "import-report.json"
            path = report.write(result.report, Path(options["report_dir"]) / name)
            if not options["json"]:
                self.stdout.write(f"Report: {path}")
        if options["json"]:
            self.stdout.write(report.dumps(result.report), ending="")
        else:
            verb = "Dry run (rolled back)" if result.dry_run else ("Retry" if retry else "Run")
            self.stdout.write(f"{verb} of job {job.pk}: {result.report['status']}, coverage {job.coverage}")
            self.table(result.report)
        if result.blocked is not None:
            raise CommandError(str(result.blocked))

    def do_retry(self, job, options):
        return self.do_run(job, options, retry=True)

    def do_status(self, job, options):
        from crm_import import report

        built = report.build(job)
        if options["json"]:
            self.stdout.write(report.dumps(built), ending="")
            return
        self.stdout.write(f"Job {job.pk}: {job.status}, coverage {job.coverage}, source {job.source}")
        self.table(built)
        line = built["consequence"]
        self.stdout.write(f"Consent: {line['contacts_no_consent_for_marketing']} of {line['contacts']} "
                          "contacts will have no marketing consent in golden (spec 2.11).")

    def table(self, built):
        self.stdout.write(f"  {'step':<16}{'state':<11}{'rows':>7}{'applied':>9}{'pending':>9}"
                          f"{'review':>8}{'failed':>8}{'n/a':>7}  coverage")
        for otype, item in built["types"].items():
            cov = item["coverage"]
            coverage = cov["coverage"] + (f" ({', '.join(cov['reasons'])})" if cov["reasons"] else "")
            na = item["unchanged"] + sum(item["skipped"].values())
            failed = item["failed"]["total"] + item["dependency_pending"]
            label = f"{item['step']:>2} {otype}"
            state = item["state"] + (f" by {','.join(item['blocked_by'])}" if item["blocked_by"] else "")
            self.stdout.write(f"  {label:<16}{state:<11}{item['rows']:>7}{item['applied']['total']:>9}"
                              f"{item['approved_pending']:>9}{item['need_review']['total']:>8}{failed:>8}"
                              f"{na:>7}  {coverage}")

    def do_undo(self, job, options):
        from crm_import import runner

        if options["batch"]:
            result = runner.undo_batch(job, options["batch"], actor=self.actor(options))
        else:
            result = runner.undo_job(job, actor=self.actor(options))
        self.stdout.write(f"Undo of job {job.pk}: {result.undone_rows} row(s) undone, {result.reverted} "
                          f"write(s) reverted; job is {result.job.status}.")
        for item in result.refused:
            self.stderr.write(f"  refused: row {item['row']}: {item['error']}")
        if result.refused:
            raise CommandError(f"{len(result.refused)} row(s) refused to undo and stay applied (listed above).")

    def do_cancel(self, job, options):
        from crm_import import runner

        runner.cancel_job(job)
        self.stdout.write(f"Job {job.pk} canceled; its source is free.")

    def stage(self, options):
        from core.models import Company
        from crm_import import stage as staging
        from crm_import.models import ImportJob

        directory = Path(options["dir"])
        if not directory.is_dir():
            raise CommandError(f"{directory} is not a directory.")
        try:
            if options["resume"]:
                job = ImportJob.objects.filter(pk=options["resume"]).first()
                if job is None:
                    raise CommandError(f"No import job {options['resume']}.")
                job = staging.stage_job(job, directory, resume=True)
            else:
                if not options["company"]:
                    raise CommandError("--company is required (the tenant the export belongs to).")
                company = Company.objects.filter(pk=options["company"]).first()
                if company is None:
                    raise CommandError(f"No company {options['company']}.")
                previous = None
                if options["previous_job"]:
                    previous = ImportJob.objects.filter(pk=options["previous_job"]).first()
                    if previous is None:
                        raise CommandError(f"No import job {options['previous_job']}.")
                job = staging.stage(
                    company, directory, previous_job=previous, mapping=self.mapping(options["mapping"]),
                    options={"include_archived": options["include_archived"],
                             "tickets_as_engagements": options["tickets_as_engagements"],
                             "ai_match": options["ai_hints"]},
                )
        except (CanonicalFormatError, staging.StagingError) as exc:
            raise CommandError(str(exc)) from exc
        if options["json"]:
            self.stdout.write(json.dumps({"job": str(job.pk), "status": job.status, "coverage": job.coverage,
                                          "report": job.report}, indent=2, sort_keys=True, default=str))
        else:
            self.summary(job)
        if job.status == ImportJob.Status.INVALID:
            raise CommandError(f"Job {job.pk} is invalid: run `manage.py check_canonical_export {directory}`.")

    def ai(self, options):
        from crm_import import ai
        from crm_import.models import ImportJob

        job = ImportJob.objects.filter(pk=options["job"]).first()
        if job is None:
            raise CommandError(f"No import job {options['job']}.")
        if job.status != ImportJob.Status.REVIEWING:
            raise CommandError(f"Job {job.pk} is {job.status}: AI suggestions are for a job in review.")
        if options["enable"] and not ai.enabled(job):
            job.options = {**(job.options or {}), ai.OPTION: True}
            job.save(update_fields=["options", "updated_at"])
        if not ai.enabled(job):
            raise CommandError(f"Job {job.pk} did not opt in to AI suggestions: pass --enable.")
        status = ai.route_status()
        if not status.ok:
            raise CommandError(f"AI suggestions are off: {status.reason} (purpose {status.purpose}).")
        from crm_import.stage import StagingError, job_run_lock

        try:
            with job_run_lock(job):  # one AI run per job at a time (the budget is atomic either way)
                summary = ai.run(job, force=options["force"])
        except StagingError as exc:
            raise CommandError(str(exc)) from exc
        if options["json"]:
            self.stdout.write(json.dumps(summary, indent=2, sort_keys=True, default=str))
        else:
            self.ai_summary(summary)
        failed = {k: v["error"] for k, v in summary.items() if isinstance(v, dict) and v.get("error")}
        if failed:
            raise CommandError(f"The AI pass stopped: {failed}")
        if any(isinstance(v, dict) and v.get("budget_exhausted") for v in summary.values()):
            raise CommandError("The AI call budget ran out: the rows past it have no suggestion "
                               "(GOLDEN_CRM_IMPORT['ai_budget_per_batch']).")

    def ai_summary(self, summary):
        if not summary.get("enabled"):
            self.stdout.write(f"AI suggestions off: {summary.get('reason')}")
            return
        for otype, parts in summary.items():
            if isinstance(parts, dict) and parts.get("error"):
                self.stdout.write(f"  {otype:<14} STOPPED: {parts['error']}")
                continue
            if not isinstance(parts, dict) or "match" not in parts:
                continue
            m, d = parts["match"], parts["dedupe"]
            line = (f"  {otype:<14} suggested {m['suggested']} of {m['eligible']} (disagree {m['disagrees']}), "
                    f"duplicate pairs {d['pairs']} (same {d['pairs_same']}), calls {m['calls'] + d['calls']}"
                    f"/{m['budget']}")
            if parts["budget_exhausted"]:
                line += "  BUDGET EXHAUSTED"
            stopped = m.get("stopped") or d.get("stopped")
            if stopped:
                line += f"  STOPPED ({stopped})"
            self.stdout.write(line)

    def mapping(self, path):
        if not path:
            return {}
        try:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CommandError(f"{path} is not a readable TOML mapping file ({exc}).") from exc
        return data

    def summary(self, job):
        self.stdout.write(f"Job {job.pk}: {job.status}, coverage {job.coverage}")
        for otype, counts in (job.report.get("staging") or {}).items():
            states = ", ".join(f"{k} {v}" for k, v in sorted(counts["by_state"].items()))
            self.stdout.write(f"  {otype:<14} {counts['rows']:>7} rows  {states}")
        if (job.report or {}).get("ai"):
            self.ai_summary(job.report["ai"])
