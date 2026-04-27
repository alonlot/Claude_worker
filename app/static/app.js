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
  const editorHost = ide.querySelector("#web-ide-editor-host");
  const current = ide.querySelector("#web-ide-current-file");
  const save = ide.querySelector("#web-ide-save");
  const status = ide.querySelector("#web-ide-status");
  const diffToggle = ide.querySelector("#web-ide-diff-toggle");
  let activePath = "";
  let activeOriginalContent = "";
  let monacoEditor = null;
  let monacoDiffEditor = null;
  let monacoOriginalModel = null;
  let monacoModifiedModel = null;
  let monacoCodeHost = null;
  let monacoDiffHost = null;

  const extensionLanguage = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sh": "shell",
    ".sql": "sql",
    ".txt": "plaintext",
  };

  const setStatus = (message) => {
    status.textContent = message;
  };

  const languageForPath = (path) => {
    const dot = path.lastIndexOf(".");
    if (dot < 0) return "plaintext";
    const ext = path.slice(dot).toLowerCase();
    return extensionLanguage[ext] || "plaintext";
  };

  const loadMonaco = async () => {
    if (window.monaco) return window.monaco;
    if (!window.require || !window.require.config) {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs/loader.js";
      await new Promise((resolve, reject) => {
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
    }
    window.require.config({ paths: { vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs" } });
    await new Promise((resolve) => window.require(["vs/editor/editor.main"], resolve));
    return window.monaco;
  };

  const ensureMonaco = async () => {
    if (monacoEditor || monacoDiffEditor) return true;
    if (!editorHost) return false;
    try {
      const monaco = await loadMonaco();
      monaco.editor.setTheme("vs-dark");
      monacoCodeHost = document.createElement("div");
      monacoDiffHost = document.createElement("div");
      monacoCodeHost.className = "web-ide-monaco";
      monacoDiffHost.className = "web-ide-monaco";
      editorHost.textContent = "";
      editorHost.appendChild(monacoCodeHost);
      editorHost.appendChild(monacoDiffHost);
      monacoEditor = monaco.editor.create(monacoCodeHost, {
        value: "",
        language: "plaintext",
        automaticLayout: true,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        fontSize: 13,
        theme: "vs-dark",
      });
      monacoOriginalModel = monaco.editor.createModel("", "plaintext");
      monacoModifiedModel = monaco.editor.createModel("", "plaintext");
      monacoDiffEditor = monaco.editor.createDiffEditor(monacoDiffHost, {
        automaticLayout: true,
        renderSideBySide: true,
        minimap: { enabled: false },
        readOnly: true,
        theme: "vs-dark",
      });
      monacoDiffEditor.setModel({ original: monacoOriginalModel, modified: monacoModifiedModel });
      monacoDiffHost.style.display = "none";
      return true;
    } catch {
      setStatus("Monaco failed to load; using plain editor");
      return false;
    }
  };

  const setEditorContent = async (path, content, originalContent) => {
    const hasMonaco = await ensureMonaco();
    if (!hasMonaco || !window.monaco || !editorHost) {
      editor.value = content || "";
      return;
    }
    const language = languageForPath(path);
    monacoModifiedModel.setValue(content || "");
    window.monaco.editor.setModelLanguage(monacoModifiedModel, language);
    monacoOriginalModel.setValue(originalContent || "");
    window.monaco.editor.setModelLanguage(monacoOriginalModel, language);
    monacoEditor.setModel(monacoModifiedModel);
    const diffOn = !!(diffToggle && diffToggle.checked);
    monacoCodeHost.style.display = diffOn ? "none" : "block";
    monacoDiffHost.style.display = diffOn ? "block" : "none";
  };

  const getEditorContent = () => {
    if (monacoModifiedModel) return monacoModifiedModel.getValue();
    return editor.value;
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
    activeOriginalContent = data.original_content || "";
    current.textContent = activePath;
    await setEditorContent(activePath, data.content || "", activeOriginalContent);
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
    const body = new URLSearchParams({ path: activePath, content: getEditorContent() });
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

  if (diffToggle) {
    diffToggle.addEventListener("change", async () => {
      if (!activePath) return;
      await setEditorContent(activePath, getEditorContent(), activeOriginalContent);
    });
  }
}

function initCodeReviewAutoScan(root = document) {
  const panel = root.querySelector("#code-review-auto");
  if (!panel || panel.dataset.autoScanBound === "1") return;
  panel.dataset.autoScanBound = "1";
  const runId = panel.dataset.runId;
  if (!runId) return;
  const sourceInput = document.querySelector("#review-source-url");
  const autoCrCheckbox = document.querySelector("#review-auto-cr");
  const status = document.querySelector("#review-auto-status");
  let currentNotes = Number.parseInt(panel.dataset.currentNotes || "0", 10) || 0;
  let currentCi = Number.parseInt(panel.dataset.currentCi || "0", 10) || 0;
  let inFlight = false;

  const setStatus = (message) => {
    if (status) status.textContent = message;
  };

  const refresh = async () => {
    if (inFlight) return;
    const sourceUrl = (sourceInput && sourceInput.value ? sourceInput.value : "").trim();
    if (!sourceUrl) {
      setStatus("Auto-scan paused: enter a PR/MR URL.");
      return;
    }
    inFlight = true;
    try {
      const body = new URLSearchParams({
        source_url: sourceUrl,
        auto_cr: autoCrCheckbox && autoCrCheckbox.checked ? "1" : "0",
      });
      const response = await fetch(`/runs/${runId}/code-review/auto-scan`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      let data = {};
      try {
        data = await response.json();
      } catch {
        setStatus("Auto-scan returned an unexpected response.");
        announce("Auto-scan failed");
        return;
      }
      if (!response.ok || !data.ok) {
        const msg =
          (data && data.error) ||
          (data && data.detail) ||
          (response.status === 404 ? "Server is missing auto-scan (restart the app)." : "Auto-scan failed.");
        setStatus(typeof msg === "string" ? msg : "Auto-scan failed.");
        announce("Auto-scan failed");
        return;
      }
      setStatus(`Auto-scan OK - notes ${data.notes_count}, CI ${data.ci_count}.`);
      if (data.notes_count !== currentNotes || data.ci_count !== currentCi) {
        window.location.reload();
        return;
      }
    } catch {
      setStatus("Auto-scan temporarily unavailable.");
    } finally {
      inFlight = false;
    }
  };

  refresh();
  const intervalId = window.setInterval(refresh, 45000);
  window.addEventListener("beforeunload", () => window.clearInterval(intervalId), { once: true });
}

bindBusyButtons(document);
initFollowLogs(document);
initLiveLogsPolling(document);
initWebIde(document);
initCodeReviewAutoScan(document);
document.addEventListener("htmx:afterSwap", (event) => {
  const root = event.target instanceof Element ? event.target : document;
  bindBusyButtons(root);
  initFollowLogs(document);
  initLiveLogsPolling(document);
  initWebIde(document);
  initCodeReviewAutoScan(document);
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
