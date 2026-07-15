const captures = new Map();

async function startCapture(message) {
  if (captures.has(message.tabId)) return;
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {mandatory: {chromeMediaSource: "tab", chromeMediaSourceId: message.streamId}},
    video: false
  });
  const context = new AudioContext({sampleRate: 24000});
  await context.audioWorklet.addModule(chrome.runtime.getURL("pcm-worklet.js"));
  const source = context.createMediaStreamSource(stream);
  const processor = new AudioWorkletNode(context, "dalistener-pcm", {numberOfInputs: 1, numberOfOutputs: 0});
  source.connect(processor);
  source.connect(context.destination); // tabCapture otherwise mutes normal playback

  const socket = new WebSocket(message.pairing.audio_url);
  socket.binaryType = "arraybuffer";
  const capture = {stream, context, processor, socket, ready: false};
  captures.set(message.tabId, capture);
  const started = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("DaListener bridge did not acknowledge tab audio within 15 seconds")), 15000);
    capture.resolveStart = () => { clearTimeout(timeout); resolve(); };
    capture.rejectStart = reason => { clearTimeout(timeout); reject(reason); };
  });
  socket.onopen = () => socket.send(JSON.stringify({
    type: "start",
    token: message.pairing.token,
    tab_id: message.tabId,
    title: message.title,
    url: message.url,
    browser: "Chromium",
    sample_rate: context.sampleRate,
    channels: 1
  }));
  socket.onmessage = event => {
    const response = JSON.parse(event.data);
    if (response.type === "started") {
      capture.ready = true;
      capture.resolveStart();
    }
    if (response.type === "error") {
      const reason = new Error(response.message || "DaListener bridge rejected tab audio");
      if (!capture.ready) capture.rejectStart(reason);
      else reportError(message.tabId, reason.message);
    }
  };
  socket.onerror = () => {
    const reason = new Error("Could not connect to the DaListener bridge");
    if (!capture.ready) capture.rejectStart(reason);
    else reportError(message.tabId, reason.message);
  };
  socket.onclose = () => {
    if (!capture.ready) capture.rejectStart(new Error("DaListener bridge closed before capture started"));
    stopCapture(message.tabId, false);
  };
  processor.port.onmessage = event => {
    if (capture.ready && socket.readyState === WebSocket.OPEN) socket.send(event.data);
  };
  stream.getAudioTracks()[0].onended = () => stopCapture(message.tabId);
  try {
    await started;
  } catch (error) {
    await stopCapture(message.tabId);
    throw error;
  }
}

async function stopCapture(tabId, closeSocket = true) {
  const capture = captures.get(tabId);
  if (!capture) return;
  captures.delete(tabId);
  capture.processor.disconnect();
  capture.stream.getTracks().forEach(track => track.stop());
  if (closeSocket && capture.socket.readyState < WebSocket.CLOSING) capture.socket.close(1000, "Capture stopped");
  await capture.context.close();
  chrome.runtime.sendMessage({target: "worker", type: "stopped", tabId}).catch(() => {});
}

function reportError(tabId, message) {
  console.error("DaListener", message);
  chrome.runtime.sendMessage({target: "worker", type: "error", tabId, message}).catch(() => {});
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.target !== "offscreen") return;
  if (message.type === "start") {
    startCapture(message)
      .then(() => sendResponse({ok: true}))
      .catch(error => sendResponse({ok: false, error: String(error)}));
    return true;
  }
  if (message.type === "stop") stopCapture(message.tabId);
});
