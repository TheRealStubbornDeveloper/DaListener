const params = new URLSearchParams(location.search);
const pendingId = params.get("pending");
const key = `pending_${pendingId}`;
const service = document.querySelector("#service");
const warning = document.querySelector("#warning");
const remember = document.querySelector("#remember");
const error = document.querySelector("#error");
const continueButton = document.querySelector("#continue");

let pending;

function apiUrl(pairing) {
  if (pairing.api_url) return pairing.api_url.replace(/\/$/, "");
  return pairing.audio_url.replace(/^ws/, "http").replace(/\/api\/v1\/extension\/audio$/, "");
}

async function initialize() {
  pending = (await chrome.storage.session.get(key))[key];
  if (!pending) throw new Error("This capture request expired. Click the DaListener icon again.");
  service.textContent = `${pending.result.service_label} · ${pending.result.domain}`;
  warning.textContent = pending.result.warning_message;
}

document.querySelector("#cancel").addEventListener("click", async () => {
  await chrome.storage.session.remove(key);
  window.close();
});

continueButton.addEventListener("click", async () => {
  continueButton.disabled = true;
  error.textContent = "";
  try {
    // Obtain the stream identifier directly inside this user click so Chrome
    // recognizes the deliberate capture gesture.
    const streamId = await chrome.tabCapture.getMediaStreamId({targetTabId: pending.tab.id});
    if (remember.checked) {
      const response = await fetch(`${apiUrl(pending.pairing)}/api/v1/extension/capture-warning/acknowledge`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-DaListener-Extension-Token": pending.pairing.token
        },
        body: JSON.stringify({domain: pending.result.domain, suppress_for_domain: true})
      });
      if (!response.ok) throw new Error("The website preference could not be saved");
    }
    const result = await chrome.runtime.sendMessage({
      target: "worker",
      type: "start-confirmed",
      tab: pending.tab,
      pairing: pending.pairing,
      streamId
    });
    if (!result?.ok) throw new Error(result?.error || "Capture could not start");
    await chrome.storage.session.remove(key);
    window.close();
  } catch (reason) {
    error.textContent = String(reason);
    continueButton.disabled = false;
  }
});

initialize().catch(reason => {
  error.textContent = String(reason);
  continueButton.disabled = true;
});
