// R10 "Setup" (docs/release-hardening-plan.md section 4, R10 row).
//
// new user and roles -> team -> pipeline and stages -> labels (tenant
// rewording) -> theme -> integration account (the fixture provider the
// harness settings mount) -> webhook subscription with a test delivery to
// the local receiver (/harness/webhooks/, which dedupes by
// X-Golden-Delivery-Id) -> a non-staff login on /erp/ gets the styled 403.
//
// Runs on the fresh seed only (docs/release-testing.md). Login: crm-admin
// (core.manage_teams, crm.manage_pipelines, core.change_integrationaccount);
// the last step signs in as a login with no staff role, which
// core.release_test.ensure_nonstaff_login writes into the run's logins file.
import { journey, expect, logins } from "../lib/harness";
import { RUN_DIR, apiJson, bodyText, clickNav, count, manage, receiver, str } from "../lib/erp-ui";

const stamp = Date.now().toString(36);

journey("R10", "Setup", (step) => {
  step(1, "new user: created from Users, listed, holds the staff role", async (ctx) => {
    const { page, state, db, login } = ctx;
    await login("crm-admin");
    await page.goto("/erp/");
    state.username = str(state.username || `r10.user.${stamp}`);
    const existing = await db("auth.User", { username: str(state.username) });
    await clickNav(page, "Users");
    if (existing.length === 0) {
      await page.locator("a[href='/erp/users/create/']").first().click();
      await expect(page).toHaveURL(/\/erp\/users\/create\/$/);
      await page.fill("#id_first_name", "Rowan");
      await page.fill("#id_last_name", "Setup");
      await page.fill("#id_username", str(state.username));
      await page.fill("#id_email", `${state.username}@example.com`);
      const password = `Harness-${stamp}-Pw!`;
      await page.fill("#id_password1", password);
      await page.fill("#id_password2", password);
      await page.getByRole("button", { name: /Create User/ }).click();
      await expect(page).toHaveURL(/\/erp\/users\/$/);
    }
    await expect(page.locator("body")).toContainText(str(state.username));
    const user = (await db("auth.User", { username: str(state.username) }))[0];
    expect(user, "the user row").toBeTruthy();
    expect(Boolean(user.is_staff)).toBe(true);
    state.userId = str(user.id);
    const groups = await db("auth.Group", { user: str(user.id) });
    expect(groups.map((g) => str(g.name))).toContain("staff");
  });

  step(2, "roles: the new user can be given a role from the back office", async (ctx) => {
    const { page, state, db } = ctx;
    // Found 2026-10-17 (fixed the same day, core.tests.test_user_roles_screen):
    // roles were Groups in the Django admin only. The Users list now links
    // a Roles screen per user (auth.change_user); a staff role keeps
    // is_staff, and the row lists what the user holds.
    await clickNav(page, "Users");
    const row = page.locator("tr", { hasText: str(state.username) }).first();
    await row.getByRole("link", { name: /roles/i }).first().click();
    await expect(page).toHaveURL(new RegExp(`/erp/users/${state.userId}/roles/$`));
    await page.locator("#role-sales").check();
    await page.getByRole("button", { name: /Save roles/ }).click();
    await expect(page).toHaveURL(/\/erp\/users\/$/);
    await expect(page.locator(".alert").first()).toContainText(/granted sales/);
    const groups = await db("auth.Group", { user: str(state.userId) });
    expect(groups.map((g) => str(g.name)).sort()).toEqual(["sales", "staff"]);
    const user = (await db("auth.User", { username: str(state.username) }))[0];
    expect(Boolean(user.is_staff)).toBe(true);
    await expect(page.locator("tr", { hasText: str(state.username) }).first()).toContainText("Sales");
  });

  step(3, "team: created with a member, listed", async (ctx) => {
    const { page, state, db } = ctx;
    state.teamKey = str(state.teamKey || `r10-${stamp}`);
    state.teamName = str(state.teamName || `Setup team ${stamp}`);
    const existing = await db("core.Team", { key: str(state.teamKey) });
    await clickNav(page, "Teams");
    if (existing.length === 0) {
      await page.locator("a[href='/erp/settings/teams/new/']").first().click();
      await expect(page).toHaveURL(/\/erp\/settings\/teams\/new\/$/);
      await page.fill("#id_name", str(state.teamName));
      await page.fill("#id_key", str(state.teamKey));
      await page.locator("#id_is_active").check();
      await page.locator("input[name=members]").first().check();
      await page.getByRole("button", { name: /^Save$/ }).click();
      await expect(page).toHaveURL(/\/erp\/settings\/teams\/(#.*)?$/);
    }
    const teams = await db("core.Team", { key: str(state.teamKey) });
    expect(teams.length).toBe(1);
    state.teamId = str(teams[0].id);
    await expect(page.locator("body")).toContainText(str(teams[0].name));
    expect(await count(page, "core.UserProfile", { team: str(state.teamId) })).toBeGreaterThanOrEqual(1);
  });

  step(4, "pipeline and stages: a new pipeline with a stage", async (ctx) => {
    const { page, state, db } = ctx;
    state.pipelineKey = str(state.pipelineKey || `partners-${stamp}`);
    let pipelines = await db("crm.Pipeline", { key: str(state.pipelineKey) });
    if (pipelines.length === 0) {
      await clickNav(page, "Pipelines");
      await page.locator("a[href='/erp/crm/pipelines/new/']").first().click();
      await expect(page).toHaveURL(/\/erp\/crm\/pipelines\/new\/$/);
      await page.fill("#id_key", str(state.pipelineKey));
      await page.fill("#id_name", `Partners ${stamp}`);
      await page.locator("#id_is_active").check();
      await page.getByRole("button", { name: /^Save$/ }).click();
      await expect(page.locator(".alert").first()).toContainText(/Created/);
      pipelines = await db("crm.Pipeline", { key: str(state.pipelineKey) });
    }
    expect(pipelines.length).toBe(1);
    state.pipelineId = str(pipelines[0].id);
    let stages = await db("crm.Stage", { pipeline: str(state.pipelineId) });
    if (stages.length === 0) {
      await clickNav(page, "Pipelines");
      await page.locator(`a[href='/erp/crm/pipelines/${state.pipelineId}/stages/new/']`).first().click();
      await expect(page).toHaveURL(/\/stages\/new\/$/);
      await page.fill("#id_key", "introduced");
      await page.fill("#id_name", "Introduced");
      await page.fill("#id_default_probability", "10");
      await page.locator("#id_is_active").check();
      await page.getByRole("button", { name: /^Save$/ }).click();
      await expect(page.locator(".alert").first()).toContainText(/Added Introduced/);
      stages = await db("crm.Stage", { pipeline: str(state.pipelineId) });
    }
    expect(stages.map((s) => str(s.key))).toContain("introduced");
    await clickNav(page, "Pipelines");
    await expect(page.locator("body")).toContainText("Introduced");
    // Found 2026-10-17 (fixed the same day): the list scrolled the page
    // sideways at 360 px; the stages table now scrolls in its own box.
  });

  step(5, "labels: a tenant can reword a noun from the back office", async (ctx) => {
    const { page, db } = ctx;
    // Found 2026-10-17 (fixed the same day, core.tests.test_label_overrides_screen):
    // the tenant layer was a setting only. Admin -> Labels now stores a
    // rewording (core.LabelOverride) that every staff page shows at once.
    // Reworded and put back in one step, so later steps (and reruns) still
    // find "Engagements" in the sidebar.
    await clickNav(page, "Teams");
    await page.locator("a[href*='/erp/settings/labels/']").first().click();
    await expect(page).toHaveURL(/\/erp\/settings\/labels\/$/);
    const reword = page.locator("form[aria-label='Reword crm.engagement']");
    await reword.locator("input[name=singular]").fill("Deal");
    await reword.locator("input[name=plural]").fill("Deals");
    await reword.getByRole("button", { name: /^Save$/ }).click();
    await expect(page.locator("[data-testid='label-effective-crm.engagement']")).toHaveText("Deal");
    expect((await db("core.LabelOverride", { key: "crm.engagement" })).length).toBe(1);
    await expect(page.locator("a.nav-link .nav-link-title", { hasText: /^Deals$/ }).first()).toBeAttached();
    await reword.locator("input[name=singular]").fill("");
    await reword.locator("input[name=plural]").fill("");
    await reword.getByRole("button", { name: /^Save$/ }).click();
    await expect(page.locator("[data-testid='label-effective-crm.engagement']")).toHaveText("Engagement");
    expect((await db("core.LabelOverride", { key: "crm.engagement" })).length).toBe(0);
  });

  step(6, "theme: picked from the navbar, applied, kept across a reload", async (ctx) => {
    const { page } = ctx;
    await clickNav(page, "Teams");
    await page.locator("#navbarThemeDropdown").click();
    await page.locator("[data-theme-pick='midnight']").click();
    await expect(page.locator("#theme-css")).toHaveAttribute("href", /themes\/midnight\.css/);
    await page.reload();
    await expect(page.locator("#theme-css")).toHaveAttribute("href", /themes\/midnight\.css/);
    await expect(page.locator("[data-theme-pick='midnight']")).toHaveClass(/active/);
    // Back to the default so the remaining screens are checked on it.
    await page.locator("#navbarThemeDropdown").click();
    await page.locator("[data-theme-pick='golden']").click();
    await expect(page.locator("#theme-css")).toHaveAttribute("href", /themes\/golden\.css/);
  });

  step(7, "integration account: the fixture provider added, a credential stored, the connection tested", async (ctx) => {
    const { page, state, db } = ctx;
    await clickNav(page, "Integrations");
    let accounts = await db("core.IntegrationAccount", { provider: "fixture" });
    if (accounts.length === 0) {
      await page.locator("#new-provider").selectOption("fixture");
      await page.getByRole("button", { name: /^Add$/ }).click();
      await expect(page).toHaveURL(/\/erp\/integrations\//);
      accounts = await db("core.IntegrationAccount", { provider: "fixture" });
    }
    expect(accounts.length).toBe(1);
    state.accountId = str(accounts[0].id);
    const credForm = page.locator(`form[action='/erp/integrations/accounts/${state.accountId}/credentials/']`);
    // The forms sit under "Settings and credentials" (a <details>).
    const details = page.locator("details", { has: credForm });
    if (!(await credForm.isVisible())) await details.locator("summary").click();
    await expect(credForm).toBeVisible();
    await credForm.locator("select[name=kind]").selectOption({ index: 0 });
    await credForm.locator("input[name=secret]").fill(`harness-${stamp}-token`);
    await credForm.getByRole("button").click();
    await expect(page).toHaveURL(/\/erp\/integrations\//);
    const creds = (await db("core.IntegrationCredential", { account: str(state.accountId) })).filter(
      (c) => c.revoked_at == null,
    );
    expect(creds.length, "an active credential on the account").toBeGreaterThanOrEqual(1);
    // A new account starts disabled ("Add its credentials, then enable it").
    const enable = page.locator(`form[action='/erp/integrations/accounts/${state.accountId}/'] input[name=enabled][value='1']`);
    if (await enable.count()) {
      await enable.locator("xpath=..").getByRole("button", { name: /^Enable$/ }).click();
      await expect(page).toHaveURL(/\/erp\/integrations\//);
    }
    // Found 2026-10-17 (fixed the same day): the fixture adapter's API only
    // existed inside the test runner. config.settings.harness now installs
    // an offline stub transport (config/harness_transport.py).
    await page.locator(`form[action='/erp/integrations/accounts/${state.accountId}/test/'] button`).click();
    await expect(page).toHaveURL(/\/erp\/integrations\//);
    await expect(page.locator("body")).toContainText(/Connected\./);
  });

  step(8, "webhooks: a subscription added from the back office, then a test delivery", async (ctx) => {
    const { page, state, db } = ctx;
    // Found 2026-10-17 (fixed the same day, webhooks.tests.test_erp_screens):
    // subscriptions lived in the API and the Django admin only. The
    // Integrations page now lists, adds (secret shown once), toggles and
    // pings them. A target of its own, so step 9's API subscription stays one.
    const target = `${ctx.base}/harness/webhooks/?via=ui`;
    await clickNav(page, "Integrations");
    let subs = await db("webhooks.WebhookSubscription", { target_url: target });
    if (subs.length === 0) {
      await page.locator("#webhook-add summary").click();
      await page.fill("#webhook-target-url", target);
      await page.fill("#webhook-description", `Release harness (R10.08) ${stamp}`);
      await page.locator("#webhook-event-all").check();
      await page.getByRole("button", { name: /Add subscription/ }).click();
      await expect(page).toHaveURL(/\/erp\/integrations\//);
      await expect(page.locator(".alert").first()).toContainText(/shown once/);
      subs = await db("webhooks.WebhookSubscription", { target_url: target });
    }
    expect(subs.length).toBe(1);
    state.uiSubscriptionId = str(subs[0].id);
    const before = (await receiver(page)).count;
    await page.locator(`form[action='/erp/integrations/webhooks/${state.uiSubscriptionId}/ping/'] button`).click();
    await expect(page.locator(".alert").first()).toContainText(/Test delivery .* reached/);
    const after = await receiver(page);
    expect(after.count).toBe(before + 1);
    const ping = after.deliveries[after.deliveries.length - 1];
    expect(ping.event).toBe("webhook.ping");
    expect(ping.delivery_id, "the ping carries X-Golden-Delivery-Id").toBeTruthy();
    const logs = await db("webhooks.WebhookDeliveryLog", { subscription: state.uiSubscriptionId, event_type: "webhook.ping" });
    expect(logs.map((l) => str(l.delivery_id))).toContain(ping.delivery_id);
  });

  step(9, "webhook subscription through the API, a ping, then an outbox delivery to the local receiver", async (ctx) => {
    const { page, state, db, csrf } = ctx;
    await clickNav(page, "Integrations");
    const token = await csrf();
    const target = `${ctx.base}/harness/webhooks/`;
    let subs = await db("webhooks.WebhookSubscription", { target_url: target });
    if (subs.length === 0) {
      const made = await apiJson(page, token, "POST", "/api/v1/webhooks/subscriptions/", {
        target_url: target,
        secret: `harness-${stamp}-secret`,
        subscribed_events: ["party.created"],
        is_active: true,
        description: "Release harness receiver (R10)",
      });
      expect(made.status, made.text).toBe(201);
      subs = await db("webhooks.WebhookSubscription", { target_url: target });
    }
    expect(subs.length).toBe(1);
    state.subscriptionId = str(subs[0].id);
    // The operator's test delivery: one attempt, straight to the receiver.
    const before = (await receiver(page)).count;
    const ping = await apiJson(page, token, "POST", `/api/v1/webhooks/subscriptions/${state.subscriptionId}/ping/`);
    expect(ping.status, ping.text).toBe(200);
    expect(ping.json?.success, ping.text).toBe(true);
    const afterPing = await receiver(page);
    expect(afterPing.count).toBe(before + 1);
    const pingDelivery = afterPing.deliveries[afterPing.deliveries.length - 1];
    expect(pingDelivery.event).toBe("webhook.ping");
    // Observed 2026-10-17 (fixed the same day): the ping carried no
    // X-Golden-Delivery-Id; now it does, and the API answers it.
    expect(pingDelivery.delivery_id, "the API ping carries X-Golden-Delivery-Id").toBe(ping.json?.delivery_id);
    state.pingDeliveryId = pingDelivery.delivery_id;
    // A real event: a new party from the back office -> party.created ->
    // an outbox row -> process_outbox delivers it with a delivery id.
    await clickNav(page, "Parties");
    await page.locator("a[href='/erp/parties/new/']").first().click();
    await expect(page).toHaveURL(/\/erp\/parties\/new\/$/);
    await page.selectOption("#id_kind", "person");
    await page.fill("#id_given_name", "Webb");
    await page.fill("#id_family_name", `Hook ${stamp}`);
    await page.fill("#id_display_name", `Webb Hook ${stamp}`);
    await page.locator("button[name='_action'][value='save']").click();
    await expect(page).not.toHaveURL(/\/erp\/parties\/new\/$/);
    const party = (await db("parties.Party", { family_name: `Hook ${stamp}` }))[0];
    expect(party, "the party row").toBeTruthy();
    const queued = await count(page, "core.OutboxEvent", { consumer: "webhooks" });
    expect(queued, "outbox rows for the webhooks consumer").toBeGreaterThanOrEqual(1);
    const out = manage("process_outbox", "--once");
    expect(out).toMatch(/outbox: claimed \d+/);
    const after = await receiver(page);
    // Only this step's subscription: on a shared database (every journey in
    // one run, 2026-10-18) an earlier journey's subscription receives the
    // same party.created, and its delivery id is in another subscription's log.
    const delivered = after.deliveries.filter(
      (d) => d.event === "party.created" && d.delivery_id && d.subscription === str(state.subscriptionId),
    );
    expect(delivered.length, `party.created deliveries with an X-Golden-Delivery-Id (receiver: ${JSON.stringify(after.deliveries.map((d) => [d.event, d.delivery_id]))})`).toBeGreaterThanOrEqual(1);
    const logs = await db("webhooks.WebhookDeliveryLog", { subscription: str(state.subscriptionId), event_type: "party.created" });
    expect(logs.length).toBeGreaterThanOrEqual(1);
    expect(logs.map((l) => str(l.delivery_id))).toContain(delivered[0].delivery_id);
    expect(Object.keys(after.repeats)).toEqual([]);
  }, { axe: false, fit360: false });

  step(10, "a non-staff login on /erp/ gets the styled 403", async (ctx) => {
    const { page, state } = ctx;
    // The fixture: a login with no staff role (core.release_test), written
    // into the run's logins file; then a sign-in and the request to /erp/
    // made through the page's request context (no navigation, so the step's
    // own 4xx check does not fire on the status the step expects).
    manage("shell", "-c", `from core.release_test import ensure_nonstaff_login; ensure_nonstaff_login(${JSON.stringify(RUN_DIR)})`);
    const visitor = logins().find((l) => l.role === "nonstaff");
    expect(visitor, "the nonstaff login in logins.txt").toBeTruthy();
    await page.context().clearCookies();
    const form = await page.request.get(`${ctx.base}/erp/login/`);
    const token = (await page.context().cookies(ctx.base)).find((c) => c.name === "csrftoken")?.value ?? "";
    const html = await form.text();
    const field = html.match(/name="csrfmiddlewaretoken" value="([^"]+)"/)?.[1] ?? token;
    const signin = await page.request.post(`${ctx.base}/erp/login/`, {
      form: { username: visitor!.username, password: visitor!.password, csrfmiddlewaretoken: field },
      headers: { Referer: `${ctx.base}/erp/login/` },
      maxRedirects: 0,
    });
    expect([302, 200]).toContain(signin.status());
    const erp = await page.request.get(`${ctx.base}/erp/`, { maxRedirects: 0 });
    state.nonstaffStatus = erp.status();
    expect(erp.status(), "a non-staff session on /erp/").toBe(403);
    const body = await erp.text();
    // Found 2026-10-17; fixed by PR #131 (the styled 403 with sign-out and back links).
    expect(body).toMatch(/sign out|log out/i);
    expect(body).toMatch(/back|home/i);
    expect(body).toMatch(/<link[^>]+stylesheet/i);
  }, { axe: false, fit360: false });
});
