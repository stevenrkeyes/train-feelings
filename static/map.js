const statusEl = document.getElementById("status");
const REFRESH_MS = 15000;

function setStatus(message, isError = false) {
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.classList.toggle("error", isError);
}

const NYC_CENTER = [40.72, -73.96];
const NYC_ZOOM = 13;
const BASE_ICON_SIZE = 42;
const BASE_FONT_REM = 2.03;
// Map tiles scale 2x per zoom level; icons grow 1.5x per level (~50% of map scaling).
const ZOOM_SIZE_GROWTH = 1.5;

const map = L.map("map", {
  zoomControl: true,
}).setView(NYC_CENTER, NYC_ZOOM);

L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  subdomains: "abcd",
  maxZoom: 20,
}).addTo(map);

let shapesLayer = null;

async function loadShapes() {
  const response = await fetch("/static/subway-shapes.geojson");
  if (!response.ok) {
    throw new Error(`Shapes error (${response.status})`);
  }
  const geojson = await response.json();

  if (shapesLayer) {
    map.removeLayer(shapesLayer);
  }

  shapesLayer = L.geoJSON(geojson, {
    style(feature) {
      return {
        color: feature.properties.color,
        weight: 4,
        opacity: 0.9,
        lineCap: "round",
        lineJoin: "round",
      };
    },
  }).addTo(map);
}

const markersByTrainId = new Map();
const trainStateById = new Map();
const lastEmojiByTrainId = new Map();
const emojiNoticeByTrainId = new Map();
let animationFrameId = null;
let lastTrainCount = 0;

const EMOJI_NOTICE_MS = 30000;

function zoomIconScale() {
  return ZOOM_SIZE_GROWTH ** (map.getZoom() - NYC_ZOOM);
}

function trainEmoji(train) {
  if (train.saw_old_friend) {
    return "😀";
  }
  if (train.snoozing_at_station) {
    return "😴";
  }
  if (train.is_late) {
    if (train.train_in_front_also_late) {
      return "😡";
    }
    return "😞";
  }
  if (train.is_early) {
    return "😏";
  }
  if (train.consistent_day) {
    return "🤓";
  }
  return "🚆";
}

function trainIconLabel(train) {
  if (train.saw_old_friend) {
    return "saw an old friend";
  }
  if (train.snoozing_at_station) {
    return "snoozing at the station";
  }
  if (train.is_late) {
    if (train.train_in_front_also_late) {
      return "delayed by train in front";
    }
    return "late train";
  }
  if (train.is_early) {
    return "early train";
  }
  if (train.consistent_day) {
    return "consistently on-time train";
  }
  return "train";
}

function trainIcon(train) {
  const emoji = trainEmoji(train);
  const label = trainIconLabel(train);
  const scale = zoomIconScale();
  const size = Math.round(BASE_ICON_SIZE * scale);
  const fontSize = BASE_FONT_REM * scale;
  return L.divIcon({
    className: "train-marker",
    html: `<span class="train-marker__emoji" style="font-size:${fontSize}rem" role="img" aria-label="${label}">${emoji}</span>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

function updateMarkerIcons() {
  recomputeStationJogOffsets();
  for (const [trainId, marker] of markersByTrainId) {
    const train = trainStateById.get(trainId);
    if (train) {
      marker.setIcon(trainIcon(train));
      marker.setLatLng(markerDisplayLatLng(train));
    }
  }
}

map.on("zoom", updateMarkerIcons);

function lerp(start, end, progress) {
  return start + (end - start) * progress;
}

function interpolationProgress(train, nowMs = Date.now()) {
  if (train.position_mode !== "interpolated" || !train.depart_at || !train.arrive_at) {
    return 0;
  }

  const departMs = Date.parse(train.depart_at);
  const arriveMs = Date.parse(train.arrive_at);
  if (Number.isNaN(departMs) || Number.isNaN(arriveMs) || arriveMs <= departMs) {
    return 0.5;
  }

  const elapsed = nowMs - departMs;
  const duration = arriveMs - departMs;
  return Math.max(0, Math.min(1, elapsed / duration));
}

function markerLatLng(train, nowMs = Date.now()) {
  if (train.position_mode === "interpolated") {
    const progress = interpolationProgress(train, nowMs);
    return [
      lerp(train.from_lat, train.to_lat, progress),
      lerp(train.from_lon, train.to_lon, progress),
    ];
  }
  return [train.to_lat, train.to_lon];
}

function isAtStation(train) {
  const status = train.location_status;
  return status === "STOPPED_AT" || status === "INCOMING_AT";
}

function iconPixelSize() {
  return Math.round(BASE_ICON_SIZE * zoomIconScale());
}

function applyPixelOffset(latLng, offsetPx) {
  if (!offsetPx) {
    return latLng;
  }
  const point = map.latLngToContainerPoint(latLng);
  return map.containerPointToLatLng([point.x + offsetPx.x, point.y + offsetPx.y]);
}

function stationGroupKey(train) {
  const stopId = train.location_stop_id;
  if (stopId) {
    // MTA platform stops share a parent station id without the N/S suffix (e.g. 701N + 701S → 701).
    return stopId.replace(/[NS]$/, "");
  }
  return train.stop_name || null;
}

let stationJogOffsetsByTrainId = new Map();

function recomputeStationJogOffsets() {
  const byStation = new Map();

  for (const train of trainStateById.values()) {
    if (!isAtStation(train)) {
      continue;
    }
    const stationKey = stationGroupKey(train);
    if (!stationKey) {
      continue;
    }
    if (!byStation.has(stationKey)) {
      byStation.set(stationKey, []);
    }
    byStation.get(stationKey).push(train.train_id);
  }

  const offsets = new Map();
  const step = iconPixelSize() * 0.12;

  for (const trainIds of byStation.values()) {
    if (trainIds.length < 2) {
      continue;
    }
    trainIds.sort();
    for (let index = 0; index < trainIds.length; index += 1) {
      offsets.set(trainIds[index], { x: index * step, y: index * step });
    }
  }

  stationJogOffsetsByTrainId = offsets;
}

function markerDisplayLatLng(train, nowMs = Date.now()) {
  const base = markerLatLng(train, nowMs);
  const offset = stationJogOffsetsByTrainId.get(train.train_id);
  return applyPixelOffset(base, offset);
}

function emojiStatusHtml(train) {
  const delays = [train.trip_arrival_delay, train.trip_departure_delay].filter((value) => value != null);
  const maxDelay = delays.length ? Math.max(...delays) : null;
  const minDelay = delays.length ? Math.min(...delays) : null;
  const line = scheduleStatusLine(train, maxDelay, minDelay);
  return line || null;
}

function hideEmojiNotice(trainId) {
  const notice = emojiNoticeByTrainId.get(trainId);
  if (!notice) return;
  clearTimeout(notice.timeoutId);
  const marker = markersByTrainId.get(trainId);
  if (marker?.getTooltip()) {
    marker.unbindTooltip();
  }
  emojiNoticeByTrainId.delete(trainId);
}

function showEmojiNotice(trainId, train) {
  const statusHtml = emojiStatusHtml(train);
  if (!statusHtml) {
    hideEmojiNotice(trainId);
    return;
  }

  const marker = markersByTrainId.get(trainId);
  if (!marker) return;

  let notice = emojiNoticeByTrainId.get(trainId);
  if (!notice) {
    notice = { timeoutId: null };
    emojiNoticeByTrainId.set(trainId, notice);
  } else {
    clearTimeout(notice.timeoutId);
  }

  const content = `<div class="train-popup">${statusHtml}</div>`;
  if (marker.getTooltip()) {
    marker.setTooltipContent(content);
  } else {
    marker
      .bindTooltip(content, {
        permanent: true,
        direction: "top",
        className: "train-emoji-notice",
        offset: [0, -8],
        interactive: false,
      })
      .openTooltip();
  }

  notice.timeoutId = setTimeout(() => hideEmojiNotice(trainId), EMOJI_NOTICE_MS);
}

function syncMarkers(trains) {
  const seen = new Set();

  for (const train of trains) {
    seen.add(train.train_id);
    trainStateById.set(train.train_id, train);
  }

  for (const [trainId, marker] of markersByTrainId) {
    if (!seen.has(trainId)) {
      hideEmojiNotice(trainId);
      map.removeLayer(marker);
      markersByTrainId.delete(trainId);
      trainStateById.delete(trainId);
      lastEmojiByTrainId.delete(trainId);
    }
  }

  recomputeStationJogOffsets();

  for (const train of trains) {
    const latLng = markerDisplayLatLng(train);
    let marker = markersByTrainId.get(train.train_id);

    if (!marker) {
      marker = L.marker(latLng, { icon: trainIcon(train) });
      marker.addTo(map);
      markersByTrainId.set(train.train_id, marker);
    } else {
      marker.setLatLng(latLng);
      marker.setIcon(trainIcon(train));
    }

    const newEmoji = trainEmoji(train);
    const prevEmoji = lastEmojiByTrainId.get(train.train_id);
    if (prevEmoji !== undefined && prevEmoji !== newEmoji) {
      showEmojiNotice(train.train_id, train);
    }
    lastEmojiByTrainId.set(train.train_id, newEmoji);

    marker.bindPopup(
      () => trainPopupHtml(trainStateById.get(train.train_id) || train),
      { closeButton: true, autoClose: true, closeOnClick: true }
    );
  }

  lastTrainCount = trains.length;
}

function animateMarkers() {
  if (document.hidden) {
    animationFrameId = requestAnimationFrame(animateMarkers);
    return;
  }

  for (const [trainId, marker] of markersByTrainId) {
    const train = trainStateById.get(trainId);
    if (!train) continue;
    marker.setLatLng(markerDisplayLatLng(train));
  }

  animationFrameId = requestAnimationFrame(animateMarkers);
}

function startAnimationLoop() {
  if (animationFrameId !== null) return;
  animationFrameId = requestAnimationFrame(animateMarkers);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function trainPageUrl(trainId) {
  return `/trains?train_id=${encodeURIComponent(trainId)}`;
}

function formatTime(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function formatTrackingMinutes(minutes) {
  if (minutes == null || Number.isNaN(minutes)) {
    return "0 min";
  }
  if (minutes < 60) {
    return `${Math.round(minutes)} min`;
  }
  const hours = Math.floor(minutes / 60);
  const remainder = Math.round(minutes % 60);
  if (remainder === 0) {
    return `${hours}h`;
  }
  return `${hours}h ${remainder}m`;
}

function stationStatusLine(train) {
  const name = train.stop_name || train.location_stop_id || "Unknown";
  const status = train.location_status;

  if (status === "STOPPED_AT") {
    return `At ${name}`;
  }
  if (status === "INCOMING_AT") {
    return `Arriving at ${name}`;
  }
  if (status === "IN_TRANSIT_TO") {
    return `Going to ${name}`;
  }
  return `At ${name}`;
}

function trainPopupHtml(train) {
  const delays = [train.trip_arrival_delay, train.trip_departure_delay].filter((value) => value != null);
  const maxDelay = delays.length ? Math.max(...delays) : null;
  const minDelay = delays.length ? Math.min(...delays) : null;

  const stationLine = `<div class="train-popup__station">${escapeHtml(stationStatusLine(train))}</div>`;

  let departureLine = "";
  if (train.location_status === "STOPPED_AT" && train.next_stop_departure_time) {
    const departTime = formatTime(train.next_stop_departure_time);
    if (departTime) {
      departureLine = `<div class="train-popup__departure">Expected departure: ${escapeHtml(departTime)}</div>`;
    }
  }

  const modeLine =
    train.position_mode === "interpolated" && train.departed_from_stop_name
      ? `<div class="train-popup__mode">From ${escapeHtml(train.departed_from_stop_name)}</div>`
      : "";

  const title = train.train_label || "Special Train";
  const scheduleLine = scheduleStatusLine(train, maxDelay, minDelay);

  return `<div class="train-popup"><div class="train-popup__title"><a class="train-popup__link" href="${trainPageUrl(train.train_id)}">${escapeHtml(title)}</a></div>${stationLine}${departureLine}${modeLine}${scheduleLine}</div>`;
}

function scheduleStatusLine(train, maxDelay, minDelay) {
  if (train.saw_old_friend && train.old_friend_train_label) {
    return `<div class="train-popup__old-friend">Saw an old friend (${escapeHtml(train.old_friend_train_label)})</div>`;
  }
  if (train.snoozing_at_station && train.station_dwell_minutes != null) {
    return `<div class="train-popup__snoozing">Snoozing (${train.station_dwell_minutes} min)</div>`;
  }
  if (train.is_late) {
    if (train.train_in_front_also_late && train.train_in_front_id) {
      return `<div class="train-popup__train-in-front">Delayed by preceding train (<a class="train-popup__link" href="${trainPageUrl(train.train_in_front_id)}">${escapeHtml(train.train_in_front_id)}</a>)</div>`;
    }
    if (maxDelay != null) {
      return `<div class="train-popup__late">${maxDelay}s behind schedule</div>`;
    }
    return "";
  }
  if (train.is_early && minDelay != null) {
    return `<div class="train-popup__early">${Math.abs(minDelay)}s ahead of schedule</div>`;
  }
  if (train.consistent_day && train.day_on_time_rate != null) {
    return `<div class="train-popup__punctual">Punctual all day</div>`;
  }
  return "";
}

async function loadTrains() {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 45000);

  try {
    const response = await fetch("/api/map/trains", { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`API error (${response.status})`);
    }
    const data = await response.json();
    syncMarkers(data.trains || []);
    setStatus(`Updated ${new Date().toLocaleTimeString()} · ${lastTrainCount} train(s) on map`);
  } catch (error) {
    const message =
      error.name === "AbortError"
        ? "Request timed out — server may be busy"
        : error.message;
    console.error("loadTrains failed:", error);
    setStatus(`Could not load trains: ${message}`, true);
  } finally {
    clearTimeout(timeoutId);
  }
}

loadShapes().catch((error) => {
  setStatus(`Could not load subway lines: ${error.message}`, true);
});

startAnimationLoop();
loadTrains();
setInterval(loadTrains, REFRESH_MS);
