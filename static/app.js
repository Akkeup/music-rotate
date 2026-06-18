if (!SPOTIFY_CONNECTED || !YANDEX_CONNECTED) {
  // Nothing to initialize on the connect page
} else {
  let direction = "spotify_to_yandex";
  let total = 0;
  let processed = 0;

  const select    = document.getElementById("playlist-select");
  const btn       = document.getElementById("transfer-btn");
  const progSec   = document.getElementById("progress-section");
  const progBar   = document.getElementById("progress-bar");
  const progLabel = document.getElementById("progress-label");
  const trackList = document.getElementById("track-list");
  const summary   = document.getElementById("summary");

  // ── Direction toggle ──────────────────────────────────

  document.querySelectorAll(".dir-btn").forEach(b => {
    b.addEventListener("click", () => {
      document.querySelectorAll(".dir-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      direction = b.dataset.dir;
      loadPlaylists();
    });
  });

  // ── Load playlists ────────────────────────────────────

  async function loadPlaylists() {
    select.innerHTML = "<option>Загрузка...</option>";
    btn.disabled = true;

    const source = direction === "spotify_to_yandex" ? "spotify" : "yandex";
    try {
      const res = await fetch(`/api/playlists/${source}`);
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      select.innerHTML = data.playlists
        .map(p => `<option value="${p.id}" data-name="${esc(p.name)}">${esc(p.name)} (${p.count})</option>`)
        .join("");
      btn.disabled = false;
    } catch (e) {
      select.innerHTML = `<option>Ошибка: ${e.message}</option>`;
    }
  }

  loadPlaylists();

  // ── Start transfer ────────────────────────────────────

  btn.addEventListener("click", async () => {
    const opt = select.options[select.selectedIndex];
    if (!opt) return;

    const playlist_id   = opt.value;
    const playlist_name = opt.dataset.name;

    // Reset UI
    trackList.innerHTML = "";
    summary.style.display = "none";
    progBar.style.width = "0%";
    progLabel.textContent = "Подключаюсь...";
    progSec.style.display = "block";
    btn.disabled = true;
    total = processed = 0;

    progSec.scrollIntoView({ behavior: "smooth", block: "start" });

    // Start job
    let job_id;
    try {
      const res = await fetch("/api/transfer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction, playlist_id, playlist_name }),
      });
      ({ job_id } = await res.json());
    } catch (e) {
      progLabel.textContent = "Ошибка запуска: " + e.message;
      btn.disabled = false;
      return;
    }

    // Listen to SSE stream
    const es = new EventSource(`/api/transfer/${job_id}/stream`);

    es.onmessage = (e) => {
      const msg = JSON.parse(e.data);

      if (msg.type === "total") {
        total = msg.count;
        progLabel.textContent = `Найдено треков: ${total}`;
      }

      if (msg.type === "track") {
        processed++;
        const pct = total > 0 ? Math.round((processed / total) * 100) : 0;
        progBar.style.width = pct + "%";
        progLabel.textContent = `${processed} / ${total}`;

        const row = document.createElement("div");
        row.className = `track-row ${msg.status}`;
        row.innerHTML = `
          <span class="track-status">${msg.status === "ok" ? "✓" : "✗"}</span>
          <span class="track-name">${esc(msg.title)}</span>
          <span class="track-artist">${esc(msg.artist)}</span>
        `;
        trackList.appendChild(row);
        trackList.scrollTop = trackList.scrollHeight;
      }

      if (msg.type === "error") {
        progLabel.textContent = "Ошибка: " + msg.message;
      }

      if (msg.type === "done") {
        es.close();
        progBar.style.width = "100%";
        progLabel.textContent = "Готово!";
        btn.disabled = false;
        showSummary(msg);
      }
    };

    es.onerror = () => {
      es.close();
      progLabel.textContent = "Соединение прервано.";
      btn.disabled = false;
    };
  });

  // ── Summary ───────────────────────────────────────────

  function showSummary(msg) {
    let html = `
      <h3>Итог переноса</h3>
      <div class="stat"><span>Найдено и добавлено</span><strong>${msg.found}</strong></div>
      <div class="stat"><span>Не найдено</span><strong>${msg.not_found}</strong></div>
    `;
    if (msg.missing && msg.missing.length) {
      html += `<div class="missing-list"><p>Не найденные треки:</p><ul>`;
      msg.missing.forEach(t => { html += `<li>${esc(t)}</li>`; });
      html += `</ul></div>`;
    }
    summary.innerHTML = html;
    summary.style.display = "block";
  }

  function esc(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
}
