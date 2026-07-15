const activeTabs = new Set();

const activeKey = tabId => `active_${tabId}`;
const pendingKey = id => `pending_${id}`;

async function isActive(tabId) {
  if (activeTabs.has(tabId)) return true;
  return Boolean((await chrome.storage.session.get(activeKey(tabId)))[activeKey(tabId)]);
}

async function markActive(tabId, active) {
  if (active) {
    activeTabs.add(tabId);
    await chrome.storage.session.set({[activeKey(tabId)]: true});
  } else {
    activeTabs.delete(tabId);
    await chrome.storage.session.remove(activeKey(tabId));
  }
}

async function ensureOffscreen() {
  const url = chrome.runtime.getURL("offscreen.html");
  const contexts = await chrome.runtime.getContexts({contextTypes: ["OFFSCREEN_DOCUMENT"], documentUrls: [url]});
  if (!contexts.length) {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "Capture audio from user-selected browser tabs"
    });
  }
}

function apiUrl(pairing) {
  if (pairing.api_url) return pairing.api_url.replace(/\/$/, "");
  return pairing.audio_url.replace(/^ws/, "http").replace(/\/api\/v1\/extension\/audio$/, "");
}

async function preflight(tab, pairing) {
  const response = await fetch(`${apiUrl(pairing)}/api/v1/extension/capture-preflight`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-DaListener-Extension-Token": pairing.token
    },
    body: JSON.stringify({tab_id: tab.id, title: tab.title || "", url: tab.url || ""})
  });
  if (!response.ok) throw new Error((await response.text()) || "Capture preflight failed");
  return response.json();
}

async function beginCapture(tab, pairing, streamId) {
  await ensureOffscreen();
  const id = streamId || await chrome.tabCapture.getMediaStreamId({targetTabId: tab.id});
  await chrome.runtime.sendMessage({
    target: "offscreen",
    type: "start",
    tabId: tab.id,
    title: tab.title || `Tab ${tab.id}`,
    url: tab.url || "",
    streamId: id,
    pairing
  });
  await markActive(tab.id, true);
  await chrome.action.setBadgeBackgroundColor({tabId: tab.id, color: "#ef4444"});
  await chrome.action.setBadgeText({tabId: tab.id, text: "REC"});
}

async function showWarning(tab, pairing, result) {
  const id = crypto.randomUUID();
  await chrome.storage.session.set({[pendingKey(id)]: {
    tab: {id: tab.id, title: tab.title || "", url: tab.url || ""},
    pairing,
    result
  }});
  await chrome.windows.create({
    url: chrome.runtime.getURL(`confirm.html?pending=${encodeURIComponent(id)}`),
    type: "popup",
    width: 560,
    height: 500,
    focused: true
  });
}

async function reportError(tabId, error) {
  console.error("DaListener capture failed", error);
  await chrome.storage.local.set({lastError: String(error)});
  await chrome.action.setBadgeBackgroundColor({tabId, color: "#b91c1c"});
  await chrome.action.setBadgeText({tabId, text: "ERR"});
}

chrome.action.onClicked.addListener(async tab => {
  if (!tab.id) return;
  try {
    if (await isActive(tab.id)) {
      await chrome.runtime.sendMessage({target: "offscreen", type: "stop", tabId: tab.id});
      await markActive(tab.id, false);
      await chrome.action.setBadgeText({tabId: tab.id, text: ""});
      return;
    }
    const pairing = (await chrome.storage.local.get("pairing")).pairing;
    if (!pairing?.audio_url || !pairing?.token) {
      await chrome.runtime.openOptionsPage();
      return;
    }
    const result = await preflight(tab, pairing);
    if (!result.supported) throw new Error("DaListener cannot capture browser-internal or extension pages");
    if (result.warning_required) await showWarning(tab, pairing, result);
    else await beginCapture(tab, pairing);
  } catch (error) {
    await reportError(tab.id, error);
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.target !== "worker") return;
  if (message.type === "start-confirmed") {
    beginCapture(message.tab, message.pairing, message.streamId)
      .then(() => sendResponse({ok: true}))
      .catch(error => reportError(message.tab.id, error).then(() => sendResponse({ok: false, error: String(error)})));
    return true;
  }
  if (message.type === "stopped") {
    markActive(message.tabId, false).catch(() => {});
    chrome.action.setBadgeText({tabId: message.tabId, text: ""}).catch(() => {});
  } else if (message.type === "error") {
    reportError(message.tabId, message.message).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener(tabId => {
  isActive(tabId).then(active => {
    if (active) chrome.runtime.sendMessage({target: "offscreen", type: "stop", tabId}).catch(() => {});
    return markActive(tabId, false);
  }).catch(() => {});
});
