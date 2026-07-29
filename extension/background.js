// Fetches state from the local btcbot server.
//
// The content script cannot reach http://127.0.0.1 from an https://kalshi.com
// page -- that is mixed content, and the page's own CSP would block it anyway.
// The service worker has host_permissions and is not bound by the page CSP, so
// it does the fetching and passes results back over runtime messaging.
//
// This only ever reads. There is no message this worker accepts that places an
// order, and the server it talks to has no order-placing path either.

const ENDPOINT = "http://127.0.0.1:8787/api/state";

async function fetchState() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    const resp = await fetch(ENDPOINT, {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!resp.ok) {
      return { ok: false, error: `server returned ${resp.status}` };
    }
    return { ok: true, state: await resp.json() };
  } catch (err) {
    return {
      ok: false,
      error:
        err.name === "AbortError"
          ? "timed out — is `python -m btcbot serve` running?"
          : "cannot reach btcbot — run `python -m btcbot serve`",
    };
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "GET_STATE") {
    fetchState().then(sendResponse);
    return true; // keep the channel open for the async reply
  }
  return false;
});
