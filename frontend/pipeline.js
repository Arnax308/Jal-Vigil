// Live view of the LangGraph run: each node lights up as its trace events stream in.

const STEPS = [
  { id: "interpret_request", name: "Understand the request" },
  { id: "load_area", name: "Map city, grid & terrain" },
  { par: [{ id: "fetch_rainfall", name: "Rain forecast" }, { id: "fetch_climatology", name: "20-yr extremes" }], name: "Fetch rain data in parallel" },
  { id: "assess_risk", name: "Score flood risk" },
  { id: "find_facilities", name: "Find buildings at risk", alert: true },
  { id: "plan_routes", name: "Route around flooding", alert: true },
  { id: "draft_alert", name: "Draft & fact-check alert", alert: true },
  { id: "translate_alert", name: "Translate to Tamil", alert: true },
  { id: "voice", name: "Tamil voice alert", alert: true },
  { id: "all_clear", name: "Report all clear", clear: true },
];
const ALL_IDS = STEPS.flatMap((s) => (s.par ? s.par.map((p) => p.id) : [s.id]));

export function createPipeline(list, { onProgress } = {}) {
  let state = {};

  function reset() {
    state = Object.fromEntries(ALL_IDS.map((id) => [id, { status: "pending", msg: "", retries: 0, tools: 0 }]));
    state.all_clear.hidden = true;
    render();
  }

  function render() {
    list.replaceChildren(...STEPS.filter((s) => !(s.clear && state.all_clear.hidden)).map((s, i) => {
      const li = document.createElement("li");
      if (s.par) {
        const subs = s.par.map((p) => state[p.id]);
        const status = subs.every((x) => x.status === "done") ? "done" : subs.some((x) => x.status === "active" || x.status === "done") ? "active" : "pending";
        li.className = `node ${status}`;
        li.innerHTML = `<div class="node-dot">${status === "done" ? "✓" : i + 1}</div>
          <div class="node-name">${s.name}</div>
          <div class="node-par">${s.par.map((p) => `<div class="node-sub ${state[p.id].status}"><b>${p.name}</b><span>${state[p.id].time || state[p.id].status}</span></div>`).join("")}</div>`;
        return li;
      }
      const st = state[s.id];
      li.className = `node ${st.status}`;
      const dot = st.status === "done" ? "✓" : st.status === "error" ? "!" : i + 1;
      const badges = (st.retries ? `<span class="node-badge">${st.retries} ${s.id === "plan_routes" ? "reroutes" : "retries"}</span>` : "") +
        (st.progress ? `<span class="node-badge tool">${st.progress}</span>` : "");
      li.innerHTML = `<div class="node-dot">${dot}</div>
        <div class="node-name">${s.name}${badges}<span class="node-time">${st.time || ""}</span></div>
        ${st.msg ? `<div class="node-msg"></div>` : ""}`;
      if (st.msg) li.querySelector(".node-msg").textContent = st.msg;
      return li;
    }));
    const visible = ALL_IDS.filter((id) => !state[id].hidden && state[id].status !== "skipped");
    const done = visible.filter((id) => state[id].status === "done").length;
    onProgress?.(visible.length ? done / visible.length : 0);
  }

  function handle(ev) {
    const id = ev.step || (ev.type.startsWith("voice_") ? "voice" : null);
    if (ev.type === "done") {
      // Anything never reached on the chosen branch is shown as skipped.
      const clear = ev.data?.status === "all_clear";
      for (const s of STEPS) {
        if (s.par) continue;
        if (clear && s.alert && state[s.id].status === "pending") state[s.id].status = "skipped";
      }
      if (!clear) state.all_clear.hidden = true;
      render();
      return;
    }
    if (ev.type === "end") {
      if (state.voice.status === "pending") state.voice.status = "skipped";
      render();
      return;
    }
    if (!id || !state[id]) {
      if (ev.type === "warning" && /voice/i.test(ev.message)) {
        Object.assign(state.voice, { status: "error", msg: ev.message });
        render();
      }
      return;
    }
    const st = state[id];
    if (id === "all_clear") st.hidden = false;
    switch (ev.type) {
      case "step_start": st.status = "active"; st.start = ev.ts; break;
      case "step_end": st.status = "done"; st.time = st.start ? `${(ev.ts - st.start).toFixed(1)}s` : ""; break;
      case "step_error": st.status = "error"; st.msg = ev.message; break;
      case "voice_start": st.status = "active"; st.start = ev.ts; st.msg = "Synthesising speech in parallel parts"; break;
      case "voice_chunk": st.progress = `${(st.ready = (st.ready || 0) + 1)}/${ev.data.total}`; st.msg = ev.message; break;
      case "voice_ready": st.status = "done"; st.time = st.start ? `${(ev.ts - st.start).toFixed(1)}s` : ""; st.msg = ev.message; break;
      case "retry": st.retries += 1; st.msg = ev.message; break;
      default: st.msg = ev.message;
    }
    render();
  }

  function fail() {
    for (const id of ALL_IDS) if (state[id].status === "active") state[id].status = "error";
    render();
  }

  reset();
  return { reset, handle, fail };
}
