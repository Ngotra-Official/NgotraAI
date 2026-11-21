// Announce the connector to the platform page and bridge one-click connects.
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

// Let the page know a connection was just completed so it can refresh status.
chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === "ngotra-connected") {
    document.dispatchEvent(new CustomEvent("ngotra-connected"));
  }
});

document.dispatchEvent(new CustomEvent("ngotra-connector-ready"));
