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
      <div id="bb-portfolio" style="display:none"></div>
      <div id="bb-controls" style="display:none"></div>
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

  function renderPortfolio(p) {
    const el = document.getElementById("bb-portfolio");
    if (!p || !p.active) {
      el.style.display = "none";
      return;
    }
    el.style.display = "block";
    const pnlCls = p.pnl > 0 ? "bb-up" : p.pnl < 0 ? "bb-down" : "";
    const posDetail = (p.positions || []).length
      ? p.positions.map((x) => `${x.side} ${x.shares.toFixed(1)}`).join(" · ")
      : "none open";
    el.innerHTML = `
      <div class="bb-portfolio-row">
        <span class="${pnlCls}">P&L ${usd(p.pnl)}</span>
        <span class="bb-dim">eq ${usd(p.equity)}</span>
        <span class="bb-dim">${p.n_positions} pos</span>
      </div>
      <div class="bb-pos-detail">${posDetail}</div>`;
  }

  function renderControls(s) {
    const el = document.getElementById("bb-controls");
    const running = s && s.running;
    el.style.display = "block";
    el.innerHTML = `
      <div class="bb-status-line">
        <span class="bb-status-dot ${running ? "bb-live" : "bb-idle"}"></span>
        <span class="bb-dim">${running ? "paper · " + s.ticks + " ticks" : "paper idle"}</span>
        <span class="bb-grow"></span>
        ${running
          ? `<button class="bb-btn bb-btn-stop" id="bb-stop">stop</button>`
          : `<button class="bb-btn bb-btn-start" id="bb-start">start</button>`}
      </div>`;
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
    document.getElementById("bb-portfolio").style.display = "none";
    document.getElementById("bb-controls").style.display = "none";
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
    renderPortfolio(s.portfolio);
    renderControls(s);
    renderMarkets(s.markets, volAnnual);
  }

  function poll() {
    sendBg({ type: "GET_STATE" }).then((res) => {
      render(res);
      setTimeout(poll, 2000);
    });
  }

  poll();
})();
