(function indexBoot() {
  const form = document.getElementById("compare-form");
  const statusNode = document.getElementById("status");
  const submitBtn = document.getElementById("submit-btn");
  const originalInput = document.getElementById("original");
  const revisedInput = document.getElementById("revised");
  const revisedHint = document.getElementById("revised-hint");
  const revisedList = document.getElementById("revised-list");
  const modeInputs = Array.from(document.querySelectorAll("input[name='compare_mode']"));
  const modeSummary = document.getElementById("mode-summary");
  const profileSelect = form.querySelector("select[name='profile']");
  const formatCheckboxes = Array.from(form.querySelectorAll("input[name='formats']"));
  const metricMode = document.getElementById("metric-mode");
  const metricModeMeta = document.getElementById("metric-mode-meta");
  const metricQueue = document.getElementById("metric-queue");
  const metricQueueMeta = document.getElementById("metric-queue-meta");
  const metricFormats = document.getElementById("metric-formats");
  const metricFormatsMeta = document.getElementById("metric-formats-meta");
  const metricReady = document.getElementById("metric-ready");
  const metricReadyMeta = document.getElementById("metric-ready-meta");
  const metricReadyFill = document.getElementById("metric-ready-fill");
  const batchPanel = document.getElementById("batch-panel");
  const batchSummary = document.getElementById("batch-summary");
  const batchProgressFill = document.getElementById("batch-progress-fill");
  const batchProgressLabel = document.getElementById("batch-progress-label");
  const batchResults = document.getElementById("batch-results");
  const retryFailedBtn = document.getElementById("batch-retry-failed");
  const batchOpenRow = document.getElementById("batch-open-row");
  const batchOpenSelect = document.getElementById("batch-open-select");
  const batchOpenBtn = document.getElementById("batch-open-btn");
  const BATCH_HISTORY_KEY = "blackline_batch_history_v1";
  let lastBatchContext = null;
  let isBusy = false;

  function encodeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function setMetricValue(node, value) {
    if (!node) return;
    const normalized = String(value);
    if (node.dataset.metricValue === normalized) return;
    node.dataset.metricValue = normalized;
    node.textContent = normalized;
    node.classList.remove("pulse");
    void node.offsetWidth;
    node.classList.add("pulse");
  }

  function summarizeFormats(formats) {
    if (!formats.length) return "Select at least one format";
    if (formats.length <= 2) return formats.map((fmt) => fmt.toUpperCase()).join(" + ");
    return `${formats.slice(0, 2).map((fmt) => fmt.toUpperCase()).join(", ")} +${formats.length - 2}`;
  }

  function updateLiveMetrics() {
    const mode = getMode();
    const revisedCount = Array.from(revisedInput.files || []).length;
    const formatValues = formatCheckboxes.filter((item) => item.checked).map((item) => item.value);
    const hasOriginal = !!(originalInput.files && originalInput.files[0]);
    const hasRevised = revisedCount > 0;
    const readinessScore = Number(hasOriginal) + Number(hasRevised) + Number(formatValues.length > 0);
    const readinessPct = Math.round((readinessScore / 3) * 100);
    const queueText = revisedCount ? String(revisedCount) : "0";
    const queueMeta = mode === "batch"
      ? (revisedCount ? `${revisedCount} revised draft${revisedCount === 1 ? "" : "s"} queued` : "Add revised drafts to start queue")
      : (revisedCount ? "Single revised draft selected" : "Select one revised draft");
    const selectedProfile = profileSelect ? String(profileSelect.value || "default") : "default";

    setMetricValue(metricMode, mode === "batch" ? "Batch" : "Single");
    if (metricModeMeta) {
      metricModeMeta.textContent = mode === "batch"
        ? "One original against many revised drafts"
        : "One original versus one revised draft";
    }
    setMetricValue(metricQueue, queueText);
    if (metricQueueMeta) metricQueueMeta.textContent = queueMeta;
    setMetricValue(metricFormats, String(formatValues.length));
    if (metricFormatsMeta) metricFormatsMeta.textContent = summarizeFormats(formatValues);

    const readyText = isBusy ? "Processing" : (readinessScore === 3 ? "Ready" : readinessScore === 2 ? "Almost Ready" : "Not Ready");
    setMetricValue(metricReady, readyText);
    if (metricReadyMeta) {
      if (isBusy) {
        metricReadyMeta.textContent = "Generating comparison runs";
      } else if (readinessScore === 3) {
        metricReadyMeta.textContent = `Profile: ${selectedProfile.replace(/[_-]+/g, " ")}`;
      } else if (!hasOriginal && !hasRevised) {
        metricReadyMeta.textContent = "Upload original and revised drafts";
      } else if (!hasOriginal) {
        metricReadyMeta.textContent = "Original draft is missing";
      } else if (!hasRevised) {
        metricReadyMeta.textContent = "Revised draft is missing";
      } else {
        metricReadyMeta.textContent = "Choose at least one output format";
      }
    }
    if (metricReadyFill) {
      metricReadyFill.style.width = `${Math.max(readinessScore ? 12 : 0, readinessPct)}%`;
    }
  }

  function getMode() {
    const current = modeInputs.find((item) => item.checked);
    return current ? current.value : "single";
  }

  function extractRunIdFromUrl(runUrl) {
    const match = String(runUrl || "").match(/\/runs\/([A-Za-z0-9-]+)/);
    return match ? match[1] : "";
  }

  function loadBatchHistory() {
    try {
      const raw = window.localStorage.getItem(BATCH_HISTORY_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_error) {
      return [];
    }
  }

  function saveBatchHistory(history) {
    try {
      const trimmed = Array.isArray(history) ? history.slice(0, 10) : [];
      window.localStorage.setItem(BATCH_HISTORY_KEY, JSON.stringify(trimmed));
    } catch (_error) {
      // Ignore local storage issues so compare flow is never blocked.
    }
  }

  function persistBatchSession(common, originalFile, rows) {
    const items = rows
      .filter((row) => row.status === "done" && row.run_url)
      .map((row, idx) => ({
        index: idx + 1,
        run_id: extractRunIdFromUrl(row.run_url),
        run_url: row.run_url,
        revised_name: row.file.name
      }))
      .filter((item) => item.run_id);
    if (!items.length) return null;
    const session = {
      session_id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      created_at: new Date().toISOString(),
      original_name: originalFile.name || "",
      base_name: common.baseName || "",
      profile: common.profile || "",
      items
    };
    const history = loadBatchHistory().filter((entry) => entry && Array.isArray(entry.items));
    history.unshift(session);
    saveBatchHistory(history);
    return session;
  }

  function renderBatchOpenPicker(session) {
    if (!session || !Array.isArray(session.items) || session.items.length < 2) {
      batchOpenRow.classList.remove("show");
      batchOpenSelect.innerHTML = "";
      return;
    }
    const options = session.items.map((item) => `
      <option value="${encodeHtml(item.run_id)}">${item.index}. ${encodeHtml(item.revised_name)}</option>
    `).join("");
    batchOpenSelect.innerHTML = options;
    batchOpenRow.classList.add("show");
  }

  function setStatus(message, tone = "info") {
    if (!message) {
      statusNode.textContent = "";
      statusNode.className = "";
      return;
    }
    if (typeof tone === "boolean") {
      tone = tone ? "error" : "info";
    }
    const normalizedTone = new Set(["info", "working", "warning", "error", "success"]).has(tone) ? tone : "info";
    statusNode.textContent = message;
    statusNode.className = `show tone-${normalizedTone}`;
  }

  function sanitizeStem(name) {
    return String(name || "file")
      .replace(/\.[^.]*$/, "")
      .replace(/[^A-Za-z0-9_-]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 38) || "file";
  }

  function deriveBaseName(baseName, revisedName, index, total) {
    if (total <= 1) return baseName;
    const seq = String(index + 1).padStart(2, "0");
    return `${baseName}_${seq}_${sanitizeStem(revisedName)}`;
  }

  function updateUploadBadge(input, zoneId, nameId) {
    const zone = document.getElementById(zoneId);
    const name = document.getElementById(nameId);
    const files = Array.from(input.files || []);
    if (!files.length) {
      zone.classList.remove("has-file");
      name.textContent = "Selected";
      return;
    }
    zone.classList.add("has-file");
    name.textContent = files.length === 1 ? files[0].name : `${files.length} files selected`;
  }

  function updateRevisedList() {
    const files = Array.from(revisedInput.files || []);
    const isBatch = getMode() === "batch";
    if (!isBatch || files.length <= 1) {
      revisedList.classList.remove("show");
      revisedList.innerHTML = "";
      return;
    }
    revisedList.innerHTML = files.map((file, idx) => `<li>${idx + 1}. ${encodeHtml(file.name)}</li>`).join("");
    revisedList.classList.add("show");
  }

  function updateModeUi() {
    const isBatch = getMode() === "batch";
    revisedInput.multiple = isBatch;
    revisedHint.textContent = isBatch ? "Queue one or more revised drafts (.docx, .txt)" : "Latest edits (.docx, .txt)";
    submitBtn.textContent = isBatch ? "Run Batch Queue" : "Generate Review Run";
    if (modeSummary) {
      modeSummary.textContent = isBatch
        ? "Batch queue runs one original against multiple revised drafts, then lets you switch versions instantly."
        : "Single review compares one original and one revised draft.";
    }
    if (!isBatch) {
      batchPanel.classList.remove("show");
      retryFailedBtn.hidden = true;
      batchOpenRow.classList.remove("show");
    }
    updateUploadBadge(revisedInput, "z-revised", "n-revised");
    updateRevisedList();
    updateLiveMetrics();
  }

  function attachDrop(zoneId, input) {
    const zone = document.getElementById(zoneId);
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    });
    zone.addEventListener("dragleave", (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
    });
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
      if (!event.dataTransfer.files.length) return;
      input.files = event.dataTransfer.files;
      if (input === revisedInput) {
        updateUploadBadge(revisedInput, "z-revised", "n-revised");
        updateRevisedList();
      } else {
        updateUploadBadge(originalInput, "z-original", "n-original");
      }
      updateLiveMetrics();
    });
  }

  async function fileToBase64(file) {
    const buffer = await file.arrayBuffer();
    let binary = "";
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  }

  function readSettings(formData) {
    const formats = formData.getAll("formats");
    if (!formats.length) throw new Error("Select at least one output format.");
    return {
      baseName: String(formData.get("base_name") || "blackline_report"),
      profile: String(formData.get("profile") || "default"),
      formats,
      strict_legal: formData.get("strict_legal") === "on",
      ignore_case: formData.get("ignore_case") === "on",
      ignore_whitespace: formData.get("ignore_whitespace") === "on",
      ignore_smart_punctuation: formData.get("ignore_smart_punctuation") === "on",
      ignore_punctuation: formData.get("ignore_punctuation") === "on",
      ignore_numbering: formData.get("ignore_numbering") === "on",
      detect_moves: formData.get("detect_moves") === "on"
    };
  }

  async function buildPayload(common, originalFile, revisedFile, index, total) {
    return {
      original_name: originalFile.name,
      original_content: await fileToBase64(originalFile),
      revised_name: revisedFile.name,
      revised_content: await fileToBase64(revisedFile),
      base_name: deriveBaseName(common.baseName, revisedFile.name, index, total),
      profile: common.profile,
      formats: common.formats,
      strict_legal: common.strict_legal,
      ignore_case: common.ignore_case,
      ignore_whitespace: common.ignore_whitespace,
      ignore_smart_punctuation: common.ignore_smart_punctuation,
      ignore_punctuation: common.ignore_punctuation,
      ignore_numbering: common.ignore_numbering,
      detect_moves: common.detect_moves
    };
  }

  async function requestCompare(payload) {
    const response = await fetch("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Comparison failed.");
    return result;
  }

  function renderBatchRows(rows) {
    if (!rows.length) {
      batchResults.innerHTML = '<li class="batch-empty">Queue items will appear here once processing starts.</li>';
      return;
    }
    batchResults.innerHTML = rows.map((row, idx) => `
      <li class="batch-row ${row.status}">
        <span class="batch-index">${idx + 1}</span>
        <span class="batch-name" title="${encodeHtml(row.file.name)}">${encodeHtml(row.file.name)}</span>
        <span class="batch-state">${encodeHtml(row.status_label)}</span>
        <span class="batch-link">${row.run_url ? `<a href="${encodeHtml(row.run_url)}" target="_blank" rel="noopener">Open</a>` : ""}</span>
      </li>
    `).join("");
  }

  function updateBatchProgress(done, total) {
    const pct = total ? Math.round((done / total) * 100) : 0;
    batchProgressFill.style.width = `${pct}%`;
    batchProgressLabel.textContent = `${done} / ${total} processed`;
  }

  async function runBatch(common, originalFile, revisedFiles) {
    batchPanel.classList.add("show");
    const rows = revisedFiles.map((file) => ({
      file,
      status: "pending",
      status_label: "Queued",
      run_url: "",
      error: ""
    }));
    renderBatchRows(rows);
    updateBatchProgress(0, rows.length);
    retryFailedBtn.hidden = true;
    batchSummary.textContent = `Queued ${rows.length} comparisons`;
    let completed = 0;
    for (let i = 0; i < rows.length; i += 1) {
      rows[i].status = "running";
      rows[i].status_label = "Running";
      renderBatchRows(rows);
      setStatus(`Processing ${i + 1} of ${rows.length}: ${rows[i].file.name}`, "working");
      try {
        const payload = await buildPayload(common, originalFile, rows[i].file, i, rows.length);
        const result = await requestCompare(payload);
        rows[i].status = "done";
        rows[i].status_label = "Done";
        rows[i].run_url = result.run_url || "";
      } catch (error) {
        rows[i].status = "failed";
        rows[i].status_label = "Failed";
        rows[i].error = error && error.message ? error.message : String(error);
      }
      completed += 1;
      renderBatchRows(rows);
      updateBatchProgress(completed, rows.length);
    }
    const successCount = rows.filter((row) => row.status === "done").length;
    const failedRows = rows.filter((row) => row.status === "failed");
    const hasFailures = failedRows.length > 0;
    batchSummary.textContent = hasFailures
      ? `${successCount} done, ${failedRows.length} failed`
      : `All ${successCount} comparisons complete`;
    const session = persistBatchSession(common, originalFile, rows);
    renderBatchOpenPicker(session);
    lastBatchContext = {
      common,
      originalFile,
      failedRows: failedRows.map((row) => row.file)
    };
    retryFailedBtn.hidden = !hasFailures;
    setStatus(
      hasFailures
        ? `Batch finished with ${failedRows.length} failures. Review queue details below.`
        : `Batch complete. ${successCount} review runs are ready.`,
      hasFailures ? "warning" : "success"
    );
  }

  function setFormBusy(busy) {
    isBusy = busy;
    submitBtn.disabled = busy;
    document.body.classList.toggle("is-processing", busy);
    modeInputs.forEach((input) => {
      input.disabled = busy;
    });
    originalInput.disabled = busy;
    revisedInput.disabled = busy;
    Array.from(form.querySelectorAll("input[type='checkbox'], select, input[type='text']")).forEach((input) => {
      input.disabled = busy;
    });
    retryFailedBtn.disabled = busy;
    updateLiveMetrics();
  }

  originalInput.addEventListener("change", () => {
    updateUploadBadge(originalInput, "z-original", "n-original");
    updateLiveMetrics();
  });
  revisedInput.addEventListener("change", () => {
    updateUploadBadge(revisedInput, "z-revised", "n-revised");
    updateRevisedList();
    updateLiveMetrics();
  });
  modeInputs.forEach((mode) => mode.addEventListener("change", updateModeUi));
  if (profileSelect) profileSelect.addEventListener("change", updateLiveMetrics);
  formatCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", updateLiveMetrics));
  attachDrop("z-original", originalInput);
  attachDrop("z-revised", revisedInput);
  updateModeUi();
  updateLiveMetrics();

  retryFailedBtn.addEventListener("click", async () => {
    if (isBusy || !lastBatchContext || !lastBatchContext.failedRows.length) return;
    setFormBusy(true);
    try {
      setStatus("Retrying failed batch items...", "working");
      await runBatch(lastBatchContext.common, lastBatchContext.originalFile, lastBatchContext.failedRows);
    } finally {
      setFormBusy(false);
    }
  });

  batchOpenBtn.addEventListener("click", () => {
    const runId = batchOpenSelect.value;
    if (!runId) return;
    window.location.assign(`/runs/${encodeURIComponent(runId)}`);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (isBusy) return;
    setStatus("Processing documents...", "working");
    try {
      const formData = new FormData(form);
      const originalFile = originalInput.files && originalInput.files[0];
      const revisedFiles = Array.from(revisedInput.files || []);
      if (!originalFile) throw new Error("Select an original file.");
      if (!revisedFiles.length) throw new Error("Select at least one revised file.");
      const common = readSettings(formData);
      setFormBusy(true);
      const isBatch = getMode() === "batch";
      if (!isBatch) {
        if (revisedFiles.length > 1) throw new Error("Single mode supports one revised file. Switch to Batch Queue.");
        const payload = await buildPayload(common, originalFile, revisedFiles[0], 0, 1);
        const result = await requestCompare(payload);
        window.location.assign(result.run_url);
        return;
      }
      await runBatch(common, originalFile, revisedFiles);
    } catch (error) {
      setStatus(error && error.message ? error.message : String(error), "error");
    } finally {
      setFormBusy(false);
    }
  });
})();
