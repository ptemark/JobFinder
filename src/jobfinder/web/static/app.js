"use strict";

// Vanilla dashboard client (LLD §9.3): fetch ranked jobs from the local backend,
// render cards, drive the filter/sort form, post status updates optimistically,
// and trigger a manual poll. No build step, no framework. Talks only to the
// same-origin local API. API errors surface in the role="alert" region; they are
// never swallowed.

const API = {
  jobs: "/api/jobs",
  status: (id) => `/api/jobs/${encodeURIComponent(id)}/status`,
  poll: "/api/poll",
  runsLatest: "/api/runs/latest",
};

// Status values a user can set from a card (LLD §9.1; "new" is the default state,
// not an action button).
const STATUS_ACTIONS = [
  { state: "interested", label: "Interested" },
  { state: "applied", label: "Applied" },
  { state: "dismissed", label: "Dismissed" },
];

// The two top-level views (LLD §9.3). "all" reads the filter form (the backend
// hides applied+dismissed by default); "applied" forces status=applied&sort=newest.
const TAB_ALL = "all";
const TAB_APPLIED = "applied";
let currentTab = TAB_ALL;

const els = {
  alert: document.getElementById("alert"),
  filters: document.getElementById("filters"),
  jobList: document.getElementById("job-list"),
  resultsSummary: document.getElementById("results-summary"),
  results: document.querySelector(".results"),
  pollNow: document.getElementById("poll-now"),
  runStatus: document.getElementById("run-status"),
  tabs: document.getElementById("tabs"),
  statusNote: document.getElementById("status-note"),
};

function showError(message) {
  els.alert.textContent = message;
  els.alert.hidden = false;
}

function clearError() {
  els.alert.textContent = "";
  els.alert.hidden = true;
}

// A non-error confirmation (e.g. the Sheets sync result on "applied"). Distinct
// from showError — this is a status, never a failure.
function showStatusNote(message) {
  els.statusNote.textContent = message;
  els.statusNote.hidden = false;
}

function clearStatusNote() {
  els.statusNote.textContent = "";
  els.statusNote.hidden = true;
}

// Build the /api/jobs query string for the active tab (LLD §9.3). The Applied tab
// always asks for applied jobs, newest-first, ignoring the sidebar filters; the All
// tab reads the filter form (blank fields dropped so the backend's defaults apply —
// which already hide applied+dismissed).
function buildQuery() {
  const params = new URLSearchParams();
  if (currentTab === TAB_APPLIED) {
    params.set("status", TAB_APPLIED);
    params.set("sort", "newest");
    params.set("include_ineligible", "false");
    return params.toString();
  }
  const data = new FormData(els.filters);
  for (const [key, value] of data.entries()) {
    if (key === "include_ineligible") {
      continue; // handled explicitly below (unchecked boxes are absent here)
    }
    if (value !== "") {
      params.set(key, value);
    }
  }
  params.set("include_ineligible", els.filters.elements.include_ineligible.checked);
  return params.toString();
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`Request to ${url} failed (${response.status})`);
  }
  return response.json();
}

function ageText(card) {
  if (card.date_unknown || card.age_days === null) {
    return "Date unknown";
  }
  return `${card.age_days}d ago`;
}

function createBadge(text, className) {
  const span = document.createElement("span");
  span.className = `badge ${className}`;
  span.textContent = text;
  return span;
}

function createStatusButtons(card) {
  const group = document.createElement("div");
  group.className = "card__status";
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", `Set status for ${card.title}`);
  for (const action of STATUS_ACTIONS) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn--status";
    button.dataset.jobId = card.id;
    button.dataset.state = action.state;
    button.textContent = action.label;
    button.setAttribute("aria-pressed", String(card.status === action.state));
    group.appendChild(button);
  }
  return group;
}

function createCard(card) {
  const item = document.createElement("li");
  item.className = "job-card";
  item.dataset.jobId = card.id;
  item.dataset.status = card.status;

  const header = document.createElement("div");
  header.className = "job-card__header";

  const score = createBadge(`${Math.round(card.score)}`, "badge--score");
  score.setAttribute("aria-label", `Match score ${Math.round(card.score)} of 100`);
  header.appendChild(score);

  const titleWrap = document.createElement("div");
  titleWrap.className = "job-card__title-wrap";
  const title = document.createElement("h2");
  title.className = "job-card__title";
  if (card.url) {
    const link = document.createElement("a");
    link.href = card.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = card.title;
    title.appendChild(link);
  } else {
    title.textContent = card.title;
  }
  titleWrap.appendChild(title);

  const company = document.createElement("p");
  company.className = "job-card__company";
  company.textContent = card.company || "Unknown company";
  titleWrap.appendChild(company);
  header.appendChild(titleWrap);

  if (card.is_new_since_last_poll) {
    header.appendChild(createBadge("NEW", "badge--new"));
  }
  item.appendChild(header);

  const meta = document.createElement("div");
  meta.className = "job-card__meta";
  meta.appendChild(createBadge(card.location_bucket.replace(/_/g, " "), "badge--location"));
  if (card.is_remote) {
    meta.appendChild(createBadge("remote", "badge--remote"));
  }
  meta.appendChild(createBadge(ageText(card), "badge--age"));
  item.appendChild(meta);

  if (card.matched_skills.length > 0) {
    const skills = document.createElement("div");
    skills.className = "job-card__skills";
    skills.setAttribute("aria-label", "Matched skills");
    for (const skill of card.matched_skills) {
      skills.appendChild(createBadge(skill, "badge--skill"));
    }
    item.appendChild(skills);
  }

  item.appendChild(createStatusButtons(card));
  return item;
}

function renderJobs(payload) {
  els.jobList.replaceChildren();
  const count = payload.items.length;
  els.resultsSummary.textContent =
    payload.total === 0
      ? "No matching jobs yet. Run a poll to fetch postings."
      : `Showing ${count} of ${payload.total} job${payload.total === 1 ? "" : "s"}.`;
  for (const card of payload.items) {
    els.jobList.appendChild(createCard(card));
  }
}

async function loadJobs() {
  els.results.setAttribute("aria-busy", "true");
  try {
    const payload = await fetchJson(`${API.jobs}?${buildQuery()}`);
    renderJobs(payload);
    clearError();
  } catch (error) {
    showError(`Could not load jobs: ${error.message}`);
  } finally {
    els.results.setAttribute("aria-busy", "false");
  }
}

async function loadRunStatus() {
  try {
    const response = await fetch(API.runsLatest);
    if (response.status === 404) {
      els.runStatus.textContent = "No polls yet";
      return;
    }
    if (!response.ok) {
      throw new Error(`status ${response.status}`);
    }
    const run = await response.json();
    if (run.finished_at) {
      els.runStatus.textContent = `Last poll: run ${run.run_id}`;
    } else {
      els.runStatus.textContent = `Polling… (run ${run.run_id})`;
    }
  } catch (error) {
    // A run-status read failure is non-fatal to browsing; surface it but keep going.
    showError(`Could not load run status: ${error.message}`);
  }
}

function handleFilterSubmit(event) {
  event.preventDefault();
  loadJobs();
}

function handleTabClick(event) {
  const button = event.target.closest("button[data-tab]");
  if (button === null) {
    return;
  }
  const tab = button.dataset.tab;
  if (tab === currentTab) {
    return;
  }
  currentTab = tab;
  for (const sibling of els.tabs.querySelectorAll("button[data-tab]")) {
    sibling.setAttribute("aria-selected", String(sibling.dataset.tab === tab));
  }
  clearStatusNote();
  loadJobs();
}

// Whether a card should remain visible after its status changed to `state`, given
// the active view: the Applied tab shows only applied jobs; the All tab honours an
// explicit status filter, else hides applied+dismissed (matching the backend).
function isCardVisibleAfterStatus(state) {
  if (currentTab === TAB_APPLIED) {
    return state === TAB_APPLIED;
  }
  const statusFilter = els.filters.elements.status.value;
  if (statusFilter !== "") {
    return state === statusFilter;
  }
  return state !== "applied" && state !== "dismissed";
}

async function handleStatusClick(event) {
  const button = event.target.closest("button[data-state]");
  if (button === null) {
    return;
  }
  const { jobId, state } = button.dataset;
  const card = els.jobList.querySelector(`.job-card[data-job-id="${CSS.escape(jobId)}"]`);
  try {
    const result = await fetchJson(API.status(jobId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state }),
    });
    clearError();
    // Marking applied reports the optional Sheets sync outcome (LLD §9.3/§16).
    if (state === "applied") {
      showStatusNote(
        result.sheet_synced
          ? "Marked applied · added to tracking sheet."
          : "Marked applied · tracking sheet not configured.",
      );
    } else {
      clearStatusNote();
    }
    // Optimistically reflect the new state: toggle pressed buttons, and drop the
    // card when its new state is no longer visible in the active view.
    if (card !== null) {
      card.dataset.status = state;
      for (const sibling of card.querySelectorAll("button[data-state]")) {
        sibling.setAttribute("aria-pressed", String(sibling.dataset.state === state));
      }
      if (!isCardVisibleAfterStatus(state)) {
        card.remove();
      }
    }
  } catch (error) {
    showError(`Could not update status: ${error.message}`);
  }
}

async function handlePollNow() {
  els.pollNow.disabled = true;
  els.runStatus.textContent = "Starting poll…";
  try {
    const result = await fetchJson(API.poll, { method: "POST" });
    clearError();
    els.runStatus.textContent = `Polling… (run ${result.run_id})`;
  } catch (error) {
    showError(`Could not start poll: ${error.message}`);
  } finally {
    els.pollNow.disabled = false;
  }
}

els.filters.addEventListener("submit", handleFilterSubmit);
els.jobList.addEventListener("click", handleStatusClick);
els.pollNow.addEventListener("click", handlePollNow);
els.tabs.addEventListener("click", handleTabClick);

loadRunStatus();
loadJobs();
