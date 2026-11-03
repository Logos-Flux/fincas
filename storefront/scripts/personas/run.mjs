// Persona run (docs/release-hardening-plan.md section 5): one role agent with a
// goal and no click-path drives the back office through a real browser and
// writes down every dead end, unclear label, missing link, surprising
// default, error and slow page it meets.
//
//   GATEWAY_URL=https://…/v1 GATEWAY_KEY=sk-… ERP_URL=http://127.0.0.1:8799 \
//   HARNESS_LOGINS=../.runs-persona/logins.txt PERSONA=sales-rep \
//   node scripts/personas/run.mjs
//
// Env: PERSONA (id in personas.json; "all" runs them one at a time), MODEL
// (override the persona's model), MAX_STEPS (default 40; a smoke run uses 8),
// OUT (default ../.runs-persona/personas), HARNESS_SERVER_LOG (the harness
// server's request/exception log, read for 500s inside the run's window).
//
// The agent sees, each step: the URL, the page title, flash messages, a
// text excerpt and up to 90 interactive elements with short refs (e12). It
// acts through tools: navigate, click, fill, select, press, back, note, finish.
// Each step is screenshotted. Nothing here calls Claude: the model is the
// gateway lane named in personas.json (decision 4), pinned in the run log.

import fs from "node:fs";
import path from "node:path";
import { chromium } from "@playwright/test";
import { loginStaff } from "../lib/erp-checks.mjs";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const SPEC = JSON.parse(
  fs.readFileSync(path.join(HERE, "personas.json"), "utf-8"),
).personas;
const BASE = process.env.ERP_URL ?? "http://127.0.0.1:8799";
const GATEWAY_URL = (process.env.GATEWAY_URL ?? "").replace(/\/$/, "");
const GATEWAY_KEY = process.env.GATEWAY_KEY ?? "";
const LOGINS =
  process.env.HARNESS_LOGINS ??
  path.resolve(HERE, "../../../.runs-persona/logins.txt");
const SERVER_LOG = process.env.HARNESS_SERVER_LOG ?? "";
const OUT = path.resolve(
  process.env.OUT ?? path.resolve(HERE, "../../../.runs-persona/personas"),
);
const MAX_STEPS = Number(process.env.MAX_STEPS ?? 40);
const SLOW_MS = 2000;
if (!GATEWAY_URL || !GATEWAY_KEY)
  throw new Error("set GATEWAY_URL and GATEWAY_KEY");

const TOOLS = [
  tool(
    "navigate",
    "Open a back-office path such as /erp/orders/.",
    { path: { type: "string" } },
    ["path"],
  ),
  tool(
    "click",
    "Click an element by its ref (e12).",
    { ref: { type: "string" } },
    ["ref"],
  ),
  tool(
    "fill",
    "Type into an input, textarea or search box by ref, replacing its contents.",
    { ref: { type: "string" }, text: { type: "string" } },
    ["ref", "text"],
  ),
  tool(
    "select",
    "Choose an option of a <select> by ref; value is the visible option text or its value.",
    { ref: { type: "string" }, value: { type: "string" } },
    ["ref", "value"],
  ),
  tool(
    "press",
    "Press a key on the focused element (Enter, Escape, Tab).",
    { key: { type: "string" } },
    ["key"],
  ),
  tool("back", "Go back one page.", {}, []),
  tool(
    "note",
    "Write down something a user would report: kind is blocks (could not do the task), workaround (did it another way), friction (slower or confusing than it should be).",
    {
      kind: { type: "string", enum: ["blocks", "workaround", "friction"] },
      text: { type: "string" },
    },
    ["kind", "text"],
  ),
  tool(
    "finish",
    "Stop: the goal is done, or cannot be done. Summarise what happened and what was confusing.",
    { summary: { type: "string" }, done: { type: "boolean" } },
    ["summary", "done"],
  ),
];

function tool(name, description, properties, required) {
  return {
    type: "function",
    function: {
      name,
      description,
      parameters: { type: "object", properties, required },
    },
  };
}

function logins() {
  return fs
    .readFileSync(LOGINS, "utf-8")
    .split("\n")
    .filter((l) => l.trim() && !l.startsWith("#"))
    .map((l) => {
      const [username, password, role] = l.trim().split(/\s+/);
      return { username, password, role };
    });
}

// What the agent sees. Refs are assigned per observation and resolved by a
// data attribute the page keeps until the next observation.
async function observe(page) {
  // A page that navigates itself mid-evaluate destroys the context; settle and retry.
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await observeOnce(page);
    } catch (err) {
      if (
        attempt >= 2 ||
        !String(err).includes("Execution context was destroyed")
      )
        throw err;
      await page
        .waitForLoadState("networkidle", { timeout: 10000 })
        .catch(() => {});
    }
  }
}

async function observeOnce(page) {
  await page.waitForLoadState("domcontentloaded").catch(() => {});
  return page.evaluate((cap) => {
    const vis = (el) => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return (
        r.width > 0 &&
        r.height > 0 &&
        s.visibility !== "hidden" &&
        s.display !== "none"
      );
    };
    const name = (el) => {
      const lab =
        el.getAttribute("aria-label") ||
        (el.id && document.querySelector(`label[for="${el.id}"]`)?.innerText) ||
        el.getAttribute("placeholder") ||
        el.getAttribute("title") ||
        el.innerText ||
        el.value ||
        el.getAttribute("name") ||
        "";
      return lab.replace(/\s+/g, " ").trim().slice(0, 60);
    };
    document
      .querySelectorAll("[data-persona-ref]")
      .forEach((el) => el.removeAttribute("data-persona-ref"));
    const els = [];
    let n = 0;
    const all = Array.from(
      document.querySelectorAll(
        "a[href], button, input, select, textarea, [role=button], [role=tab], summary, [data-bs-toggle]",
      ),
    );
    // Controls first (buttons, fields, tabs), then links: a long page's
    // actions must never fall past the cap behind its navigation links.
    const ranked = [
      ...all.filter((el) => el.tagName !== "A"),
      ...all.filter((el) => el.tagName === "A"),
    ];
    for (const el of ranked) {
      if (!vis(el) || el.disabled || el.type === "hidden") continue;
      if (els.length >= cap) break;
      n += 1;
      const ref = `e${n}`;
      el.setAttribute("data-persona-ref", ref);
      const tag = el.tagName.toLowerCase();
      const kind =
        tag === "a"
          ? "link"
          : tag === "input"
            ? `input:${el.type || "text"}`
            : tag;
      let extra = "";
      if (tag === "a") extra = ` -> ${el.getAttribute("href")}`;
      if (tag === "select")
        extra = ` [${Array.from(el.options)
          .slice(0, 12)
          .map((o) => o.text.trim())
          .join(" | ")}${el.options.length > 12 ? " | …" : ""}]`;
      if (
        (tag === "input" || tag === "textarea") &&
        el.value &&
        el.type !== "password"
      ) {
        const v = String(el.value);
        extra = ` ="${v.slice(0, 80)}${v.length > 80 ? "…" : ""}"`; // a cut must look like one: an agent read its own typed text as truncated by the app
      }
      els.push(`${ref} ${kind} "${name(el)}"${extra}`);
    }
    const main = document.querySelector(
      "main, .content, .container-fluid, body",
    );
    const text = (main?.innerText || "")
      .replace(/\n{2,}/g, "\n")
      .slice(0, 1600);
    const messages = Array.from(
      document.querySelectorAll(".alert, .messages li, [role=alert]"),
    )
      .map((m) => m.innerText.trim())
      .filter(Boolean)
      .slice(0, 6);
    return {
      url: location.pathname + location.search,
      title: document.title,
      messages,
      text,
      elements: els,
    };
  }, 110);
}

async function act(page, name, args) {
  const by = (ref) => page.locator(`[data-persona-ref="${ref}"]`).first();
  switch (name) {
    case "navigate": {
      const target = args.path.startsWith("http")
        ? args.path
        : BASE + args.path;
      await page.goto(target, {
        waitUntil: "domcontentloaded",
        timeout: 20000,
      });
      return `opened ${args.path}`;
    }
    case "click": {
      await by(args.ref).click({ timeout: 8000 });
      await page.waitForLoadState("domcontentloaded").catch(() => {});
      // A submit the browser refused (HTML5 validation) shows a person a
      // bubble the page text never contains; tell the agent which field.
      const invalid = await page
        .evaluate(() =>
          Array.from(document.querySelectorAll("form:invalid")).flatMap((f) =>
            Array.from(f.querySelectorAll(":invalid"))
              .filter((el) => el.name)
              .slice(0, 4)
              .map((el) => `${el.name}: ${el.validationMessage}`),
          ),
        )
        .catch(() => []);
      return (
        `clicked ${args.ref}` +
        (invalid.length
          ? ` (the browser blocked the submit: ${invalid.join("; ")})`
          : "")
      );
    }
    case "fill":
      await by(args.ref).fill(args.text, { timeout: 8000 });
      return `filled ${args.ref}`;
    case "select": {
      const loc = by(args.ref);
      try {
        await loc.selectOption({ label: args.value }, { timeout: 4000 });
      } catch {
        await loc.selectOption(args.value, { timeout: 4000 });
      }
      return `selected ${args.value}`;
    }
    case "press":
      await page.keyboard.press(args.key);
      await page.waitForLoadState("domcontentloaded").catch(() => {});
      return `pressed ${args.key}`;
    case "back":
      await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => {});
      return "went back";
    default:
      return `unknown tool ${name}`;
  }
}

const noTemperature = new Set(); // models that refuse a temperature (Kimi: "only 1 is allowed")

async function chat(model, messages) {
  const request = {
    model,
    messages,
    tools: TOOLS,
    tool_choice: "auto",
    max_tokens: 700,
  };
  if (!noTemperature.has(model)) request.temperature = 0.2;
  const call = () =>
    fetch(`${GATEWAY_URL}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${GATEWAY_KEY}`,
      },
      body: JSON.stringify(request),
    });
  let res = await call();
  let body = await res.json().catch(() => ({}));
  if (
    res.status === 400 &&
    /temperature/i.test(JSON.stringify(body)) &&
    "temperature" in request
  ) {
    noTemperature.add(model);
    delete request.temperature;
    res = await call();
    body = await res.json().catch(() => ({}));
  }
  if (!res.ok)
    throw new Error(
      `gateway ${res.status}: ${JSON.stringify(body).slice(0, 300)}`,
    );
  return body;
}

function systemPrompt(persona, login) {
  return [
    `You are testing a back-office web application as a real employee would. Role: ${persona.id} (signed in as ${login.username}).`,
    "You see the current page as text each turn and act ONLY through the tools. Work towards the goal the way a person would: read the page, use the navigation and search, try the obvious thing first.",
    "Whenever something is confusing, missing, slow, surprising or broken, call `note` with the kind: blocks (you cannot do the task that way), workaround (you found another way), friction (it worked but was harder than it should be). Be concrete: what you expected, what you saw, where (the page path). Note copy you did not understand and defaults you had to change.",
    "Do not invent data that must come from the page; if a search finds nothing, note it and try another way. Never repeat the same action more than twice.",
    "When the goal is done or clearly impossible, call `finish` with an honest summary.",
  ].join("\n");
}

function render(obs) {
  return [
    `URL: ${obs.url}`,
    `Title: ${obs.title}`,
    obs.messages.length ? `Messages: ${obs.messages.join(" | ")}` : "",
    `Elements:\n${obs.elements.join("\n")}`,
    `Text:\n${obs.text}`,
  ]
    .filter(Boolean)
    .join("\n");
}

function serverErrorsSince(offset) {
  if (!SERVER_LOG || !fs.existsSync(SERVER_LOG)) return { errors: [], offset };
  const text = fs.readFileSync(SERVER_LOG, "utf-8");
  const chunk = text.slice(offset);
  const errors = chunk
    .split("\n")
    .filter((l) => /Internal Server Error|Traceback/.test(l))
    .slice(0, 20);
  return { errors, offset: text.length };
}

async function runPersona(persona) {
  const login = logins().find((l) => l.role === persona.login);
  if (!login) throw new Error(`no seeded login for role ${persona.login}`);
  const model = process.env.MODEL ?? persona.model;
  const dir = path.join(OUT, persona.id);
  fs.mkdirSync(path.join(dir, "steps"), { recursive: true });
  const stepsLog = fs.createWriteStream(path.join(dir, "steps.jsonl"), {
    flags: "w",
  });
  const notes = [];
  let usage = { prompt_tokens: 0, completion_tokens: 0 };
  let logOffset =
    SERVER_LOG && fs.existsSync(SERVER_LOG)
      ? fs.readFileSync(SERVER_LOG, "utf-8").length
      : 0;
  const serverErrors = [];
  const started = new Date();

  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
  });
  // Many actions confirm first (window.confirm). Playwright dismisses dialogs
  // unless told otherwise, which silently cancelled the first run's sends.
  // A person reads the question and clicks OK; the agent gets the text.
  const dialogs = [];
  page.on("dialog", (d) => {
    dialogs.push(`${d.type()}: ${d.message()}`);
    d.accept().catch(() => {});
  });
  await loginStaff(page, BASE, login.username, login.password);
  const messages = [
    { role: "system", content: systemPrompt(persona, login) },
    {
      role: "user",
      content: `GOAL: ${persona.goal}\n\nYou are signed in. Here is the page you landed on.`,
    },
  ];
  let finish = null;
  let step = 0;
  const slowPages = [];
  while (step < MAX_STEPS && !finish) {
    step += 1;
    const t0 = Date.now();
    const obs = await observe(page);
    const observeMs = Date.now() - t0;
    messages.push({ role: "user", content: render(obs) });
    // Keep the context bounded: system, goal, and the last turns. A turn is
    // an observation (user), the reply (assistant, with tool_calls) and its
    // tool results; drop whole turns, never a lone tool result (Moonshot
    // rejects a tool message whose call is gone: "tool_call_id is not found").
    while (messages.length > 20) {
      let next = 3;
      while (next < messages.length && messages[next].role !== "user")
        next += 1;
      if (next >= messages.length) break;
      messages.splice(2, next - 2);
    }
    let reply;
    try {
      reply = await chat(model, messages);
    } catch (err) {
      stepsLog.write(JSON.stringify({ step, error: String(err) }) + "\n");
      finish = {
        summary: `gateway failure: ${String(err).slice(0, 200)}`,
        done: false,
        aborted: true,
      };
      break;
    }
    usage.prompt_tokens += reply.usage?.prompt_tokens ?? 0;
    usage.completion_tokens += reply.usage?.completion_tokens ?? 0;
    const msg = reply.choices?.[0]?.message ?? {};
    messages.push(msg);
    const calls = msg.tool_calls ?? [];
    if (!calls.length) {
      messages.push({
        role: "user",
        content: "Use a tool: act on the page, note something, or finish.",
      });
      stepsLog.write(
        JSON.stringify({ step, url: obs.url, text: msg.content }) + "\n",
      );
      continue;
    }
    for (const call of calls) {
      let args = {};
      try {
        args = JSON.parse(call.function.arguments || "{}");
      } catch {
        args = {};
      }
      const name = call.function.name;
      let result;
      const a0 = Date.now();
      try {
        if (name === "note") {
          notes.push({ step, url: obs.url, ...args });
          result = "noted";
        } else if (name === "finish") {
          finish = args;
          result = "finished";
        } else result = await act(page, name, args);
      } catch (err) {
        result = `failed: ${String(err).split("\n")[0].slice(0, 200)}`;
      }
      const actMs = Date.now() - a0;
      if (actMs > SLOW_MS && name !== "note" && name !== "finish")
        slowPages.push({ step, action: name, ms: actMs, url: obs.url });
      const since = serverErrorsSince(logOffset);
      logOffset = since.offset;
      if (since.errors.length)
        serverErrors.push({ step, url: obs.url, lines: since.errors });
      const seen = dialogs
        .splice(0)
        .map((d) => `(A browser dialog appeared and was accepted: ${d})`)
        .join("\n");
      messages.push({
        role: "tool",
        tool_call_id: call.id,
        content: [
          result,
          since.errors.length
            ? "(The server logged an error during this action.)"
            : "",
          seen,
        ]
          .filter(Boolean)
          .join("\n"),
      });
      stepsLog.write(
        JSON.stringify({
          step,
          url: obs.url,
          action: name,
          args,
          result,
          ms: actMs,
          observe_ms: observeMs,
        }) + "\n",
      );
    }
    await page
      .screenshot({
        path: path.join(dir, "steps", `${String(step).padStart(2, "0")}.png`),
        fullPage: false,
      })
      .catch(() => {});
  }
  if (!finish)
    finish = {
      summary: `stopped after ${MAX_STEPS} steps without finishing`,
      done: false,
    };
  await browser.close();
  stepsLog.end();

  const lines = [
    `# Persona run: ${persona.id}`,
    "",
    `- Model: \`${model}\` (${persona.family}), gateway key alias golden-persona-run`,
    `- Login: ${login.username} (role ${login.role}); base ${BASE}`,
    `- Started ${started.toISOString()}, ${step} steps, tokens ${usage.prompt_tokens} in / ${usage.completion_tokens} out`,
    `- Outcome: ${finish.done ? "done" : "not done"}${finish.aborted ? " (aborted)" : ""}`,
    "",
    "## Goal",
    "",
    persona.goal,
    "",
    "## Summary (the agent's words)",
    "",
    finish.summary ?? "",
    "",
    `## Notes (${notes.length})`,
    "",
    ...(notes.length
      ? notes.map(
          (n) => `- **${n.kind}** (step ${n.step}, \`${n.url}\`): ${n.text}`,
        )
      : ["- none"]),
    "",
    `## Server errors during the run (${serverErrors.length})`,
    "",
    ...(serverErrors.length
      ? serverErrors.map(
          (e) => `- step ${e.step} on \`${e.url}\`: ${e.lines[0]}`,
        )
      : ["- none"]),
    "",
    `## Slow actions over ${SLOW_MS} ms (${slowPages.length})`,
    "",
    ...(slowPages.length
      ? slowPages.map(
          (s) =>
            `- step ${s.step}: ${s.action} on \`${s.url}\` took ${s.ms} ms`,
        )
      : ["- none"]),
    "",
    "Screenshots: `steps/NN.png`; every tool call: `steps.jsonl`.",
  ];
  fs.writeFileSync(path.join(dir, "findings.md"), lines.join("\n") + "\n");
  console.log(
    `${persona.id}: ${step} steps, ${notes.length} notes, ${serverErrors.length} server errors, ${finish.done ? "done" : "not done"} (${model})`,
  );
  return {
    persona: persona.id,
    model,
    steps: step,
    notes: notes.length,
    serverErrors: serverErrors.length,
    done: !!finish.done,
  };
}

const which = process.env.PERSONA ?? "all";
const wanted = which
  .split(",")
  .map((w) => w.trim())
  .filter(Boolean);
const selected =
  which === "all" ? SPEC : SPEC.filter((p) => wanted.includes(p.id));
if (!selected.length)
  throw new Error(
    `no persona ${which}; known: ${SPEC.map((p) => p.id).join(", ")}`,
  );
fs.mkdirSync(OUT, { recursive: true });
const results = [];
for (const persona of selected) results.push(await runPersona(persona));
const summaryPath = path.join(OUT, "summary.json");
const previous = fs.existsSync(summaryPath)
  ? (JSON.parse(fs.readFileSync(summaryPath, "utf-8")).results ?? [])
  : [];
const merged = [
  ...previous.filter((r) => !results.some((n) => n.persona === r.persona)),
  ...results,
];
fs.writeFileSync(
  summaryPath,
  JSON.stringify(
    { base: BASE, at: new Date().toISOString(), results: merged },
    null,
    2,
  ),
);
