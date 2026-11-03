/* LLM Settings page behaviour.
 *
 * Lane tests are one short POST per purpose, each aborted client-side at the
 * server's test timeout (+2s grace), never one blocking multi-lane request: a
 * cold scale-to-zero lane can legally take 90s, and holding a request open
 * that long is exactly the idle-socket failure mode worth designing out.
 */
(function () {
  "use strict";

  const root = document.getElementById("llm-settings");
  if (!root) return;

  const csrf = () => (document.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "";
  const timeoutMs = (parseInt(root.dataset.timeoutMs, 10) || 15000) + 2000;

  const LABELS = {
    bad_lane: "bad lane (the gateway does not know this route)",
    auth: "auth failure — token rejected",
    edge_forbidden: "edge refused this path/caller",
    rate_limited: "budget or rate limit hit",
    cold_lane: "cold lane — warming up",
    timeout: "no response — lane may be warming up",
    unreachable: "edge unreachable",
    shape: "unexpected response shape",
    config: "not configured",
    dimension: "wrong embedding dimension",
    no_tool_call: "answered, but cannot call tools",
    upstream: "upstream error",
  };

  function render(purpose, html, cls) {
    const cell = root.querySelector(`[data-llm-result="${purpose}"]`);
    if (!cell) return;
    cell.className = `small mt-1 ${cls || ""}`;
    cell.innerHTML = html;
  }

  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s == null ? "" : String(s);
    return div.innerHTML;
  }

  async function testPurpose(purpose) {
    const button = root.querySelector(`[data-llm-test="${purpose}"]`);
    if (button) button.disabled = true;
    render(purpose, '<span class="spinner-border spinner-border-sm me-1" role="status"></span>testing…', "text-secondary");
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const resp = await fetch(root.dataset.testUrl.replace("PURPOSE", encodeURIComponent(purpose)), {
        method: "POST",
        headers: { "X-CSRFToken": csrf() },
        signal: ctrl.signal,
      });
      const data = await resp.json();
      if (data.status === "skipped") {
        render(purpose, escapeHtml(data.message), "text-secondary");
      } else if (data.status === "ok") {
        const tool = data.tool_call === "ok" ? " · tool call OK" : "";
        const model = data.resolved_model ? ` · ${escapeHtml(data.resolved_model)}` : "";
        render(purpose, `OK ${data.elapsed_ms}ms${model}${tool}`, "text-success");
      } else {
        const label = LABELS[data.category] || data.category || "failed";
        render(
          purpose,
          `<strong>${escapeHtml(label)}</strong><div class="text-secondary">${escapeHtml(data.message || data.error || "")}</div>`,
          "text-danger",
        );
      }
    } catch (e) {
      render(purpose, "<strong>no response — lane may be warming up</strong>", "text-danger");
    } finally {
      clearTimeout(timer);
      if (button) button.disabled = false;
    }
  }

  root.querySelectorAll("[data-llm-test]").forEach((btn) => {
    btn.addEventListener("click", () => testPurpose(btn.dataset.llmTest));
  });

  const testAll = root.querySelector("[data-llm-test-all]");
  if (testAll) {
    testAll.addEventListener("click", async () => {
      testAll.disabled = true;
      const purposes = Array.from(root.querySelectorAll("[data-llm-test]")).map((b) => b.dataset.llmTest);
      // Concurrent, but each lane is still its own request with its own timeout.
      await Promise.allSettled(purposes.map(testPurpose));
      testAll.disabled = false;
    });
  }

  // ---- Mode change: a deliberately heavy control ----------------------------
  const form = document.getElementById("llm-connection-form");
  const modalEl = document.getElementById("llm-mode-modal");
  if (form && modalEl) {
    let confirmed = false;
    form.addEventListener("submit", (ev) => {
      const chosen = form.querySelector("input[name=mode]:checked");
      const current = form.dataset.currentMode;
      if (confirmed || !chosen || chosen.value === current) return;
      ev.preventDefault();
      modalEl.querySelector("[data-llm-mode-copy]").textContent =
        `Switch from ${current.toUpperCase()} to ${chosen.value.toUpperCase()}? Every ERP chat turn and research call changes route immediately.`;
      const lanes = parseInt(form.dataset.laneCount, 10) || 0;
      modalEl.querySelector("[data-llm-orphans]").textContent = lanes
        ? `${lanes} lane assignment(s) were made for ${current} mode and will be orphaned in ${chosen.value} mode (kept, not deleted).`
        : "";
      const Modal = window.bootstrap && window.bootstrap.Modal;
      if (Modal) {
        Modal.getOrCreateInstance(modalEl).show();
      } else if (window.confirm(modalEl.querySelector("[data-llm-mode-copy]").textContent)) {
        confirmed = true;
        form.submit();
      }
    });
    modalEl.querySelector("[data-llm-mode-confirm]").addEventListener("click", () => {
      confirmed = true;
      form.requestSubmit();
    });
  }

  // ---- Destructive row actions ----------------------------------------------
  root.querySelectorAll("form[data-llm-confirm]").forEach((f) => {
    f.addEventListener("submit", (ev) => {
      if (!window.confirm(f.dataset.llmConfirm)) ev.preventDefault();
    });
  });
})();
