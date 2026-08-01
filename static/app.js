const trainsEl = document.getElementById("trains");
const statusEl = document.getElementById("status");
const windowMinutesEl = document.getElementById("window-minutes");

const REFRESH_MS = 15000;
const HISTORY_LIMIT = 30;

const expandedTrains = new Set();
const focusTrainId = new URLSearchParams(window.location.search).get("train_id");
if (focusTrainId) {
  expandedTrains.add(focusTrainId);
}

function formatTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function formatLocationStatus(status) {
  if (!status) return "";
  return status.replaceAll("_", " ").toLowerCase();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderField(label, value, kind) {
  if (!value) return "";
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
    renderField(
      "Train location",
      item.location_stop_id
        ? `${item.location_stop_id} (${formatLocationStatus(item.location_status)})`
        : null,
      "actual"
    ),
    renderField("Recorded at", formatTime(item.collected_at), "meta"),
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
  const history = (train.arrivals || []).slice(0, HISTORY_LIMIT);
  const isOpen = expandedTrains.has(train.train_id);
  const openAttr = isOpen ? " open" : "";
  const summary = history.length
    ? `${history.length} past event${history.length === 1 ? "" : "s"} (last ${windowMinutesEl.textContent} min)`
    : "No past events in window";

  const historyHtml = history.length
    ? history.map(renderHistoryItem).join("")
    : '<li class="history-item history-item--empty">No stop updates recorded yet.</li>';

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
  windowMinutesEl.textContent = data.window_minutes;
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
