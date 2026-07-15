const activeTabs = new Set();

const activeKey = tabId => `active_${tabId}`;
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
      justification: "Capture audio from user-selected meeting tabs"
    });
  }
}

chrome.action.onClicked.addListener(async tab => {
  if (!tab.id) return;
  try {
    await ensureOffscreen();
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
    const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: tab.id});
    await chrome.runtime.sendMessage({
      target: "offscreen",
      type: "start",
      tabId: tab.id,
      title: tab.title || `Tab ${tab.id}`,
      url: tab.url || "",
      streamId,
      pairing
    });
    await markActive(tab.id, true);
    await chrome.action.setBadgeBackgroundColor({tabId: tab.id, color: "#ef4444"});
    await chrome.action.setBadgeText({tabId: tab.id, text: "REC"});
  } catch (error) {
    console.error("DaListener capture failed", error);
    await chrome.action.setBadgeBackgroundColor({tabId: tab.id, color: "#b91c1c"});
    await chrome.action.setBadgeText({tabId: tab.id, text: "ERR"});
  }
});

chrome.runtime.onMessage.addListener(message => {
  if (message.target !== "worker") return;
  if (message.type === "stopped") {
    markActive(message.tabId, false).catch(() => {});
    chrome.action.setBadgeText({tabId: message.tabId, text: ""}).catch(() => {});
  } else if (message.type === "error") {
    chrome.action.setBadgeBackgroundColor({tabId: message.tabId, color: "#b91c1c"}).catch(() => {});
    chrome.action.setBadgeText({tabId: message.tabId, text: "ERR"}).catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener(tabId => {
  isActive(tabId).then(active => {
    if (active) chrome.runtime.sendMessage({target: "offscreen", type: "stop", tabId}).catch(() => {});
    return markActive(tabId, false);
  }).catch(() => {});
});
