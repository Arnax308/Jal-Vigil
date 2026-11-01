// 3D flood-risk map: MapLibre GL on OpenFreeMap's keyless dark basemap.
/* global maplibregl */

export const LEVEL_COLORS = { low: "#0ca30c", moderate: "#fab219", high: "#ec835a", severe: "#d03b3b" };
export const KIND_GROUP = {
  hospital: "health", care_home: "health", clinic: "health",
  shelter: "relief", school: "relief", college: "relief",
  police: "emergency", fire_station: "emergency",
};
export const KIND_LABEL = {
  hospital: "Hospital", care_home: "Care home", clinic: "Clinic", shelter: "Shelter", school: "School",
  college: "College", police: "Police", fire_station: "Fire station",
};
const METRES_PER_MM = 22; // column height per mm of rain so far
const CHENNAI = [80.235, 13.045];

// Building kinds are told apart by shape, not color: color already means risk level on this map.
function drawIcon(group, size = 48) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const s = size / 48;
  g.scale(s, s);
  g.lineWidth = 3;
  g.strokeStyle = "#061021";
  g.fillStyle = "#f1f5f9";
  g.beginPath();
  if (group === "health") {
    g.roundRect(6, 6, 36, 36, 9);
  } else if (group === "relief") {
    g.moveTo(24, 4); g.lineTo(44, 20); g.lineTo(44, 44); g.lineTo(4, 44); g.lineTo(4, 20); g.closePath();
  } else {
    g.moveTo(24, 3); g.lineTo(45, 24); g.lineTo(24, 45); g.lineTo(3, 24); g.closePath();
  }
  g.fill(); g.stroke();
  g.fillStyle = "#061021";
  if (group === "health") { g.fillRect(20, 13, 8, 22); g.fillRect(13, 20, 22, 8); }
  else if (group === "relief") { g.fillRect(19, 28, 10, 16); }
  else { g.beginPath(); g.arc(24, 24, 6, 0, Math.PI * 2); g.fill(); }
  return c;
}
export const ICON_URLS = Object.fromEntries(["health", "relief", "emergency"].map((k) => [k, drawIcon(k, 48).toDataURL()]));

const empty = () => ({ type: "FeatureCollection", features: [] });
const flip = (coords) => coords.map(([lat, lon]) => [lon, lat]);

function circle(lon, lat, km, steps = 72) {
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * Math.PI * 2;
    pts.push([lon + (km / (111.32 * Math.cos((lat * Math.PI) / 180))) * Math.cos(a), lat + (km / 111.32) * Math.sin(a)]);
  }
  return { type: "Feature", geometry: { type: "Polygon", coordinates: [pts] }, properties: {} };
}

export function createMap(container, { onPick, onReady } = {}) {
  const map = new maplibregl.Map({
    container,
    style: "https://tiles.openfreemap.org/styles/dark",
    center: CHENNAI, zoom: 10.4, pitch: 52, bearing: -18,
    attributionControl: { compact: true },
    maxBounds: [[79.6, 12.4], [80.9, 13.7]],
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");

  let cells = null, rainByCell = null, idleSpin = true, picking = false, dashTimer = null;
  const popup = new maplibregl.Popup({ closeButton: true, maxWidth: "300px" });

  map.on("load", () => {
    for (const [k, url] of Object.entries(ICON_URLS)) {
      const img = new Image(48, 48);
      img.onload = () => map.addImage(`icon-${k}`, img, { pixelRatio: 2 });
      img.src = url;
    }
    const buildingLayer = map.getStyle().layers.find((l) => l["source-layer"] === "building");
    if (buildingLayer) {
      map.addLayer({
        id: "3d-buildings", type: "fill-extrusion", source: buildingLayer.source, "source-layer": "building", minzoom: 14,
        paint: {
          "fill-extrusion-color": "#1b2740",
          "fill-extrusion-height": ["coalesce", ["get", "render_height"], 8],
          "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
          "fill-extrusion-opacity": 0.85,
        },
      });
    }
    for (const id of ["boundary", "cells", "focus", "facilities", "routes", "sites", "safe-route", "start"]) {
      map.addSource(id, { type: "geojson", data: empty() });
    }
    map.addLayer({ id: "boundary-glow", type: "line", source: "boundary", paint: { "line-color": "#38bdf8", "line-width": 8, "line-opacity": 0.18, "line-blur": 6 } });
    map.addLayer({ id: "boundary", type: "line", source: "boundary", paint: { "line-color": "#7dd3fc", "line-width": 1.4, "line-dasharray": [3, 2] } });
    map.addLayer({
      id: "cells", type: "fill-extrusion", source: "cells",
      paint: {
        "fill-extrusion-color": ["get", "color"],
        "fill-extrusion-height": ["get", "height"],
        "fill-extrusion-base": 0,
        "fill-extrusion-opacity": ["interpolate", ["linear"], ["zoom"], 11, 0.72, 14, 0.3],
        "fill-extrusion-vertical-gradient": true,
      },
    });
    map.addLayer({ id: "focus", type: "line", source: "focus", paint: { "line-color": "#c4b5fd", "line-width": 2, "line-dasharray": [2, 2] } });
    // Buildings: plain dots at city scale, shaped icons once zoomed in far enough to tell them apart.
    map.addLayer({
      id: "facility-dots", type: "circle", source: "facilities", maxzoom: 12,
      paint: { "circle-radius": 1.8, "circle-color": "#e2e8f0", "circle-opacity": 0.75 },
    });
    map.addLayer({
      id: "facilities", type: "symbol", source: "facilities", minzoom: 12,
      layout: {
        "icon-image": ["concat", "icon-", ["get", "group"]], "icon-allow-overlap": true,
        "icon-size": ["interpolate", ["linear"], ["zoom"], 12, 0.5, 16, 0.9],
        "symbol-sort-key": ["get", "rank"],
      },
    });
    map.addLayer({ id: "routes-glow", type: "line", source: "routes", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 12, "line-opacity": 0.25, "line-blur": 6 } });
    map.addLayer({ id: "routes", type: "line", source: "routes", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": 3.5, "line-dasharray": [0, 4, 3] } });
    map.addLayer({ id: "safe-route-glow", type: "line", source: "safe-route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#22d3ee", "line-width": 16, "line-opacity": 0.35, "line-blur": 8 } });
    map.addLayer({ id: "safe-route", type: "line", source: "safe-route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#a5f3fc", "line-width": 4.5, "line-dasharray": [0, 4, 3] } });
    map.addLayer({ id: "sites-halo", type: "circle", source: "sites", paint: { "circle-radius": 14, "circle-color": "#4ade80", "circle-opacity": 0.18, "circle-blur": 0.6 } });
    map.addLayer({ id: "sites", type: "circle", source: "sites", paint: { "circle-radius": 6, "circle-color": "#4ade80", "circle-stroke-color": "#052e16", "circle-stroke-width": 2 } });
    map.addLayer({ id: "start", type: "circle", source: "start", paint: { "circle-radius": 8, "circle-color": "#22d3ee", "circle-stroke-color": "#fff", "circle-stroke-width": 3 } });

    map.on("click", "cells", (e) => {
      if (picking || map.queryRenderedFeatures(e.point, { layers: ["facilities", "sites"] }).length) return;
      popup.setLngLat(e.lngLat).setHTML(cellHtml(e.features[0].properties)).addTo(map);
    });
    map.on("click", "facilities", (e) => {
      if (picking) return;
      const p = e.features[0].properties;
      popup.setLngLat(e.features[0].geometry.coordinates).setHTML(facilityHtml(p)).addTo(map);
    });
    map.on("click", "sites", (e) => {
      if (picking) return;
      popup.setLngLat(e.features[0].geometry.coordinates).setHTML(`<div class="pop-title">${esc(e.features[0].properties.name)}</div><div class="pop-row">Relief site in a lower-risk cell</div>`).addTo(map);
    });
    map.on("click", (e) => {
      if (!picking) return;
      picking = false;
      document.body.classList.remove("picking");
      onPick?.(e.lngLat.lat, e.lngLat.lng);
    });
    for (const id of ["cells", "facilities", "sites"]) {
      map.on("mouseenter", id, () => { if (!picking) map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", id, () => { if (!picking) map.getCanvas().style.cursor = ""; });
    }
    startDashes();
    spin();
    onReady?.();
  });

  ["mousedown", "touchstart", "wheel"].forEach((ev) => map.on(ev, () => { idleSpin = false; }));
  function spin() {
    if (!idleSpin) return;
    map.easeTo({ bearing: map.getBearing() + 6, duration: 4000, easing: (t) => t });
    map.once("moveend", spin);
  }

  function startDashes() {
    // Marching-ants animation on route lines.
    const seq = [[0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1], [2.5, 4, 0.5], [3, 4, 0], [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5], [0, 2, 3, 2], [0, 2.5, 3, 1.5], [0, 3, 3, 1], [0, 3.5, 3, 0.5]];
    let step = 0;
    clearInterval(dashTimer);
    dashTimer = setInterval(() => {
      step = (step + 1) % seq.length;
      if (map.getLayer("routes")) map.setPaintProperty("routes", "line-dasharray", seq[step]);
      if (map.getLayer("safe-route")) map.setPaintProperty("safe-route", "line-dasharray", seq[step]);
    }, 70);
  }

  function setHour(hour) {
    if (!cells) return;
    const features = cells.features.map((f) => {
      const series = rainByCell[f.properties.cell_id] || [];
      let sum = 0;
      for (let i = 0; i <= hour && i < series.length; i++) sum += series[i];
      return { ...f, properties: { ...f.properties, height: Math.max(40, sum * METRES_PER_MM), rain_so_far: sum } };
    });
    map.getSource("cells")?.setData({ type: "FeatureCollection", features });
  }

  return {
    map,
    stopSpin() { idleSpin = false; map.stop(); },
    flyHome() {
      idleSpin = false;
      map.flyTo({ center: CHENNAI, zoom: 10.5, pitch: 55, bearing: -20, duration: 2200, essential: true });
    },
    setResult(result) {
      // A run on a warm cache can finish before the basemap has loaded; draw once it has.
      if (!map.getSource("cells")) {
        map.once("load", () => this.setResult(result));
        return;
      }
      idleSpin = false;
      popup.remove();
      rainByCell = result.rain_hourly?.by_cell || {};
      cells = {
        type: "FeatureCollection",
        features: result.cells.features.map((f) => ({
          ...f, properties: { ...f.properties, color: LEVEL_COLORS[f.properties.level], height: 40 },
        })),
      };
      map.getSource("boundary").setData({ type: "Feature", geometry: result.boundary, properties: {} });
      map.getSource("focus").setData(result.focus ? circle(result.focus.lon, result.focus.lat, result.focus.radius_km) : empty());
      const facilities = result.facilities_at_risk || [];
      map.getSource("facilities").setData({
        type: "FeatureCollection",
        features: facilities.map((f, i) => ({
          type: "Feature", geometry: { type: "Point", coordinates: [f.lon, f.lat] },
          properties: { ...f, group: KIND_GROUP[f.kind] || "relief", rank: i },
        })),
      });
      const routes = result.routes || [];
      map.getSource("routes").setData({
        type: "FeatureCollection",
        features: routes.map((r) => ({
          type: "Feature", geometry: { type: "LineString", coordinates: flip(r.coords) },
          properties: { color: r.hazard_overlap_km === 0 ? "#4ade80" : "#fbbf24" },
        })),
      });
      const sites = new Map(routes.map((r) => [r.to.name, r.to]));
      map.getSource("sites").setData({
        type: "FeatureCollection",
        features: [...sites.values()].map((s) => ({ type: "Feature", geometry: { type: "Point", coordinates: [s.lon, s.lat] }, properties: { name: s.name } })),
      });
      this.clearSafeRoute();
      setHour(-1);
      const b = new maplibregl.LngLatBounds();
      for (const f of result.cells.features) for (const [x, y] of f.geometry.coordinates[0]) b.extend([x, y]);
      map.fitBounds(b, { padding: padding(), pitch: 55, bearing: -20, duration: 1800 });
    },
    setHour,
    focusFacility(f) {
      map.flyTo({ center: [f.lon, f.lat], zoom: 15.2, pitch: 60, duration: 1600, essential: true });
      popup.setLngLat([f.lon, f.lat]).setHTML(facilityHtml(f)).addTo(map);
    },
    fitLine(coords) {
      const b = new maplibregl.LngLatBounds();
      for (const c of flip(coords)) b.extend(c);
      map.fitBounds(b, { padding: padding(80), pitch: 45, duration: 1400 });
    },
    showSafeRoute(route) {
      map.getSource("safe-route").setData({ type: "Feature", geometry: { type: "LineString", coordinates: flip(route.coords) }, properties: {} });
      map.getSource("start").setData({ type: "Feature", geometry: { type: "Point", coordinates: [route.from.lon, route.from.lat] }, properties: {} });
      this.fitLine(route.coords);
    },
    showStart(lat, lon) {
      map.getSource("start")?.setData({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] }, properties: {} });
    },
    clearSafeRoute() {
      map.getSource("safe-route")?.setData(empty());
      map.getSource("start")?.setData(empty());
    },
    setPicking(on) {
      picking = on;
      document.body.classList.toggle("picking", on);
    },
  };
}

function padding(extra = 0) {
  if (window.innerWidth <= 980) return 30 + extra / 3;
  const css = getComputedStyle(document.documentElement);
  const left = parseFloat(css.getPropertyValue("--left-w")) + 30;
  const right = parseFloat(css.getPropertyValue("--right-w")) + 30;
  return { top: 110 + extra / 2, bottom: 190 + extra / 2, left: left + extra / 2, right: right + extra / 2 };
}

export function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function cellHtml(p) {
  const so = p.rain_so_far != null ? `<div class="pop-row">Rain so far in time-lapse: <b>${Number(p.rain_so_far).toFixed(0)} mm</b></div>` : "";
  return `<div class="pop-title">${esc(p.locality || p.cell_id)}</div>
    <div class="pop-row"><span class="level" style="--c:${LEVEL_COLORS[p.level]}">${esc(p.level)}</span> score <b>${p.score}</b></div>
    <div class="pop-row">Wettest 24 h: <b>${p.rain_24h_max_mm} mm</b> (${esc(p.imd_category)})</div>
    <div class="pop-row">Window total: <b>${p.rain_total_mm} mm</b> · peak <b>${p.peak_hourly_mm} mm/h</b></div>
    <div class="pop-row">Ground: <b>${p.elevation_m} m</b> above sea level</div>${so}`;
}

function facilityHtml(f) {
  return `<div class="pop-title">${esc(f.name)}</div>
    <div class="pop-row">${esc(KIND_LABEL[f.kind] || f.kind)} · <span class="level" style="--c:${LEVEL_COLORS[f.level]}">${esc(f.level)} risk</span></div>
    ${f.locality ? `<div class="pop-row">${esc(f.locality)}</div>` : ""}
    ${f.address ? `<div class="pop-row">${esc(f.address)}</div>` : ""}
    ${f.phone ? `<div class="pop-row">☎ <b>${esc(f.phone)}</b></div>` : ""}
    ${f.rain_24h_max_mm != null ? `<div class="pop-row">Wettest 24 h here: <b>${Number(f.rain_24h_max_mm).toFixed(0)} mm</b></div>` : ""}`;
}
