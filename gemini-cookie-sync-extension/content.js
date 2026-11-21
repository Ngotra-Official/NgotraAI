// Bridge between the platform page and the extension.
window.__NGOTRA_GEMINI_CONNECTOR__ = true;

document.addEventListener("ngotra-connect-request", () => {
  chrome.runtime.sendMessage(
    { type: "ngotra-connect", origin: location.origin },
    (response) => {
      document.dispatchEvent(new CustomEvent("ngotra-connect-result", {
        detail: response || { ok: false, message: "No response from connector." },
      }));
    }
  );
});

// The extension popup/service worker cannot send our SameSite login cookie
// from a chrome-extension:// origin. The page can, so all platform API calls
// are proxied through this content script (same-origin fetch).
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "ngotra-api") {
    (async () => {
      try {
        const init = {
          method: message.method || "GET",
          credentials: "same-origin",
          headers: Object.assign({}, message.headers || {}),
        };
        if (message.body !== undefined) {
          init.headers["Content-Type"] = "application/json";
          init.body = JSON.stringify(message.body);
        }
        const res = await fetch(message.path, init);
        let data = null;
        try { data = await res.json(); } catch (e) { data = null; }
        sendResponse({ ok: res.ok, status: res.status, data: data });
      } catch (e) {
        sendResponse({ ok: false, status: 0, data: null, error: e.message || String(e) });
      }
    })();
    return true; // async response
  }

  if (message && message.type === "ngotra-connected") {
    document.dispatchEvent(new CustomEvent("ngotra-connected"));
  }
});

document.dispatchEvent(new CustomEvent("ngotra-connector-ready"));
