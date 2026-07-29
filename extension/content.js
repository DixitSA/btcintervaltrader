(() => {
  const PANEL_ID = "btcbot-overlay";
  if (document.getElementById(PANEL_ID)) return;
  let selectedFamily = "All";
  let shadowFamily = "All";

  const panel = document.createElement("div");
  panel.id = PANEL_ID;
  panel.innerHTML = `
    <div class="bb-head">
      <span class="bb-dot" id="bb-dot" aria-hidden="true"></span>
      <span class="bb-title">btcbot</span>
      <span class="bb-grow"></span>
      <span class="bb-vol" id="bb-vol">—</span>
      <button class="bb-min" id="bb-min" aria-label="Collapse panel">–</button>
    </div>
    <div class="bb-body" id="bb-body">
      <div class="bb-msg" id="bb-msg" role="status">Connecting to bot…</div>
      <div class="bb-tabs" id="bb-tabs" style="display:none" role="tablist">
        <button class="bb-tab bb-tab-active" data-tab="paper" role="tab" aria-selected="true" aria-controls="bb-paper-panel">Paper</button>
        <button class="bb-tab" data-tab="live" role="tab" aria-selected="false" aria-controls="bb-live-panel">Live</button>
      </div>
      <div id="bb-paper-panel" role="tabpanel" aria-live="polite"></div>
      <div id="bb-live-panel" style="display:none" role="tabpanel" aria-live="polite"></div>
      <div class="bb-filter-bar" id="bb-family-filter"></div>
      <div id="bb-markets" aria-live="polite"></div>
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
    document.querySelectorAll(".bb-tab").forEach((t) => {
      t.classList.remove("bb-tab-active");
      t.setAttribute("aria-selected", "false");
    });
    tab.classList.add("bb-tab-active");
    tab.setAttribute("aria-selected", "true");
    const tt = tab.dataset.tab;
    document.getElementById("bb-paper-panel").style.display = tt === "paper" ? "" : "none";
    document.getElementById("bb-live-panel").style.display = tt === "live" ? "" : "none";
  });

  // Rewriting identical markup every 2s repaints the overlay, collapses any
  // open <details>, and drops scroll position mid-read. Compare against the
  // last string we wrote rather than against the DOM, so markup other code
  // appends afterwards (the start/stop button) does not force a rewrite.
  const _lastHtml = Object.create(null);
  function setHTML(el, key, html) {
    if (!el || _lastHtml[key] === html) return false;
    _lastHtml[key] = html;
    el.innerHTML = html;
    return true;
  }
  const setText = (el, v) => { if (el && el.textContent !== v) el.textContent = v; };

  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
  const usd = (v) => (v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2));
  const mmss = (s) => {
    if (s == null) return "—";
    s = Math.max(0, Math.round(s));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  };

  const FAMILIES = ["All", "BTC", "ETH", "SOL"];

  function renderFamilyFilter() {
    const bar = document.getElementById("bb-family-filter");
    if (!bar) return "";
    const key = "family-filter";
    const html = FAMILIES.map((f) =>
      `<button class="bb-pill${f === selectedFamily ? " bb-pill-on" : ""}" data-fam="${f}">${f}</button>`
    ).join("");
    setHTML(bar, key, html);
  }

  function renderShadowFilter() {
    const bar = document.getElementById("bb-shadow-filter");
    if (!bar) return "";
    const key = "shadow-filter";
    const html = FAMILIES.map((f) =>
      `<button class="bb-pill${f === shadowFamily ? " bb-pill-on" : ""}" data-shadow="${f}">${f}</button>`
    ).join("");
    setHTML(bar, key, html);
  }

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

  const clockTime = (ts) => (ts == null ? "—"
    : new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false }));
  const heldStr = (s) => {
    if (s == null) return "—";
    s = Math.round(s);
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
  };

  // Trade history, newest first. The overlay is narrow, so each trade gets two
  // lines rather than a wide table: the verdict and money on top, the mechanics
  // underneath. Every field the panel shows is here too.
  function tradeHistoryHtml(trades, truncated) {
    if (!trades || !trades.length) {
      return `<div class="bb-section-label">Trade history</div>
        <div class="bb-dim" style="padding:4px 0">no closed trades yet</div>`;
    }
    const wins = trades.filter((t) => t.won).length;
    const net = trades.reduce((a, t) => a + t.pnl, 0);
    const netCls = net > 0 ? "bb-up" : net < 0 ? "bb-down" : "";

    let html = `<div class="bb-section-label">Trade history</div>
      <div class="bb-trade-summary">
        <span>${trades.length} closed</span>
        <span class="bb-up">${wins} won</span>
        <span class="bb-down">${trades.length - wins} lost</span>
        <span class="bb-dim">${((wins / trades.length) * 100).toFixed(0)}%</span>
        <span class="bb-grow"></span>
        <span class="${netCls}">net ${usd(net)}</span>
      </div>
      <div class="bb-trade-list">`;

    for (const t of trades) {
      const cls = t.won ? "bb-up" : "bb-down";
      const ret = t.return_pct == null ? "—" : (t.return_pct * 100).toFixed(1) + "%";
      // Avoid "settled · settled Up" when the reason is already the settlement.
      const reason = !t.outcome ? t.exit_reason
        : t.exit_reason === "settled" ? `settled ${t.outcome}`
        : `${t.exit_reason} · settled ${t.outcome}`;
      html += `<div class="bb-trade">
        <div class="bb-trade-top">
          <span class="${cls} bb-trade-badge">${t.won ? "WON" : "LOST"}</span>
          <span class="bb-trade-side">${t.side}</span>
          <span class="bb-grow"></span>
          <span class="${cls} bb-trade-pnl">${usd(t.pnl)}</span>
          <span class="${cls} bb-dim">${ret}</span>
        </div>
        <div class="bb-trade-mid">
          entered ${(t.entry_price * 100).toFixed(1)}¢ → exited ${(t.exit_price * 100).toFixed(1)}¢
          · ${t.shares.toFixed(1)} shares · ${usd(t.cost_basis)} bet
          · ${usd(t.fees_paid)} fees
        </div>
        <div class="bb-trade-bot">
          ${clockTime(t.exit_ts)} · held ${heldStr(t.held_seconds)} · ${reason}
        </div>
        <div class="bb-trade-slug">${t.slug}</div>
      </div>`;
    }
    html += `</div>`;
    if (truncated) {
      html += `<div class="bb-dim" style="font-size:9px;padding:2px 0">
        showing ${trades.length} most recent of ${trades.length + truncated}</div>`;
    }
    return html;
  }

  function renderPortfolio(p, mode) {
    const paperPanel = document.getElementById("bb-paper-panel");
    const livePanel = document.getElementById("bb-live-panel");
    document.getElementById("bb-tabs").style.display = "flex";

    if (!p || !p.active) {
      setHTML(paperPanel, "paper", `<div class="bb-section-label">Paper Trading</div><div class="bb-dim" style="padding:6px 0">not started</div><div id="bb-shadow-section"></div>`);
      renderLivePanel(livePanel, p, mode);
      return;
    }

    const pnlCls = p.pnl > 0 ? "bb-up" : p.pnl < 0 ? "bb-down" : "";
    const posDetail = (p.positions || []).length
      ? p.positions.map((x) => `${x.side} ${x.shares.toFixed(1)} @ ${(x.entry_price * 100).toFixed(1)}¢`).join(" · ")
      : "none open";

    setHTML(paperPanel, "paper", `
      <div class="bb-section-label">Paper Trading</div>
      <div class="bb-panel-grid">
        <div>
          <div class="bb-portfolio-row">
            <span class="${pnlCls}" style="font-size:15px;font-weight:650">P&L ${usd(p.pnl)}</span>
            <span class="bb-dim">value ${usd(p.equity)}</span>
          </div>
          <div class="bb-pos-detail">${posDetail}</div>
          <div class="bb-dim" style="font-size:9px;padding-top:1px;line-height:1.3">
            Floating — moves with the market. Not final until window ends.
          </div>
        </div>
        <div class="bb-panel-stats">
          <span class="bb-dim">${p.closed_trades} closed (${p.wins} won, ${p.losses} lost)</span>
          <span class="bb-dim">locked in: ${usd(p.realized_pnl)}</span>
        </div>
      </div>
      <div class="bb-status-line">
        <span class="bb-status-dot ${p.active ? "bb-live" : "bb-idle"}"></span>
        <span class="bb-dim">${p.active ? "running" : "idle"}</span>
        <span class="bb-grow"></span>
      </div>
      ${tradeHistoryHtml(p.trades, p.trades_truncated)}
      <div id="bb-shadow-section"></div>`);

    renderLivePanel(livePanel, p, mode);
  }

  function renderLivePanel(livePanel, p, mode) {
    const isLive = mode === "live";
    if (!isLive) {
      setHTML(livePanel, "live", `
        <div class="bb-section-label">Live Trading</div>
        <div class="bb-live-off">
          <div class="bb-live-off-title">Not trading real money</div>
          <div class="bb-dim">config mode: <b>${mode || "paper"}</b></div>
          <div class="bb-dim" style="margin-top:6px">
            Live requires <b>mode: live</b> in config.yaml and
            <b>BTCBOT_I_UNDERSTAND_REAL_MONEY=yes</b>.
          </div>
        </div>
        <div class="bb-foot" style="padding-top:6px">
          This panel never places orders. It is a read-only view.
        </div>`);
      return;
    }
    const pnlCls = p && p.pnl > 0 ? "bb-up" : p && p.pnl < 0 ? "bb-down" : "";
    setHTML(livePanel, "live", `
      <div class="bb-section-label">Live Trading</div>
      <div class="bb-live-on">REAL MONEY — mode: live</div>
      <div class="bb-panel-grid">
        <div class="bb-portfolio-row">
          <span class="${pnlCls}" style="font-size:15px;font-weight:650">P&L ${usd(p && p.pnl)}</span>
          <span class="bb-dim">eq ${usd(p && p.equity)}</span>
        </div>
        <div class="bb-panel-stats">
          <span class="bb-dim">closed: ${(p && p.closed_trades) ?? 0} (${(p && p.wins) ?? 0}W / ${(p && p.losses) ?? 0}L)</span>
          <span class="bb-dim">realized: ${usd(p && p.realized_pnl)}</span>
        </div>
      </div>`);
  }

  function renderControls(s) {
    const paperPanel = document.getElementById("bb-paper-panel");
    const running = s && s.running;
    const existingBtn = paperPanel.querySelector(".bb-btn");
    // Match the button to the CURRENT state. "Any button exists" was only ever
    // safe because the panel was wiped every poll; now that it is cached, that
    // test would freeze the button on whichever state it first rendered.
    const wantId = running ? "bb-stop" : "bb-start";
    if (existingBtn && existingBtn.id === wantId) return;
    if (existingBtn) existingBtn.remove();
    paperPanel.insertAdjacentHTML("beforeend",
      running
        ? `<button class="bb-btn bb-btn-stop" id="bb-stop" aria-label="Stop paper trading">stop paper</button>`
        : `<button class="bb-btn bb-btn-start" id="bb-start" aria-label="Start paper trading">start paper</button>`
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
  const DIR_LABEL = { follow: "With trend", fade: "Against trend" };
  const DIR_SHORT = { follow: "Trend", fade: "Counter" };

  // Unit size for the "what would I have made" readout. Shadow records are
  // logged at a fixed notional (usually $1); P&L is exactly linear in
  // notional, so rescaling is exact rather than an estimate.
  let unitUsd = Number(localStorage.getItem("bb-unit-usd")) || 5;

  const scaleToUnit = (row) => {
    const notional = row.notional_usd || 1;
    return (row.total_net_pnl / notional) * unitUsd;
  };

  function renderShadowTab(status, rows) {
    const box = document.getElementById("bb-shadow-section");
    if (!box) return;

    // Summary bar
    let html = '<div class="bb-section-label">Simulated history</div>';
    if (status && status.exists) {
      html += `<div class="bb-shadow-summary">
        <span>${status.records} trades</span>
        <span>${status.settled} resolved</span>
        <span>${status.windows} rounds</span>
        <span class="bb-dim">${status.unsettled_slugs} pending</span>
      </div>`;
    } else {
      html += '<div class="bb-dim" style="padding:4px 0">no simulation data yet</div>';
    }

    // Replay button
    const replayDisabled = shadowReplayBusy ? " disabled" : "";
    html += `<div class="bb-shadow-actions">
      <button class="bb-btn${replayDisabled}" id="bb-shadow-replay">${shadowReplayBusy ? "Running…" : "Run simulation"}</button>
    </div>`;

    // Replay result
    const resultEl = document.getElementById("bb-shadow-result");
    if (resultEl) html += resultEl.outerHTML;

    // Shadow asset filter pills.
    html += `<div class="bb-filter-bar" id="bb-shadow-filter">${FAMILIES.map((f) =>
      `<button class="bb-pill${f === shadowFamily ? " bb-pill-on" : ""}" data-shadow="${f}">${f}</button>`
    ).join("")}</div>`;

    // "What would I have made" -- per strategy, at the chosen unit size.
    if (rows && rows.length) {
      const filteredRows = shadowFamily === "All"
        ? rows
        : rows.filter((r) => (r.family || "BTC").toUpperCase() === shadowFamily);
      if (!filteredRows.length) {
        setHTML(box, "shadow", `<div class="bb-dim" style="padding:4px 0">No data for ${shadowFamily}</div>`);
        return;
      }
      // Rank on LCB95, not on raw dollars. One longshot win at a 1c entry
      // returns ~100x notional and would otherwise top the table forever on a
      // sample of seven windows. LCB is the number that survives variance.
      const ranked = filteredRows.slice().sort((a, b) => b.lcb95 - a.lcb95);
      const best = ranked[0];
      const bestUsd = scaleToUnit(best);
      const proven = best.lcb95 > 0;
      const bestCls = !proven ? "bb-dim" : bestUsd > 0 ? "bb-up" : "bb-down";
      const thinSample = best.windows < 30;

      html += `<div class="bb-unit-bar">
        <span class="bb-dim">unit size</span>
        <button class="bb-unit-btn" id="bb-unit-dec">–</button>
        <span class="bb-unit-val">$${unitUsd}</span>
        <button class="bb-unit-btn" id="bb-unit-inc">+</button>
        <span class="bb-grow"></span>
        <span class="bb-dim">per trade</span>
      </div>`;

      html += `<div class="bb-would-make">
        <div class="bb-dim">Best strategy so far (highest confidence)</div>
        <div class="bb-would-headline ${bestCls}">${usd(bestUsd)}</div>
        <div class="bb-dim">
          ${RUNG_TABLE[best.rung] || best.rung} · ${DIR_LABEL[best.direction] || best.direction}
          · ${best.windows} rounds · ${(best.win_rate * 100).toFixed(1)}% won
        </div>
        ${proven
          ? ""
          : `<div class="bb-unproven">Not reliable yet — confidence score is
             ${best.lcb95.toFixed(3)}. Treat this as a guess.</div>`}
        ${thinSample
          ? `<div class="bb-unproven">Only ${best.windows} rounds so far. Need 100+ for
             meaningful results.</div>`
          : ""}
      </div>`;

      // The overlay is ~380px wide; a 7-column table plus caveat swamps it.
      // Keep the headline always visible, put the breakdown behind a
      // disclosure. Cached rendering means the open state now survives polls.
      html += `<details class="bb-details"><summary>Details by entry timing (${filteredRows.length} strategies)</summary>`;
      html += `<div class="bb-shadow-table-wrap"><table class="bb-shadow-table">
        <tr><th>Asset</th><th>Timing</th><th>Side</th><th>Rounds</th><th>Won</th><th>P&L</th><th>Confidence</th><th>vs fastest</th></tr>`;
      for (const r of filteredRows) {
        const wrCls = r.win_rate > 0.5 ? "bb-up" : "bb-down";
        const dollars = scaleToUnit(r);
        const pnlCls = dollars > 0 ? "bb-up" : dollars < 0 ? "bb-down" : "";
        const lcbCls = r.lcb95 > 0 ? "bb-up" : r.lcb95 < 0 ? "bb-down" : "";
        const pairCls = r.paired_diff_r0 > 0 ? "bb-up" : r.paired_diff_r0 < 0 ? "bb-down" : "";
        const recFam = (r.family || "BTC").toUpperCase();
        html += `<tr>
          <td class="bb-dim"><b>${recFam}</b></td>
          <td class="bb-dim">${RUNG_TABLE[r.rung] || r.rung}</td>
          <td class="bb-dim">${DIR_SHORT[r.direction] || r.direction}</td>
          <td class="bb-dim">${r.windows}</td>
          <td class="${wrCls}">${(r.win_rate * 100).toFixed(1)}</td>
          <td class="${pnlCls}"><b>${usd(dollars)}</b></td>
          <td class="${lcbCls}">${r.lcb95.toFixed(4)}</td>
          <td class="${pairCls}">${r.paired_diff_r0 != null ? r.paired_diff_r0.toFixed(4) : "—"}</td>
        </tr>`;
      }
      html += `</table></div>`;
      html += `<div class="bb-caveat">
        Each row is a <b>separate</b> approach — you can only pick one per round.
        Don't add them up. Sample size is <b>rounds</b>.
      </div></details>`;
    } else if (status && status.exists) {
      html += '<div class="bb-dim" style="padding:4px 0">no settled records yet</div>';
    }

    setHTML(box, "shadow", html);
  }

  const UNIT_STEPS = [1, 2, 5, 10, 25, 50, 100];

  // Delegated so the poll loop can re-render freely without leaking listeners.
  document.addEventListener("click", (e) => {
    const famBtn = e.target.closest("[data-fam]");
    if (famBtn) {
      selectedFamily = famBtn.dataset.fam;
      renderFamilyFilter();
      return;
    }
    const shdBtn = e.target.closest("[data-shadow]");
    if (shdBtn) {
      shadowFamily = shdBtn.dataset.shadow;
      renderShadowFilter();
      return;
    }

    const unitBtn = e.target.closest("#bb-unit-inc, #bb-unit-dec");
    if (unitBtn) {
      const i = UNIT_STEPS.indexOf(unitUsd);
      const cur = i === -1 ? UNIT_STEPS.indexOf(5) : i;
      const next = unitBtn.id === "bb-unit-inc"
        ? Math.min(cur + 1, UNIT_STEPS.length - 1)
        : Math.max(cur - 1, 0);
      unitUsd = UNIT_STEPS[next];
      try { localStorage.setItem("bb-unit-usd", String(unitUsd)); } catch (_) {}
      renderShadowTab(shadowStatus, shadowReport);
      return;
    }

    const btn = e.target.closest("#bb-shadow-replay");
    if (!btn || btn.disabled) return;
    shadowReplayBusy = true;
    renderShadowTab(shadowStatus, shadowReport);
    sendBg({ type: "RUN_SHADOW_REPLAY" }).then((res) => {
      shadowReplayBusy = false;
      const replayRes = res && res.state;
      const resultHtml = replayRes && replayRes.ok
        ? `<div class="bb-shadow-result" id="bb-shadow-result"><span class="bb-up">✓</span> ${replayRes.records} trades (${replayRes.windows} rounds)</div>`
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
    setText(document.getElementById("bb-vol"),
      volAnnual != null ? "volatility " + pct(volAnnual) : "—");

    // Build filter bar.
    const filterBox = document.getElementById("bb-family-filter");
    if (filterBox) renderFamilyFilter();

    if (!rows || !rows.length) {
      setHTML(box, "markets", `  <div class="bb-msg" style="display:block">No markets open right now</div>`);
      return;
    }

    const fam = (m) => (m.family || "BTC").toUpperCase();
    const filtered = selectedFamily === "All" ? rows : rows.filter((m) => fam(m) === selectedFamily);

    if (!filtered.length) {
      setHTML(box, "markets", `<div class="bb-msg" style="display:block">No ${selectedFamily} windows open</div>`);
      return;
    }

    setHTML(box, "markets", filtered
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
          ? ` bid ${pct(m.up_bid)} / ask ${pct(m.up_ask)}`
          : "";
        const impossible =
          m.breakeven_up != null && m.breakeven_up >= 1.0
            ? `<div class="bb-warn">Break-even above $1 — can't profit</div>`
            : "";
        return `
        <div class="bb-mkt">
          <div class="bb-slug"><span class="bb-asset">${fam(m)}</span> ${slug}<span class="bb-left">${mmss(m.seconds_left)}</span></div>
          ${spread ? `<div class="bb-spread">bid/ask${spread}</div>` : ""}
          <div class="bb-grid">
            <span>Market odds</span><b>${pct(m.market_p_up)}</b>
            <span>Bot's view</span><b>${m.model_p_up == null ? "—" : pct(m.model_p_up)}</b>
            <span>Advantage</span><b>${edge}</b>
            <span>Break-even</span><b>${pct(m.breakeven_up)}</b>
          </div>
          ${impossible}
        </div>`;
      })
      .join(""));
  }

  function renderError(msg) {
    const dot = document.getElementById("bb-dot");
    dot.className = "bb-dot bb-bad";
    dot.setAttribute("aria-label", "Cannot connect to server");
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
    const dot = document.getElementById("bb-dot");
    const dotStatus = s.running ? "bb-live" : "bb-idle";
    dot.className = "bb-dot " + dotStatus;
    dot.setAttribute("aria-label", s.running ? "Paper trading is running" : "Paper trading is idle");
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

  // The report is heavier than the status ping, so fetch it less often. Without
  // this it only loaded after a manual replay, leaving the P&L readout blank.
  let pollCount = 0;
  function poll() {
    sendBg({ type: "GET_STATE" }).then((res) => {
      render(res);
      fetchShadowStatus();
      if (pollCount % 5 === 0) maybeFetchShadowReport();
      pollCount += 1;
      setTimeout(poll, 2000);
    });
  }

  poll();
})();
