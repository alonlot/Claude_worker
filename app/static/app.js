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

function initWebIde(root = document) {
  const ide = root.querySelector("#web-ide");
  if (!ide || ide.dataset.ideBound === "1") return;
  ide.dataset.ideBound = "1";
  const runId = ide.dataset.runId;
  const fileList = ide.querySelector("#web-ide-file-list");
  const editor = ide.querySelector("#web-ide-content");
  const current = ide.querySelector("#web-ide-current-file");
  const save = ide.querySelector("#web-ide-save");
  const status = ide.querySelector("#web-ide-status");
  let activePath = "";

  const setStatus = (message) => {
    status.textContent = message;
  };

  const loadFile = async (path) => {
    setStatus("Loading...");
    const response = await fetch(`/runs/${runId}/workspace/file?path=${encodeURIComponent(path)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) {
      setStatus(data.error || "Could not open file");
      return;
    }
    activePath = data.path;
    current.textContent = activePath;
    editor.value = data.content || "";
    save.disabled = false;
    setStatus("Ready");
  };

  const loadFiles = async () => {
    try {
      const response = await fetch(`/runs/${runId}/workspace/files`, { cache: "no-store" });
      const files = await response.json();
      fileList.textContent = "";
      if (!files.length) {
        const empty = document.createElement("span");
        empty.className = "empty";
        empty.textContent = "No workspace files yet.";
        fileList.appendChild(empty);
        setStatus("Workspace unavailable");
        return;
      }
      for (const file of files) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "web-ide-file";
        button.textContent = file.path;
        button.addEventListener("click", () => loadFile(file.path));
        fileList.appendChild(button);
      }
      await loadFile(files[0].path);
    } catch {
      fileList.textContent = "";
      const empty = document.createElement("span");
      empty.className = "empty";
      empty.textContent = "Could not load workspace.";
      fileList.appendChild(empty);
      setStatus("Workspace unavailable");
    }
  };

  save.addEventListener("click", async () => {
    if (!activePath) return;
    save.disabled = true;
    setStatus("Saving...");
    const body = new URLSearchParams({ path: activePath, content: editor.value });
    const response = await fetch(`/runs/${runId}/workspace/file`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    const data = await response.json();
    save.disabled = false;
    if (!response.ok) {
      setStatus(data.error || "Save failed");
      announce("Save failed");
      return;
    }
    setStatus("Saved");
    announce("File saved");
  });

  loadFiles();
}

bindBusyButtons(document);
initFollowLogs(document);
initLiveLogsPolling(document);
initWebIde(document);
document.addEventListener("htmx:afterSwap", (event) => {
  const root = event.target instanceof Element ? event.target : document;
  bindBusyButtons(root);
  initFollowLogs(document);
  initLiveLogsPolling(document);
  initWebIde(document);
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
