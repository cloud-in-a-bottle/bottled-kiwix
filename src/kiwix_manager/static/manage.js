"use strict";

const API = "/_manage/api";
const PAGE_SIZE = 24;
const BROWSER_LANGUAGES = {
  en: "eng", fr: "fra", de: "deu", es: "spa", it: "ita", pt: "por", ru: "rus", zh: "zho", ja: "jpn",
  ar: "ara", hi: "hin", nl: "nld", pl: "pol", sv: "swe", tr: "tur", ko: "kor", fa: "fas", uk: "ukr",
};

let state = null;
let searchStart = 0;
let lastResults = null;
let pollTimer = null;

const $ = (id) => document.getElementById(id);

function formatBytes(bytes) {
  if (!bytes) return "unknown size";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

function formatCount(count) {
  return new Intl.NumberFormat().format(count);
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(API + path, init);
  if (response.status === 401) {
    window.location.reload();
    throw new Error("Your session has expired.");
  }
  if (!response.ok) {
    let detail = `Request failed (HTTP ${response.status})`;
    let code = "";
    try {
      const payload = await response.json();
      if (payload.detail) detail = payload.detail;
      if (payload.extra && payload.extra.code) code = payload.extra.code;
    } catch (_) { /* not JSON */ }
    const error = new Error(detail);
    error.code = code;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function showError(error) {
  const banner = $("banner");
  banner.textContent = error.message || String(error);
  banner.hidden = false;
}

function clearError() {
  $("banner").hidden = true;
}

function fill(template, fields) {
  const node = $(template).content.firstElementChild.cloneNode(true);
  for (const [name, value] of Object.entries(fields)) {
    const target = node.querySelector(`[data-field="${name}"]`);
    if (target) target.textContent = value;
  }
  return node;
}

function bookMeta(book) {
  const parts = [book.language, book.flavour, book.date, formatBytes(book.size_bytes)];
  if (book.article_count) parts.push(`${formatCount(book.article_count)} articles`);
  return parts.filter(Boolean).join(" · ");
}

function renderBooks() {
  const list = $("books");
  list.replaceChildren();
  $("library-empty").hidden = state.books.length > 0;
  for (const book of state.books) {
    const node = fill("book-template", { link: book.title, meta: bookMeta(book) });
    const link = node.querySelector('[data-field="link"]');
    link.href = `/content/${encodeURIComponent(book.content_name)}`;
    const button = node.querySelector('[data-action="delete"]');
    button.setAttribute("aria-label", `Delete ${book.title}`);
    button.addEventListener("click", async () => {
      if (!window.confirm(`Delete "${book.title}" (${formatBytes(book.size_bytes)}) from this instance?`)) return;
      button.disabled = true;
      try {
        await api(`/books/${encodeURIComponent(book.id)}`, { method: "DELETE" });
        clearError();
      } catch (error) {
        showError(error);
      }
      await refresh();
    });
    list.append(node);
  }
  const invalid = $("invalid-files");
  invalid.hidden = state.invalid_files.length === 0;
  invalid.textContent = `These files are not valid ZIM files and were skipped: ${state.invalid_files.join(", ")}`;
}

function downloadMeta(job) {
  const total = formatBytes(job.size_bytes);
  switch (job.status) {
    case "queued": return `Queued · ${total}`;
    case "downloading": return `${formatBytes(job.bytes_done)} of ${total}`;
    case "verifying": return `Verifying checksum · ${total}`;
    case "completed": return `Done · ${total}`;
    case "cancelled": return "Cancelled";
    case "failed": return `Failed: ${job.error}`;
    default: return job.status;
  }
}

function actionButton(label, className, handler, ariaLabel) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `small ${className}`;
  button.textContent = label;
  button.setAttribute("aria-label", ariaLabel);
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await handler();
      clearError();
    } catch (error) {
      showError(error);
    }
    await refresh();
  });
  return button;
}

function renderDownloads() {
  const list = $("downloads");
  list.replaceChildren();
  $("downloads-section").hidden = state.downloads.length === 0;
  for (const job of state.downloads) {
    const node = fill("download-template", { title: job.title, meta: downloadMeta(job) });
    const progress = node.querySelector('[data-field="progress"]');
    progress.value = job.size_bytes ? Math.floor((100 * job.bytes_done) / job.size_bytes) : 0;
    progress.setAttribute("aria-label", `${job.title} progress`);
    progress.hidden = !["downloading", "verifying", "queued", "failed"].includes(job.status);
    if (job.status === "failed") node.querySelector('[data-field="meta"]').classList.add("warning");
    const actions = node.querySelector('[data-field="actions"]');
    const id = encodeURIComponent(job.id);
    if (["queued", "downloading", "verifying"].includes(job.status)) {
      actions.append(actionButton("Cancel", "secondary", () => api(`/downloads/${id}/cancel`, { method: "POST" }),
        `Cancel download of ${job.title}`));
    }
    if (["failed", "cancelled"].includes(job.status)) {
      actions.append(actionButton(job.status === "failed" ? "Resume" : "Retry", "",
        () => api(`/downloads/${id}/retry`, { method: "POST" }), `Retry download of ${job.title}`));
    }
    if (["failed", "cancelled", "completed"].includes(job.status)) {
      actions.append(actionButton("Dismiss", "secondary", () => api(`/downloads/${id}`, { method: "DELETE" }),
        `Dismiss ${job.title}`));
    }
    list.append(node);
  }
}

function renderAccess() {
  $("public-toggle").checked = state.public;
  $("access-hint").textContent = state.public
    ? "Anyone who knows this address can read the library. Managing it still requires your login."
    : "Only you can read the library.";
  $("storage-line").textContent = `${formatBytes(state.free_bytes)} free on this instance's disk. Downloads stop if less than ${formatBytes(state.reserve_bytes)} would be left.`;
}

function render() {
  renderAccess();
  renderDownloads();
  renderBooks();
  if (lastResults) renderResults(lastResults);
}

async function refresh() {
  try {
    state = await api("/state");
    render();
  } catch (error) {
    showError(error);
  }
  schedulePoll();
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  const active = state && state.downloads.some((job) => ["queued", "downloading", "verifying"].includes(job.status));
  pollTimer = window.setTimeout(refresh, active ? 2000 : 15000);
}

function entryButtonLabel(view) {
  switch (view.status) {
    case "installed": return "Installed";
    case "downloading": return "Downloading";
    case "update_available": return `Update · ${formatBytes(view.entry.size_bytes)}`;
    default: return `Download · ${formatBytes(view.entry.size_bytes)}`;
  }
}

function liveStatus(view) {
  // The catalog page was fetched earlier; reflect downloads and installs that happened since.
  if (!state) return view.status;
  if (state.downloads.some((job) => job.book_id === view.entry.id && ["queued", "downloading", "verifying"].includes(job.status))) {
    return "downloading";
  }
  if (state.books.some((book) => book.id === view.entry.id)) return "installed";
  return view.status === "installed" || view.status === "downloading" ? "available" : view.status;
}

async function startDownload(entry) {
  try {
    await api("/downloads", { method: "POST", body: { book_id: entry.id } });
  } catch (error) {
    if (error.code !== "insufficient_space") throw error;
    const proceed = window.confirm(
      `${error.message}\n\nIf your archive storage is backed by S3 the local disk is not the limit and you can go ahead. `
      + "Otherwise the download will stop when the disk is nearly full.\n\nDownload anyway?",
    );
    if (!proceed) return;
    await api("/downloads", { method: "POST", body: { book_id: entry.id, ignore_space_check: true } });
  }
}

function renderResults(page) {
  const list = $("results");
  list.replaceChildren();
  for (const original of page.entries) {
    const view = { ...original, status: liveStatus(original) };
    const entry = view.entry;
    const meta = [entry.language, entry.flavour, entry.issued, `${formatCount(entry.article_count)} articles`]
      .filter(Boolean).join(" · ");
    const node = fill("result-template", { title: entry.title, summary: entry.summary, meta });
    const icon = node.querySelector('[data-field="icon"]');
    if (entry.has_illustration) {
      icon.src = `${API}/catalog/illustration/${encodeURIComponent(entry.id)}`;
      icon.loading = "lazy";
    } else {
      icon.remove();
    }
    const button = node.querySelector('[data-action="download"]');
    button.textContent = entryButtonLabel(view);
    button.disabled = view.status === "installed" || view.status === "downloading";
    button.setAttribute("aria-label", `${entryButtonLabel(view)}: ${entry.title}${entry.flavour ? ` (${entry.flavour})` : ""}`);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await startDownload(entry);
        clearError();
      } catch (error) {
        showError(error);
        button.disabled = false;
      }
      await refresh();
    });
    list.append(node);
  }
  const end = page.start + page.entries.length;
  $("catalog-status").textContent = page.total
    ? `Showing ${page.start + 1}–${end} of ${formatCount(page.total)}`
    : "No matching content.";
  $("prev-page").hidden = page.start === 0;
  $("next-page").hidden = end >= page.total;
}

async function search(start) {
  searchStart = Math.max(0, start);
  const params = new URLSearchParams({
    q: $("search-query").value.trim(),
    lang: $("search-language").value,
    category: $("search-category").value,
    start: String(searchStart),
    count: String(PAGE_SIZE),
  });
  $("catalog-status").textContent = "Searching…";
  try {
    lastResults = await api(`/catalog?${params}`);
    renderResults(lastResults);
  } catch (error) {
    $("catalog-status").textContent = "";
    showError(error);
  }
}

async function loadFacets() {
  const [languages, categories] = await Promise.allSettled([api("/catalog/languages"), api("/catalog/categories")]);
  const preferred = BROWSER_LANGUAGES[(navigator.language || "").slice(0, 2).toLowerCase()];
  if (languages.status === "fulfilled") {
    const select = $("search-language");
    const sorted = [...languages.value].sort((a, b) => a.label.localeCompare(b.label));
    for (const facet of sorted) {
      const option = new Option(`${facet.label} (${facet.count})`, facet.value);
      select.append(option);
    }
    if (preferred && sorted.some((facet) => facet.value === preferred)) select.value = preferred;
  }
  if (categories.status === "fulfilled") {
    const select = $("search-category");
    for (const facet of categories.value) select.append(new Option(facet.label, facet.value));
  }
  if (languages.status === "rejected") showError(languages.reason);
}

document.addEventListener("DOMContentLoaded", async () => {
  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    search(0);
  });
  $("prev-page").addEventListener("click", () => search(searchStart - PAGE_SIZE));
  $("next-page").addEventListener("click", () => search(searchStart + PAGE_SIZE));
  $("public-toggle").addEventListener("change", async (event) => {
    const toggle = event.target;
    toggle.disabled = true;
    try {
      state = await api("/settings", { method: "POST", body: { public: toggle.checked } });
      clearError();
      render();
    } catch (error) {
      toggle.checked = !toggle.checked;
      showError(error);
    } finally {
      toggle.disabled = false;
    }
  });
  $("rescan").addEventListener("click", async (event) => {
    const button = event.target;
    button.disabled = true;
    try {
      state = await api("/rescan", { method: "POST" });
      clearError();
      render();
    } catch (error) {
      showError(error);
    } finally {
      button.disabled = false;
    }
  });
  await refresh();
  await loadFacets();
  await search(0);
});
