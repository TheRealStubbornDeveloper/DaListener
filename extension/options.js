const input = document.querySelector("#pairing");
const status = document.querySelector("#status");
const lastError = document.querySelector("#last-error");
chrome.storage.local.get(["pairing", "lastError"]).then(value => {
  if (value.pairing) input.value = JSON.stringify(value.pairing);
  if (value.lastError) lastError.textContent = `Last capture error: ${value.lastError}`;
});
document.querySelector("#save").addEventListener("click", async () => {
  try {
    const pairing = JSON.parse(input.value);
    if (!pairing.audio_url?.startsWith("ws://127.0.0.1:") && !pairing.audio_url?.startsWith("ws://localhost:")) throw new Error("Only a local DaListener bridge is allowed");
    if (!pairing.token) throw new Error("Pairing token is missing");
    await chrome.storage.local.set({pairing});
    await chrome.storage.local.remove("lastError");
    lastError.textContent = "";
    status.className = "ok";
    status.textContent = "Paired. Click the extension icon in each meeting or media tab to start capture.";
  } catch (error) {
    status.className = "";
    status.textContent = String(error);
  }
});
