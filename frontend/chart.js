// Hourly rainfall bars for the wettest cell, with a playhead that drives the 3D map time-lapse.

const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

export function fmtTime(iso, opts = { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) {
  return new Date(`${iso}:00+05:30`).toLocaleString("en-IN", { ...opts, timeZone: "Asia/Kolkata", hour12: false });
}

export function createTimeline(svg, tip, readout, { onScrub }) {
  let times = [], values = [], mean = [], head = -1, bars = [], headLine, headDot, geom;

  function render(data) {
    ({ times, values, mean } = data);
    svg.replaceChildren();
    const w = svg.clientWidth || 600, h = svg.clientHeight || 70;
    const top = 6, bottom = 14, left = 26, right = 4;
    const max = Math.max(1, ...values);
    const nice = max <= 5 ? 5 : max <= 10 ? 10 : max <= 20 ? 20 : Math.ceil(max / 10) * 10;
    const x = (i) => left + (i / values.length) * (w - left - right);
    const y = (v) => top + (1 - v / nice) * (h - top - bottom);
    geom = { x, left, right, w };
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    for (const v of [0, nice / 2, nice]) {
      svg.append(el("line", { class: "grid", x1: left, x2: w - right, y1: y(v), y2: y(v) }));
      const t = el("text", { class: "axis", x: left - 4, y: y(v) + 3, "text-anchor": "end" });
      t.textContent = v;
      svg.append(t);
    }
    const bw = Math.max(1, (w - left - right) / values.length - 1.5);
    bars = values.map((v, i) => {
      const bh = Math.max(0, y(0) - y(v));
      const r = el("rect", { class: "bar", x: x(i) + 0.75, y: y(0) - bh, width: bw, height: bh, rx: Math.min(2, bw / 2) });
      svg.append(r);
      return r;
    });
    // Day ticks at local midnight.
    times.forEach((t, i) => {
      if (t.endsWith("T00:00")) {
        const lbl = el("text", { class: "axis", x: x(i) + 2, y: h - 2 });
        lbl.textContent = fmtTime(t, { day: "numeric", month: "short" });
        svg.append(lbl);
      }
    });
    headLine = el("line", { class: "head", y1: top - 4, y2: h - bottom });
    headDot = el("circle", { class: "head-dot", r: 4, cy: top - 4 });
    svg.append(headLine, headDot);
    setHead(head < 0 ? values.length - 1 : head, false);
  }

  function setHead(i, notify = true) {
    head = Math.max(0, Math.min(values.length - 1, i));
    if (!geom) return;
    const px = geom.x(head + 1);
    headLine.setAttribute("x1", px); headLine.setAttribute("x2", px);
    headDot.setAttribute("cx", px);
    bars.forEach((b, j) => b.classList.toggle("future", j > head));
    const soFar = values.slice(0, head + 1).reduce((a, b) => a + b, 0);
    readout.textContent = `${fmtTime(times[head])} · ${soFar.toFixed(0)} mm so far`;
    if (notify) onScrub(head);
  }

  function indexAt(clientX) {
    const r = svg.getBoundingClientRect();
    const sx = ((clientX - r.left) / r.width) * geom.w;
    return Math.floor(((sx - geom.left) / (geom.w - geom.left - geom.right)) * values.length);
  }

  let dragging = false;
  svg.addEventListener("pointerdown", (e) => { dragging = true; svg.setPointerCapture(e.pointerId); setHead(indexAt(e.clientX)); });
  svg.addEventListener("pointerup", () => { dragging = false; });
  svg.addEventListener("pointermove", (e) => {
    if (!geom || !values.length) return;
    const i = Math.max(0, Math.min(values.length - 1, indexAt(e.clientX)));
    if (dragging) setHead(i);
    tip.hidden = false;
    tip.replaceChildren();
    const line1 = document.createElement("div");
    const b = document.createElement("b");
    b.textContent = `${values[i].toFixed(1)} mm/h`;
    line1.append(b, " wettest cell");
    const line2 = document.createElement("div");
    line2.textContent = `${mean[i].toFixed(1)} mm/h city average`;
    const line3 = document.createElement("div");
    line3.className = "muted";
    line3.textContent = fmtTime(times[i]);
    tip.append(line1, line2, line3);
    const r = svg.getBoundingClientRect();
    tip.style.left = `${Math.min(Math.max(e.clientX - r.left, 70), r.width - 70)}px`;
    tip.style.top = `${svg.offsetTop}px`;
  });
  svg.addEventListener("pointerleave", () => { tip.hidden = true; });
  window.addEventListener("resize", () => { if (values.length) render({ times, values, mean }); });

  return { render, setHead, get head() { return head; }, get length() { return values.length; } };
}
