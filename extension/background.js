(() => {
  const NATIVE_HOST = "btcbot.native";
  const RUNTIME = (typeof browser !== "undefined" ? browser : chrome).runtime;

  function nativeRequest(path, method = "GET", body = null) {
    return new Promise((resolve) => {
      const msg = { path, method };
      if (body) msg.body = body;
      const api = typeof browser !== "undefined" ? browser : chrome;
      const p = api.runtime.sendNativeMessage(NATIVE_HOST, msg);
      if (p && typeof p.then === "function") {
        p.then((resp) => {
          resolve(resp && resp.ok ? { ok: true, state: resp.data } : (resp || { ok: false, error: "no response" }));
        }).catch((e) => {
          resolve({ ok: false, error: `native host: ${e.message}` });
        });
      } else {
        api.runtime.sendNativeMessage(NATIVE_HOST, msg, (resp) => {
          const err = api.runtime.lastError;
          resolve(err ? { ok: false, error: `native host: ${err.message}` } : (resp && resp.ok ? { ok: true, state: resp.data } : { ok: false, error: "no response" }));
        });
      }
    });
  }

  RUNTIME.onMessage.addListener((msg, _sender) => {
    if (msg && msg.type === "GET_STATE") {
      return nativeRequest("/api/state");
    }
    if (msg && msg.type === "START_PAPER") {
      return nativeRequest("/api/paper/start", "POST");
    }
    if (msg && msg.type === "STOP_PAPER") {
      return nativeRequest("/api/paper/stop", "POST");
    }
    if (msg && msg.type === "GET_SHADOW_STATUS") {
      return nativeRequest("/api/shadow/status");
    }
    if (msg && msg.type === "GET_SHADOW_REPORT") {
      return nativeRequest("/api/shadow/report");
    }
    if (msg && msg.type === "RUN_SHADOW_REPLAY") {
      return nativeRequest("/api/shadow/replay", "POST");
    }
    return undefined;
  });
})();
