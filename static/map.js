const statusEl = document.getElementById("status");
const REFRESH_MS = 15000;

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
let animationFrameId = null;
let lastTrainCount = 0;

function zoomIconScale() {
  return ZOOM_SIZE_GROWTH ** (map.getZoom() - NYC_ZOOM);
}

function trainEmoji(train) {
  if (train.is_late) {
    if (train.train_in_front_also_late) {
      return "😡";
    }
    return "😞";
  }
  if (train.is_early) {
    return "😏";
  }
  return "🚆";
}

function trainIconLabel(train) {
  if (train.is_late) {
    if (train.train_in_front_also_late) {
      return "delayed by train in front";
    }
    return "late train";
  }
  if (train.is_early) {
    return "early train";
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
  for (const [trainId, marker] of markersByTrainId) {
    const train = trainStateById.get(trainId);
    if (train) {
      marker.setIcon(trainIcon(train));
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

function syncMarkers(trains) {
  const seen = new Set();

  for (const train of trains) {
    seen.add(train.train_id);
    trainStateById.set(train.train_id, train);

    const latLng = markerLatLng(train);
    let marker = markersByTrainId.get(train.train_id);

    if (!marker) {
      marker = L.marker(latLng, { icon: trainIcon(train) });
      marker.addTo(map);
      markersByTrainId.set(train.train_id, marker);
    } else {
      marker.setLatLng(latLng);
      marker.setIcon(trainIcon(train));
    }

    marker.bindPopup(
      () => trainPopupHtml(trainStateById.get(train.train_id) || train),
      { closeButton: true, autoClose: true, closeOnClick: true }
    );
  }

  for (const [trainId, marker] of markersByTrainId) {
    if (!seen.has(trainId)) {
      map.removeLayer(marker);
      markersByTrainId.delete(trainId);
      trainStateById.delete(trainId);
    }
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
    marker.setLatLng(markerLatLng(train));
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

  const lateLine =
    train.is_late && maxDelay != null
      ? `<div class="train-popup__late">${maxDelay}s behind schedule</div>`
      : "";

  const earlyLine =
    !train.is_late && train.is_early && minDelay != null
      ? `<div class="train-popup__early">${Math.abs(minDelay)}s ahead of schedule</div>`
      : "";

  const trainInFrontLine =
    train.train_in_front_also_late && train.train_in_front_id
      ? `<div class="train-popup__train-in-front">Train in front also late (<a class="train-popup__link" href="${trainPageUrl(train.train_in_front_id)}">${escapeHtml(train.train_in_front_id)}</a>${train.train_in_front_stops_ahead != null ? `, ${train.train_in_front_stops_ahead} stop${train.train_in_front_stops_ahead === 1 ? "" : "s"} ahead` : ""})</div>`
      : "";

  return `<div class="train-popup"><strong>Train ID</strong><br><a class="train-popup__link" href="${trainPageUrl(train.train_id)}">${escapeHtml(train.train_id)}</a>${stationLine}${departureLine}${modeLine}${lateLine}${earlyLine}${trainInFrontLine}</div>`;
}

async function loadTrains() {
  try {
    const response = await fetch("/api/map/trains");
    if (!response.ok) {
      throw new Error(`API error (${response.status})`);
    }
    const data = await response.json();
    syncMarkers(data.trains || []);
    statusEl.textContent = `Updated ${new Date().toLocaleTimeString()} · ${lastTrainCount} train(s) on map`;
    statusEl.classList.remove("error");
  } catch (error) {
    statusEl.textContent = `Could not load trains: ${error.message}`;
    statusEl.classList.add("error");
  }
}

loadShapes().catch((error) => {
  statusEl.textContent = `Could not load subway lines: ${error.message}`;
  statusEl.classList.add("error");
});

startAnimationLoop();
loadTrains();
setInterval(loadTrains, REFRESH_MS);
