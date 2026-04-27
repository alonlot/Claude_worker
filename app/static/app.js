const list = document.querySelector("#queue-list");
const orderInput = document.querySelector("#queue-order");
const startDrop = document.querySelector("#start-drop");
let dragged = null;

function syncOrder() {
  if (!list || !orderInput) return;
  orderInput.value = [...list.querySelectorAll("[data-id]")].map((el) => el.dataset.id).join(",");
}

if (list) {
  list.addEventListener("dragstart", (event) => {
    dragged = event.target.closest("[data-id]");
  });
  list.addEventListener("dragover", (event) => {
    event.preventDefault();
    const target = event.target.closest("[data-id]");
    if (!dragged || !target || dragged === target) return;
    const box = target.getBoundingClientRect();
    const after = event.clientY > box.top + box.height / 2;
    target.parentNode.insertBefore(dragged, after ? target.nextSibling : target);
    syncOrder();
  });
  list.addEventListener("drop", syncOrder);
  syncOrder();
}

if (startDrop) {
  startDrop.addEventListener("dragover", (event) => {
    event.preventDefault();
    startDrop.classList.add("active");
  });
  startDrop.addEventListener("dragleave", () => startDrop.classList.remove("active"));
  startDrop.addEventListener("drop", async (event) => {
    event.preventDefault();
    startDrop.classList.remove("active");
    if (!dragged || !dragged.dataset.id) return;
    if (!["needs_plan", "plan_ready", "queued"].includes(dragged.dataset.state)) {
      announce("This ticket is not ready to build");
      return;
    }
    try {
      const response = await fetch(`/queue/${dragged.dataset.id}/build`, { method: "POST" });
      if (response.ok) {
        announce("Build started");
        window.setTimeout(() => window.location.reload(), 350);
      } else {
        announce("Could not start build");
      }
    } catch {
      announce("Network error");
    }
  });
}

async function pollNotifications() {
  if (!("Notification" in window)) return;
  if (Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch { return; }
  }
  if (Notification.permission !== "granted") return;
  try {
    const response = await fetch("/notifications/unread");
    if (!response.ok) return;
    const notifications = await response.json();
    for (const item of notifications) {
      new Notification(item.title, { body: item.message || item.level });
    }
  } catch {
    // Notification polling should never disturb the page.
  }
}

setInterval(pollNotifications, 10000);
pollNotifications();

function bindBusyButtons(root = document) {
  const forms = root.querySelectorAll("form");
  for (const form of forms) {
    if (form.dataset.busyBound === "1") continue;
    form.dataset.busyBound = "1";
    form.addEventListener("submit", () => {
      const submitter = form.querySelector('button[type="submit"], button:not([type])');
      if (!submitter) return;
      const loadingText = submitter.dataset.loadingText;
      if (!loadingText) return;
      submitter.dataset.originalText = submitter.textContent;
      submitter.textContent = loadingText;
      submitter.disabled = true;
      form.dataset.submitting = "1";
    });
  }
}

function restoreBusyForm(form) {
  if (!form || form.dataset.submitting !== "1") return;
  const submitter = form.querySelector('button[type="submit"], button:not([type])');
  if (submitter && submitter.dataset.originalText) {
    submitter.textContent = submitter.dataset.originalText;
    submitter.disabled = false;
  }
  form.dataset.submitting = "0";
}

function announce(message) {
  let toast = document.querySelector("#toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.className = "toast";
    toast.setAttribute("role", "status");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(announce.timeoutId);
  announce.timeoutId = window.setTimeout(() => toast.classList.remove("show"), 2200);
}

function initFollowLogs(root = document) {
  const terminal = root.querySelector("#live-logs-terminal");
  const toggle = root.querySelector("#follow-logs-toggle");
  if (!terminal || !toggle || toggle.dataset.followBound === "1") return;
  toggle.dataset.followBound = "1";

  let userScrolledUp = false;
  const nearBottom = () => terminal.scrollTop + terminal.clientHeight >= terminal.scrollHeight - 24;
  const scrollToBottom = () => {
    terminal.scrollTop = terminal.scrollHeight;
  };

  const updateFromScroll = () => {
    userScrolledUp = !nearBottom();
    if (userScrolledUp && toggle.checked) {
      toggle.checked = false;
    }
  };

  terminal.addEventListener("scroll", updateFromScroll);
  toggle.addEventListener("change", () => {
    if (toggle.checked) {
      userScrolledUp = false;
      scrollToBottom();
    }
  });

  const observer = new MutationObserver(() => {
    if (toggle.checked && !userScrolledUp) {
      scrollToBottom();
    }
  });
  observer.observe(terminal, { childList: true, characterData: true, subtree: true });
  scrollToBottom();
}

function initLiveLogsPolling(root = document) {
  const terminal = root.querySelector("#live-logs-terminal");
  if (!terminal || terminal.dataset.pollBound === "1") return;
  const runId = terminal.dataset.runId;
  if (!runId) return;
  terminal.dataset.pollBound = "1";

  const refresh = async () => {
    try {
      const response = await fetch(`/runs/${runId}/logs`, { cache: "no-store" });
      if (!response.ok) return;
      const content = await response.text();
      if (terminal.textContent !== content) {
        terminal.textContent = content;
      }
    } catch {
      // Ignore transient polling failures.
    }
  };

  refresh();
  const intervalId = window.setInterval(refresh, 2000);
  window.addEventListener("beforeunload", () => window.clearInterval(intervalId), { once: true });
}

bindBusyButtons(document);
initFollowLogs(document);
initLiveLogsPolling(document);
document.addEventListener("htmx:afterSwap", (event) => {
  const root = event.target instanceof Element ? event.target : document;
  bindBusyButtons(root);
  initFollowLogs(document);
  initLiveLogsPolling(document);
  if (event.target && event.target.id === "run-interaction") {
    announce("Updated");
  }
});

document.addEventListener("htmx:responseError", (event) => {
  restoreBusyForm(event.target);
  announce("Request failed");
});

document.addEventListener("htmx:sendError", (event) => {
  restoreBusyForm(event.target);
  announce("Network error");
});
