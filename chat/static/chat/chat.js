/* ERP chat assistant — drawer logic.
 *
 * - Toggles the drawer from the FAB.
 * - Sends user messages via fetch() to /erp/chat/stream/, reads the SSE
 *   response body in chunks, and renders frames as they arrive.
 * - Scrapes page context (pathname + body.dataset.chatContext) and includes
 *   it with every message so the assistant knows what page the user is on.
 * - Persists the chosen model and current session id in localStorage so the
 *   drawer survives full page navigations.
 */

(function () {
  "use strict";

  // ---- DOM lookup -------------------------------------------------------

  const fab = document.getElementById("erp-chat-fab");
  const drawer = document.getElementById("erp-chat-drawer");
  const closeBtn = document.getElementById("erp-chat-close");
  const newBtn = document.getElementById("erp-chat-new");
  const historyToggle = document.getElementById("erp-chat-history-toggle");
  const historyPanel = document.getElementById("erp-chat-history");
  const historyList = document.getElementById("erp-chat-history-list");
  const transcript = document.getElementById("erp-chat-transcript");
  const form = document.getElementById("erp-chat-form");
  const input = document.getElementById("erp-chat-input");
  const sendBtn = document.getElementById("erp-chat-send");
  const status = document.getElementById("erp-chat-status");
  // Direct mode: the dropdown only. Gateway mode: the served-model readout,
  // plus the dropdown once more than one lane is configured.
  const modelSelect = document.getElementById("erp-chat-model");
  const modelName = document.getElementById("erp-chat-model-name");
  const configEl = document.getElementById("erp-chat-config");

  if (!fab || !drawer || !configEl) {
    // Drawer not on this page (e.g. login screen). Bail quietly.
    return;
  }

  const config = JSON.parse(configEl.textContent);

  // ---- Local state ------------------------------------------------------

  const STORAGE_MODEL_KEY = "erp-chat-model";
  const STORAGE_SESSION_KEY = "erp-chat-session";

  let currentSessionId = localStorage.getItem(STORAGE_SESSION_KEY) || null;
  const defaultModelKey = (
    config.models.find((m) => m.default) || config.models[0]
  ).key;
  const storedModelKey = localStorage.getItem(STORAGE_MODEL_KEY);
  // A stored key can outlive the model it names (registry swap). Keeping it
  // would leave the dropdown showing one model while requests carry another,
  // so anything the server no longer offers falls back to the default.
  let currentModelKey = config.models.some((m) => m.key === storedModelKey)
    ? storedModelKey
    : defaultModelKey;
  let isStreaming = false;
  let historyLoaded = false;
  // Tracks the in-progress assistant message DOM node so streaming text
  // deltas append to it instead of creating new bubbles.
  let activeAssistantBubble = null;
  // Maps tool_use_id → DOM node of the chip in the transcript so we can
  // mutate it in place when the result frame arrives.
  const activeToolChips = {};

  // ---- Render the model dropdown ---------------------------------------

  function populateModelDropdown() {
    if (!modelSelect) return;
    modelSelect.innerHTML = "";
    for (const m of config.models) {
      const opt = document.createElement("option");
      opt.value = m.key;
      opt.textContent = m.label;
      if (m.cost_hint) opt.title = m.cost_hint;
      if (m.key === currentModelKey) opt.selected = true;
      modelSelect.appendChild(opt);
    }
  }

  populateModelDropdown();

  if (modelSelect) {
    modelSelect.addEventListener("change", () => {
      currentModelKey = modelSelect.value;
      localStorage.setItem(STORAGE_MODEL_KEY, currentModelKey);
      clearRemovedOption();
      if (config.gateway) showLaneObservation(currentModelKey);
    });
  }

  // Gateway mode: name the concrete model the gateway reports serving.
  // `at` (ISO) is when it was observed; absent means "just now".
  const observed = config.observed || {};
  function showServedModel(name, at) {
    if (!modelName || !name) return;
    modelName.textContent = `served: ${name}`;
    const when = at ? new Date(at).toLocaleString() : "just now";
    modelName.title = `Last observed ${when} — the gateway lane decides the model`;
  }

  // Switching lanes: show what that lane was last seen serving, if anything.
  function showLaneObservation(key) {
    if (!modelName) return;
    const obs = observed[key];
    if (obs && obs.model) {
      showServedModel(obs.model, obs.at);
    } else {
      const m = config.models.find((x) => x.key === key);
      modelName.textContent = m ? m.label : "";
      modelName.title = "Not observed yet — the gateway lane decides the model";
    }
  }

  // A session can outlive the model it was started on. Show the stored id
  // greyed out so the switch is visible, while the next send goes to the
  // default (the server already resolved model_id to it).
  function clearRemovedOption() {
    if (!modelSelect) return;
    const stale = modelSelect.querySelector("option[data-removed]");
    if (stale) stale.remove();
  }

  function showRemovedModel(storedKey, liveKey) {
    if (modelSelect && storedKey) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.disabled = true;
      opt.dataset.removed = "1";
      opt.textContent = `${storedKey} (removed)`;
      modelSelect.insertBefore(opt, modelSelect.firstChild);
      modelSelect.value = liveKey;
    }
    const live = config.models.find((m) => m.key === liveKey);
    status.textContent = `This chat's model (${storedKey}) was removed — replies now use ${live ? live.label : "the default"}.`;
  }

  // ---- Drawer open / close ---------------------------------------------

  function openDrawer() {
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    drawer.inert = false;
    // Lazy-load the history list on first open so we don't pay the cost
    // on pages where the user never opens the drawer.
    if (!historyLoaded) {
      historyLoaded = true;
      refreshHistory();
    }
    setTimeout(() => input.focus(), 250);
  }

  function closeDrawer() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    // inert, not just aria-hidden: a closed drawer must not take focus.
    drawer.inert = true;
  }

  fab.addEventListener("click", openDrawer);
  closeBtn.addEventListener("click", closeDrawer);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drawer.classList.contains("open")) {
      closeDrawer();
    }
  });

  // ---- New chat ---------------------------------------------------------

  newBtn.addEventListener("click", async () => {
    if (isStreaming) return;
    currentSessionId = null;
    localStorage.removeItem(STORAGE_SESSION_KEY);
    transcript.innerHTML = `
      <div class="erp-chat-empty text-center text-secondary py-5">
        <i class="fas fa-robot fa-2x mb-3 d-block opacity-50"></i>
        <p class="mb-1">New chat started.</p>
      </div>
    `;
    setStatus("");
    markActiveSession(null);
    input.focus();
  });

  // ---- History sidebar --------------------------------------------------

  historyToggle.addEventListener("click", () => {
    historyPanel.classList.toggle("open");
    const open = historyPanel.classList.contains("open");
    historyPanel.setAttribute("aria-hidden", open ? "false" : "true");
    historyPanel.inert = !open;
    if (open) refreshHistory();
  });

  async function refreshHistory() {
    try {
      const resp = await fetch(config.endpoints.sessions, {
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) {
        historyList.innerHTML = `<div class="text-center text-secondary p-3"><small>Failed to load.</small></div>`;
        return;
      }
      const data = await resp.json();
      renderHistoryList(data.results || []);
    } catch (e) {
      historyList.innerHTML = `<div class="text-center text-secondary p-3"><small>Network error.</small></div>`;
    }
  }

  function renderHistoryList(sessions) {
    if (!sessions.length) {
      historyList.innerHTML = `<div class="text-center text-secondary p-3"><small>No past chats yet.</small></div>`;
      return;
    }
    historyList.innerHTML = "";
    for (const s of sessions) {
      const item = document.createElement("div");
      item.className = "erp-chat-history-item";
      if (s.id === currentSessionId) item.classList.add("active");
      item.dataset.sessionId = s.id;
      item.title = s.title || "Untitled";
      const ts = formatTimestamp(s.updated_at);
      item.innerHTML = `${escapeHtml(s.title || "Untitled")}<span class="timestamp">${escapeHtml(ts)}</span>`;
      item.addEventListener("click", () => loadSession(s.id));
      historyList.appendChild(item);
    }
  }

  function markActiveSession(id) {
    historyList.querySelectorAll(".erp-chat-history-item").forEach((el) => {
      el.classList.toggle("active", el.dataset.sessionId === id);
    });
  }

  async function loadSession(sessionId) {
    if (isStreaming) return;
    setStatus("Loading…");
    try {
      const resp = await fetch(`${config.endpoints.sessions}${sessionId}/`);
      if (!resp.ok) {
        addErrorMessage(`Failed to load session: ${resp.status}`);
        return;
      }
      const data = await resp.json();
      currentSessionId = sessionId;
      localStorage.setItem(STORAGE_SESSION_KEY, sessionId);
      if (data.model_id) {
        currentModelKey = data.model_id;
        if (modelSelect) modelSelect.value = data.model_id;
        localStorage.setItem(STORAGE_MODEL_KEY, data.model_id);
      }
      clearRemovedOption();
      if (data.model_removed) showRemovedModel(data.stored_model_id, data.model_id);
      showServedModel(data.served_model);
      renderTranscript(data.messages || []);
      markActiveSession(sessionId);
    } catch (e) {
      addErrorMessage(`Network error loading session: ${e.message}`);
    } finally {
      setStatus("");
    }
  }

  function renderTranscript(messages) {
    transcript.innerHTML = "";
    if (!messages.length) {
      transcript.innerHTML = `
        <div class="erp-chat-empty text-center text-secondary py-5">
          <p class="mb-1">Empty session.</p>
        </div>
      `;
      return;
    }
    for (const m of messages) {
      if (m.role === "user") {
        const node = document.createElement("div");
        node.className = "erp-chat-msg erp-chat-msg-user";
        node.textContent = m.content;
        transcript.appendChild(node);
      } else if (m.role === "assistant") {
        if (m.content) {
          const node = document.createElement("div");
          node.className = "erp-chat-msg erp-chat-msg-assistant";
          node.textContent = m.content;
          transcript.appendChild(node);
        }
        for (const call of m.tool_calls || []) {
          const chip = document.createElement("div");
          chip.className = "erp-chat-tool-call success";
          chip.innerHTML = `
            <i class="fas fa-check-circle"></i>
            <span class="tool-name">${escapeHtml(call.name || "")}</span>
            <span class="tool-args">${escapeHtml(summariseArgs(call.input || {}))}</span>
          `;
          transcript.appendChild(chip);
        }
      } else if (m.role === "tool") {
        // Already rendered as a chip on the assistant turn above; skip.
      }
    }
    scrollToBottom();
  }

  function formatTimestamp(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    if (sameDay) {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  // ---- Page context scraping -------------------------------------------

  function gatherContext() {
    const ctx = {
      path: window.location.pathname,
      page_title: document.title,
    };
    // Templates opt into richer context with a <meta name="chat-context">
    // tag containing a JSON object. Any template can override the
    // chat_context block in base.html to add its record IDs.
    const meta = document.querySelector('meta[name="chat-context"]');
    if (meta && meta.content) {
      try {
        Object.assign(ctx, JSON.parse(meta.content));
      } catch (e) {
        console.warn("erp-chat: invalid chat-context meta JSON", e);
      }
    }
    return ctx;
  }

  // ---- CSRF token cookie ------------------------------------------------

  function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(";").shift();
    return "";
  }

  // ---- Session management -----------------------------------------------

  async function ensureSession() {
    if (currentSessionId) return currentSessionId;
    const resp = await fetch(config.endpoints.session_new, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCookie("csrftoken"),
      },
      body: JSON.stringify({ model_id: currentModelKey }),
    });
    if (!resp.ok) {
      throw new Error(`Failed to create session: ${resp.status}`);
    }
    const data = await resp.json();
    currentSessionId = data.id;
    localStorage.setItem(STORAGE_SESSION_KEY, currentSessionId);
    // Refresh the history list in the background so the new session shows
    // up in the sidebar after the title gets auto-set on the first message.
    setTimeout(() => refreshHistory(), 1500);
    return currentSessionId;
  }

  // ---- Rendering helpers ------------------------------------------------

  function clearEmptyState() {
    const empty = transcript.querySelector(".erp-chat-empty");
    if (empty) empty.remove();
  }

  function addUserMessage(text) {
    clearEmptyState();
    const node = document.createElement("div");
    node.className = "erp-chat-msg erp-chat-msg-user";
    node.textContent = text;
    transcript.appendChild(node);
    scrollToBottom();
  }

  function startAssistantMessage() {
    const node = document.createElement("div");
    node.className = "erp-chat-msg erp-chat-msg-assistant erp-chat-msg-streaming";
    node.textContent = "";
    transcript.appendChild(node);
    activeAssistantBubble = node;
    scrollToBottom();
  }

  function appendAssistantText(text) {
    if (!activeAssistantBubble) startAssistantMessage();
    activeAssistantBubble.textContent += text;
    scrollToBottom();
  }

  function finalizeAssistantMessage() {
    if (activeAssistantBubble) {
      activeAssistantBubble.classList.remove("erp-chat-msg-streaming");
      // If the assistant only emitted tool calls and no text, drop the empty bubble.
      if (!activeAssistantBubble.textContent.trim()) {
        activeAssistantBubble.remove();
      }
      activeAssistantBubble = null;
    }
  }

  function addToolUseChip(frame) {
    clearEmptyState();
    finalizeAssistantMessage();
    const chip = document.createElement("div");
    chip.className = "erp-chat-tool-call";
    chip.innerHTML = `
      <i class="fas fa-cog fa-spin"></i>
      <span class="tool-name">${escapeHtml(frame.name)}</span>
      <span class="tool-args">${escapeHtml(summariseArgs(frame.input))}</span>
    `;
    transcript.appendChild(chip);
    activeToolChips[frame.id] = chip;
    scrollToBottom();
  }

  function updateToolResultChip(frame) {
    const chip = activeToolChips[frame.tool_use_id];
    if (!chip) return;
    const result = frame.content;
    const isError = result && typeof result === "object" && "error" in result;
    chip.classList.add(isError ? "error" : "success");
    chip.querySelector(".fa-cog")?.remove();
    const icon = document.createElement("i");
    icon.className = isError ? "fas fa-circle-exclamation" : "fas fa-check-circle";
    chip.prepend(icon);
    const summary = document.createElement("span");
    summary.className = "tool-result-summary";
    summary.textContent = " → " + summariseResult(result);
    chip.appendChild(summary);
    scrollToBottom();
  }

  function addErrorMessage(message) {
    finalizeAssistantMessage();
    const node = document.createElement("div");
    node.className = "erp-chat-msg erp-chat-msg-error";
    node.innerHTML = `<i class="fas fa-triangle-exclamation me-2"></i>${escapeHtml(message)}`;
    transcript.appendChild(node);
    scrollToBottom();
  }

  function scrollToBottom() {
    transcript.scrollTop = transcript.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  function summariseArgs(input) {
    if (!input || typeof input !== "object") return "";
    const keys = Object.keys(input);
    if (keys.length === 0) return "()";
    const parts = keys.slice(0, 3).map((k) => {
      const v = input[k];
      const s = typeof v === "string" ? `"${v}"` : JSON.stringify(v);
      return `${k}=${s.length > 30 ? s.slice(0, 30) + "…" : s}`;
    });
    if (keys.length > 3) parts.push("…");
    return `(${parts.join(", ")})`;
  }

  function summariseResult(result) {
    if (result == null) return "ok";
    if (typeof result !== "object") return String(result).slice(0, 60);
    if ("error" in result) return result.message || result.error || "error";
    if (Array.isArray(result)) return `${result.length} row(s)`;
    if ("id" in result) return `${result.id.toString().slice(0, 8)}…`;
    if ("deleted" in result) return "deleted";
    return Object.keys(result).slice(0, 3).join(", ");
  }

  function setStatus(text) {
    status.textContent = text;
  }

  // ---- Sending a message ------------------------------------------------

  async function sendMessage(text) {
    if (!text.trim() || isStreaming) return;
    isStreaming = true;
    sendBtn.disabled = true;
    input.value = "";
    autosizeTextarea();

    addUserMessage(text);
    setStatus("Thinking…");

    let sessionId;
    try {
      sessionId = await ensureSession();
    } catch (e) {
      addErrorMessage(`Couldn't create chat session: ${e.message}`);
      isStreaming = false;
      sendBtn.disabled = false;
      setStatus("");
      return;
    }

    const payload = {
      session_id: sessionId,
      message: text,
      context: gatherContext(),
      model_id: currentModelKey,
    };

    try {
      const resp = await fetch(config.endpoints.stream, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // ChatStreamView enforces CSRF (SEC-106) — the token is required.
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: JSON.stringify(payload),
      });

      if (!resp.ok) {
        const errText = await resp.text().catch(() => "");
        addErrorMessage(`Server returned ${resp.status}: ${errText.slice(0, 200)}`);
        return;
      }

      await consumeStream(resp.body);
    } catch (e) {
      addErrorMessage(`Network error: ${e.message}`);
    } finally {
      finalizeAssistantMessage();
      isStreaming = false;
      sendBtn.disabled = false;
      setStatus("");
      input.focus();
    }
  }

  async function consumeStream(stream) {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by blank lines (\n\n).
      let frameEnd;
      while ((frameEnd = buffer.indexOf("\n\n")) !== -1) {
        const rawFrame = buffer.slice(0, frameEnd);
        buffer = buffer.slice(frameEnd + 2);
        for (const line of rawFrame.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const data = line.slice("data: ".length);
          try {
            handleFrame(JSON.parse(data));
          } catch (e) {
            console.warn("erp-chat: bad SSE frame", data, e);
          }
        }
      }
    }
  }

  function handleFrame(frame) {
    switch (frame.type) {
      case "text_delta":
        appendAssistantText(frame.text || "");
        break;
      case "model":
        showServedModel(frame.name);
        break;
      case "tool_use":
        addToolUseChip(frame);
        break;
      case "tool_result":
        updateToolResultChip(frame);
        break;
      case "turn_end":
        finalizeAssistantMessage();
        if (frame.input_tokens || frame.output_tokens) {
          setStatus(
            `${frame.input_tokens || 0} in / ${frame.output_tokens || 0} out tokens`,
          );
        }
        break;
      case "error":
        addErrorMessage(frame.message || "Unknown error");
        break;
      default:
        // Forward-compat: unknown types are no-ops, not crashes.
        break;
    }
  }

  // ---- Form submission --------------------------------------------------

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage(input.value);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input.value);
    }
  });

  // ---- Textarea autosize ------------------------------------------------

  // Keep in step with max-height on .erp-chat-input-row textarea (chat.css).
  const INPUT_MAX_HEIGHT = 120;

  function autosizeTextarea() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, INPUT_MAX_HEIGHT) + "px";
  }
  input.addEventListener("input", autosizeTextarea);
})();
