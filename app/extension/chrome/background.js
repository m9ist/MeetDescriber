const HOST_NAME = "com.for_meets.host";

console.log("[for_meets] service worker started");

let port = null;
let meetTabs = new Set();

function connect() {
  console.log("[for_meets] connecting to native host...");
  port = chrome.runtime.connectNative(HOST_NAME);

  port.onMessage.addListener((msg) => {
    console.log("[for_meets] host message:", msg);
  });

  console.log("[for_meets] port created, waiting for messages");

  port.onDisconnect.addListener(() => {
    const err = chrome.runtime.lastError;
    console.error("[for_meets] disconnect:", err ? err.message : "unknown reason");
    port = null;
    setTimeout(connect, 5000);
  });
}

function send(msg) {
  if (!port) {
    console.warn("[for_meets] send skipped — no port, msg:", msg.type);
    return;
  }
  try {
    port.postMessage(msg);
  } catch (e) {
    console.error("[for_meets] send error:", e);
  }
}

async function getAllTabs() {
  const tabs = await chrome.tabs.query({});
  return tabs.map(t => ({ id: t.id, title: t.title || "", url: t.url || "" }));
}

function isMeetUrl(url) {
  return url && url.includes("meet.google.com/");
}

// Слушаем обновления вкладок
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  console.log("[for_meets] tab updated:", tabId, changeInfo.status, tab.url);
  if (changeInfo.status !== "complete") return;

  const wasMeet = meetTabs.has(tabId);
  const isMeet = isMeetUrl(tab.url);
  console.log("[for_meets] isMeet:", isMeet, "wasMeet:", wasMeet, "url:", tab.url);

  if (isMeet && !wasMeet) {
    meetTabs.add(tabId);
    const tabs = await getAllTabs();
    console.log("[for_meets] meet detected, sending meet_started");
    send({ type: "meet_started", tab_id: tabId, title: tab.title || "", tabs });
  } else if (!isMeet && wasMeet) {
    meetTabs.delete(tabId);
    send({ type: "meet_ended", tab_id: tabId });
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (meetTabs.has(tabId)) {
    meetTabs.delete(tabId);
    send({ type: "meet_ended", tab_id: tabId });
  }
});

// Отвечаем на запросы списка вкладок (из Python через port)
chrome.runtime.onMessage.addListener(async (msg, sender, sendResponse) => {
  if (msg.type === "get_tabs") {
    const tabs = await getAllTabs();
    sendResponse({ type: "tabs", tabs });
  }
  return true;
});

// Обрабатываем входящие запросы от хоста
// (хост может попросить список вкладок через port)
function handleHostRequest(msg) {
  if (msg.type === "get_tabs") {
    getAllTabs().then(tabs => send({ type: "tabs", tabs }));
  } else if (msg.type === "ping") {
    send({ type: "pong" });
  }
}

connect();
