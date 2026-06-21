if (!SPOTIFY_CONNECTED || !YANDEX_CONNECTED) {
  // Nothing to initialize on the connect page
} else {
  let direction = "spotify_to_yandex";
  let total = 0;
  let processed = 0;
  let playlistReqId = 0; // for cancelling stale requests

  const select    = document.getElementById("playlist-select");
  const btn       = document.getElementById("transfer-btn");
  const progSec   = document.getElementById("progress-section");
  const progBar   = document.getElementById("progress-bar");
  const progLabel = document.getElementById("progress-label");
  const trackList = document.getElementById("track-list");
  const summary   = document.getElementById("summary");
  const swapBtn   = document.getElementById("swap-btn");

  const spotifyIconSVG = `<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
    <path d="M12 2C6.477 2 2 6.477 2 12s4.477 10 10 10 10-4.477 10-10S17.523 2 12 2zm4.586 14.424a.622.622 0 01-.857.207c-2.348-1.435-5.304-1.76-8.785-.964a.622.622 0 01-.277-1.215c3.809-.87 7.076-.496 9.712 1.115a.622.622 0 01.207.857zm1.223-2.722a.779.779 0 01-1.07.257c-2.687-1.652-6.785-2.131-9.965-1.166a.778.778 0 01-.972-.519.779.779 0 01.52-.972c3.632-1.102 8.147-.568 11.23 1.33a.779.779 0 01.257 1.07zm.105-2.835C14.692 8.95 9.375 8.775 6.297 9.71a.935.935 0 11-.543-1.79c3.532-1.072 9.404-.865 13.115 1.338a.935.935 0 01-.955 1.609z"/>
  </svg>`;

  const yandexIconSVG = `<svg width="24" height="24" viewBox="0 0 144 144" fill="none">
    <path fill="#000" d="m130.863 57.739-.468-2.327-19.788-3.457 11.498-15.557-1.337-1.462-16.913 8.11 2.139-21.54-1.738-.997-10.295 17.418L82.395 12H80.39l2.74 25.064-29.08-23.269-2.474.732 22.396 28.122-44.323-14.76-2.006 2.261L67.22 52.686l-54.618 4.521-.602 3.39 56.757 6.184-47.33 39.157 2.005 2.726 56.356-30.648-11.164 53.983h3.41l21.592-50.792 13.17 39.756 2.34-1.795-5.415-40.42 20.524 23.268 1.337-2.128-15.711-28.853 21.928 8.111.201-2.46-19.655-14.493 18.518-4.454Z"/>
  </svg>`;

  // Static service info (read user names once from DOM)
  const spotifyUser = document.getElementById("dir-source-user") ? document.getElementById("dir-source-user").textContent.trim() : "";
  const yandexUser  = document.getElementById("dir-dest-user")   ? document.getElementById("dir-dest-user").textContent.trim()   : "";

  const SERVICES = {
    spotify: { name: "Spotify",       iconClass: "spotify-icon", iconHTML: spotifyIconSVG, user: spotifyUser },
    yandex:  { name: "Яндекс Музыка", iconClass: "yandex-icon",  iconHTML: yandexIconSVG,  user: yandexUser  },
  };

  // ── Direction card ────────────────────────────────────

  function updateDirectionCard() {
    const src = direction === "spotify_to_yandex" ? "spotify" : "yandex";
    const dst = direction === "spotify_to_yandex" ? "yandex"  : "spotify";

    ["source", "dest"].forEach((role, i) => {
      const svc = i === 0 ? SERVICES[src] : SERVICES[dst];
      const icon = document.getElementById(`dir-${role}-icon`);
      const name = document.getElementById(`dir-${role}-name`);
      const user = document.getElementById(`dir-${role}-user`);
      if (icon) { icon.className = `p-icon ${svc.iconClass}`; icon.innerHTML = svc.iconHTML; }
      if (name) name.textContent = svc.name;
      if (user) user.textContent = svc.user;
    });

    document.body.classList.remove("dir-s2y", "dir-y2s");
    document.body.classList.add(direction === "spotify_to_yandex" ? "dir-s2y" : "dir-y2s");
  }

  updateDirectionCard();

  // Swap button
  const swapBtnEl = document.getElementById("swap-btn");
  if (swapBtnEl) {
    swapBtnEl.addEventListener("click", function() {
      direction = direction === "spotify_to_yandex" ? "yandex_to_spotify" : "spotify_to_yandex";
      updateDirectionCard();
      loadPlaylists();
    });
  }

  // ── Load playlists ────────────────────────────────────

  async function loadPlaylists() {
    const reqId = ++playlistReqId;
    select.innerHTML = "<option>Загрузка...</option>";
    select.disabled = true;
    btn.disabled = true;

    const source = direction === "spotify_to_yandex" ? "spotify" : "yandex";
    try {
      const res  = await fetch(`/api/playlists/${source}`);
      const data = await res.json();

      // Ignore stale responses if direction changed while loading
      if (reqId !== playlistReqId) return;

      if (data.error) throw new Error(data.error);

      // Filter out playlists created by this tool
      const filtered = data.playlists.filter(p =>
        !(source === "yandex"   && p.name.includes("(from Spotify)")) &&
        !(source === "spotify"  && p.name.includes("(from Yandex)"))
      );

      select.innerHTML = filtered
        .map(p => `<option value="${p.id}" data-name="${esc(p.name)}">${esc(p.name)} (${p.count})</option>`)
        .join("");
      select.disabled = false;
      btn.disabled = false;
    } catch (e) {
      if (reqId !== playlistReqId) return;
      select.innerHTML = `<option>Ошибка: ${esc(e.message)}</option>`;
    }
  }

  loadPlaylists();

  // ── Start transfer ────────────────────────────────────

  btn.addEventListener("click", async () => {
    const opt = select.options[select.selectedIndex];
    if (!opt) return;

    const playlist_id   = opt.value;
    const playlist_name = opt.dataset.name;

    trackList.innerHTML = "";
    summary.style.display = "none";
    progBar.style.width = "0%";
    progBar.style.background = "";
    progLabel.textContent = "Подключаюсь...";
    progSec.style.display = "block";
    btn.disabled = true;
    total = processed = 0;
    document.getElementById("main").classList.remove("layout-done");

    progSec.scrollIntoView({ behavior: "smooth", block: "start" });

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
        es.close();
        progBar.style.background = "var(--danger)";
        progLabel.textContent = "Ошибка: " + msg.message;
        btn.disabled = false;
      }

      if (msg.type === "done") {
        es.close();
        progBar.style.width = "100%";
        progLabel.textContent = "Готово!";
        btn.disabled = false;
        showSummary(msg);
        const mainEl = document.getElementById("main");
        mainEl.classList.add("layout-done");
        // Align summary top with the platform card top
        requestAnimationFrame(() => {
          const card = document.querySelector(".transfer-dir-card");
          const leftCol = document.getElementById("left-col");
          const summaryEl = document.getElementById("summary");
          if (card && leftCol && summaryEl) {
            // #left-col and #summary are siblings; measure card offset from #left-col top
            const offset = card.getBoundingClientRect().top - leftCol.getBoundingClientRect().top;
            summaryEl.style.marginTop = Math.max(0, offset) + "px";
          }
        });
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
      <h3>Итог</h3>
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
