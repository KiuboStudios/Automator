const state = {
  backlog: { defaults: {}, tasks: [] },
  runs: [],
  selectedTaskIds: new Set(),
  selectedRunId: null,
  currentTaskId: null,
  draftAttachments: [],
  pendingAttachmentLoads: [],
  attachmentDraftSession: 0,
  previousFocusedElement: null,
  overview: null,
  detailOpenKeys: new Set(),
};

const RELATIVE_TIME_FORMATTER = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const contentType = response.headers.get("Content-Type") || "";

  if (!response.ok) {
    if (contentType.includes("application/json")) {
      const payload = await response.json();
      throw new Error(payload.error || `Request failed: ${response.status}`);
    }
    const text = await response.text();
    if (response.status === 404 && path.includes("/reviewed")) {
      throw new Error(
        "The dashboard server needs a restart to pick up the new Done action. Restart `python3 dashboard.py` and try again."
      );
    }
    throw new Error(text || `Request failed: ${response.status}`);
  }

  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

function setSaveStatus(message, tone = "muted") {
  const node = document.getElementById("save-status");
  if (!node) {
    return;
  }
  node.textContent = message;
  node.className = `visually-hidden ${tone}`;
}

function activeBacklogTasks() {
  return state.backlog.tasks.filter((task) => !task.reviewed);
}

function setNodeText(id, value) {
  const node = document.getElementById(id);
  if (!node) {
    return;
  }
  node.textContent = value;
}

function pluralize(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

function joinSummary(parts) {
  return parts.filter(Boolean).join(" · ");
}

function formatRelativeTime(value) {
  if (!value) {
    return "time unavailable";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "time unavailable";
  }

  const diffSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const ranges = [
    ["year", 31536000],
    ["month", 2592000],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
    ["second", 1],
  ];

  for (const [unit, seconds] of ranges) {
    if (Math.abs(diffSeconds) >= seconds || unit === "second") {
      return RELATIVE_TIME_FORMATTER.format(Math.round(diffSeconds / seconds), unit);
    }
  }

  return "just now";
}

function formatLabel(value) {
  const normalized = String(value || "").replaceAll("_", " ").trim();
  if (!normalized) {
    return "Unknown";
  }
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

function formatStageName(value) {
  const known = {
    backlog: "Backlog",
    agent_execution: "Agent execution",
    pull_request: "PR",
  };
  return known[value] || formatLabel(value);
}

function runnerStateMeta(stateName) {
  switch (stateName) {
    case "running":
      return { title: "Runner Busy", pill: "Running", tone: "current" };
    case "attention":
      return { title: "Needs Attention", pill: "Attention", tone: "warning" };
    case "blocked":
      return { title: "Runner Blocked", pill: "Blocked", tone: "failed" };
    case "idle":
      return { title: "Runner Idle", pill: "Idle", tone: "success" };
    default:
      return { title: "Runner Status", pill: "Checking", tone: "neutral" };
  }
}

function renderRunnerStatus(overview) {
  const meta = runnerStateMeta(overview.runner_state);
  const statusPill = document.getElementById("runner-status-pill");
  if (statusPill) {
    statusPill.textContent = meta.pill;
    statusPill.className = `status-pill ${meta.tone}`;
  }
  setNodeText("runner-status-title", meta.title);
  setNodeText(
    "runner-status-copy",
    overview.runner_summary || "Runner telemetry is available."
  );

  const activity = overview.current_activity || {};
  setNodeText(
    "runner-activity-primary",
    activity.title || (overview.active_run_count ? "Runner process active" : "No active task")
  );
  setNodeText(
    "runner-activity-secondary",
    joinSummary([
      activity.summary || (overview.active_run_count ? "The runner is active." : "No task is running right now."),
      activity.stage ? `stage: ${formatStageName(activity.stage)}` : "",
      activity.timestamp ? formatRelativeTime(activity.timestamp) : "",
    ]) || "No recent task activity was recorded."
  );

  if (overview.runner_launch_ready) {
    setNodeText("runner-launch-primary", "Ready to start runs");
    setNodeText(
      "runner-launch-secondary",
      joinSummary([
        overview.current_branch ? `branch: ${overview.current_branch}` : "",
        overview.dirty_worktree ? "current repo has local changes" : "current repo clean",
        overview.configured_repositories_ready
          ? "configured repos on main + clean"
          : "configured repos need main + clean",
        overview.github_token_available ? "GitHub token ready" : "GitHub token missing",
        overview.codex_cli_available ? "Codex CLI ready" : "Codex CLI missing",
      ])
    );
  } else {
    const blockers = overview.runner_launch_blockers || [];
    setNodeText("runner-launch-primary", "Blocked");
    setNodeText(
      "runner-launch-secondary",
      blockers.length
        ? joinSummary(blockers.slice(0, 2))
        : "The runner cannot start new runs yet."
    );
  }

  if (overview.active_run_count) {
    setNodeText("runner-active-primary", `${pluralize(overview.active_run_count, "run")} active`);
    setNodeText(
      "runner-active-secondary",
      joinSummary([
        pluralize(overview.active_task_count || 0, "task"),
        overview.running_task_count ? `${pluralize(overview.running_task_count, "task")} running` : "",
        overview.failed_task_count ? `${pluralize(overview.failed_task_count, "task")} failed` : "",
      ])
    );
  } else {
    setNodeText("runner-active-primary", "Runner idle");
    setNodeText(
      "runner-active-secondary",
      "No child runner processes are active right now."
    );
  }

  const latestRun = overview.latest_run;
  if (latestRun) {
    setNodeText("runner-latest-primary", latestRun.run_id || "Recent run");
    setNodeText(
      "runner-latest-secondary",
      joinSummary([
        `status: ${formatLabel(latestRun.status)}`,
        latestRun.task_count ? `${latestRun.passed_count} passed` : "",
        latestRun.failed_count ? `${latestRun.failed_count} failed` : "",
        latestRun.running_count ? `${latestRun.running_count} running` : "",
        latestRun.started_at ? `started ${formatRelativeTime(latestRun.started_at)}` : "",
      ])
    );
  } else {
    setNodeText("runner-latest-primary", "No runs yet");
    setNodeText(
      "runner-latest-secondary",
      "Start the first backlog run to populate execution history."
    );
  }

  setNodeText(
    "runner-access-primary",
    overview.dashboard_accessible ? "Dashboard reachable" : "Dashboard unavailable"
  );
  setNodeText(
    "runner-access-secondary",
    joinSummary([
      overview.observed_at ? `refreshed ${formatRelativeTime(overview.observed_at)}` : "",
      overview.github_token_available ? "GitHub publish ready" : "GitHub token missing",
      overview.codex_cli_available ? "Codex CLI reachable" : "Codex CLI missing",
    ])
  );

  setNodeText(
    "runner-limits-primary",
    overview.quota_telemetry_available ? "Telemetry available" : "Not exposed here"
  );
  setNodeText(
    "runner-limits-secondary",
    overview.quota_telemetry_summary ||
      "Rate-limit and credit telemetry is not available from the local runner."
  );
}

function syncRunButtons() {
  const runAllButton = document.getElementById("run-all-button");
  const runSelectedButton = document.getElementById("run-selected-button");
  if (!runAllButton || !runSelectedButton) {
    return;
  }

  const flowAllowed = !state.overview || Boolean(state.overview.development_flow_allowed);
  const blockedReason = (state.overview && state.overview.development_flow_error) || "";
  const activeTasks = activeBacklogTasks();
  const selectedCount = activeTasks.filter((task) => state.selectedTaskIds.has(task.id)).length;

  runAllButton.disabled = !flowAllowed || activeTasks.length === 0;
  runSelectedButton.disabled = !flowAllowed || selectedCount === 0;
  runAllButton.title = blockedReason || (activeTasks.length === 0 ? "No backlog tasks are ready to run." : "");
  runSelectedButton.title =
    blockedReason || (selectedCount === 0 ? "Select at least one backlog task to run." : "");
}

function syncRunHistoryControls() {
  const clearRunsButton = document.getElementById("clear-runs-button");
  if (!clearRunsButton) {
    return;
  }

  const activeRunCount = Number((state.overview && state.overview.active_run_count) || 0);
  const runCount = state.runs.length;
  clearRunsButton.disabled = runCount === 0 || activeRunCount > 0;
  if (activeRunCount > 0) {
    clearRunsButton.title = "Wait for active runs to finish before clearing history.";
  } else if (runCount === 0) {
    clearRunsButton.title = "No runs to clear.";
  } else {
    clearRunsButton.title = `Delete ${pluralize(runCount, "run")} from local history.`;
  }
}

function renderOverview(overview) {
  state.overview = overview;
  document.getElementById("repo-root").textContent = overview.repo_root;
  const indicator = document.getElementById("dirty-indicator");
  if (overview.repo_clean_badge) {
    indicator.textContent = "Repo Clean";
    indicator.className = "status-pill success";
    indicator.title =
      "Current repo is clean and all configured repositories are on `main` with clean worktrees.";
  } else if (overview.dirty_worktree) {
    indicator.textContent = "Repo has uncommitted changes";
    indicator.className = "status-pill warning";
    indicator.title = overview.repo_clean_badge_error || "Current repo has uncommitted changes.";
  } else if (!overview.configured_repositories_ready) {
    indicator.textContent = "Configured repos need main + clean";
    indicator.className = "status-pill warning";
    indicator.title =
      overview.repo_clean_badge_error ||
      "At least one configured repository is not on `main` or has uncommitted changes.";
  } else {
    indicator.textContent = "Repo checks pending";
    indicator.className = "status-pill neutral";
    indicator.title = overview.repo_clean_badge_error || "Repository checks are still in progress.";
  }

  const tokenIndicator = document.getElementById("github-token-indicator");
  const tokenIndicatorCopy = document.getElementById("github-token-indicator-copy");
  if (overview.github_token_available) {
    tokenIndicator.textContent = "GITHUB_TOKEN loaded";
    tokenIndicator.className = "status-pill success";
    tokenIndicatorCopy.textContent = "The dashboard process can publish GitHub pull requests.";
  } else {
    tokenIndicator.textContent = "Restart dashboard after exporting token";
    tokenIndicator.className = "status-pill warning";
    tokenIndicatorCopy.textContent =
      "Export GITHUB_TOKEN first, then restart `python3 dashboard.py`.";
  }

  const codexIndicator = document.getElementById("codex-cli-indicator");
  const codexIndicatorCopy = document.getElementById("codex-cli-indicator-copy");
  if (overview.codex_cli_available) {
    codexIndicator.textContent = "Codex CLI installed";
    codexIndicator.className = "status-pill success";
    codexIndicatorCopy.textContent = overview.codex_cli_path
      ? `Found at ${overview.codex_cli_path}.`
      : "Codex CLI is available on PATH.";
  } else {
    codexIndicator.textContent = "Codex CLI not found";
    codexIndicator.className = "status-pill warning";
    codexIndicatorCopy.textContent =
      "Add `codex` to PATH and restart the dashboard process to pick it up.";
  }

  const cleanupBranchesButton = document.getElementById("cleanup-merged-branches-button");
  const cleanupBranchesCopy = document.getElementById("cleanup-merged-branches-copy");
  const mergedBranchCount = Number(overview.merged_task_branch_count || 0);
  const cleanupAllowed = Boolean(overview.branch_cleanup_allowed);
  const cleanupBlockedReason = overview.branch_cleanup_error || "";

  cleanupBranchesButton.disabled = mergedBranchCount === 0 || !cleanupAllowed;
  cleanupBranchesButton.textContent =
    mergedBranchCount > 0
      ? `Remove merged branches (${mergedBranchCount})`
      : "Remove merged branches";
  cleanupBranchesButton.title =
    cleanupBlockedReason ||
    (mergedBranchCount
      ? "Delete merged local task branches and their clean worktrees."
      : "No merged task branches found.");
  if (cleanupBlockedReason) {
    cleanupBranchesCopy.textContent = cleanupBlockedReason;
  } else if (mergedBranchCount === 0) {
    cleanupBranchesCopy.textContent = "No merged task branches ready for cleanup.";
  } else if (mergedBranchCount === 1) {
    cleanupBranchesCopy.textContent = "1 merged task branch is ready to remove.";
  } else {
    cleanupBranchesCopy.textContent = `${mergedBranchCount} merged task branches are ready to remove.`;
  }
  renderRunnerStatus(overview);
  syncRunButtons();
  syncRunHistoryControls();
}

function setSettingsModalOpen(isOpen) {
  const modal = document.getElementById("settings-modal");
  const closeButton = document.getElementById("close-settings-button");

  if (isOpen) {
    state.previousFocusedElement = document.activeElement;
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    closeButton.focus();
    return;
  }

  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
  if (state.previousFocusedElement && typeof state.previousFocusedElement.focus === "function") {
    state.previousFocusedElement.focus();
  }
  state.previousFocusedElement = null;
}

function taskCommand(task) {
  if (Array.isArray(task.test_command)) {
    return task.test_command.join(" ");
  }
  return task.test_command || "";
}

function taskCommandLabel(task) {
  return taskCommand(task) || "No test command configured";
}

function repositories() {
  return Array.isArray(state.backlog.repositories) ? state.backlog.repositories : [];
}

function repositoryById(repositoryId) {
  return repositories().find((repository) => repository.id === repositoryId) || null;
}

function taskRepositoryId(task) {
  return String(task.repository_id || "").trim();
}

function taskRepositoryLabel(task) {
  const repositoryId = taskRepositoryId(task);
  if (!repositoryId) {
    return "Repository not selected";
  }
  const repository = repositoryById(repositoryId);
  if (!repository) {
    return repositoryId;
  }
  return `${repository.id} · ${repository.path}`;
}

function taskPipeline(task) {
  return (
    task.pipeline || {
      summary: "Waiting in Backlog",
      overall_status: "pending",
      latest_run_id: "",
      failure_detail: "",
      stages: [],
    }
  );
}

function taskAttachments(task) {
  return Array.isArray(task.attachments) ? task.attachments : [];
}

function formatFileSize(sizeBytes) {
  const size = Number(sizeBytes || 0);
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function attachmentBadgeLabel(attachment) {
  return attachment.kind === "image" ? "Photo" : "File";
}

function attachmentSummary(task) {
  const attachments = taskAttachments(task);
  if (!attachments.length) {
    return "";
  }
  const imageCount = attachments.filter((attachment) => attachment.kind === "image").length;
  const fileCount = attachments.length - imageCount;
  const parts = [];
  if (imageCount) {
    parts.push(`${imageCount} photo${imageCount === 1 ? "" : "s"}`);
  }
  if (fileCount) {
    parts.push(`${fileCount} file${fileCount === 1 ? "" : "s"}`);
  }
  return parts.join(" · ");
}

function attachmentPreviewUrl(attachment) {
  if (attachment.kind !== "image") {
    return "";
  }
  return attachment.preview_url || attachment.url || "";
}

function attachmentCardMarkup(attachment, removable = false) {
  const previewUrl = attachmentPreviewUrl(attachment);
  return `
    <article class="attachment-card ${attachment.kind === "image" ? "image" : "file"}">
      ${
        previewUrl
          ? `<img class="attachment-thumb" src="${escapeHtml(previewUrl)}" alt="${escapeHtml(attachment.name)}" />`
          : `<div class="attachment-thumb attachment-thumb-fallback">${escapeHtml(attachmentBadgeLabel(attachment))}</div>`
      }
      <div class="attachment-copy">
        <div class="attachment-copy-top">
          <strong>${escapeHtml(attachment.name)}</strong>
          <span class="status-pill neutral attachment-kind">${escapeHtml(attachmentBadgeLabel(attachment))}</span>
        </div>
        <p>${escapeHtml(formatFileSize(attachment.size_bytes || 0))}</p>
      </div>
      ${
        removable
          ? `<button type="button" class="danger attachment-remove-button" data-action="remove-attachment" data-attachment-id="${escapeHtml(attachment.id || attachment.name)}">Remove</button>`
          : ""
      }
    </article>
  `;
}

function renderTaskAttachments() {
  const container = document.getElementById("task-attachments");
  if (!container) {
    return;
  }

  if (!state.draftAttachments.length) {
    container.className = "attachment-list empty-state";
    container.textContent = "No attachments yet. Add files or photos to give this task more context.";
    return;
  }

  container.className = "attachment-list";
  container.innerHTML = state.draftAttachments
    .map((attachment) => attachmentCardMarkup(attachment, true))
    .join("");
}

function resetDraftAttachments(attachments = []) {
  state.draftAttachments = attachments.map((attachment) => ({
    ...attachment,
    existing: true,
    preview_url: attachment.kind === "image" ? attachment.url : "",
  }));
  renderTaskAttachments();
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

async function addDraftAttachments(fileList, draftSession = state.attachmentDraftSession) {
  const files = Array.from(fileList || []);
  if (!files.length) {
    return;
  }

  const prepared = await Promise.all(
    files.map(async (file, index) => {
      const dataUrl = await readFileAsDataUrl(file);
      const [header, dataBase64] = dataUrl.split(",", 2);
      const isImage = header.startsWith("data:image/");
      return {
        id: `draft-${Date.now()}-${index}-${file.name}`,
        name: file.name,
        content_type: file.type || "application/octet-stream",
        size_bytes: file.size,
        kind: isImage ? "image" : "file",
        data_base64: dataBase64 || "",
        preview_url: isImage ? dataUrl : "",
        existing: false,
      };
    })
  );

  if (draftSession !== state.attachmentDraftSession) {
    return;
  }
  state.draftAttachments = [...state.draftAttachments, ...prepared];
  renderTaskAttachments();
}

function removeDraftAttachment(attachmentId) {
  state.draftAttachments = state.draftAttachments.filter(
    (attachment) => String(attachment.id || attachment.name) !== String(attachmentId)
  );
  renderTaskAttachments();
}

function trackPendingAttachmentLoad(promise) {
  state.pendingAttachmentLoads = [...state.pendingAttachmentLoads, promise];
  promise.finally(() => {
    state.pendingAttachmentLoads = state.pendingAttachmentLoads.filter((item) => item !== promise);
  });
  return promise;
}

async function waitForPendingAttachmentLoads() {
  if (!state.pendingAttachmentLoads.length) {
    return;
  }
  setSaveStatus("Processing attachments before saving...", "muted");
  await Promise.allSettled([...state.pendingAttachmentLoads]);
}

function pipelineStagesMarkup(pipeline) {
  return (pipeline.stages || [])
    .map(
      (stage) =>
        `<span class="stage-pill ${stage.status}" title="${escapeHtml(stage.detail || stage.label)}">${escapeHtml(stage.label)}</span>`
    )
    .join("");
}

function taskSelectionMarkup(task) {
  if (task.reviewed) {
    return `
      <div class="task-select task-select-static">
        <span>${escapeHtml(task.title)}</span>
        <span class="status-pill success">Done</span>
      </div>
    `;
  }

  const selected = state.selectedTaskIds.has(task.id) ? "checked" : "";
  return `
    <label class="task-select">
      <input type="checkbox" data-action="toggle-task" data-task-id="${task.id}" ${selected} />
      <span>${escapeHtml(task.title)}</span>
    </label>
  `;
}

function taskCardMarkup(task) {
  const active = state.currentTaskId === task.id ? "task-card active" : "task-card";
  const pipeline = taskPipeline(task);
  const reviewActionLabel = task.reviewed ? "Move to Backlog" : "Move to Done";
  const nextReviewedState = task.reviewed ? "false" : "true";
  const attachments = taskAttachments(task);
  const attachmentsCopy = attachmentSummary(task);

  return `
    <article class="${active}">
      ${taskSelectionMarkup(task)}
      <p class="task-id">${escapeHtml(task.id)}</p>
      <p class="task-description"><strong>Repository:</strong> ${escapeHtml(taskRepositoryLabel(task))}</p>
      <p class="task-description">${escapeHtml(task.description || "No description yet.")}</p>
      ${
        attachments.length
          ? `<div class="task-attachment-row">
              <span class="status-pill neutral">${attachments.length} attachment${attachments.length === 1 ? "" : "s"}</span>
              <span class="task-attachment-copy">${escapeHtml(attachmentsCopy)}</span>
            </div>`
          : ""
      }
      <div class="task-status-row">
        <span class="status-pill ${pipeline.overall_status}">${escapeHtml(pipeline.summary)}</span>
        <span class="task-run-ref">${escapeHtml(pipeline.latest_run_id || "Never run")}</span>
      </div>
      <code class="task-command">${escapeHtml(taskCommandLabel(task))}</code>
      <div class="pipeline-track">${pipelineStagesMarkup(pipeline)}</div>
      ${
        pipeline.failure_detail
          ? `<p class="pipeline-note error">${escapeHtml(pipeline.failure_detail)}</p>`
          : ""
      }
      <div class="task-actions">
        <button data-action="edit-task" data-task-id="${task.id}">Edit</button>
        <button data-action="toggle-reviewed" data-task-id="${task.id}" data-reviewed="${nextReviewedState}">${reviewActionLabel}</button>
        <button class="danger" data-action="remove-task" data-task-id="${task.id}">Remove</button>
      </div>
    </article>
  `;
}

function taskGroupMarkup(title, copy, tasks, emptyMessage) {
  return `
    <section class="task-group">
      <div class="task-group-header">
        <div>
          <h3>${escapeHtml(title)}</h3>
          <p>${escapeHtml(copy)}</p>
        </div>
        <span class="status-pill neutral task-group-count">${tasks.length} task${tasks.length === 1 ? "" : "s"}</span>
      </div>
      <div class="task-list-section">
        ${
          tasks.length
            ? tasks.map((task) => taskCardMarkup(task)).join("")
            : `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`
        }
      </div>
    </section>
  `;
}

function detailsOpenAttribute(key) {
  return state.detailOpenKeys.has(key) ? "open" : "";
}

function renderTaskList() {
  const container = document.getElementById("task-list");
  const tasks = [...state.backlog.tasks].sort((a, b) => a.id.localeCompare(b.id));
  const backlogTasks = tasks.filter((task) => !task.reviewed);
  const doneTasks = tasks.filter((task) => task.reviewed);

  if (!tasks.length) {
    container.innerHTML = `<div class="empty-state">No tasks yet. Create the first one from the editor.</div>`;
    syncRunButtons();
    return;
  }

  container.innerHTML = [
    taskGroupMarkup(
      "Backlog",
      "Active tasks stay here and are included in new automation runs.",
      backlogTasks,
      "No active tasks in Backlog. Move one back from Done or create a new task."
    ),
    taskGroupMarkup(
      "Done",
      "Reviewed tasks land here so they stay visible without joining new runs.",
      doneTasks,
      "No reviewed tasks yet."
    ),
  ].join("");
  syncRunButtons();
}

function renderRepositoryOptions(selectedRepositoryId = "") {
  const select = document.getElementById("task-repository-select");
  if (!select) {
    return;
  }
  const repositoryList = repositories();
  const fallbackRepositoryId =
    String((state.backlog.defaults && state.backlog.defaults.repository_id) || "").trim() ||
    (repositoryList[0] && repositoryList[0].id) ||
    "";
  const resolvedSelection = selectedRepositoryId || fallbackRepositoryId;

  select.innerHTML = [
    `<option value="">Select a repository</option>`,
    ...repositoryList.map((repository) => {
      const selected = repository.id === resolvedSelection ? "selected" : "";
      return `<option value="${escapeHtml(repository.id)}" ${selected}>${escapeHtml(
        `${repository.id} · ${repository.path}`
      )}</option>`;
    }),
  ].join("");
}

function repositoryStatusPill(repository) {
  if (repository.development_flow_allowed) {
    return `<span class="status-pill success">Ready</span>`;
  }
  if (repository.error) {
    return `<span class="status-pill warning">Needs attention</span>`;
  }
  return `<span class="status-pill neutral">Checking</span>`;
}

function renderRepositoryRegistry() {
  const container = document.getElementById("repository-list");
  if (!container) {
    return;
  }
  const repositoryList = repositories();
  if (!repositoryList.length) {
    container.className = "repository-list empty-state";
    container.textContent = "No repositories registered yet.";
    return;
  }

  container.className = "repository-list";
  container.innerHTML = repositoryList
    .map(
      (repository) => `
        <article class="repository-item">
          <div class="repository-item-row">
            <strong>${escapeHtml(repository.id)}</strong>
            ${repositoryStatusPill(repository)}
          </div>
          <code>${escapeHtml(repository.path || "")}</code>
          ${
            repository.error
              ? `<p class="repository-item-copy error">${escapeHtml(repository.error)}</p>`
              : `<p class="repository-item-copy">Branch: ${escapeHtml(
                  repository.current_branch || "unknown"
                )} · ${repository.clean_worktree ? "clean" : "dirty worktree"}</p>`
          }
          <div class="task-actions">
            <button class="danger" type="button" data-action="delete-repository" data-repository-id="${escapeHtml(
              repository.id
            )}">Remove</button>
          </div>
        </article>
      `
    )
    .join("");
}

function renderRuns() {
  const container = document.getElementById("run-list");
  if (!state.runs.length) {
    container.innerHTML = `<div class="empty-state">No runs yet. Launch one from the backlog controls.</div>`;
    return;
  }

  container.innerHTML = state.runs
    .map((run) => {
      const selected = run.run_id === state.selectedRunId ? "run-card active" : "run-card";
      return `
        <article class="${selected}" data-action="select-run" data-run-id="${run.run_id}">
          <div class="run-card-top">
            <strong>${escapeHtml(run.run_id)}</strong>
            <span class="status-pill ${run.status}">${run.status}</span>
          </div>
          <p>${run.task_count} tasks · ${run.passed_count} passed · ${run.failed_count} failed</p>
          <p>${run.is_running ? "Running now" : "Finished"}</p>
        </article>
      `;
    })
    .join("");
}

function renderRunDetail(run) {
  const subtitle = document.getElementById("run-detail-subtitle");
  const container = document.getElementById("run-detail");

  if (!run) {
    subtitle.textContent = "Select a run to inspect it.";
    container.className = "run-detail empty-state";
    container.textContent = "No run selected yet.";
    return;
  }

  subtitle.textContent = `${run.run_id} · ${run.is_running ? "running" : "finished"} · exit code ${run.exit_code != null ? run.exit_code : "pending"}`;
  container.className = "run-detail";

  const taskMarkup = run.tasks.length
    ? run.tasks
        .map(
          (task) => {
            const eventsKey = `${run.run_id}:${task.task_id}:events`;
            const stdoutKey = `${run.run_id}:${task.task_id}:stdout`;
            const stderrKey = `${run.run_id}:${task.task_id}:stderr`;
            const workflowKey = `${run.run_id}:${task.task_id}:workflow`;
            const historyKey = `${run.run_id}:${task.task_id}:history`;
            const requestsKey = `${run.run_id}:${task.task_id}:requests`;
            const resume = task.resume || { available: false, label: "" };
            const workflowState = task.workflow_state || {};
            const resumeHistory = task.resume_history || [];
            const lastBlocker = task.last_blocker || {};
            const resumeRequests = task.resume_requests || [];
            return `
          <article class="task-detail-card">
            <div class="task-detail-header">
              <div>
                <strong>${escapeHtml(task.title)}</strong>
                <p>${escapeHtml(task.task_id)}</p>
              </div>
              <span class="status-pill ${task.status}">${task.status}</span>
            </div>
            <p>Attempts: ${task.attempts} · Repository: ${escapeHtml(
              task.repository_id || "default"
            )} · Branch: ${escapeHtml(task.branch_name || "pending")}</p>
            ${
              task.pull_request_url
                ? `<p><a href="${escapeHtml(task.pull_request_url)}" target="_blank" rel="noreferrer">Open pull request</a></p>`
                : ""
            }
            <div class="task-detail-meta">
              <span class="stage-pill">${escapeHtml(`Resumes ${task.resume_count || 0}`)}</span>
              ${
                task.last_resume_from
                  ? `<span class="stage-pill">${escapeHtml(`Last resume ${task.last_resume_from}`)}</span>`
                  : ""
              }
              ${
                workflowState.next_resume_label
                  ? `<span class="stage-pill warning">${escapeHtml(workflowState.next_resume_label)}</span>`
                  : ""
              }
            </div>
            ${
              lastBlocker.detail
                ? `<p class="pipeline-note error">Latest blocker: ${escapeHtml(lastBlocker.detail)}</p>`
                : ""
            }
            ${
              resume.available && !run.is_running
                ? `<div class="task-detail-actions"><button class="primary" data-action="resume-task" data-run-id="${run.run_id}" data-task-id="${task.task_id}">${escapeHtml(resume.label)}</button></div>`
                : ""
            }
            <div class="pipeline-track">${pipelineStagesMarkup(task.pipeline || { stages: [] })}</div>
            <details data-detail-key="${workflowKey}" ${detailsOpenAttribute(workflowKey)}>
              <summary>Workflow metadata</summary>
              <pre class="log-pre">${escapeHtml(JSON.stringify(workflowState, null, 2))}</pre>
            </details>
            ${
              resumeHistory.length
                ? `<details data-detail-key="${historyKey}" ${detailsOpenAttribute(historyKey)}>
              <summary>Resume history</summary>
              <pre class="log-pre">${escapeHtml(JSON.stringify(resumeHistory, null, 2))}</pre>
            </details>`
                : ""
            }
            ${
              resumeRequests.length
                ? `<details data-detail-key="${requestsKey}" ${detailsOpenAttribute(requestsKey)}>
              <summary>Resume requests</summary>
              <pre class="log-pre">${escapeHtml(JSON.stringify(resumeRequests, null, 2))}</pre>
            </details>`
                : ""
            }
            <details data-detail-key="${eventsKey}" ${detailsOpenAttribute(eventsKey)}>
              <summary>Recent events</summary>
              <pre class="log-pre">${escapeHtml(JSON.stringify(task.events, null, 2))}</pre>
            </details>
            <details data-detail-key="${stdoutKey}" ${detailsOpenAttribute(stdoutKey)}>
              <summary>Latest stdout</summary>
              <pre class="log-pre">${escapeHtml(task.logs.stdout || "")}</pre>
            </details>
            <details data-detail-key="${stderrKey}" ${detailsOpenAttribute(stderrKey)}>
              <summary>Latest stderr</summary>
              <pre class="log-pre">${escapeHtml(task.logs.stderr || "")}</pre>
            </details>
          </article>
        `;
          }
        )
        .join("")
    : `<div class="empty-state">This run has not created task artifacts yet.</div>`;

  container.innerHTML = `
    <div class="detail-grid">
      <section class="detail-section">
        <h3>Runner stderr</h3>
        <pre class="log-pre">${escapeHtml(run.runner_stderr || "")}</pre>
      </section>
      <section class="detail-section">
        <h3>Runner stdout</h3>
        <pre class="log-pre">${escapeHtml(run.runner_stdout || "")}</pre>
      </section>
    </div>
    <section class="detail-section tasks-section">
      <h3>Tasks</h3>
      <div class="task-detail-list">${taskMarkup}</div>
    </section>
  `;
}

function populateTaskForm(task = null) {
  const form = document.getElementById("task-form");
  const fields = form.elements;
  const taskData = task || {};
  const executor = taskData.executor || {};
  const defaultExecutor = state.backlog.defaults.executor || {};
  state.attachmentDraftSession += 1;
  state.currentTaskId = task ? task.id : null;
  fields.id.value = taskData.id || "";
  fields.title.value = taskData.title || "";
  renderRepositoryOptions(taskRepositoryId(taskData));
  fields.repository_id.value =
    taskRepositoryId(taskData) ||
    fields.repository_id.value ||
    String((state.backlog.defaults && state.backlog.defaults.repository_id) || "").trim();
  fields.description.value = taskData.description || "";
  fields.test_command.value = task ? taskCommand(task) : "";
  fields.base_branch.value = taskData.base_branch || state.backlog.defaults.base_branch || "main";
  fields.working_directory.value =
    taskData.working_directory || state.backlog.defaults.working_directory || ".";
  fields.retry_limit.value =
    taskData.retry_limit != null
      ? taskData.retry_limit
      : state.backlog.defaults.retry_limit != null
        ? state.backlog.defaults.retry_limit
        : 2;
  fields.executor_type.value = executor.type || defaultExecutor.type || "codex";
  fields.executor_prompt.value = executor.prompt || "";
  fields.attachments.value = "";
  resetDraftAttachments(taskAttachments(taskData));
  renderTaskList();
}

function escapeHtml(text) {
  return String(text || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function loadBacklog() {
  state.backlog = await request("/api/backlog");
  const activeTaskIds = new Set(activeBacklogTasks().map((task) => task.id));
  for (const taskId of [...state.selectedTaskIds]) {
    if (!activeTaskIds.has(taskId)) {
      state.selectedTaskIds.delete(taskId);
    }
  }
  renderRepositoryRegistry();
  renderRepositoryOptions();
  renderTaskList();
  if (!state.currentTaskId) {
    populateTaskForm(null);
  } else {
    const current = state.backlog.tasks.find((task) => task.id === state.currentTaskId);
    if (current) {
      populateTaskForm(current);
      return;
    }
  }
}

async function setTaskReviewed(taskId, reviewed) {
  try {
    await request(`/api/tasks/${encodeURIComponent(taskId)}/reviewed`, {
      method: "POST",
      body: JSON.stringify({ reviewed }),
    });
    if (reviewed) {
      state.selectedTaskIds.delete(taskId);
    }
    setSaveStatus(
      reviewed ? `Moved ${taskId} to Done.` : `Moved ${taskId} back to Backlog.`,
      "success-text"
    );
    await loadBacklog();
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function loadRuns() {
  const payload = await request("/api/runs");
  state.runs = payload.runs;

  if (
    state.selectedRunId &&
    !state.runs.some((run) => run.run_id === state.selectedRunId)
  ) {
    state.selectedRunId = null;
  }

  renderRuns();
  syncRunHistoryControls();

  if (!state.selectedRunId && state.runs.length) {
    state.selectedRunId = state.runs[0].run_id;
  }

  if (state.selectedRunId) {
    try {
      const run = await request(`/api/runs/${encodeURIComponent(state.selectedRunId)}`);
      renderRunDetail(run);
    } catch (error) {
      renderRunDetail(null);
    }
  } else {
    renderRunDetail(null);
  }
}

async function clearRuns() {
  if (!state.runs.length) {
    setSaveStatus("No runs to clear.", "muted");
    return;
  }

  const activeRunCount = Number((state.overview && state.overview.active_run_count) || 0);
  if (activeRunCount > 0) {
    setSaveStatus("Wait for active runs to finish before clearing runs.", "error");
    return;
  }

  const confirmed = window.confirm(
    `Delete ${state.runs.length} saved ${state.runs.length === 1 ? "run" : "runs"} from local history? This cannot be undone.`
  );
  if (!confirmed) {
    return;
  }

  try {
    const result = await request("/api/runs", {
      method: "DELETE",
    });
    state.selectedRunId = null;
    renderRunDetail(null);
    await Promise.all([
      loadRuns(),
      (async () => {
        const overview = await request("/api/overview");
        renderOverview(overview);
      })(),
    ]);
    const deletedCount = Number(result.deleted_run_count || 0);
    setSaveStatus(
      deletedCount
        ? `Cleared ${pluralize(deletedCount, "run")} from local history.`
        : "No runs were cleared.",
      "success-text"
    );
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function refresh() {
  try {
    const overview = await request("/api/overview");
    renderOverview(overview);
    await Promise.all([loadBacklog(), loadRuns()]);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function handleTaskSubmit(event) {
  event.preventDefault();
  await waitForPendingAttachmentLoads();
  const form = event.currentTarget;
  const fields = form.elements;
  const payload = {
    id: fields.id.value.trim(),
    title: fields.title.value.trim(),
    repository_id: fields.repository_id.value.trim(),
    description: fields.description.value.trim(),
    test_command: fields.test_command.value.trim(),
    base_branch: fields.base_branch.value.trim(),
    working_directory: fields.working_directory.value.trim(),
    retry_limit: Number(fields.retry_limit.value || 2),
    executor: {
      type: fields.executor_type.value.trim() || "codex",
      prompt: fields.executor_prompt.value.trim(),
    },
    kept_attachment_ids: state.draftAttachments
      .filter((attachment) => attachment.existing && attachment.id)
      .map((attachment) => attachment.id),
    new_attachments: state.draftAttachments
      .filter((attachment) => !attachment.existing)
      .map((attachment) => ({
        name: attachment.name,
        content_type: attachment.content_type,
        size_bytes: attachment.size_bytes,
        data_base64: attachment.data_base64,
      })),
  };

  try {
    await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setSaveStatus(`Saved ${payload.id}.`, "success-text");
    await loadBacklog();
    const savedTask = state.backlog.tasks.find((task) => task.id === payload.id);
    populateTaskForm(savedTask || null);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function deleteCurrentTask() {
  if (!state.currentTaskId) {
    setSaveStatus("Select a task before deleting it.", "error");
    return;
  }

  await deleteTaskById(state.currentTaskId);
}

async function deleteTaskById(taskId) {
  try {
    await request(`/api/tasks/${encodeURIComponent(taskId)}`, {
      method: "DELETE",
    });
    state.selectedTaskIds.delete(taskId);
    if (state.currentTaskId === taskId) {
      populateTaskForm(null);
    }
    setSaveStatus(`Deleted ${taskId}.`, "success-text");
    await loadBacklog();
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function handleRepositorySubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const fields = form.elements;
  const payload = {
    id: fields.id.value.trim(),
    path: fields.path.value.trim(),
  };

  try {
    await request("/api/repositories", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    form.reset();
    await Promise.all([
      loadBacklog(),
      (async () => {
        const overview = await request("/api/overview");
        renderOverview(overview);
      })(),
    ]);
    setSaveStatus(`Saved repository ${payload.id}.`, "success-text");
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function deleteRepository(repositoryId) {
  if (!repositoryId) {
    return;
  }
  const confirmed = window.confirm(
    `Remove repository ${repositoryId}? Tasks linked to it must be moved first.`
  );
  if (!confirmed) {
    return;
  }

  try {
    await request(`/api/repositories/${encodeURIComponent(repositoryId)}`, {
      method: "DELETE",
    });
    await Promise.all([
      loadBacklog(),
      (async () => {
        const overview = await request("/api/overview");
        renderOverview(overview);
      })(),
    ]);
    setSaveStatus(`Removed repository ${repositoryId}.`, "success-text");
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function startRun(taskIds = []) {
  if (state.overview && !state.overview.development_flow_allowed) {
    const message =
      state.overview.development_flow_error ||
      "Development flow can only start from `main` with a clean working tree.";
    setSaveStatus(message, "error");
    window.alert(message);
    return;
  }

  try {
    const payload = {
      task_ids: taskIds,
    };
    const run = await request("/api/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.selectedRunId = run.run_id;
    setSaveStatus(`Started run ${run.run_id}.`, "success-text");
    await loadRuns();
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function resumeRunTask(runId, taskId) {
  try {
    const run = await request(`/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(taskId)}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.selectedRunId = run.run_id;
    setSaveStatus(`Resumed ${taskId} from its last incomplete stage.`, "success-text");
    await loadRuns();
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

async function cleanupMergedBranches() {
  if (!state.overview) {
    return;
  }

  const mergedBranchCount = Number(state.overview.merged_task_branch_count || 0);
  if (mergedBranchCount === 0) {
    setSaveStatus("No merged task branches to remove.", "muted");
    return;
  }

  if (state.overview.branch_cleanup_error) {
    setSaveStatus(state.overview.branch_cleanup_error, "error");
    return;
  }

  const branchLabel = mergedBranchCount === 1 ? "branch" : "branches";
  const confirmed = window.confirm(
    `Remove ${mergedBranchCount} merged task ${branchLabel}? This also removes their clean worktrees.`
  );
  if (!confirmed) {
    return;
  }

  try {
    const result = await request("/api/repo/remove-merged-branches", {
      method: "POST",
      body: JSON.stringify({}),
    });
    await refresh();

    const deletedCount = Array.isArray(result.deleted_branches) ? result.deleted_branches.length : 0;
    const skippedCount = Array.isArray(result.skipped) ? result.skipped.length : 0;

    if (deletedCount > 0 && skippedCount > 0) {
      setSaveStatus(
        `Removed ${deletedCount} merged ${deletedCount === 1 ? "branch" : "branches"} and skipped ${skippedCount}.`,
        "warning-text"
      );
      return;
    }
    if (deletedCount > 0) {
      setSaveStatus(
        `Removed ${deletedCount} merged ${deletedCount === 1 ? "branch" : "branches"}.`,
        "success-text"
      );
      return;
    }
    if (skippedCount > 0) {
      setSaveStatus(
        `Skipped ${skippedCount} merged ${skippedCount === 1 ? "branch" : "branches"}.`,
        "warning-text"
      );
      return;
    }
    setSaveStatus("No merged task branches to remove.", "muted");
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}

function wireEvents() {
  document.getElementById("task-form").addEventListener("submit", handleTaskSubmit);
  document
    .getElementById("repository-form")
    .addEventListener("submit", handleRepositorySubmit);
  document.getElementById("clear-runs-button").addEventListener("click", clearRuns);
  document
    .getElementById("task-attachments-input")
    .addEventListener("change", async (event) => {
      const draftSession = state.attachmentDraftSession;
      const loadPromise = trackPendingAttachmentLoad(
        addDraftAttachments(event.target.files, draftSession)
      );
      try {
        setSaveStatus("Preparing attachments...", "muted");
        await loadPromise;
        setSaveStatus("Attachments ready to save.", "muted");
        event.target.value = "";
      } catch (error) {
        setSaveStatus(error.message, "error");
      }
    });
  document.getElementById("delete-task-button").addEventListener("click", deleteCurrentTask);
  document.getElementById("new-task-button").addEventListener("click", () => populateTaskForm(null));
  document.getElementById("run-all-button").addEventListener("click", () => startRun([]));
  document.getElementById("run-selected-button").addEventListener("click", () =>
    startRun([...state.selectedTaskIds])
  );
  document
    .getElementById("cleanup-merged-branches-button")
    .addEventListener("click", cleanupMergedBranches);
  document.getElementById("settings-button").addEventListener("click", () => setSettingsModalOpen(true));
  document
    .getElementById("close-settings-button")
    .addEventListener("click", () => setSettingsModalOpen(false));

  document.body.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "close-settings") {
      setSettingsModalOpen(false);
    }
    if (action === "edit-task") {
      const task = state.backlog.tasks.find((item) => item.id === button.dataset.taskId);
      populateTaskForm(task || null);
    }
    if (action === "remove-task") {
      deleteTaskById(button.dataset.taskId);
    }
    if (action === "delete-repository") {
      deleteRepository(button.dataset.repositoryId);
    }
    if (action === "remove-attachment") {
      removeDraftAttachment(button.dataset.attachmentId);
    }
    if (action === "toggle-reviewed") {
      setTaskReviewed(button.dataset.taskId, button.dataset.reviewed === "true");
    }
    if (action === "select-run") {
      state.selectedRunId = button.dataset.runId;
      loadRuns();
    }
    if (action === "resume-task") {
      resumeRunTask(button.dataset.runId, button.dataset.taskId);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setSettingsModalOpen(false);
    }
  });

  document.body.addEventListener("change", (event) => {
    const input = event.target.closest('[data-action="toggle-task"]');
    if (!input) return;
    if (input.checked) {
      state.selectedTaskIds.add(input.dataset.taskId);
    } else {
      state.selectedTaskIds.delete(input.dataset.taskId);
    }
    syncRunButtons();
  });

  document.addEventListener(
    "toggle",
    (event) => {
      const details = event.target;
      if (!(details instanceof HTMLDetailsElement)) return;
      const key = details.dataset.detailKey;
      if (!key) return;
      if (details.open) {
        state.detailOpenKeys.add(key);
      } else {
        state.detailOpenKeys.delete(key);
      }
    },
    true
  );
}

wireEvents();
refresh();
setInterval(async () => {
  try {
    await loadRuns();
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}, 2000);
setInterval(async () => {
  try {
    const overview = await request("/api/overview");
    renderOverview(overview);
  } catch (error) {
    setSaveStatus(error.message, "error");
  }
}, 4000);
