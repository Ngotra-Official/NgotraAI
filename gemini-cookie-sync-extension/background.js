// Background service worker: reads the signed-in Gemini session and connects
// it to the platform when the site asks (one-click flow).

const COOKIE_ORDER = [
  "SID", "HSID", "SSID", "APISID", "SAPISID", "LSID", "OSID", "SIDCC", "AEC", "NID",
  "COMPASS", "__Secure-1PAPISID", "__Secure-1PSID", "__Secure-1PSIDTS",
  "__Secure-1PSIDCC", "__Secure-3PAPISID", "__Secure-3PSID", "__Secure-3PSIDTS",
  "__Secure-3PSIDCC", "__Secure-OSID"
];
const CORE = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];

async function readBestGeminiCookies() {
  const stores = await chrome.cookies.getAllCookieStores();
  const best = new Map();
  for (const store of stores) {
    const cookies = await chrome.cookies.getAll({ storeId: store.id });
    for (const c of cookies) {
      if (!c.value) continue;
      const domain = (c.domain || "").replace(/^\./, "").toLowerCase();
      if (domain !== "google.com" && !domain.endsWith(".google.com")) continue;
      const prev = best.get(c.name);
      if (!prev || (c.path === "/" && prev.path !== "/") || (c.secure && !prev.secure)) {
        best.set(c.name, c);
      }
    }
  }
  const hasCore = CORE.every((n) => best.has(n));
  const hasSession = SESSION_ALTERNATIVES.some((n) => best.has(n));
  if (!hasCore || !hasSession) return null;
  return COOKIE_ORDER
    .filter((n) => best.has(n))
    .map((n) => `${n}=${best.get(n).value}`)
    .join("; ");
}

// Send the API call through the platform page's content script so our SameSite
// login cookie is included. A service worker fetch cannot send that cookie.
async function detectAuthUser() {
  try {
    const tabs = await new Promise((resolve) => {
      chrome.tabs.query({ url: "https://gemini.google.com/*" }, (t) => resolve(t || []));
    });
    for (const tab of tabs) {
      const m = (tab.url || "").match(/\/u\/(\d+)(?:\/|$)/);
      if (m) return m[1];
    }
  } catch (e) {}
  return "";
}

function connectToPlatform(tabId, cookie, authUser) {
  return new Promise((resolve) => {
    try {
      chrome.tabs.sendMessage(
        tabId,
        { type: "ngotra-api", path: "/api/user/cookie", method: "POST", body: { cookie, auth_user: authUser } },
        (res) => {
          if (chrome.runtime && chrome.runtime.lastError) {
            resolve({ ok: false, message: "Reload the Ngotra tab to activate the connector." });
          } else {
            const data = (res && res.data) || {};
            resolve({
              ok: !!(res && res.ok),
              message: data.message || (data.detail && data.detail.message) ||
                (res && res.error) || `HTTP ${res ? res.status : 0}`,
            });
          }
        }
      );
    } catch (e) {
      resolve({ ok: false, message: e.message || String(e) });
    }
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "ngotra-connect") return false;

  (async () => {
    try {
      const cookie = await readBestGeminiCookies();
      const authUser = await detectAuthUser();
      if (!cookie) {
        sendResponse({
          ok: false,
          message: "No Gemini session found. Open gemini.google.com, sign in, then try again.",
          openGemini: true,
        });
        return;
      }
      const tabId = sender.tab && sender.tab.id;
      let result = tabId
        ? await connectToPlatform(tabId, cookie, authUser)
        : { ok: false, message: "Open the Ngotra website and keep it signed in." };
      // Tell the platform tab to refresh its connection status immediately.
      if (result.ok && sender.tab && sender.tab.id) {
        try {
          chrome.tabs.sendMessage(sender.tab.id, { type: "ngotra-connected" });
        } catch (e) { /* tab may have closed */ }
      }
      sendResponse(result);
    } catch (e) {
      sendResponse({ ok: false, message: e.message || String(e) });
    }
  })();

  return true; // keep message channel open for async response
});
