(() => {
  const PANEL_ID = "btcbot-overlay";
  if (document.getElementById(PANEL_ID)) return;

  const panel = document.createElement("div");
  panel.id = PANEL_ID;
  panel.innerHTML = `
    <div class="bb-head">
      <span class="bb-dot" id="bb-dot"></span>
      <span class="bb-title">btcbot</span>
      <span class="bb-grow"></span>
      <span class="bb-vol" id="bb-vol">—</span>
      <button class="bb-min" id="bb-min" title="collapse">–</button>
    </div>
    <div class="bb-body" id="bb-body">
      <div class="bb-msg" id="bb-msg">connecting…</div>
      <div id="bb-tabs" style="display:none">
        <button class="bb-tab bb-tab-active" data-tab="paper">Paper</button>
        <button class="bb-tab" data-tab="live">Live</button>
      </div>
      <div id="bb-paper-panel"></div>
      <div id="bb-live-panel" style="display:none"></div>
      <div id="bb-markets"></div>
      <div class="bb-foot">read-only · places no orders</div>
    </div>`;
  document.documentElement.appendChild(panel);

  const head = panel.querySelector(".bb-head");
  document.getElementById("bb-min").addEventListener("click", () => {
    panel.classList.toggle("bb-collapsed");
  });
  head.addEventListener("dblclick", () => {
    panel.classList.toggle("bb-collapsed");
  });

  // Tab switching
  document.getElementById("bb-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".bb-tab");
    if (!tab) return;
    document.querySelectorAll(".bb-tab").forEach((t) => t.classList.remove("bb-tab-active"));
    tab.classList.add("bb-tab-active");
    const tt = tab.dataset.tab;
    document.getElementById("bb-paper-panel").style.display = tt === "paper" ? "" : "none";
    document.getElementById("bb-live-panel").style.display = tt === "live" ? "" : "none";
  });

  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
  const usd = (v) => (v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2));
  const mmss = (s) => {
    if (s == null) return "—";
    s = Math.max(0, Math.round(s));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  };

  function sendBg(msg) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve({ ok: false, error: "timed out" }), 5000);
      try {
        const api = typeof browser !== "undefined" ? browser : chrome;
        const p = api.runtime.sendMessage(msg);
        if (p && typeof p.then === "function") {
          p.then((res) => { clearTimeout(timer); resolve(res || { ok: false, error: "no response" }); })
           .catch((e) => { clearTimeout(timer); resolve({ ok: false, error: e.message }); });
        } else {
          api.runtime.sendMessage(msg, (res) => {
            clearTimeout(timer);
            const err = api.runtime.lastError;
            resolve(err ? { ok: false, error: err.message } : (res || { ok: false, error: "no response" }));
          });
        }
      } catch (e) {
        clearTimeout(timer);
        resolve({ ok: false, error: "extension context lost" });
      }
    });
  }

  function renderPortfolio(p, mode) {
    const paperPanel = document.getElementById("bb-paper-panel");
    const livePanel = document.getElementById("bb-live-panel");
    document.getElementById("bb-tabs").style.display = "flex";

    if (!p || !p.active) {
      paperPanel.innerHTML = `<div class="bb-section-label">Paper Trading</div><div class="bb-dim" style="padding:6px 0">not started</div><div id="bb-shadow-section"></div>`;
      livePanel.innerHTML = `<div class="bb-section-label">Live Trading</div><div class="bb-dim" style="padding:6px 0">disabled (mode: ${mode})</div>`;
      return;
    }

    const pnlCls = p.pnl > 0 ? "bb-up" : p.pnl < 0 ? "bb-down" : "";
    const posDetail = (p.positions || []).length
      ? p.positions.map((x) => `${x.side} ${x.shares.toFixed(1)}`).join(" · ")
      : "none open";

    paperPanel.innerHTML = `
      <div class="bb-section-label">Paper Trading</div>
      <div class="bb-panel-grid">
        <div>
          <div class="bb-portfolio-row">
            <span class="${pnlCls}" style="font-size:15px;font-weight:650">P&L ${usd(p.pnl)}</span>
            <span class="bb-dim">eq ${usd(p.equity)}</span>
          </div>
          <div class="bb-pos-detail">${posDetail}</div>
        </div>
        <div class="bb-panel-stats">
          <span class="bb-dim">closed: ${p.closed_trades} (${p.wins}W / ${p.losses}L)</span>
          <span class="bb-dim">realized: ${usd(p.realized_pnl)}</span>
        </div>
      </div>
      <div class="bb-status-line">
        <span class="bb-status-dot ${p.active ? "bb-live" : "bb-idle"}"></span>
        <span class="bb-dim">${p.active ? "running" : "idle"}</span>
        <span class="bb-grow"></span>
      </div>
      <div id="bb-shadow-section"></div>`;

    livePanel.innerHTML = `
      <div class="bb-section-label">Live Trading</div>
      <div class="bb-dim" style="padding:6px 0">not available (paper mode)</div>`;
  }

  function renderControls(s) {
    const paperPanel = document.getElementById("bb-paper-panel");
    const running = s && s.running;
    const existingBtn = paperPanel.querySelector(".bb-btn");
    if (existingBtn) return; // already rendered
    paperPanel.insertAdjacentHTML("beforeend",
      running
        ? `<button class="bb-btn bb-btn-stop" id="bb-stop">stop paper</button>`
        : `<button class="bb-btn bb-btn-start" id="bb-start">start paper</button>`
    );
    const startBtn = document.getElementById("bb-start");
    const stopBtn = document.getElementById("bb-stop");
    if (startBtn) {
      startBtn.addEventListener("click", () => {
        startBtn.disabled = true;
        sendBg({ type: "START_PAPER" });
      });
    }
    if (stopBtn) {
      stopBtn.addEventListener("click", () => {
        stopBtn.disabled = true;
        sendBg({ type: "STOP_PAPER" });
      });
    }
  }

  // -- shadow ledger ---------------------------------------------------

  let shadowStatus = null;
  let shadowReport = null;
  let shadowReplayBusy = false;

  const RUNG_TABLE = { 0: "≤2m", 1: "≤4m", 2: "≤7m", 3: "≤15m" };

  function renderShadowTab(status, rows) {
    const box = document.getElementById("bb-shadow-section");
    if (!box) return;

    // Summary bar
    let html = '<div class="bb-section-label">Shadow Ledger</div>';
    if (status && status.exists) {
      html += `<div class="bb-shadow-summary">
        <span>${status.records} records</span>
        <span>${status.settled} settled</span>
        <span>${status.windows} windows</span>
        <span class="bb-dim">${status.unsettled_slugs} unsettled</span>
      </div>`;
    } else {
      html += '<div class="bb-dim" style="padding:4px 0">no shadow data</div>';
    }

    // Replay button
    const replayDisabled = shadowReplayBusy ? " disabled" : "";
    html += `<div class="bb-shadow-actions">
      <button class="bb-btn${replayDisabled}" id="bb-shadow-replay">${shadowReplayBusy ? "replaying…" : "replay from snapshots"}</button>
    </div>`;

    // Replay result
    const resultEl = document.getElementById("bb-shadow-result");
    if (resultEl) html += resultEl.outerHTML;

    // Report table
    if (rows && rows.length) {
      html += `<div class="bb-shadow-table-wrap"><table class="bb-shadow-table">
        <tr><th>Rung</th><th>Dir</th><th>W</th><th>R</th><th>Win%</th><th>Mean$</th><th>LCB₉₅</th><th>vsR0</th></tr>`;
      for (const r of rows) {
        const wrCls = r.win_rate > 0.5 ? "bb-up" : "bb-down";
        const pnlCls = r.mean_net_pnl > 0 ? "bb-up" : r.mean_net_pnl < 0 ? "bb-down" : "";
        const lcbCls = r.lcb95 > 0 ? "bb-up" : r.lcb95 < 0 ? "bb-down" : "";
        const pairCls = r.paired_diff_r0 > 0 ? "bb-up" : r.paired_diff_r0 < 0 ? "bb-down" : "";
        html += `<tr>
          <td class="bb-dim">${RUNG_TABLE[r.rung] || r.rung}</td>
          <td>${r.direction === "up" ? "▲" : "▼"}</td>
          <td class="bb-dim">${r.windows}</td>
          <td class="bb-dim">${r.records}</td>
          <td class="${wrCls}">${(r.win_rate * 100).toFixed(1)}</td>
          <td class="${pnlCls}">${r.mean_net_pnl.toFixed(4)}</td>
          <td class="${lcbCls}">${r.lcb95.toFixed(4)}</td>
          <td class="${pairCls}">${r.paired_diff_r0 != null ? r.paired_diff_r0.toFixed(4) : "—"}</td>
        </tr>`;
      }
      html += `</table></div>`;
    } else if (status && status.exists) {
      html += '<div class="bb-dim" style="padding:4px 0">no settled records yet</div>';
    }

    box.innerHTML = html;
  }

  // Single delegated click handler for shadow panel (avoids listener leaks during poll)
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("#bb-shadow-replay");
    if (!btn || btn.disabled) return;
    shadowReplayBusy = true;
    renderShadowTab(shadowStatus, shadowReport);
    sendBg({ type: "RUN_SHADOW_REPLAY" }).then((res) => {
      shadowReplayBusy = false;
      const replayRes = res && res.state;
      const resultHtml = replayRes && replayRes.ok
        ? `<div class="bb-shadow-result" id="bb-shadow-result"><span class="bb-up">✓</span> ${replayRes.records} records (${replayRes.windows} windows)</div>`
        : `<div class="bb-shadow-result bb-shadow-result-err" id="bb-shadow-result">✗ ${(replayRes && replayRes.error) || (res && res.error) || "failed"}</div>`;
      const existing = document.getElementById("bb-shadow-result");
      if (existing) existing.outerHTML = resultHtml;
      else {
        const r = document.getElementById("bb-shadow-replay");
        if (r) r.insertAdjacentHTML("afterend", resultHtml);
      }
      maybeFetchShadowReport();
    });
  });

  function renderMarkets(rows, volAnnual) {
    const box = document.getElementById("bb-markets");
    document.getElementById("bb-vol").textContent =
      volAnnual != null ? "vol " + pct(volAnnual) : "—";

    if (!rows || !rows.length) {
      box.innerHTML = `<div class="bb-msg" style="display:block">no open windows in range</div>`;
      return;
    }

    box.innerHTML = rows
      .map((m) => {
        let edge = '<span class="bb-dim">—</span>';
        if (m.edge != null) {
          const cls = m.edge > 0 ? "bb-up" : "bb-down";
          const sign = m.edge > 0 ? "+" : "";
          edge = `<span class="${cls}">${sign}${(m.edge * 100).toFixed(1)}pp</span>`;
        }
        const slug = m.held
          ? `<span class="bb-held">● ${m.slug}</span>`
          : m.slug;
        const spread = m.up_bid != null && m.up_ask != null
          ? ` ${pct(m.up_bid)} / ${pct(m.up_ask)}`
          : "";
        const impossible =
          m.breakeven_up != null && m.breakeven_up >= 1.0
            ? `<div class="bb-warn">BE+fee exceeds $1 — cannot profit</div>`
            : "";
        return `
        <div class="bb-mkt">
          <div class="bb-slug">${slug}<span class="bb-left">${mmss(m.seconds_left)}</span></div>
          ${spread ? `<div class="bb-spread">spread${spread}</div>` : ""}
          <div class="bb-grid">
            <span>market</span><b>${pct(m.market_p_up)}</b>
            <span>model</span><b>${m.model_p_up == null ? "—" : pct(m.model_p_up)}</b>
            <span>edge</span><b>${edge}</b>
            <span>BE+fee</span><b>${pct(m.breakeven_up)}</b>
          </div>
          ${impossible}
        </div>`;
      })
      .join("");
  }

  function renderError(msg) {
    document.getElementById("bb-dot").className = "bb-dot bb-bad";
    document.getElementById("bb-msg").textContent = msg;
    document.getElementById("bb-msg").style.display = "block";
    document.getElementById("bb-markets").innerHTML = "";
    document.getElementById("bb-tabs").style.display = "none";
    document.getElementById("bb-paper-panel").innerHTML = "";
    document.getElementById("bb-live-panel").innerHTML = "";
  }

  function render(res) {
    if (!res || !res.ok) {
      renderError((res && res.error) || "no connection");
      return;
    }

    const s = res.state;
    document.getElementById("bb-dot").className = "bb-dot " + (s.running ? "bb-live" : "bb-idle");
    document.getElementById("bb-msg").style.display = "none";

    if (s.last_error) {
      const err = document.getElementById("bb-msg");
      err.textContent = "⚠ " + s.last_error;
      err.style.display = "block";
    }

    const volAnnual = s.markets && s.markets[0] ? s.markets[0].vol_annual : null;
    renderPortfolio(s.portfolio, s.mode);
    renderControls(s);
    renderMarkets(s.markets, volAnnual);
  }

  function fetchShadowStatus() {
    sendBg({ type: "GET_SHADOW_STATUS" }).then((res) => {
      if (res && res.ok && res.state) shadowStatus = res.state;
      renderShadowTab(shadowStatus, shadowReport);
    });
  }

  function maybeFetchShadowReport() {
    sendBg({ type: "GET_SHADOW_REPORT" }).then((res) => {
      if (res && res.ok && res.state && res.state.rows) shadowReport = res.state.rows;
      renderShadowTab(shadowStatus, shadowReport);
    });
  }

  function poll() {
    sendBg({ type: "GET_STATE" }).then((res) => {
      render(res);
      fetchShadowStatus();
      setTimeout(poll, 2000);
    });
  }

  poll();
})();
