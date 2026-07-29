// Injects a small read-only panel on kalshi.com showing what the local btcbot
// model thinks about the open KXBTC15M window.
//
// It reads. It does not click anything, fill anything, or place anything. If
// you want to act on what it says, you do that yourself in the Kalshi UI.

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
      <div id="bb-markets"></div>
      <div class="bb-foot">read-only · places no orders</div>
    </div>`;
  document.documentElement.appendChild(panel);

  document.getElementById("bb-min").addEventListener("click", () => {
    panel.classList.toggle("bb-collapsed");
  });

  const pct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
  const mmss = (s) => {
    if (s == null) return "—";
    s = Math.max(0, Math.round(s));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  };

  function render(res) {
    const msg = document.getElementById("bb-msg");
    const box = document.getElementById("bb-markets");
    const dot = document.getElementById("bb-dot");

    if (!res || !res.ok) {
      dot.className = "bb-dot bb-bad";
      msg.textContent = (res && res.error) || "no connection";
      msg.style.display = "block";
      box.innerHTML = "";
      return;
    }

    const s = res.state;
    dot.className = "bb-dot " + (s.running ? "bb-live" : "bb-idle");
    document.getElementById("bb-vol").textContent =
      "vol " + (s.markets && s.markets[0] ? pct(s.markets[0].vol_annual) : "—");

    const rows = s.markets || [];
    if (!rows.length) {
      msg.textContent = "no open windows in range";
      msg.style.display = "block";
      box.innerHTML = "";
      return;
    }
    msg.style.display = "none";

    box.innerHTML = rows
      .map((m) => {
        let edge = '<span class="bb-dim">no vol</span>';
        if (m.edge != null) {
          const cls = m.edge > 0 ? "bb-up" : "bb-down";
          const sign = m.edge > 0 ? "+" : "";
          edge = `<span class="${cls}">${sign}${(m.edge * 100).toFixed(1)}pp</span>`;
        }
        const impossible =
          m.breakeven_up != null && m.breakeven_up >= 1.0
            ? `<div class="bb-warn">break-even ${pct(m.breakeven_up)} — entry + fee exceeds the $1 payout, cannot profit at any win rate</div>`
            : "";
        return `
        <div class="bb-mkt">
          <div class="bb-slug">${m.slug}<span class="bb-left">${mmss(m.seconds_left)}</span></div>
          <div class="bb-grid">
            <span>market</span><b>${pct(m.market_p_up)}</b>
            <span>model</span><b>${m.model_p_up == null ? "—" : pct(m.model_p_up)}</b>
            <span>edge</span><b>${edge}</b>
            <span>BE + fee</span><b>${pct(m.breakeven_up)}</b>
          </div>
          ${impossible}
        </div>`;
      })
      .join("");
  }

  function poll() {
    try {
      chrome.runtime.sendMessage({ type: "GET_STATE" }, (res) => {
        if (chrome.runtime.lastError) {
          render({ ok: false, error: "extension reloading…" });
          return;
        }
        render(res);
      });
    } catch (e) {
      render({ ok: false, error: "extension context lost — reload the page" });
    }
  }

  poll();
  setInterval(poll, 2000);
})();
