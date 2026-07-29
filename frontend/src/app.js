import { createClient } from "@supabase/supabase-js";

const config = Object.freeze(window.__CV_MATCHER_CONFIG__ ?? {});
const apiBaseUrl = String(config.apiBaseUrl ?? "").replace(/\/+$/, "");
const allowAnonymous = config.allowAnonymous === true;
const pollIntervalMs = clampNumber(config.pollIntervalMs, 1000, 30000, 2500);
const maxCvBytes = clampNumber(config.maxCvBytes, 1024, 50 * 1024 * 1024, 10 * 1024 * 1024);
const taskStorageKey = "job-lens.active-task.v1";

const terminalStatuses = new Set([
  "complete",
  "completed",
  "failed",
  "cancelled",
  "canceled",
  "expired",
]);
const successfulStatuses = new Set(["complete", "completed"]);
const failureStatuses = new Set(["failed", "cancelled", "canceled", "expired"]);
const statusLabels = {
  queued: "Queued",
  launching: "Launching worker",
  running: "Running",
  extracting: "Reading CV",
  matching: "Matching jobs",
  reviewing: "Reviewing matches",
  reporting: "Creating reports",
  emailing: "Sending email",
  complete: "Complete",
  completed: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
  canceled: "Cancelled",
  expired: "Expired",
  retrying: "Retrying",
};
const statusDescriptions = {
  queued: "Waiting for an available matching worker.",
  launching: "Starting the matching worker.",
  running: "The matching worker is processing this task.",
  extracting: "Reading skills and role evidence from your CV.",
  matching: "Comparing your profile with the stored job inventory.",
  reviewing: "Reviewing the most likely vacancies.",
  reporting: "Preparing the result files.",
  emailing: "Preparing the result notification.",
  complete: "The server reports that this task has finished.",
  completed: "The server reports that this task has finished.",
  failed: "The server could not finish this task.",
  cancelled: "This task was cancelled.",
  canceled: "This task was cancelled.",
  expired: "This task can no longer be retried.",
  retrying: "The task will be attempted again.",
};

const elements = {
  authBadge: document.querySelector("#auth-badge"),
  authEmail: document.querySelector("#auth-email"),
  authForm: document.querySelector("#auth-form"),
  authMessage: document.querySelector("#auth-message"),
  clearTaskButton: document.querySelector("#clear-task-button"),
  configurationAlert: document.querySelector("#configuration-alert"),
  cvFile: document.querySelector("#cv-file"),
  fileDropZone: document.querySelector("#file-drop-zone"),
  fileHelp: document.querySelector("#file-help"),
  fileLabel: document.querySelector("#file-label"),
  localModePanel: document.querySelector("#local-mode-panel"),
  matchFields: document.querySelector("#match-fields"),
  matchForm: document.querySelector("#match-form"),
  refreshResultsButton: document.querySelector("#refresh-results-button"),
  refreshTaskButton: document.querySelector("#refresh-task-button"),
  resultList: document.querySelector("#result-list"),
  resultMessage: document.querySelector("#result-message"),
  resultPanel: document.querySelector("#result-panel"),
  sendLinkButton: document.querySelector("#send-link-button"),
  sessionSummary: document.querySelector("#session-summary"),
  signOutButton: document.querySelector("#sign-out-button"),
  signedInEmail: document.querySelector("#signed-in-email"),
  signedInPanel: document.querySelector("#signed-in-panel"),
  signedOutPanel: document.querySelector("#signed-out-panel"),
  submissionMessage: document.querySelector("#submission-message"),
  submitButton: document.querySelector("#submit-button"),
  taskError: document.querySelector("#task-error"),
  taskProgress: document.querySelector("#task-progress"),
  taskProgressLabel: document.querySelector("#task-progress-label"),
  taskReference: document.querySelector("#task-reference"),
  taskSection: document.querySelector("#task-section"),
  taskStatusBadge: document.querySelector("#task-status-badge"),
  taskStatusMessage: document.querySelector("#task-status-message"),
};

let supabase = null;
let session = null;
let activeTask = loadActiveTask();
let pollTimer = null;
let pollInFlight = false;
let submitInFlight = false;
let submissionKey = createIdempotencyKey();

void initialise();

async function initialise() {
  bindEvents();
  updateFileHelp();

  if (!apiBaseUrl) {
    showConfigurationError("The API address is missing. Set VITE_API_BASE_URL before building.");
  }

  const hasSupabaseConfig = Boolean(config.supabaseUrl && config.supabasePublishableKey);
  if (hasSupabaseConfig) {
    try {
      supabase = createClient(config.supabaseUrl, config.supabasePublishableKey, {
        auth: {
          persistSession: true,
          autoRefreshToken: true,
          detectSessionInUrl: true,
        },
      });
      const { data, error } = await supabase.auth.getSession();
      if (error) {
        throw error;
      }
      session = data.session;
      supabase.auth.onAuthStateChange((_event, nextSession) => {
        session = nextSession;
        renderAuthState();
        if (activeTask && canCallApi()) {
          schedulePoll(0);
        }
      });
    } catch (error) {
      showConfigurationError(`Authentication could not start: ${friendlyError(error)}`);
    }
  } else if (!allowAnonymous) {
    showConfigurationError(
      "Supabase browser authentication is not configured. Set the public project URL and publishable key.",
    );
  }

  renderAuthState();
  if (activeTask) {
    renderStoredTask(activeTask);
    if (canCallApi()) {
      schedulePoll(0);
    }
  }
}

function bindEvents() {
  elements.authForm.addEventListener("submit", sendMagicLink);
  elements.signOutButton.addEventListener("click", signOut);
  elements.matchForm.addEventListener("submit", submitTask);
  elements.matchForm.addEventListener("input", () => {
    if (!submitInFlight) {
      submissionKey = createIdempotencyKey();
    }
  });
  elements.cvFile.addEventListener("change", renderSelectedFile);
  elements.refreshTaskButton.addEventListener("click", () => schedulePoll(0));
  elements.refreshResultsButton.addEventListener("click", fetchAndRenderResults);
  elements.clearTaskButton.addEventListener("click", clearActiveTask);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && activeTask && canCallApi()) {
      schedulePoll(0);
    }
  });

  for (const eventName of ["dragenter", "dragover"]) {
    elements.fileDropZone.addEventListener(eventName, () => {
      elements.fileDropZone.classList.add("drag-active");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    elements.fileDropZone.addEventListener(eventName, () => {
      elements.fileDropZone.classList.remove("drag-active");
    });
  }
}

async function sendMagicLink(event) {
  event.preventDefault();
  setMessage(elements.authMessage, "");
  if (!supabase) {
    setMessage(elements.authMessage, "Authentication is not configured.", true);
    return;
  }

  const email = elements.authEmail.value.trim();
  if (!email || !elements.authEmail.checkValidity()) {
    elements.authEmail.reportValidity();
    return;
  }

  elements.sendLinkButton.disabled = true;
  elements.sendLinkButton.textContent = "Sending…";
  try {
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin },
    });
    if (error) {
      throw error;
    }
    setMessage(
      elements.authMessage,
      "Sign-in link sent. Open it in this browser to continue.",
    );
  } catch (error) {
    setMessage(elements.authMessage, friendlyError(error), true);
  } finally {
    elements.sendLinkButton.disabled = false;
    elements.sendLinkButton.textContent = "Send sign-in link";
  }
}

async function signOut() {
  if (!supabase) {
    return;
  }
  elements.signOutButton.disabled = true;
  try {
    const { error } = await supabase.auth.signOut();
    if (error) {
      throw error;
    }
    session = null;
    stopPolling();
    renderAuthState();
  } catch (error) {
    setMessage(elements.authMessage, friendlyError(error), true);
  } finally {
    elements.signOutButton.disabled = false;
  }
}

function renderAuthState() {
  const authenticated = Boolean(session?.access_token);
  const localMode = !supabase && allowAnonymous;
  elements.signedOutPanel.classList.toggle("hidden", authenticated || localMode);
  elements.signedInPanel.classList.toggle("hidden", !authenticated);
  elements.localModePanel.classList.toggle("hidden", !localMode);

  if (authenticated) {
    const email = session.user?.email ?? "authenticated user";
    elements.signedInEmail.textContent = email;
    elements.sessionSummary.textContent = `Signed in as ${email}`;
    elements.authBadge.textContent = "Signed in";
    elements.authBadge.className = "badge badge-success";
  } else if (localMode) {
    elements.sessionSummary.textContent = "Local development mode";
    elements.authBadge.textContent = "Local mode";
    elements.authBadge.className = "badge badge-running";
  } else {
    elements.sessionSummary.textContent = "Sign in to begin";
    elements.authBadge.textContent = supabase ? "Signed out" : "Not configured";
    elements.authBadge.className = supabase ? "badge" : "badge badge-error";
  }

  elements.matchFields.disabled = !canCallApi() || !apiBaseUrl || submitInFlight;
}

async function submitTask(event) {
  event.preventDefault();
  setMessage(elements.submissionMessage, "");
  if (!canCallApi()) {
    setMessage(elements.submissionMessage, "Sign in before starting a task.", true);
    return;
  }
  if (!apiBaseUrl) {
    setMessage(elements.submissionMessage, "The API address is not configured.", true);
    return;
  }
  if (!elements.matchForm.checkValidity()) {
    elements.matchForm.reportValidity();
    return;
  }

  const cv = elements.cvFile.files?.[0];
  if (!cv) {
    setMessage(elements.submissionMessage, "Choose a CV file.", true);
    return;
  }
  if (cv.size > maxCvBytes) {
    setMessage(
      elements.submissionMessage,
      `The selected CV is larger than ${formatBytes(maxCvBytes)}.`,
      true,
    );
    return;
  }

  const data = new FormData(elements.matchForm);
  data.set("remote", document.querySelector("#remote").checked ? "true" : "false");
  const keyUsed = submissionKey;
  submitInFlight = true;
  elements.submitButton.disabled = true;
  elements.submitButton.textContent = "Uploading CV…";

  try {
    const response = await apiFetch("/v1/tasks", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Idempotency-Key": keyUsed,
      },
      body: data,
    });
    const payload = await readResponse(response);
    const task = unwrapTask(payload);
    const taskId = String(task.id ?? task.task_id ?? payload?.task_id ?? "").trim();
    if (!taskId) {
      throw new Error("The API accepted the request but did not return a task ID.");
    }

    activeTask = {
      id: taskId,
      position: String(data.get("position") ?? ""),
      submittedAt: new Date().toISOString(),
      status: normaliseStatus(task.status ?? "queued"),
    };
    persistActiveTask();
    submissionKey = createIdempotencyKey();
    renderTask(task, activeTask);
    setMessage(
      elements.submissionMessage,
      "The API accepted your task. Progress will continue even if you close this page.",
    );
    schedulePoll(pollIntervalMs);
    elements.taskSection.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setMessage(
      elements.submissionMessage,
      `${friendlyError(error)} You can retry; this browser will reuse the same submission key.`,
      true,
    );
  } finally {
    submitInFlight = false;
    elements.submitButton.disabled = false;
    elements.submitButton.textContent = "Start matching";
    renderAuthState();
  }
}

async function pollTask() {
  if (!activeTask || !canCallApi() || pollInFlight) {
    return;
  }
  pollInFlight = true;
  clearTimeout(pollTimer);
  pollTimer = null;
  elements.refreshTaskButton.disabled = true;
  try {
    const response = await apiFetch(`/v1/tasks/${encodeURIComponent(activeTask.id)}`, {
      headers: { Accept: "application/json" },
    });
    const payload = await readResponse(response);
    const task = unwrapTask(payload);
    renderTask(task, activeTask);

    const status = normaliseStatus(task.status ?? activeTask.status);
    activeTask.status = status;
    activeTask.lastCheckedAt = new Date().toISOString();
    persistActiveTask();

    if (terminalStatuses.has(status)) {
      stopPolling();
      if (successfulStatuses.has(status)) {
        await fetchAndRenderResults(payload);
      }
    } else {
      schedulePoll(pollIntervalMs);
    }
  } catch (error) {
    const isAuthError = error instanceof ApiError && (error.status === 401 || error.status === 403);
    setTaskError(
      isAuthError
        ? "Your session cannot access this task. Sign in with the account that created it."
        : `Status refresh failed: ${friendlyError(error)} The task may still be running.`,
    );
    if (!isAuthError) {
      schedulePoll(Math.max(pollIntervalMs * 2, 5000));
    }
  } finally {
    pollInFlight = false;
    elements.refreshTaskButton.disabled = false;
  }
}

function renderTask(task, storedTask = activeTask) {
  const status = normaliseStatus(task.status ?? storedTask?.status ?? "queued");
  const taskId = String(task.id ?? task.task_id ?? storedTask?.id ?? "");
  const position = String(task.position ?? task.target_role ?? storedTask?.position ?? "").trim();
  elements.taskSection.classList.remove("hidden");
  elements.taskReference.textContent = position
    ? `${position} · Task ${taskId}`
    : `Task ${taskId}`;
  elements.taskStatusBadge.textContent = statusLabels[status] ?? humanise(status);
  elements.taskStatusBadge.className = `badge ${badgeClassForStatus(status)}`;

  const serverMessage = firstString(
    task.status_message,
    task.progress_message,
    task.current_step,
    task.message,
  );
  elements.taskStatusMessage.textContent =
    serverMessage || statusDescriptions[status] || "The server returned an updated task state.";

  const progress = readProgress(task);
  if (progress.percent === null) {
    elements.taskProgress.removeAttribute("value");
    elements.taskProgressLabel.textContent =
      progress.label || "Progress will update from the server.";
  } else {
    elements.taskProgress.value = progress.percent;
    elements.taskProgressLabel.textContent =
      progress.label || `${Math.round(progress.percent)}% reported by the server.`;
  }

  const errorMessage = firstString(
    task.error_message,
    task.last_error,
    task.error?.message,
    failureStatuses.has(status) ? task.message : "",
  );
  setTaskError(failureStatuses.has(status) ? errorMessage || statusDescriptions[status] : "");
  elements.clearTaskButton.classList.toggle("hidden", !terminalStatuses.has(status));

  const embeddedFiles = extractFiles(task);
  if (embeddedFiles.length) {
    renderResults(embeddedFiles, firstString(task.result_message));
  } else if (!successfulStatuses.has(status)) {
    elements.resultPanel.classList.add("hidden");
  }
}

function renderStoredTask(task) {
  renderTask(
    {
      id: task.id,
      status: task.status || "queued",
      status_message: "Restoring the last task saved in this browser.",
    },
    task,
  );
}

async function fetchAndRenderResults(existingPayload = null) {
  if (!activeTask || !canCallApi()) {
    return;
  }
  elements.refreshResultsButton.disabled = true;
  elements.resultPanel.classList.remove("hidden");
  elements.resultMessage.textContent = "Requesting fresh download links…";
  try {
    let payload = existingPayload;
    let files = extractFiles(payload);
    if (!files.length) {
      const response = await apiFetch(
        `/v1/tasks/${encodeURIComponent(activeTask.id)}/results`,
        { headers: { Accept: "application/json" } },
      );
      payload = await readResponse(response);
      files = extractFiles(payload);
    }
    renderResults(files, firstString(payload?.message, payload?.result_message));
  } catch (error) {
    elements.resultList.replaceChildren();
    elements.resultMessage.textContent =
      `Result links are not available right now: ${friendlyError(error)}`;
  } finally {
    elements.refreshResultsButton.disabled = false;
  }
}

function renderResults(files, message = "") {
  elements.resultPanel.classList.remove("hidden");
  elements.resultList.replaceChildren();
  let validCount = 0;

  for (const [index, file] of files.entries()) {
    const rawUrl =
      typeof file === "string"
        ? file
        : firstString(file.download_url, file.signed_url, file.url, file.href);
    const url = safeDownloadUrl(rawUrl);
    if (!url) {
      continue;
    }
    const label =
      typeof file === "string"
        ? fileNameFromUrl(url)
        : firstString(file.display_name, file.name, file.filename, file.kind) ||
          `Result file ${index + 1}`;
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = humanise(label);
    link.setAttribute("aria-label", `Open ${humanise(label)} in a new tab`);
    item.append(link);
    elements.resultList.append(item);
    validCount += 1;
  }

  if (validCount) {
    elements.resultMessage.textContent =
      message || `${validCount} result ${validCount === 1 ? "file is" : "files are"} available.`;
  } else {
    elements.resultMessage.textContent =
      message ||
      "The task is complete, but the API has not returned any downloadable files yet. Refresh the links to check again.";
  }
}

function extractFiles(payload) {
  if (!payload) {
    return [];
  }
  if (Array.isArray(payload)) {
    return payload;
  }
  const task = unwrapTask(payload);
  const candidates = [
    payload.result_files,
    payload.files,
    payload.results?.files,
    payload.results,
    task.result_files,
    task.files,
    task.results?.files,
    task.results,
  ];
  for (const candidate of candidates) {
    if (Array.isArray(candidate)) {
      return candidate;
    }
  }
  return [];
}

function readProgress(task) {
  const raw = task.progress_percent ?? task.progress_percentage ?? task.percent_complete;
  if (Number.isFinite(Number(raw))) {
    const numeric = Number(raw);
    const percent = numeric >= 0 && numeric <= 1 ? numeric * 100 : numeric;
    return {
      percent: Math.min(100, Math.max(0, percent)),
      label: firstString(task.progress_label),
    };
  }

  if (task.progress && typeof task.progress === "object") {
    const current = Number(task.progress.current ?? task.progress.completed);
    const total = Number(task.progress.total);
    if (Number.isFinite(current) && Number.isFinite(total) && total > 0) {
      return {
        percent: Math.min(100, Math.max(0, (current / total) * 100)),
        label: `${current} of ${total} items reported by the server.`,
      };
    }
  }
  return { percent: null, label: firstString(task.progress_label) };
}

function renderSelectedFile() {
  const file = elements.cvFile.files?.[0];
  elements.fileLabel.textContent = file?.name || "Choose your CV";
  elements.fileHelp.textContent = file
    ? `${formatBytes(file.size)} selected`
    : `PDF, Word, or text. Maximum ${formatBytes(maxCvBytes)}.`;
}

function updateFileHelp() {
  elements.fileHelp.textContent = `PDF, Word, or text. Maximum ${formatBytes(maxCvBytes)}.`;
}

function clearActiveTask() {
  stopPolling();
  activeTask = null;
  localStorage.removeItem(taskStorageKey);
  elements.taskSection.classList.add("hidden");
  elements.resultPanel.classList.add("hidden");
  setTaskError("");
}

function persistActiveTask() {
  if (!activeTask) {
    return;
  }
  try {
    localStorage.setItem(taskStorageKey, JSON.stringify(activeTask));
  } catch {
    // Task persistence is a convenience; the API remains authoritative.
  }
}

function loadActiveTask() {
  try {
    const parsed = JSON.parse(localStorage.getItem(taskStorageKey));
    if (parsed && typeof parsed.id === "string" && parsed.id.trim()) {
      return parsed;
    }
  } catch {
    localStorage.removeItem(taskStorageKey);
  }
  return null;
}

function schedulePoll(delay) {
  if (!activeTask || !canCallApi()) {
    return;
  }
  clearTimeout(pollTimer);
  pollTimer = window.setTimeout(() => void pollTask(), delay);
}

function stopPolling() {
  clearTimeout(pollTimer);
  pollTimer = null;
}

function canCallApi() {
  return Boolean(session?.access_token) || allowAnonymous;
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers ?? {});
  if (session?.access_token) {
    headers.set("Authorization", `Bearer ${session.access_token}`);
  }
  const response = await fetch(`${apiBaseUrl}${path}`, { ...options, headers });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.clone().json();
      detail = firstString(payload.error?.message, payload.error, payload.message, payload.detail);
    } catch {
      detail = (await response.text()).slice(0, 300);
    }
    throw new ApiError(response.status, detail || `Request failed with status ${response.status}.`);
  }
  return response;
}

async function readResponse(response) {
  if (response.status === 204) {
    return {};
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    throw new Error("The API returned an unexpected response format.");
  }
  return response.json();
}

function unwrapTask(payload) {
  if (!payload || typeof payload !== "object") {
    return {};
  }
  if (payload.task && typeof payload.task === "object") {
    return payload.task;
  }
  if (payload.data?.task && typeof payload.data.task === "object") {
    return payload.data.task;
  }
  if (payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)) {
    return payload.data;
  }
  return payload;
}

function setTaskError(message) {
  const hasMessage = Boolean(message);
  elements.taskError.textContent = hasMessage ? message : "";
  elements.taskError.classList.toggle("hidden", !hasMessage);
}

function setMessage(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
}

function showConfigurationError(message) {
  const existing = elements.configurationAlert.textContent.trim();
  elements.configurationAlert.textContent = existing ? `${existing} ${message}` : message;
  elements.configurationAlert.classList.remove("hidden");
}

function normaliseStatus(value) {
  return String(value ?? "queued")
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
}

function badgeClassForStatus(status) {
  if (successfulStatuses.has(status)) {
    return "badge-success";
  }
  if (failureStatuses.has(status)) {
    return "badge-error";
  }
  return "badge-running";
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return "";
}

function humanise(value) {
  const text = String(value ?? "")
    .replace(/\.[^.]+$/, "")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text ? text.replace(/\b\w/g, (character) => character.toUpperCase()) : "Result file";
}

function safeDownloadUrl(value) {
  if (!value) {
    return "";
  }
  try {
    const url = new URL(value, apiBaseUrl || window.location.origin);
    if (url.protocol !== "https:" && url.protocol !== "http:") {
      return "";
    }
    return url.href;
  } catch {
    return "";
  }
}

function fileNameFromUrl(value) {
  try {
    const pathname = new URL(value).pathname;
    return decodeURIComponent(pathname.split("/").filter(Boolean).at(-1) ?? "Result file");
  } catch {
    return "Result file";
  }
}

function friendlyError(error) {
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return "Too many requests were sent. Please wait and try again.";
    }
    if (error.status === 401 || error.status === 403) {
      return "Your sign-in session is missing or no longer valid.";
    }
    if (error.status >= 500) {
      return "The service is temporarily unavailable.";
    }
    return error.message;
  }
  if (error instanceof TypeError && /fetch/i.test(error.message)) {
    return "The API could not be reached. Check the connection and API address.";
  }
  return firstString(error?.message) || "Something went wrong.";
}

function createIdempotencyKey() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function clampNumber(value, minimum, maximum, fallback) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.min(maximum, Math.max(minimum, numeric)) : fallback;
}

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}
