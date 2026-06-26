let dragged = null;

function initDashboardQueue(root = document) {
  const list = root.querySelector("#queue-list");
  const orderInput = root.querySelector("#queue-order");
  const startDrop = root.querySelector("#start-drop");
  if (list && list.dataset.dragBound !== "1") {
    list.dataset.dragBound = "1";
    const syncOrder = () => {
      if (!orderInput) return;
      orderInput.value = [...list.querySelectorAll("[data-id]")].map((el) => el.dataset.id).join(",");
    };
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

  if (startDrop && startDrop.dataset.dropBound !== "1") {
    startDrop.dataset.dropBound = "1";
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
        } else {
          announce("Could not start build");
        }
      } catch {
        announce("Network error");
      }
    });
  }
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

function initLiveLogsStream(root = document) {
  const terminal = root.querySelector("#live-logs-terminal");
  if (!terminal || terminal.dataset.streamBound === "1") return;
  const runId = terminal.dataset.runId;
  if (!runId || typeof EventSource === "undefined") return; // fall back to polling
  terminal.dataset.streamBound = "1";
  terminal.dataset.pollBound = "1"; // claim the terminal so the poller stays idle

  const after = terminal.dataset.lastLogId || "0";
  const source = new EventSource(`/runs/${runId}/logs/stream?after=${encodeURIComponent(after)}`);
  source.onmessage = (event) => {
    const prefix = terminal.textContent && !terminal.textContent.endsWith("\n") ? "\n" : "";
    terminal.textContent += prefix + event.data + "\n";
  };
  source.addEventListener("done", () => source.close());
  const close = () => source.close();
  window.addEventListener("beforeunload", close, { once: true });
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

function lineDiff(aText, bText) {
  const a = aText.split("\n");
  const b = bText.split("\n");
  const n = a.length;
  const m = b.length;
  // LCS length table (files are size-capped server-side, so this is fine).
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ c: "ctx", t: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ c: "del", t: a[i] }); i++; }
    else { out.push({ c: "add", t: b[j] }); j++; }
  }
  while (i < n) out.push({ c: "del", t: a[i++] });
  while (j < m) out.push({ c: "add", t: b[j++] });
  return out;
}

const IDE_HL_LANG = {
  ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
  ".jsx": "javascript", ".json": "json", ".md": "markdown", ".html": "xml",
  ".css": "css", ".yml": "yaml", ".yaml": "yaml", ".sh": "bash", ".sql": "sql",
  ".java": "java", ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "cpp",
  ".rb": "ruby", ".php": "php", ".toml": "ini", ".ini": "ini",
};

// Inline folder/file icons (the Web IDE is air-gapped, so no icon-font/CDN).
const IDE_FOLDER_SVG =
  '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">' +
  '<path fill="currentColor" d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3.1c.4 0 .78.16 1.06.44L8.2 4.5H13A1.5 1.5 0 0 1 14.5 6v6A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12V4Z"/>' +
  "</svg>";
const IDE_FILE_SVG =
  '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
  '<path fill="currentColor" d="M4 1.5h4.6L13 5.9V13.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1Zm4.5 1.1V5a.5.5 0 0 0 .5.5h2.4L8.5 2.6Z"/>' +
  "</svg>";
// File-icon tint by extension (categorized, not one-per-language).
const IDE_FILE_COLOR = {
  ".py": "#6ca8e6", ".js": "#e6c24f", ".ts": "#4f9be6", ".jsx": "#e6c24f", ".tsx": "#4f9be6",
  ".json": "#9ad06b", ".yml": "#cc7a55", ".yaml": "#cc7a55", ".toml": "#cc7a55", ".ini": "#cc7a55",
  ".md": "#9fb0cf", ".txt": "#9fb0cf", ".html": "#e08a5b", ".xml": "#e08a5b", ".css": "#5bb0e0",
  ".sh": "#8fc98f", ".sql": "#c98fb0", ".java": "#d08a5b", ".go": "#5bc7e0", ".rs": "#d08a6b",
  ".c": "#7f9bd0", ".cpp": "#7f9bd0", ".rb": "#e06b6b", ".php": "#9b8fd0",
};
const fileTint = (name) => {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "#9fb0cf" : (IDE_FILE_COLOR[name.slice(dot).toLowerCase()] || "#9fb0cf");
};

function initWebIde(root = document) {
  const ide = root.querySelector("#web-ide");
  if (!ide || ide.dataset.ideBound === "1") return;
  ide.dataset.ideBound = "1";
  const runId = ide.dataset.runId;
  const diffBase = ide.dataset.diffBase || "";
  const baseQuery = diffBase ? `&base=${encodeURIComponent(diffBase)}` : "";
  const fileList = ide.querySelector("#web-ide-file-list");
  const editor = ide.querySelector("#web-ide-content");
  const preview = ide.querySelector("#web-ide-preview");
  const previewCode = preview ? preview.querySelector("code") : null;
  const diffView = ide.querySelector("#web-ide-diff");
  const current = ide.querySelector("#web-ide-current-file");
  const save = ide.querySelector("#web-ide-save");
  const status = ide.querySelector("#web-ide-status");
  const followToggle = ide.querySelector("#web-ide-follow");
  const modeButtons = [...ide.querySelectorAll("[data-mode]")];

  let activePath = "";
  let originalContent = "";
  let lastLoadedContent = "";
  let currentFiles = [];
  let mode = ide.dataset.defaultMode === "diff" && diffView ? "diff" : "edit";

  const setStatus = (message) => { if (status) status.textContent = message; };
  const langFor = (path) => {
    const dot = path.lastIndexOf(".");
    return dot < 0 ? "" : (IDE_HL_LANG[path.slice(dot).toLowerCase()] || "");
  };
  const getContent = () => editor.value;
  const hasUnsaved = () => !!activePath && getContent() !== lastLoadedContent;

  const renderPreview = () => {
    if (!previewCode) return;
    previewCode.textContent = getContent();
    const lang = langFor(activePath);
    previewCode.className = lang ? "language-" + lang : "";
    previewCode.removeAttribute("data-highlighted");
    if (window.hljs) {
      try { window.hljs.highlightElement(previewCode); } catch { /* highlight is best-effort */ }
    }
  };

  const renderDiff = () => {
    if (!diffView) return;
    const rows = lineDiff(originalContent || "", getContent() || "");
    if (rows.length > 8000) { diffView.textContent = "File too large to diff in the browser."; return; }
    diffView.textContent = "";
    let changed = false;
    for (const row of rows) {
      if (row.c !== "ctx") changed = true;
      const span = document.createElement("span");
      span.className = "diff-" + row.c;
      span.textContent = (row.c === "add" ? "+" : row.c === "del" ? "-" : " ") + row.t;
      diffView.appendChild(span);
    }
    if (!changed) diffView.textContent = "No changes versus HEAD.";
  };

  const applyMode = () => {
    editor.classList.toggle("hidden", mode !== "edit");
    if (preview) preview.classList.toggle("hidden", mode !== "preview");
    if (diffView) diffView.classList.toggle("hidden", mode !== "diff");
    for (const button of modeButtons) button.classList.toggle("secondary", button.dataset.mode !== mode);
    if (mode === "preview") renderPreview();
    if (mode === "diff") renderDiff();
  };

  const setMode = (next) => { mode = next; applyMode(); };
  for (const button of modeButtons) button.addEventListener("click", () => setMode(button.dataset.mode));

  // Follow is a toggle button (aria-pressed) rather than a checkbox.
  const isFollowing = () => !followToggle || followToggle.getAttribute("aria-pressed") === "true";
  if (followToggle) {
    const syncFollow = () => followToggle.classList.toggle("on", isFollowing());
    syncFollow();
    followToggle.addEventListener("click", () => {
      followToggle.setAttribute("aria-pressed", isFollowing() ? "false" : "true");
      syncFollow();
    });
  }

  // Folders the user has collapsed (empty set => everything expanded). Tracked
  // by folder path so expand/collapse state survives re-renders (file clicks,
  // live follow refreshes).
  const collapsedDirs = new Set();

  const buildTree = (files) => {
    const root = { dirs: new Map(), files: [], path: "" };
    for (const file of files) {
      const parts = file.path.split("/");
      let node = root;
      for (let i = 0; i < parts.length - 1; i++) {
        const dir = parts[i];
        if (!node.dirs.has(dir)) {
          node.dirs.set(dir, { dirs: new Map(), files: [], path: parts.slice(0, i + 1).join("/") });
        }
        node = node.dirs.get(dir);
      }
      node.files.push(file);
    }
    return root;
  };

  const byName = (a, b) => a.toLowerCase().localeCompare(b.toLowerCase());

  const renderNode = (node, container) => {
    for (const name of [...node.dirs.keys()].sort(byName)) {
      const dirNode = node.dirs.get(name);
      const details = document.createElement("details");
      details.className = "web-ide-dir";
      if (!collapsedDirs.has(dirNode.path)) details.open = true;
      const summary = document.createElement("summary");
      summary.className = "web-ide-dir-name";
      const icon = document.createElement("span");
      icon.className = "web-ide-folder-icon";
      icon.innerHTML = IDE_FOLDER_SVG;
      summary.appendChild(icon);
      summary.appendChild(document.createTextNode(name));
      details.appendChild(summary);
      details.addEventListener("toggle", () => {
        if (details.open) collapsedDirs.delete(dirNode.path);
        else collapsedDirs.add(dirNode.path);
      });
      renderNode(dirNode, details);
      container.appendChild(details);
    }
    for (const file of node.files.slice().sort((a, b) => byName(a.name, b.name))) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "web-ide-file";
      if (file.path === activePath) button.classList.add("active");
      const icon = document.createElement("span");
      icon.className = "web-ide-file-icon";
      icon.style.color = fileTint(file.name);
      icon.innerHTML = IDE_FILE_SVG;
      const label = document.createElement("span");
      label.className = "web-ide-file-label";
      label.textContent = file.name;
      button.appendChild(icon);
      button.appendChild(label);
      button.title = file.path;
      button.addEventListener("click", () => loadFile(file.path));
      container.appendChild(button);
    }
  };

  const setFiles = (files) => {
    currentFiles = files;
    fileList.textContent = "";
    if (!files.length) {
      const empty = document.createElement("span");
      empty.className = "empty";
      empty.textContent = "No workspace files yet.";
      fileList.appendChild(empty);
      return;
    }
    renderNode(buildTree(files), fileList);
  };

  const loadFile = async (path) => {
    setStatus("Loading...");
    const response = await fetch(`/runs/${runId}/workspace/file?path=${encodeURIComponent(path)}${baseQuery}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) { setStatus(data.error || "Could not open file"); return; }
    activePath = data.path;
    originalContent = data.original_content || "";
    lastLoadedContent = data.content || "";
    editor.value = lastLoadedContent;
    current.textContent = activePath;
    // Prefill the review-comment file field with the file being viewed.
    const commentFile = document.querySelector("#ide-comment-file");
    if (commentFile && !commentFile.dataset.touched) {
      commentFile.value = activePath;
      commentFile.addEventListener("input", () => { commentFile.dataset.touched = "1"; }, { once: true });
    }
    setFiles(currentFiles);
    save.disabled = false;
    setStatus("Ready");
    applyMode();
  };

  const loadFiles = async () => {
    try {
      const response = await fetch(`/runs/${runId}/workspace/files`, { cache: "no-store" });
      const files = await response.json();
      setFiles(files);
      if (!files.length) { setStatus("Workspace unavailable"); return; }
      if (activePath && files.some((file) => file.path === activePath)) return;
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

  editor.addEventListener("input", () => {
    if (mode === "preview") renderPreview();
    else if (mode === "diff") renderDiff();
  });

  save.addEventListener("click", async () => {
    if (!activePath) return;
    save.disabled = true;
    setStatus("Saving...");
    const body = new URLSearchParams({ path: activePath, content: getContent() });
    const response = await fetch(`/runs/${runId}/workspace/file`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    const data = await response.json();
    save.disabled = false;
    if (!response.ok) { setStatus(data.error || "Save failed"); announce("Save failed"); return; }
    lastLoadedContent = getContent();
    setStatus("Saved");
    announce("File saved");
  });

  loadFiles();

  const autoRefresh = async () => {
    if (!isFollowing()) return;
    try {
      const response = await fetch(`/runs/${runId}/workspace/files`, { cache: "no-store" });
      if (!response.ok) return;
      const files = await response.json();
      const oldSignature = currentFiles.map((file) => file.path).join("|");
      const newSignature = files.map((file) => file.path).join("|");
      if (oldSignature !== newSignature) setFiles(files);
      if (!activePath) return;
      if (hasUnsaved()) { setStatus("Live updates paused: unsaved edits."); return; }
      const fileResponse = await fetch(`/runs/${runId}/workspace/file?path=${encodeURIComponent(activePath)}${baseQuery}`, { cache: "no-store" });
      if (!fileResponse.ok) return;
      const fileData = await fileResponse.json();
      const remoteContent = fileData.content || "";
      if (remoteContent !== lastLoadedContent) {
        originalContent = fileData.original_content || "";
        lastLoadedContent = remoteContent;
        editor.value = remoteContent;
        applyMode();
        setStatus("Updated from live workspace.");
      }
    } catch {
      // Ignore transient auto-refresh failures.
    }
  };

  const refreshIntervalId = window.setInterval(autoRefresh, 3000);
  window.addEventListener("beforeunload", () => window.clearInterval(refreshIntervalId), { once: true });
}

function initSkillsSearch(root = document) {
  const market = root.querySelector("#skills-marketplace");
  if (!market || market.dataset.searchBound === "1") return;
  const textInput = market.querySelector("#market-search-text");
  const categorySelect = market.querySelector("#market-search-category");
  if (!textInput && !categorySelect) return;
  market.dataset.searchBound = "1";
  const groups = [...market.querySelectorAll(".market-group")];
  const noResults = market.querySelector("#market-no-results");

  const apply = () => {
    const query = (textInput ? textInput.value : "").trim().toLowerCase();
    const category = categorySelect ? categorySelect.value : "";
    let visibleCount = 0;
    for (const group of groups) {
      let groupVisible = 0;
      for (const card of group.querySelectorAll(".skill-card")) {
        const matchesText = !query || (card.dataset.search || "").includes(query);
        const matchesCategory = !category || card.dataset.category === category;
        const show = matchesText && matchesCategory;
        card.classList.toggle("hidden", !show);
        if (show) groupVisible++;
      }
      group.classList.toggle("hidden", groupVisible === 0);
      visibleCount += groupVisible;
    }
    if (noResults) noResults.classList.toggle("hidden", visibleCount !== 0);
  };

  if (textInput) textInput.addEventListener("input", apply);
  if (categorySelect) categorySelect.addEventListener("change", apply);
  apply();
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

function initCodeReviewCheckboxSync(root = document) {
  const autoCr = root.querySelector("#review-auto-cr");
  const commentBack = root.querySelector("#review-comment-back");
  const autoCrState = root.querySelector("#review-auto-cr-state");
  const commentBackState = root.querySelector("#review-comment-back-state");
  if (!autoCr || !commentBack || !autoCrState || !commentBackState) return;
  if (autoCr.dataset.syncBound === "1") return;
  autoCr.dataset.syncBound = "1";

  const sync = () => {
    autoCrState.value = autoCr.checked ? "1" : "0";
    commentBackState.value = commentBack.checked ? "1" : "0";
  };

  autoCr.addEventListener("change", sync);
  commentBack.addEventListener("change", sync);
  sync();
}

// Merge Request detail: poll the host (GitLab/GitHub) for fresh CI + review
// notes on an interval and let Claude (re)generate suggestions. The visible
// CI/notes panel auto-refreshes itself via HTMX (every 3s), so this never
// reloads the page — it just keeps the underlying data current.
function initMrAutoScan(root = document) {
  const panel = root.querySelector("#mr-auto");
  if (!panel || panel.dataset.autoScanBound === "1") return;
  panel.dataset.autoScanBound = "1";
  const mrId = panel.dataset.mrId;
  if (!mrId) return;
  const status = document.querySelector("#mr-auto-status");
  let inFlight = false;
  const setStatus = (message) => {
    if (status) status.textContent = message;
  };
  const refresh = async () => {
    if (inFlight) return;
    inFlight = true;
    try {
      const response = await fetch(`/merge-requests/${mrId}/auto-scan`, { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.ok) {
        setStatus(`Auto-refreshing — CI ${data.ci_count}, notes ${data.note_count} (${data.ci_status}).`);
      } else if (data && data.error) {
        setStatus(`Auto-refresh paused: ${data.error}`);
      }
    } catch {
      setStatus("Auto-refresh temporarily unavailable.");
    } finally {
      inFlight = false;
    }
  };
  refresh();
  const intervalId = window.setInterval(refresh, 20000);
  window.addEventListener("beforeunload", () => window.clearInterval(intervalId), { once: true });
}

// The MR live panel re-renders every 3s (HTMX outerHTML swap), which would snap
// any open <details> shut. Remember which AI-suggestion expanders the user has
// opened (by their stable data-persist key) and re-open them after each swap.
const mrOpenDetails = new Set();
document.addEventListener(
  "toggle",
  (event) => {
    const el = event.target;
    if (el && el.matches && el.matches("details[data-persist]")) {
      const key = el.getAttribute("data-persist");
      if (el.open) mrOpenDetails.add(key);
      else mrOpenDetails.delete(key);
    }
  },
  true, // capture: the toggle event does not bubble
);
function restoreMrDetails(root = document) {
  root.querySelectorAll("details[data-persist]").forEach((el) => {
    if (mrOpenDetails.has(el.getAttribute("data-persist"))) el.open = true;
  });
}

bindBusyButtons(document);
initDashboardQueue(document);
initFollowLogs(document);
initLiveLogsStream(document);
initLiveLogsPolling(document);
initWebIde(document);
initCodeReviewAutoScan(document);
initCodeReviewCheckboxSync(document);
initMrAutoScan(document);
restoreMrDetails(document);
initSkillsSearch(document);
document.addEventListener("htmx:afterSwap", (event) => {
  const root = event.target instanceof Element ? event.target : document;
  bindBusyButtons(root);
  initDashboardQueue(document);
  initFollowLogs(document);
  initLiveLogsStream(document);
  initLiveLogsPolling(document);
  initWebIde(document);
  initCodeReviewAutoScan(document);
  initCodeReviewCheckboxSync(document);
  initMrAutoScan(document);
  restoreMrDetails(document);
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
