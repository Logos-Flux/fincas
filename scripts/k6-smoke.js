import http from "k6/http";
import { check, sleep } from "k6";

// Local-dev smoke baseline (NOT staging numbers). Seed: seed_test_minimal.
// Run: k6 run --vus 10 --duration 30s scripts/k6-smoke.js
export const options = {
  vus: 10,
  duration: "30s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<1000"],
  },
};

const BASE = __ENV.K6_BASE || "http://127.0.0.1:8002";
// Login user for the session-auth leg. Staging runs pass the staging admin
// via K6_USER/K6_PASS env (never commit credentials here).
const USERNAME = __ENV.K6_USER || "tester";
const PASSWORD = __ENV.K6_PASS || "test12345";

// Per-VU session: module-scope state is VU-local in k6, so each virtual
// user logs in once and reuses its session. Besides being realistic, this
// keeps the session-auth leg under the login throttle (20/min per
// IP+username, F-046/047) instead of measuring 429s after the first seconds.
let sessionCookies = null;

export default function () {
  let res = http.get(`${BASE}/api/v1/products/products/`);
  check(res, { "catalog 200": (r) => r.status === 200 });
  res = http.get(`${BASE}/api/v1/products/products/?search=test-product-01`);
  check(res, { "search 200": (r) => r.status === 200 });
  // Login + authenticated ERP list (session auth).
  if (sessionCookies === null) {
    const loginPage = http.get(`${BASE}/erp/login/`);
    const m = loginPage.body.match(/name="csrfmiddlewaretoken" value="([^"]+)"/);
    if (m) {
      // Browsers always send Origin/Referer on POSTs; Django's HTTPS
      // CSRF check 403s without them (local HTTP never noticed).
      const login = http.post(
        `${BASE}/erp/login/`,
        { csrfmiddlewaretoken: m[1], username: USERNAME, password: PASSWORD },
        {
          cookies: loginPage.cookies,
          headers: { Origin: BASE, Referer: `${BASE}/erp/login/` },
        },
      );
      check(login, { "login ok": (r) => r.status === 200 || r.status === 302 });
      if (login.status === 200 || login.status === 302) {
        sessionCookies = login.cookies;
      }
    }
  } else {
    const orders = http.get(`${BASE}/erp/orders/`, { cookies: sessionCookies });
    check(orders, { "orders 200": (r) => r.status === 200 });
  }
  sleep(1);
}
