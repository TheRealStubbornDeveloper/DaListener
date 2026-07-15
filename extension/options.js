const input = document.querySelector("#pairing");
const status = document.querySelector("#status");
chrome.storage.local.get("pairing").then(value => {
  if (value.pairing) input.value = JSON.stringify(value.pairing);
});
document.querySelector("#save").addEventListener("click", async () => {
  try {
    const pairing = JSON.parse(input.value);
    if (!pairing.audio_url?.startsWith("ws://127.0.0.1:") && !pairing.audio_url?.startsWith("ws://localhost:")) throw new Error("Only a local DaListener bridge is allowed");
    if (!pairing.token) throw new Error("Pairing token is missing");
    await chrome.storage.local.set({pairing});
    status.className = "ok";
    status.textContent = "Paired. Click the extension icon in each meeting tab to start capture.";
  } catch (error) {
    status.className = "";
    status.textContent = String(error);
  }
});
