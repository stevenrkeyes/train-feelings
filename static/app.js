const trainsEl = document.getElementById("trains");
const statusEl = document.getElementById("status");

const REFRESH_MS = 15000;
const HISTORY_LIMIT = 30;

const expandedTrains = new Set();
const focusTrainId = new URLSearchParams(window.location.search).get("train_id");
if (focusTrainId) {
  expandedTrains.add(focusTrainId);
}

function formatTime(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function formatLocationStatus(status) {
  if (!status) return null;
  return status.replaceAll("_", " ").toLowerCase();
}

function formatDelay(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const value = Number(seconds);
  if (Number.isNaN(value)) return null;
  const sign = value > 0 ? "+" : "";
  return `${sign}${value}s`;
}

function formatRate(rate) {
  if (rate === null || rate === undefined) return null;
  return `${Math.round(Number(rate) * 100)}%`;
}

function formatMinutes(value) {
  if (value === null || value === undefined) return null;
  return `${Number(value).toFixed(1)} min`;
}

function dwellSeconds(dwellSince) {
  if (!dwellSince) return null;
  const ms = Date.now() - new Date(dwellSince).getTime();
  if (Number.isNaN(ms) || ms < 0) return null;
  return `${Math.floor(ms / 1000)}s`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderField(label, value, kind) {
  if (value === null || value === undefined || value === "") return "";
  return `
    <div class="field field--${kind}">
      <span class="field__label">${escapeHtml(label)}</span>
      <span class="field__value">${escapeHtml(value)}</span>
    </div>`;
}

function renderHistoryItem(item) {
  const fields = [
    renderField("Predicted arrival", formatTime(item.arrival_time), "predicted"),
    renderField("Predicted departure", formatTime(item.departure_time), "predicted"),
    renderField("Scheduled track", item.scheduled_track ? `Track ${item.scheduled_track}` : null, "scheduled"),
    renderField("Actual track", item.actual_track ? `Track ${item.actual_track}` : null, "actual"),
  ].join("");

  return `
    <li class="history-item">
      <div class="history-item__stop">
        <div class="stop-name">${escapeHtml(item.stop_name || item.stop_id)}</div>
        <div class="stop-id">Stop ${escapeHtml(item.stop_id || "")}</div>
      </div>
      <div class="history-item__fields">${fields}</div>
    </li>`;
}

function renderTrainCard(train) {
  const upcoming = (train.upcoming_stops || train.arrivals || []).slice(0, HISTORY_LIMIT);
  const isOpen = expandedTrains.has(train.train_id);
  const openAttr = isOpen ? " open" : "";
  const summary = upcoming.length
    ? `${upcoming.length} upcoming stop${upcoming.length === 1 ? "" : "s"}`
    : "No upcoming stops";

  const historyHtml = upcoming.length
    ? upcoming.map(renderHistoryItem).join("")
    : '<li class="history-item history-item--empty">No upcoming stops in the current poll.</li>';

  const locationLabel = train.location_stop_id
    ? `${train.location_stop_id}${
        train.location_status ? ` (${formatLocationStatus(train.location_status)})` : ""
      }`
    : null;

  const stateFields = [
    renderField("Location", locationLabel, "actual"),
    renderField("Direction", train.direction, "actual"),
    renderField("Shape", train.shape_id, "meta"),
    renderField("Stop sequence", train.current_stop_sequence, "meta"),
    renderField("Arrival delay", formatDelay(train.trip_arrival_delay), "predicted"),
    renderField("Departure delay", formatDelay(train.trip_departure_delay), "predicted"),
    renderField("Next arrival", formatTime(train.next_stop_arrival_time), "predicted"),
    renderField("Next departure", formatTime(train.next_stop_departure_time), "predicted"),
    renderField("Last position update", formatTime(train.last_position_update), "actual"),
    renderField("Last stopped at", train.last_stopped_at, "actual"),
    renderField("Departed from", train.departed_from_stop_id, "actual"),
    renderField("Dwelling since", formatTime(train.dwell_since), "actual"),
    renderField("Dwell so far", dwellSeconds(train.dwell_since), "actual"),
    renderField("On-time today", formatRate(train.day_on_time_rate), "scheduled"),
    renderField("Punctuality samples", train.day_on_time_samples, "scheduled"),
    renderField("Tracking today", formatMinutes(train.day_tracking_minutes), "scheduled"),
    renderField("Consistent day", train.consistent_day ? "yes" : null, "scheduled"),
    renderField("Feed", train.feed_id, "meta"),
    renderField("Feed timestamp", formatTime(train.feed_timestamp), "meta"),
    renderField("Polled at", formatTime(train.collected_at), "meta"),
  ].join("");

  return `
    <article class="train-card" data-train-id="${escapeHtml(train.train_id)}">
      <div class="train-card__identity">
        <div class="train-card__top">
          <span class="route-badge" title="Route">${escapeHtml(train.route_id || "?")}</span>
          <div class="train-card__ids">
            <div class="id-row">
              <span class="id-label">Train ID</span>
              <span class="id-value">${escapeHtml(train.train_id)}</span>
            </div>
            <div class="id-row">
              <span class="id-label" title="GTFS identifier for this scheduled run">Running trip ID</span>
              <span class="id-value">${escapeHtml(train.trip_id || "—")}</span>
            </div>
          </div>
        </div>
        <div class="train-card__state">${stateFields}</div>
      </div>
      <details class="train-history"${openAttr} data-train-id="${escapeHtml(train.train_id)}">
        <summary class="train-history__summary">${escapeHtml(summary)}</summary>
        <ul class="history-list">${historyHtml}</ul>
      </details>
    </article>`;
}

function bindDetailsHandlers() {
  trainsEl.querySelectorAll("details.train-history").forEach((details) => {
    details.addEventListener("toggle", () => {
      const trainId = details.dataset.trainId;
      if (details.open) {
        expandedTrains.add(trainId);
      } else {
        expandedTrains.delete(trainId);
      }
    });
  });
}

function focusTrainCard(trainId) {
  if (!trainId) return;

  const card = trainsEl.querySelector(`.train-card[data-train-id="${CSS.escape(trainId)}"]`);
  if (!card) return;

  const details = card.querySelector("details.train-history");
  if (details) {
    details.open = true;
    expandedTrains.add(trainId);
  }

  card.classList.add("train-card--focused");
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderTrains(data) {
  const trains = data.trains || [];

  if (!trains.length) {
    trainsEl.innerHTML = `<p class="empty">No trains recorded yet. If you just started the collector, wait about 30 seconds.</p>`;
    return;
  }

  trainsEl.innerHTML = trains.map(renderTrainCard).join("");
  bindDetailsHandlers();
  if (focusTrainId) {
    focusTrainCard(focusTrainId);
  }
}

async function loadTrains() {
  try {
    const response = await fetch("/api/trains");
    if (!response.ok) {
      throw new Error(`API error (${response.status})`);
    }
    const data = await response.json();
    renderTrains(data);
    statusEl.textContent = `Updated ${new Date().toLocaleTimeString()} · ${data.trains.length} train(s)`;
    statusEl.classList.remove("error");
  } catch (error) {
    statusEl.textContent = `Could not load trains: ${error.message}`;
    statusEl.classList.add("error");
  }
}

loadTrains();
setInterval(loadTrains, REFRESH_MS);
