const HOST_NAME = "com.for_meets.host";

let port = null;
let meetTabs = new Set();

function connect() {
  port = chrome.runtime.connectNative(HOST_NAME);

  port.onMessage.addListener((msg) => {
    // Ответы от Python хоста — пока только логируем
    console.log("[for_meets] host response:", msg);
  });

  port.onDisconnect.addListener(() => {
    console.log("[for_meets] native host disconnected, retry in 5s");
    port = null;
    setTimeout(connect, 5000);
  });
}

function send(msg) {
  if (!port) return;
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
  if (changeInfo.status !== "complete") return;

  const wasMeet = meetTabs.has(tabId);
  const isMeet = isMeetUrl(tab.url);

  if (isMeet && !wasMeet) {
    meetTabs.add(tabId);
    const tabs = await getAllTabs();
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
