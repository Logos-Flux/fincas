// The journey step runner for the back-office release harness
// (docs/release-testing.md, "Adding a journey").
//
// A journey is a serial list of steps sharing one browser page and a state
// bag persisted to HARNESS_RUN_DIR/state-<journey>.json (so `--step n` can
// rerun one step against the database a previous run left). After every
// step the runner checks, as docs/release-hardening-plan.md section 4 asks:
//   - the page answered without a 4xx/5xx (main-frame navigations) and the
//     console is clean (page errors, console.error);
//   - the server logged no 5xx, ERROR or traceback inside the step's time
//     window (server-error capture: HARNESS_SERVER_LOG, written by
//     config.settings.harness with UTC timestamps);
//   - axe has no serious or critical finding, and the page fits at 360 px.
// Every step appends a record to HARNESS_RUN_DIR/steps.jsonl; a failed step
// keeps a screenshot, a trace chunk and the journey's video under the run
// dir. scripts/harness/report.py turns steps.jsonl into report.md.
import {
  test as pwTest,
  expect,
  type BrowserContext,
  type Page,
  type TestInfo,
} from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import {
  awaitTransitions,
  axeSerious,
  horizontalScroll,
  loginStaff,
} from "../../lib/erp-checks.mjs";

export { expect };

export const RUN_DIR = path.resolve(
  process.env.HARNESS_RUN_DIR ?? "test-results/erp-harness",
);
export const BASE = process.env.HARNESS_BASE_URL ?? "http://127.0.0.1:8771";
const LOGINS = process.env.HARNESS_LOGINS ?? path.join(RUN_DIR, "logins.txt");
const SERVER_LOG =
  process.env.HARNESS_SERVER_LOG ?? path.join(RUN_DIR, "server.log");
const ONLY_STEP = process.env.HARNESS_STEP
  ? Number(process.env.HARNESS_STEP)
  : undefined;
const FROM_STEP = process.env.HARNESS_FROM_STEP
  ? Number(process.env.HARNESS_FROM_STEP)
  : undefined;
// Log lines this long before/after the step still count as "inside" it.
const WINDOW_BEFORE_MS = 250;
const WINDOW_AFTER_MS = 750;

export type Login = { username: string; password: string; role: string };

export function logins(): Login[] {
  if (!fs.existsSync(LOGINS))
    throw new Error(
      `no logins file at ${LOGINS}: run seed_release_test first (make harness)`,
    );
  return fs
    .readFileSync(LOGINS, "utf-8")
    .split("\n")
    .filter((l) => l.trim() && !l.startsWith("#"))
    .map((l) => {
      const [username, password, role] = l.trim().split(/\s+/);
      return { username, password, role };
    });
}

export type Row = Record<string, unknown>;

export type Ctx = {
  page: Page;
  base: string;
  /** Sign in as the login seeded for this role (staff, sales, superuser, …). */
  login: (role: string) => Promise<Login>;
  /** Survives across steps and runs (state-<journey>.json). */
  state: Record<string, string | number | boolean | null>;
  /** Rows of a model through /harness/state/<app.Model>/?field=value. */
  db: (label: string, filter?: Record<string, string | number>) => Promise<Row[]>;
  /** Exactly one row, or the step fails. */
  dbOne: (label: string, filter?: Record<string, string | number>) => Promise<Row>;
  /** Trial balance and a GL summary for a company (/harness/state/gl/). */
  gl: (companyId: string) => Promise<Row>;
  /** The service-account bearer the runner issued (HARNESS_API_KEY), for API steps. */
  apiKey: string | undefined;
  /** The page's CSRF cookie, for fetch() POSTs made from the page. */
  csrf: () => Promise<string>;
};

export type StepOptions = {
  /** R0 only: the step passes when the server-error capture caught a 5xx. */
  expectServerError?: boolean;
  /**
   * A product gap already in the findings log (its F-id or a short key).
   * The step is expected to fail: it is recorded as failed-known, the
   * journey carries on (the report still shows the journey as FAIL), and a
   * step that unexpectedly passes is flagged so the log gets updated.
   */
  knownFinding?: string;
  /**
   * axe rule ids already in the findings log, mapped to the finding key:
   * such violations are recorded as known and do not fail the step (other
   * rules still do). A journey may set it for every step (JourneyOptions).
   */
  axeKnown?: Record<string, string>;
  /** Skip axe for this step (default: run it). */
  axe?: boolean;
  /** Skip the 360 px fit check (default: run it). */
  fit360?: boolean;
};

/**
 * Thrown by a step that cannot run because an earlier known finding left
 * nothing to work on (no purchase order was saved, no refund exists). The
 * step is recorded as failed-known under that finding and the journey
 * continues; when the finding is fixed the step simply runs.
 */
export class BlockedError extends Error {
  constructor(
    public finding: string,
    why: string,
  ) {
    super(`blocked by known finding ${finding}: ${why}`);
    this.name = "BlockedError";
  }
}

/** `blocked("R1-no-po-from-back-office", "no PO was saved")` inside a step. */
export function blocked(finding: string, why: string): never {
  throw new BlockedError(finding, why);
}

export type StepFn = (ctx: Ctx) => Promise<void>;
type StepDef = { n: number; name: string; fn: StepFn; opts: StepOptions };
export type StepRegistrar = (
  n: number,
  name: string,
  fn: StepFn,
  opts?: StepOptions,
) => void;

const pad = (n: number) => String(n).padStart(2, "0");

function statePath(journey: string) {
  return path.join(RUN_DIR, `state-${journey}.json`);
}

function loadState(journey: string): Ctx["state"] {
  try {
    return JSON.parse(fs.readFileSync(statePath(journey), "utf-8"));
  } catch {
    return {};
  }
}

function saveState(journey: string, state: Ctx["state"]) {
  fs.mkdirSync(RUN_DIR, { recursive: true });
  fs.writeFileSync(statePath(journey), JSON.stringify(state, null, 2));
}

// ── server-error capture ──────────────────────────────────────────────────────

const STAMP = /^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z)\] /;
// runserver request lines end with `" <status> <length>`; only the status counts.
const FIVE_XX = /" 5\d\d \d+$/;

/** Log entries (a stamped line plus its continuation lines) inside [from, to]. */
export function serverLogEntries(from: Date, to: Date): string[] {
  if (!fs.existsSync(SERVER_LOG)) return [];
  const lo = from.getTime() - WINDOW_BEFORE_MS;
  const hi = to.getTime() + WINDOW_AFTER_MS;
  const entries: string[] = [];
  let current: string[] | null = null;
  for (const line of fs.readFileSync(SERVER_LOG, "utf-8").split("\n")) {
    const m = line.match(STAMP);
    if (m) {
      if (current) entries.push(current.join("\n"));
      const t = Date.parse(m[1]);
      current = t >= lo && t <= hi ? [line] : null;
    } else if (current && line.trim()) {
      current.push(line);
    }
  }
  if (current) entries.push(current.join("\n"));
  return entries;
}

/** The entries that fail a step: 5xx request lines, ERROR records, tracebacks. */
export function serverErrors(entries: string[]): string[] {
  return entries.filter((e) => {
    const head = e.split("\n")[0];
    return (
      / ERROR /.test(head) ||
      /Traceback \(most recent call last\)/.test(e) ||
      (/ django\.server /.test(head) && FIVE_XX.test(head))
    );
  });
}

// ── the journey ───────────────────────────────────────────────────────────────

export type StepRecord = {
  journey: string;
  step: number;
  name: string;
  status: "passed" | "failed" | "skipped";
  verdict?: string;
  /** Set when the failure is a known finding (StepOptions.knownFinding). */
  known?: string;
  retry: number;
  started: string;
  ended: string;
  duration_ms: number;
  url: string;
  error?: string;
  console: string[];
  responses: string[];
  axe: { id: string; nodes: number; sample?: string }[];
  fit360?: { scrollWidth: number; innerWidth: number; hscroll: boolean };
  server: string[];
  /** Visible alerts and validation messages on the page after the step (failed steps). */
  messages?: string[];
  screenshot?: string;
  trace?: string;
  video?: string;
};

function appendRecord(record: StepRecord) {
  fs.mkdirSync(RUN_DIR, { recursive: true });
  fs.appendFileSync(
    path.join(RUN_DIR, "steps.jsonl"),
    JSON.stringify(record) + "\n",
  );
}

export type JourneyOptions = { axeKnown?: Record<string, string> };

export function journey(
  id: string,
  title: string,
  define: (step: StepRegistrar) => void,
  options: JourneyOptions = {},
) {
  const steps: StepDef[] = [];
  define((n, name, fn, opts = {}) =>
    steps.push({
      n,
      name,
      fn,
      opts: { ...opts, axeKnown: { ...options.axeKnown, ...opts.axeKnown } },
    }),
  );

  pwTest.describe(`${id} ${title}`, () => {
    pwTest.describe.configure({ mode: "serial" });
    let context: BrowserContext;
    let page: Page;
    let failed = false;
    const state = loadState(id);
    const videoDir = path.join(RUN_DIR, "video", id);

    pwTest.beforeAll(async ({ browser }) => {
      context = await browser.newContext({
        viewport: { width: 1440, height: 900 },
        recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } },
      });
      await context.tracing.start({
        screenshots: true,
        snapshots: true,
        sources: true,
      });
      page = await context.newPage();
    });

    pwTest.afterAll(async () => {
      const video = page?.video();
      await context?.tracing.stop().catch(() => {});
      await context?.close().catch(() => {});
      if (video) {
        if (failed) {
          const p = await video.path().catch(() => null);
          if (p) {
            const dest = path.join(videoDir, `${id}.webm`);
            fs.renameSync(p, dest);
          }
        } else await video.delete().catch(() => {});
      }
    });

    for (const def of steps) {
      pwTest(`${id}.${pad(def.n)} ${def.name}`, async ({}, testInfo) => {
        if (ONLY_STEP !== undefined && def.n !== ONLY_STEP)
          testInfo.skip(true, `--step ${ONLY_STEP}`);
        if (FROM_STEP !== undefined && def.n < FROM_STEP)
          testInfo.skip(true, `--from-step ${FROM_STEP}`);
        try {
          await runStep(id, def, { page, context, state, testInfo });
        } catch (e) {
          failed = true;
          throw e;
        }
      });
    }
  });
}

async function runStep(
  id: string,
  def: StepDef,
  env: {
    page: Page;
    context: BrowserContext;
    state: Ctx["state"];
    testInfo: TestInfo;
  },
) {
  const { page, context, state, testInfo } = env;
  const consoleErrors: string[] = [];
  const responses: string[] = [];
  const onConsole = (msg: { type(): string; text(): string }) => {
    if (msg.type() === "error" && !/favicon\.ico/.test(msg.text()))
      consoleErrors.push(msg.text().slice(0, 300));
  };
  const onPageError = (err: Error) =>
    consoleErrors.push(`pageerror: ${err.message}`.slice(0, 300));
  const onResponse = (r: {
    status(): number;
    url(): string;
    request(): { isNavigationRequest(): boolean; frame(): unknown };
  }) => {
    const req = r.request();
    if (
      req.isNavigationRequest() &&
      req.frame() === page.mainFrame() &&
      r.status() >= 400
    )
      responses.push(`${r.status()} ${r.url()}`);
  };
  page.on("console", onConsole);
  page.on("pageerror", onPageError);
  page.on("response", onResponse);
  await context.tracing.startChunk({ title: `${id}.${pad(def.n)} ${def.name}` });

  const started = new Date();
  let error: string | undefined;
  let blockedBy: BlockedError | undefined;
  const ctx = makeCtx(page, state);
  try {
    // A rerun (--step / --from-step) starts on about:blank with no session:
    // sign in as the journey's default role and open the dashboard first.
    if (page.url() === "about:blank") {
      await ctx.login(process.env.HARNESS_ROLE ?? "staff");
      await page.goto(`${BASE}/erp/`);
    }
    await def.fn(ctx);
  } catch (e) {
    if (e instanceof BlockedError) blockedBy = e;
    error = e instanceof Error ? (e.stack ?? e.message) : String(e);
  }
  const ended = new Date();

  // Page checks (skipped when the step closed the page or crashed hard).
  let axe: StepRecord["axe"] = [];
  let fit360: StepRecord["fit360"];
  if (!page.isClosed() && !error) {
    await awaitTransitions(page).catch(() => {});
    if (def.opts.axe !== false) axe = await axeSerious(page).catch(() => []);
    if (def.opts.fit360 !== false) {
      await page.setViewportSize({ width: 360, height: 780 });
      await awaitTransitions(page).catch(() => {});
      fit360 = await horizontalScroll(page).catch(() => undefined);
      await page.setViewportSize({ width: 1440, height: 900 });
    }
  }
  page.off("console", onConsole);
  page.off("pageerror", onPageError);
  page.off("response", onResponse);

  // Server-error capture: anything the server logged inside the window.
  const server = serverErrors(serverLogEntries(started, new Date()));

  const problems: string[] = [];
  if (error) problems.push(`step threw: ${error.split("\n")[0]}`);
  if (responses.length) problems.push(`page answered ${responses.join(", ")}`);
  if (consoleErrors.length)
    problems.push(`console: ${consoleErrors.join(" | ")}`);
  const axeKnown = def.opts.axeKnown ?? {};
  const axeUnknown = axe.filter((a) => !(a.id in axeKnown));
  const axeKnownHits = axe.filter((a) => a.id in axeKnown);
  if (axeUnknown.length)
    problems.push(
      `axe serious: ${axeUnknown.map((a) => `${a.id}×${a.nodes} (${a.sample})`).join("; ")}`,
    );
  if (fit360?.hscroll)
    problems.push(
      `360 px: page scrolls horizontally (${fit360.scrollWidth} > ${fit360.innerWidth})`,
    );
  if (server.length)
    problems.push(
      `server logged ${server.length} error(s) inside the step: ${server[0].split("\n")[0]}`,
    );

  let status: StepRecord["status"] = problems.length ? "failed" : "passed";
  let verdict: string | undefined;
  let known: string | undefined;
  let blocking = true;
  if (axeKnownHits.length) {
    known = [...new Set(axeKnownHits.map((a) => axeKnown[a.id]))].join(", ");
    verdict = `known axe finding ${known}: ${axeKnownHits.map((a) => `${a.id}×${a.nodes} (${a.sample})`).join("; ")}`;
  }
  if (blockedBy) {
    // The step never ran: an earlier known finding left it nothing to do.
    status = "failed";
    known = blockedBy.finding;
    verdict = blockedBy.message;
    blocking = false;
  } else if (def.opts.knownFinding) {
    known = def.opts.knownFinding;
    if (problems.length) {
      // Expected: record it, keep the journey going.
      verdict = `known finding ${known}: ${problems[0]}`;
      blocking = false;
    } else {
      status = "failed";
      problems.push(
        `known finding ${known} did not reproduce: the step passed; update the findings log and drop knownFinding`,
      );
    }
  }
  if (def.opts.expectServerError) {
    // R0: the step is right when the capture caught a 5xx the page hid.
    if (server.length) {
      status = "passed";
      verdict = `capture caught the hidden 5xx (${server.length} server entr${server.length === 1 ? "y" : "ies"}); step failed as designed`;
      problems.length = 0;
    } else {
      status = "failed";
      problems.push(
        "the server-error capture missed the hidden 500 (no 5xx/ERROR in the window)",
      );
    }
  }

  const stem = `${id}-${pad(def.n)}${testInfo.retry ? `-r${testInfo.retry}` : ""}`;
  const record: StepRecord = {
    journey: id,
    step: def.n,
    name: def.name,
    status,
    verdict,
    known,
    retry: testInfo.retry,
    started: started.toISOString(),
    ended: ended.toISOString(),
    duration_ms: ended.getTime() - started.getTime(),
    url: page.isClosed() ? "" : page.url(),
    error,
    console: consoleErrors,
    responses,
    axe,
    fit360,
    server,
  };
  const stepsDir = path.join(RUN_DIR, "steps");
  fs.mkdirSync(stepsDir, { recursive: true });
  if (status === "failed") {
    if (!page.isClosed()) {
      record.messages = await page
        .evaluate(() => {
          const sel =
            "[role=alert], .alert, .invalid-feedback, .errorlist, .text-danger, .toast-body, .messages li";
          const seen = new Set<string>();
          for (const el of document.querySelectorAll(sel)) {
            const h = el as HTMLElement;
            if (!h.offsetParent && getComputedStyle(h).position !== "fixed") continue;
            const text = (h.innerText || "").replace(/\s+/g, " ").trim();
            if (text) seen.add(text.slice(0, 200));
          }
          return [...seen].slice(0, 12);
        })
        .catch(() => []);
      const shot = path.join(stepsDir, `${stem}.png`);
      await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
      record.screenshot = shot;
      await testInfo.attach("screenshot", { path: shot, contentType: "image/png" }).catch(() => {});
    }
    const trace = path.join(stepsDir, `${stem}.trace.zip`);
    await context.tracing.stopChunk({ path: trace }).catch(() => {});
    record.trace = trace;
    record.video = path.join(RUN_DIR, "video", id, `${id}.webm`);
  } else {
    await context.tracing.stopChunk().catch(() => {});
  }
  saveState(id, state);
  appendRecord(record);
  if (status === "failed" && blocking)
    throw new Error(
      `${id}.${pad(def.n)} ${def.name} failed:\n  - ${problems.join("\n  - ")}`,
    );
}

function makeCtx(page: Page, state: Ctx["state"]): Ctx {
  const query = async (label: string, filter: Record<string, string | number> = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filter)) qs.set(k, String(v));
    const res = await page.request.get(`${BASE}/harness/state/${label}/?${qs}`);
    if (!res.ok())
      throw new Error(`harness state ${label} ${qs}: HTTP ${res.status()} ${(await res.text()).slice(0, 200)}`);
    return (await res.json()) as { count: number; rows: Row[] };
  };
  return {
    page,
    base: BASE,
    state,
    async login(role) {
      const found = logins().find((l) => l.role === role);
      if (!found) throw new Error(`no seeded login for role ${role}`);
      // Sign out by dropping the session (LogoutView is POST-only).
      await page.context().clearCookies();
      await loginStaff(page, BASE, found.username, found.password);
      return found;
    },
    async db(label, filter) {
      return (await query(label, filter)).rows;
    },
    async dbOne(label, filter) {
      const { count, rows } = await query(label, filter);
      if (count !== 1)
        throw new Error(`expected one ${label} row for ${JSON.stringify(filter)}, found ${count}`);
      return rows[0];
    },
    async gl(companyId) {
      const res = await page.request.get(`${BASE}/harness/state/gl/?company=${companyId}`);
      if (!res.ok()) throw new Error(`harness gl: HTTP ${res.status()}`);
      return (await res.json()) as Row;
    },
    apiKey: apiKey(),
    async csrf() {
      const cookies = await page.context().cookies(BASE);
      const token = cookies.find((c) => c.name === "csrftoken")?.value;
      if (!token) throw new Error("no csrftoken cookie: open a back-office page first");
      return token;
    },
  };
}

function apiKey(): string | undefined {
  if (process.env.HARNESS_API_KEY) return process.env.HARNESS_API_KEY;
  const file = path.join(RUN_DIR, "api-key.txt");
  try {
    return fs.readFileSync(file, "utf-8").trim() || undefined;
  } catch {
    return undefined;
  }
}
