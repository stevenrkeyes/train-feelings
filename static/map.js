const statusEl = document.getElementById("status");
const REFRESH_MS = 15000;

const NYC_CENTER = [40.72, -73.96];
const NYC_ZOOM = 12.5;

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

function trainIcon(isLate) {
  const emoji = isLate ? "😢" : "🚆";
  const label = isLate ? "late train" : "train";
  return L.divIcon({
    className: "train-marker",
    html: `<span class="train-marker__emoji" role="img" aria-label="${label}">${emoji}</span>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
}

function updateMarkers(trains) {
  const seen = new Set();

  for (const train of trains) {
    seen.add(train.train_id);
    const latLng = [train.lat, train.lon];
    let marker = markersByTrainId.get(train.train_id);

    if (!marker) {
      marker = L.marker(latLng, { icon: trainIcon(train.is_late) });
      marker.addTo(map);
      markersByTrainId.set(train.train_id, marker);
    } else {
      marker.setLatLng(latLng);
      marker.setIcon(trainIcon(train.is_late));
    }

    marker.bindPopup(trainPopupHtml(train), {
      closeButton: true,
      autoClose: true,
      closeOnClick: true,
    });
  }

  for (const [trainId, marker] of markersByTrainId) {
    if (!seen.has(trainId)) {
      map.removeLayer(marker);
      markersByTrainId.delete(trainId);
    }
  }
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

function trainPopupHtml(train) {
  const delays = [train.trip_arrival_delay, train.trip_departure_delay].filter((value) => value != null);
  const maxDelay = delays.length ? Math.max(...delays) : null;
  const lateLine =
    train.is_late && maxDelay != null
      ? `<div class="train-popup__late">${maxDelay}s behind schedule</div>`
      : "";
  return `<div class="train-popup"><strong>Train ID</strong><br><a class="train-popup__link" href="${trainPageUrl(train.train_id)}">${escapeHtml(train.train_id)}</a>${lateLine}</div>`;
}

async function loadTrains() {
  try {
    const response = await fetch("/api/map/trains");
    if (!response.ok) {
      throw new Error(`API error (${response.status})`);
    }
    const data = await response.json();
    updateMarkers(data.trains || []);
    statusEl.textContent = `Updated ${new Date().toLocaleTimeString()} · ${data.trains.length} train(s) on map`;
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

loadTrains();
setInterval(loadTrains, REFRESH_MS);
