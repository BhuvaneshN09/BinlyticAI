/* Binlytic AI dashboard — client-side rendering only.
 * Talks to the existing dashboard_server.py API; the only backend addition
 * is an optional "destination" field on POST /api/history/clear, so this
 * keeps running on the same http://localhost:8000. */

const REFRESH_INTERVAL_MS = 3000;
const DANGER_CONFIRM_WINDOW_MS = 5000;
const HISTORY_ROWS_ON_HISTORY_PAGE = 500;
const ACTIVITY_WINDOW_HOURS = 24;
const MIN_SPLASH_VISIBLE_MS = 1400;

// Rough, clearly-labeled estimates used only in the impact tiles — not real
// sensor data. Values approximate typical mall waste items. "Days actively
// monitoring" below is a real, non-estimated figure derived from history.
const ESTIMATED_KG_DIVERTED_PER_ITEM = 0.18;
const ESTIMATED_CO2_KG_SAVED_PER_RECYCLED_ITEM = 0.5;

const DESTINATION_INFO = {
  RECYCLING: { colorVar: "--recycling", initial: "R" },
  COMPOST: { colorVar: "--compost", initial: "C" },
  GARBAGE: { colorVar: "--garbage", initial: "G" },
  "E-WASTE": { colorVar: "--ewaste", initial: "E" },
  HAZARDOUS: { colorVar: "--hazardous", initial: "H" },
  UNKNOWN: { colorVar: "--unknown", initial: "?" },
};
const DIVERTED_DESTINATIONS = new Set(["RECYCLING", "COMPOST", "E-WASTE"]);

// Used only until the browser reports a real position (or if permission is
// denied and nothing was cached yet) — matches the Toronto rule profile in
// wastevision_ai.py, not a claim about the bin's actual location.
const FALLBACK_MAP_COORDS = { lat: 43.6532, lng: -79.3832 };
const BIN_LOCATION_STORAGE_KEY = "binlytic-bin-location";

const qs = (selector) => document.querySelector(selector);
const qsa = (selector) => Array.from(document.querySelectorAll(selector));

let dashboardState = null;
let historySearchTerm = "";
let historyDestinationFilter = "";
let armedDangerButton = null;
let armedButtonResetTimer = null;
let lastSeenHistoryEventId = null;
let lastSeenUnknownEventId = null;
let hasRenderedOnce = false;
let hasHiddenSplash = false;
let leafletMap = null;
let binMarker = null;
let mapStatusText = "Locating…";

function destinationInfo(destination) {
  return DESTINATION_INFO[destination] || DESTINATION_INFO.UNKNOWN;
}

function formatText(value, fallback = "0") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function titleCase(value) {
  return formatText(value).replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatRelativeTime(isoTimestamp) {
  if (!isoTimestamp) return "No readings yet";
  const elapsedSeconds = Math.max(0, (Date.now() - new Date(isoTimestamp).getTime()) / 1000);
  if (elapsedSeconds < 10) return "Just now";
  if (elapsedSeconds < 60) return `${Math.floor(elapsedSeconds)}s ago`;
  if (elapsedSeconds < 3600) return `${Math.floor(elapsedSeconds / 60)}m ago`;
  if (elapsedSeconds < 86400) return `${Math.floor(elapsedSeconds / 3600)}h ago`;
  return `${Math.floor(elapsedSeconds / 86400)}d ago`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/* ===== Data fetch + top-level render ===================================== */

async function refreshDashboard() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    dashboardState = await response.json();
    renderDashboard();
  } catch (error) {
    console.warn("Dashboard server unavailable", error);
  }
}

function renderDashboard() {
  const bins = dashboardState.bins || [];
  const history = dashboardState.history || [];
  const unknowns = dashboardState.unknowns || [];
  const analytics = computeAnalytics(history);

  renderControllerStatus(dashboardState.controller);
  renderHero(analytics, bins, unknowns, dashboardState.controller);
  renderKpiRow(analytics, bins, unknowns);
  renderLatestDetection(bins);
  renderMapMeta(bins);
  updateBinMarkerPopup(bins);
  renderDonutChart(analytics.destinationCounts);
  renderActivityChart(analytics.hourlyBuckets);
  renderRankTable(analytics.topLabels);
  renderImpact(analytics, "#impact-grid");
  renderImpact(analytics, "#impact-grid-home");
  renderHistoryTable(history, bins);
  renderUnknownGallery(unknowns, dashboardState.learning_summary || {});

  if (hasRenderedOnce) notifyOnNewEvents(history, unknowns);
  lastSeenHistoryEventId = history[0]?.event_id ?? lastSeenHistoryEventId;
  lastSeenUnknownEventId = unknowns[0]?.event_id ?? lastSeenUnknownEventId;
  hasRenderedOnce = true;
}

/* ===== Latest detection (overview) ========================================= */

function renderLatestDetection(bins) {
  const container = qs("#last-detection-content");
  const detection = bins[0]?.last_ai_detection || null;

  if (!detection) {
    container.innerHTML = emptyStateMarkup(
      "Waiting for the first item",
      "Drop something in the bin and the camera's decision shows up here instantly.",
    );
    return;
  }

  const info = destinationInfo(detection.destination);
  const confidencePercent = Math.round((detection.confidence || 0) * 100);
  const isPending = detection.status === "waiting_for_sensor";
  container.innerHTML = `
    <div class="detect-row">
      <div class="detect-main">
        <div class="detect-label">${escapeHtml(titleCase(detection.label))}</div>
        <div class="detect-meta">${formatRelativeTime(detection.detected_at)} · ${isPending ? "awaiting sensor confirmation" : "confirmed"}</div>
        <div class="confidence-track"><div class="confidence-fill" style="width:${confidencePercent}%; background:var(${info.colorVar});"></div></div>
      </div>
      <span class="pill" style="color:var(${info.colorVar});">${detection.destination}</span>
    </div>`;
}

/* ===== Analytics (computed client-side from /api/state history) ========== */

function computeAnalytics(history) {
  const now = Date.now();
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);

  const destinationCounts = {};
  const labelCounts = {};
  const hourlyBuckets = Array.from({ length: ACTIVITY_WINDOW_HOURS }, (_unused, hourIndex) => ({
    hourIndex,
    count: 0,
  }));

  let totalToday = 0;
  let totalDiverted = 0;
  let earliestConfirmedAt = null;

  for (const event of history) {
    destinationCounts[event.destination] = (destinationCounts[event.destination] || 0) + 1;
    labelCounts[event.label] = (labelCounts[event.label] || 0) + 1;
    if (DIVERTED_DESTINATIONS.has(event.destination)) totalDiverted += 1;

    const confirmedAt = new Date(event.confirmed_at).getTime();
    if (confirmedAt >= startOfToday.getTime()) totalToday += 1;
    if (earliestConfirmedAt === null || confirmedAt < earliestConfirmedAt) earliestConfirmedAt = confirmedAt;

    const hoursAgo = Math.floor((now - confirmedAt) / 3_600_000);
    if (hoursAgo >= 0 && hoursAgo < ACTIVITY_WINDOW_HOURS) {
      hourlyBuckets[ACTIVITY_WINDOW_HOURS - 1 - hoursAgo].count += 1;
    }
  }

  const topLabels = Object.entries(labelCounts)
    .sort((left, right) => right[1] - left[1])
    .slice(0, 6);

  const daysActivelyMonitoring = earliestConfirmedAt
    ? Math.max(1, Math.ceil((now - earliestConfirmedAt) / 86_400_000))
    : 0;

  return {
    totalConfirmed: history.length,
    totalToday,
    diversionRate: history.length ? Math.round((totalDiverted / history.length) * 100) : 0,
    destinationCounts,
    hourlyBuckets,
    topLabels,
    daysActivelyMonitoring,
  };
}

/* ===== Controller / connection status ===================================== */

function renderControllerStatus(controller) {
  const online = Boolean(controller?.online);
  const port = controller?.port || "COM5";
  const pill = qs("#controller-pill");
  pill.className = `controller-pill ${online ? "online" : ""}`;
  pill.innerHTML = `
    <span class="controller-dot ${online ? "pulse" : ""}"></span>
    <div>
      <strong>${online ? "Online" : "Offline"}</strong>
      <small>${online ? `${port} detected` : `${port} not detected`}</small>
    </div>`;
}

/* ===== KPI row ============================================================= */

function renderHero(analytics, bins, unknowns, controller) {
  const learnedClasses = dashboardState.learning_summary?.learned_classes || 0;
  const online = Boolean(controller?.online);
  const port = controller?.port || "COM5";

  qs("#hero-bin-count").textContent = `${bins.length} bin${bins.length === 1 ? "" : "s"} found`;
  qs("#hero-confirmed-count").textContent = `${analytics.totalConfirmed} confirmed`;
  qs("#hero-unknown-count").textContent = `${unknowns.length} unknown`;
  qs("#hero-controller-state").textContent = online ? "Controller online" : "Controller offline";
  qs("#hero-controller-note").textContent = online ? `${port} detected` : `${port} not detected`;
  qs("#hero-today-count").textContent = analytics.totalToday;
  qs("#hero-diversion-rate").textContent = `${analytics.diversionRate}%`;
  qs("#hero-learned-count").textContent = learnedClasses;
}

function renderKpiRow(analytics, bins, unknowns) {
  const learnedClasses = dashboardState.learning_summary?.learned_classes || 0;
  const monitoredBins = bins.length;

  const kpiDefinitions = [
    { label: "Items sorted today", value: analytics.totalToday, trend: `${analytics.totalConfirmed} all-time` },
    { label: "Landfill diversion rate", value: `${analytics.diversionRate}%`, trend: "Recycling + compost + e-waste" },
    { label: "Bins monitored", value: monitoredBins, trend: unknowns.length ? `${unknowns.length} unknown captures` : "No unknown captures" },
    { label: "Learned visual classes", value: learnedClasses, trend: "From repeated unknown objects" },
  ];

  qs("#kpi-grid").innerHTML = kpiDefinitions.map((kpi) => `
    <div class="kpi-card">
      <span class="kpi-label">${kpi.label}</span>
      <span class="kpi-value">${kpi.value}</span>
      <span class="kpi-trend">${kpi.trend}</span>
    </div>
  `).join("");

  const badge = qs("#unknown-nav-badge");
  badge.textContent = unknowns.length || "";
  badge.dataset.count = String(unknowns.length);
}

/* ===== Map — real OpenStreetMap tiles via Leaflet, pin at device location == */

function renderMapMeta(bins) {
  qs("#map-subtitle").textContent = `${bins.length} bin${bins.length === 1 ? "" : "s"} monitored · ${mapStatusText}`;
}

function initBinMap() {
  if (leafletMap || typeof L === "undefined") return;
  leafletMap = L.map("map-canvas", { attributionControl: true, zoomControl: true })
    .setView([FALLBACK_MAP_COORDS.lat, FALLBACK_MAP_COORDS.lng], 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(leafletMap);
  locateBinOnMap();
}

function binMarkerIcon() {
  const binNumber = (dashboardState?.bins?.[0]?.id || "WV-01").slice(-2);
  return L.divIcon({
    className: "",
    html: `<span class="bin-pin-head">${escapeHtml(binNumber)}</span>`,
    iconSize: [26, 26],
    iconAnchor: [13, 13],
    popupAnchor: [0, -14],
  });
}

function placeBinMarker(coords) {
  if (!leafletMap) return;
  leafletMap.setView([coords.lat, coords.lng], 17);
  if (binMarker) binMarker.setLatLng([coords.lat, coords.lng]);
  else binMarker = L.marker([coords.lat, coords.lng], { icon: binMarkerIcon() }).addTo(leafletMap);
  updateBinMarkerPopup(dashboardState?.bins || []);
}

function updateBinMarkerPopup(bins) {
  if (!binMarker) return;
  const bin = (bins || [])[0];
  binMarker.bindPopup(
    bin
      ? `<strong>${escapeHtml(bin.name)}</strong><br><small>${escapeHtml(bin.location || "Live device location")}</small>`
      : "Binlytic bin",
  );
  binMarker.bindTooltip(bin ? escapeHtml(bin.name) : "Binlytic bin", { direction: "top", offset: [0, -12] });
}

function locateBinOnMap() {
  mapStatusText = "Locating…";
  renderMapMeta(dashboardState?.bins || []);

  if (!navigator.geolocation) {
    applyStoredOrFallbackLocation();
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (position) => {
      const coords = { lat: position.coords.latitude, lng: position.coords.longitude };
      localStorage.setItem(BIN_LOCATION_STORAGE_KEY, JSON.stringify(coords));
      placeBinMarker(coords);
      mapStatusText = "Centered on this device's location";
      renderMapMeta(dashboardState?.bins || []);
    },
    () => applyStoredOrFallbackLocation(),
    { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 },
  );
}

function applyStoredOrFallbackLocation() {
  const stored = localStorage.getItem(BIN_LOCATION_STORAGE_KEY);
  const coords = stored ? JSON.parse(stored) : FALLBACK_MAP_COORDS;
  placeBinMarker(coords);
  mapStatusText = stored ? "Using last known device location" : "Location permission denied; showing a default position";
  renderMapMeta(dashboardState?.bins || []);
}

/* ===== Environmental impact (Analytics page) ================================ */

function renderImpact(analytics, targetSelector = "#impact-grid") {
  if (!analytics.totalConfirmed) {
    qs(targetSelector).innerHTML = emptyStateMarkup(
      "No impact to report yet",
      "Diversion weight and CO₂ savings appear after the first confirmed item.",
    );
    return;
  }
  const divertedItemCount = Object.entries(analytics.destinationCounts)
    .filter(([destination]) => DIVERTED_DESTINATIONS.has(destination))
    .reduce((sum, [, count]) => sum + count, 0);
  const recycledItemCount = analytics.destinationCounts.RECYCLING || 0;

  const estimatedKgDiverted = (divertedItemCount * ESTIMATED_KG_DIVERTED_PER_ITEM).toFixed(1);
  const estimatedCo2Saved = (recycledItemCount * ESTIMATED_CO2_KG_SAVED_PER_RECYCLED_ITEM).toFixed(1);

  const impactTiles = [
    { value: `${estimatedKgDiverted} kg`, label: "Estimated weight diverted from landfill" },
    { value: `${estimatedCo2Saved} kg`, label: "Estimated CO₂e saved" },
    { value: divertedItemCount, label: "Items diverted from landfill" },
    { value: analytics.daysActivelyMonitoring, label: "Days actively monitoring (actual)" },
  ];

  qs(targetSelector).innerHTML = impactTiles.map((tile) => `
    <div class="impact-tile">
      <div class="impact-value">${tile.value}</div>
      <div class="impact-label">${tile.label}</div>
    </div>
  `).join("");
}

/* ===== Analytics view: donut, bar chart, rank table ========================= */

function renderDonutChart(destinationCounts) {
  const entries = Object.entries(destinationCounts);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  const wrap = qs("#donut-wrap");

  if (!total) {
    wrap.innerHTML = emptyStateMarkup("No confirmed items yet", "The breakdown fills in once items are confirmed.");
    return;
  }

  let cumulativePercent = 0;
  const gradientStops = entries.map(([destination, count]) => {
    const info = destinationInfo(destination);
    const sliceStart = cumulativePercent;
    cumulativePercent += (count / total) * 100;
    return `var(${info.colorVar}) ${sliceStart}% ${cumulativePercent}%`;
  }).join(", ");

  wrap.innerHTML = `
    <div class="donut" style="background: conic-gradient(${gradientStops});">
      <div class="donut-hole"><strong>${total}</strong><span>items</span></div>
    </div>
    <div class="legend">
      ${entries.sort((left, right) => right[1] - left[1]).map(([destination, count]) => {
        const info = destinationInfo(destination);
        return `<div class="legend-row">
          <span class="legend-swatch" style="background:var(${info.colorVar});"></span>
          <span class="legend-name">${destination.toLowerCase()}</span>
          <span class="legend-value">${count}</span>
        </div>`;
      }).join("")}
    </div>
  `;
}

function renderActivityChart(hourlyBuckets) {
  const maxCount = Math.max(1, ...hourlyBuckets.map((bucket) => bucket.count));
  qs("#activity-chart").innerHTML = hourlyBuckets.map((bucket, index) => {
    const heightPercent = Math.round((bucket.count / maxCount) * 100);
    const hoursAgo = ACTIVITY_WINDOW_HOURS - 1 - index;
    const showTick = hoursAgo % 6 === 0;
    return `<div class="bar-chart-col">
      <div class="bar-chart-bar" style="height:${bucket.count ? Math.max(heightPercent, 4) : 0}%" title="${bucket.count} item(s), ${hoursAgo}h ago"></div>
      <span class="bar-chart-tick">${showTick ? `${hoursAgo}h` : ""}</span>
    </div>`;
  }).join("");
}

function renderRankTable(topLabels) {
  const tbody = qs("#rank-table tbody");
  if (!topLabels.length) {
    tbody.innerHTML = `<tr><td>No confirmed items yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = topLabels.map(([label, count], index) => `
    <tr>
      <td class="rank-index">${index + 1}</td>
      <td>${escapeHtml(titleCase(label))}</td>
      <td class="rank-count">${count}</td>
    </tr>
  `).join("");
}

/* ===== History table ======================================================== */

function renderHistoryTable(history, bins) {
  const filtered = history
    .filter((event) => !historyDestinationFilter || event.destination === historyDestinationFilter)
    .filter((event) => !historySearchTerm || event.label.toLowerCase().includes(historySearchTerm))
    .slice(0, HISTORY_ROWS_ON_HISTORY_PAGE);

  qs("#history-count").textContent = `${history.length} item${history.length === 1 ? "" : "s"}`;

  const container = qs("#history-content");
  if (!filtered.length) {
    container.innerHTML = emptyStateMarkup(
      history.length ? "No items match this filter" : "No confirmed items yet",
      history.length
        ? "Try clearing the search or destination filter."
        : "An item appears here once the AI classification is confirmed.",
    );
    return;
  }

  container.innerHTML = `
    <table class="data-table">
      <thead><tr><th>Item</th><th>Destination</th><th>Confirmation</th><th>Bin</th><th>Confirmed</th></tr></thead>
      <tbody>${filtered.map((event) => {
        const bin = bins.find((candidate) => candidate.id === event.bin_id);
        const info = destinationInfo(event.destination);
        const method = event.confirmation_method === "timer" ? "Timer" : "Ultrasonic";
        return `<tr>
          <td>${escapeHtml(titleCase(event.label))}</td>
          <td><span class="pill" style="color:var(${info.colorVar});">${event.destination}</span></td>
          <td>${method}</td>
          <td>${escapeHtml(bin?.name || event.bin_id)}</td>
          <td>${new Date(event.confirmed_at).toLocaleString()}</td>
        </tr>`;
      }).join("")}</tbody>
    </table>`;
}

function exportHistoryToCsv() {
  const history = dashboardState?.history || [];
  const columns = ["event_id", "label", "destination", "confidence", "confirmation_method", "confirmed_at", "bin_id"];
  const rows = [columns.join(",")].concat(
    history.map((event) => columns.map((column) => `"${String(event[column] ?? "").replaceAll('"', '""')}"`).join(",")),
  );
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const downloadUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = downloadUrl;
  link.download = `binlytic-history-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(downloadUrl);
}

/* ===== Unknown object gallery ================================================ */

function renderUnknownGallery(unknowns, learningSummary) {
  const learnedClasses = learningSummary.learned_classes || 0;
  qs("#unknown-count").textContent = `${unknowns.length} capture${unknowns.length === 1 ? "" : "s"} · ${learnedClasses} learned classes`;

  const container = qs("#unknown-content");
  if (!unknowns.length) {
    container.innerHTML = emptyStateMarkup("No unknown objects captured", "Low-confidence detections are photographed here for review.");
    return;
  }

  container.innerHTML = `<div class="unknown-grid">${unknowns.map((event) => unknownCardMarkup(event)).join("")}</div>`;
  qsa(".unknown-card").forEach((card, index) => {
    card.addEventListener("click", () => openUnknownModal(unknowns[index]));
  });
}

function unknownCardMarkup(event) {
  const statusClass = event.learning_status === "auto-learned" ? "learned"
    : event.learning_status === "collecting" ? "collecting" : "waiting";
  const guesses = (event.top_guesses || []).slice(0, 3).map((guess, index) => `
    <li><span>${index + 1}. ${escapeHtml(titleCase(guess.label))}</span><strong>${Number(guess.score || 0).toFixed(3)}</strong></li>
  `).join("");

  return `<article class="unknown-card">
    <img src="${escapeHtml(event.image_url)}" alt="Unknown object captured by Binlytic" loading="lazy">
    <div class="unknown-card-body">
      <small>${new Date(event.captured_at).toLocaleString()}</small>
      <p>${escapeHtml(event.description || "No description available")}</p>
      <ul class="guess-list">${guesses}</ul>
      <span class="learning-chip ${statusClass}">${escapeHtml(event.learning_message || "Waiting for evidence")}</span>
    </div>
  </article>`;
}

function openUnknownModal(event) {
  qs("#modal-box").innerHTML = `
    <img src="${escapeHtml(event.image_url)}" alt="Unknown object captured by Binlytic">
    <div class="modal-box-body">
      <h2 style="margin-bottom:8px;">Unknown object</h2>
      <p style="color:var(--text-dim); font-size:12px; line-height:1.5;">${escapeHtml(event.description || "No description available")}</p>
      <ul class="guess-list">${(event.top_guesses || []).map((guess, index) => `
        <li><span>${index + 1}. ${escapeHtml(titleCase(guess.label))} (${guess.bin})</span><strong>${Number(guess.score || 0).toFixed(3)}</strong></li>
      `).join("")}</ul>
    </div>`;
  qs("#modal-overlay").classList.add("open");
}

function closeModal() {
  qs("#modal-overlay").classList.remove("open");
}

/* ===== Shared empty state markup ============================================ */

function emptyStateMarkup(title, description) {
  return `<div class="empty-state">
    <span class="empty-state-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg></span>
    <strong>${escapeHtml(title)}</strong>
    <p>${escapeHtml(description)}</p>
  </div>`;
}

/* ===== Toast notifications for new events =================================== */

function notifyOnNewEvents(history, unknowns) {
  const newestConfirmed = history[0];
  if (newestConfirmed && newestConfirmed.event_id !== lastSeenHistoryEventId) {
    const info = destinationInfo(newestConfirmed.destination);
    showToast(`${titleCase(newestConfirmed.label)} confirmed → ${newestConfirmed.destination}`, info.colorVar);
  }

  const newestUnknown = unknowns[0];
  if (newestUnknown && newestUnknown.event_id !== lastSeenUnknownEventId) {
    showToast("New unknown object captured", "--unknown");
  }
}

function showToast(message, colorVar) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerHTML = `<span class="toast-dot" style="background:var(${colorVar});"></span><span>${escapeHtml(message)}</span>`;
  qs("#toast-stack").appendChild(toast);
  setTimeout(() => toast.remove(), 4500);
}

/* ===== Danger / clear actions with confirm-arm =============================== */

async function runClearAction(button, endpoint, body) {
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Clearing...";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) {
      const message = await response.text();
      throw new Error(message || `HTTP ${response.status}`);
    }
    await refreshDashboard();
    showToast("Cleared", "--compost");
  } catch (error) {
    console.error(`Clear failed for ${endpoint}`, error);
    showToast("Clear failed", "--danger");
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function disarmDangerButton() {
  clearTimeout(armedButtonResetTimer);
  if (armedDangerButton) {
    armedDangerButton.textContent = armedDangerButton.dataset.defaultLabel;
    armedDangerButton.classList.remove("armed");
  }
  armedDangerButton = null;
}

/* ===== Navigation ============================================================ */

const VIEW_TITLES = {
  overview: ["Overview", "Live status for the mall's Binlytic bin network"],
  analytics: ["Analytics", "Waste stream trends and estimated environmental impact"],
  history: ["Confirmed items", "Every item confirmed by AI classification plus sensor detection"],
  unknowns: ["Unknown objects", "Low-confidence captures awaiting review or auto-learning"],
};

function switchView(viewName) {
  qsa(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${viewName}`));
  qsa(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.view === viewName));
  const [title, subtitle] = VIEW_TITLES[viewName];
  qs("#page-title").textContent = title;
  qs("#page-subtitle").textContent = subtitle;
}

function initClock() {
  const update = () => { qs("#live-clock").textContent = new Date().toLocaleTimeString(); };
  update();
  setInterval(update, 1000);
}

/* ===== Splash screen: shows the mission + headline stat, then fades ========= */

function populateSplash(analytics) {
  const estimatedKgDiverted = (analytics.totalConfirmed * ESTIMATED_KG_DIVERTED_PER_ITEM).toFixed(1);
  const estimatedCo2Saved = (analytics.destinationCounts.RECYCLING || 0) * ESTIMATED_CO2_KG_SAVED_PER_RECYCLED_ITEM;

  qs("#splash-weight-value").textContent = analytics.totalConfirmed ? `${estimatedKgDiverted} kg` : "0.0 kg";
  qs("#splash-co2-value").textContent = analytics.totalConfirmed ? `${estimatedCo2Saved.toFixed(1)} kg` : "0.0 kg";
  qs("#splash-secondary").textContent = analytics.totalConfirmed
    ? `${analytics.totalConfirmed} items confirmed · ${analytics.daysActivelyMonitoring} day${analytics.daysActivelyMonitoring === 1 ? "" : "s"} monitoring`
    : "Waiting for the first confirmed item";
}

async function initSplash() {
  const startedAt = performance.now();
  await refreshDashboard();
  populateSplash(computeAnalytics(dashboardState?.history || []));

  const elapsed = performance.now() - startedAt;
  await new Promise((resolve) => setTimeout(resolve, Math.max(0, MIN_SPLASH_VISIBLE_MS - elapsed)));
  qs("#splash-screen").classList.add("splash-hidden");
  hasHiddenSplash = true;
  if (leafletMap) leafletMap.invalidateSize();
}

/* ===== Wire up event listeners and start polling ============================= */

qsa(".nav-link").forEach((link) => link.addEventListener("click", () => switchView(link.dataset.view)));
qs("#refresh-button").addEventListener("click", refreshDashboard);
qs("#export-csv-button").addEventListener("click", exportHistoryToCsv);
qs("#modal-close").addEventListener("click", closeModal);
qs("#modal-overlay").addEventListener("click", (event) => { if (event.target.id === "modal-overlay") closeModal(); });

qs("#history-search").addEventListener("input", (event) => {
  historySearchTerm = event.target.value.trim().toLowerCase();
  if (dashboardState) renderHistoryTable(dashboardState.history || [], dashboardState.bins || []);
});
qs("#history-filter").addEventListener("change", (event) => {
  historyDestinationFilter = event.target.value;
  if (dashboardState) renderHistoryTable(dashboardState.history || [], dashboardState.bins || []);
});

qs("#clear-everything-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Clearing...";
  try {
    const historyResponse = await fetch("/api/history/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!historyResponse.ok) {
      const message = await historyResponse.text();
      throw new Error(message || `HTTP ${historyResponse.status}`);
    }

    const learningResponse = await fetch("/api/learning/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!learningResponse.ok) {
      const message = await learningResponse.text();
      throw new Error(message || `HTTP ${learningResponse.status}`);
    }

    await refreshDashboard();
    showToast("Everything cleared", "--compost");
  } catch (error) {
    console.error("Clear everything failed", error);
    showToast("Clear failed", "--danger");
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
});

qs("#clear-training-button").addEventListener("click", (event) => {
  runClearAction(event.currentTarget, "/api/learning/clear");
});

qsa(".bin-clear-action").forEach((button) => {
  button.addEventListener("click", (event) => {
    runClearAction(event.currentTarget, "/api/history/clear", { destination: button.dataset.destination });
  });
});

qs("#recenter-map-button").addEventListener("click", locateBinOnMap);

const sessionUser = localStorage.getItem("binlytic-session");
const connectedBinId = localStorage.getItem("binlytic-connected-bin");
if (sessionUser) qs("#session-user-label").textContent = connectedBinId ? `${sessionUser} · ${connectedBinId}` : sessionUser;
qs("#sign-out-button").addEventListener("click", () => {
  localStorage.removeItem("binlytic-session");
  window.location.href = "/login.html";
});

initClock();
initBinMap();
initSplash();
setInterval(refreshDashboard, REFRESH_INTERVAL_MS);
