// Progressive Tamil voice player: can play part 1 as soon as it exists while later parts are still
// being synthesised, and draws a live waveform from the audio being played.

export function createVoicePlayer(els) {
  const { root, audio, play, status, canvas, segments, download, retry, autoplay } = els;
  let parts = [], index = 0, playing = false, waiting = false, ctx = null, analyser = null, raf = 0;

  function reset() {
    audio.pause();
    audio.removeAttribute("src");
    parts = []; index = 0; playing = false; waiting = false;
    play.disabled = true;
    play.classList.remove("playing");
    status.textContent = "Waiting for the Tamil alert…";
    segments.replaceChildren();
    download.hidden = true;
    retry.hidden = true;
    root.hidden = true;
    drawIdle();
  }

  function show(text) {
    root.hidden = false;
    status.textContent = text;
  }

  function renderSegments() {
    segments.replaceChildren(...parts.map((url, i) => {
      const s = document.createElement("i");
      if (url) s.className = "ready";
      if (playing && i === index) s.className = "now";
      return s;
    }));
  }

  function setTotal(total) {
    if (parts.length !== total) parts = Array.from({ length: total }, (_, i) => parts[i] || null);
  }

  function addPart(i, total, url) {
    root.hidden = false;
    setTotal(total);
    parts[i] = url;
    const ready = parts.filter(Boolean).length;
    if (parts[0]) play.disabled = false;
    if (!playing) status.textContent = parts[0] ? `${ready} of ${total} parts ready · press play` : `Synthesising… ${ready} of ${total} parts`;
    renderSegments();
    if (i === 0 && autoplay.checked && !playing && index === 0) start();
    if (waiting && i === index) { waiting = false; playPart(); }
  }

  function finished(url) {
    download.href = url;
    download.hidden = false;
    if (!playing) status.textContent = `Ready · ${parts.length || 1} parts`;
  }

  function failed(message) {
    root.hidden = false;
    status.textContent = message;
    retry.hidden = false;
  }

  function ensureAudioGraph() {
    if (ctx) return;
    try {
      ctx = new AudioContext();
      const src = ctx.createMediaElementSource(audio);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      analyser.connect(ctx.destination);
    } catch { ctx = null; analyser = null; }
  }

  function start() {
    if (!parts[index]) { waiting = true; return; }
    ensureAudioGraph();
    ctx?.resume();
    playing = true;
    play.classList.add("playing");
    playPart();
    loop();
  }

  function playPart() {
    audio.src = parts[index];
    audio.play().catch(() => {
      // Autoplay blocked: wait for a click instead of failing.
      playing = false;
      play.classList.remove("playing");
      status.textContent = "Press play to hear the Tamil alert";
    });
    status.textContent = `Speaking part ${index + 1} of ${parts.length}`;
    renderSegments();
  }

  audio.addEventListener("ended", () => {
    if (index + 1 < parts.length) {
      index += 1;
      if (parts[index]) playPart();
      else { waiting = true; status.textContent = `Waiting for part ${index + 1}…`; }
    } else {
      playing = false; index = 0;
      play.classList.remove("playing");
      status.textContent = "Finished · press play to replay";
      renderSegments();
    }
  });

  play.addEventListener("click", () => {
    if (playing) {
      audio.pause(); playing = false; play.classList.remove("playing");
      status.textContent = "Paused";
    } else if (audio.src && !audio.ended && audio.currentTime > 0) {
      ensureAudioGraph(); ctx?.resume();
      audio.play(); playing = true; play.classList.add("playing"); loop();
      status.textContent = `Speaking part ${index + 1} of ${parts.length}`;
    } else {
      start();
    }
  });

  function loop() {
    cancelAnimationFrame(raf);
    const g = canvas.getContext("2d");
    const data = new Uint8Array(analyser ? analyser.frequencyBinCount : 64);
    const frame = () => {
      const w = (canvas.width = canvas.clientWidth * devicePixelRatio);
      const h = (canvas.height = canvas.clientHeight * devicePixelRatio);
      g.clearRect(0, 0, w, h);
      if (analyser) analyser.getByteFrequencyData(data);
      const bars = 48, gap = 3 * devicePixelRatio, bw = (w - gap * (bars - 1)) / bars;
      const grad = g.createLinearGradient(0, 0, w, 0);
      grad.addColorStop(0, "#5eead4"); grad.addColorStop(1, "#3b82f6");
      g.fillStyle = grad;
      for (let i = 0; i < bars; i++) {
        const v = analyser && playing ? data[Math.floor((i / bars) * data.length * 0.7)] / 255 : 0;
        const bh = Math.max(2 * devicePixelRatio, v * h * 0.95);
        g.beginPath();
        g.roundRect(i * (bw + gap), (h - bh) / 2, bw, bh, bw / 2);
        g.fill();
      }
      if (playing || waiting) raf = requestAnimationFrame(frame);
    };
    frame();
  }

  function drawIdle() {
    cancelAnimationFrame(raf);
    requestAnimationFrame(() => loop());
  }

  return { reset, show, setTotal, addPart, finished, failed };
}
