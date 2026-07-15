type CaptureHandle = {
  stream: MediaStream;
  context: AudioContext;
  source: MediaStreamAudioSourceNode;
  processor: AudioWorkletNode;
  sink: GainNode;
  socket: WebSocket;
};

const captures = new Map<string, CaptureHandle>();

async function createPcmProcessor(context: AudioContext): Promise<AudioWorkletNode> {
  const moduleSource = `
    class DaListenerPcmProcessor extends AudioWorkletProcessor {
      constructor() {
        super();
        this.buffer = new Float32Array(4096);
        this.offset = 0;
      }
      process(inputs) {
        const samples = inputs[0] && inputs[0][0];
        if (!samples) return true;
        let sourceOffset = 0;
        while (sourceOffset < samples.length) {
          const count = Math.min(samples.length - sourceOffset, this.buffer.length - this.offset);
          this.buffer.set(samples.subarray(sourceOffset, sourceOffset + count), this.offset);
          this.offset += count;
          sourceOffset += count;
          if (this.offset === this.buffer.length) {
            this.port.postMessage(this.buffer, [this.buffer.buffer]);
            this.buffer = new Float32Array(4096);
            this.offset = 0;
          }
        }
        return true;
      }
    }
    registerProcessor("dalistener-pcm", DaListenerPcmProcessor);
  `;
  const moduleUrl = URL.createObjectURL(new Blob([moduleSource], {type: "text/javascript"}));
  try {
    await context.audioWorklet.addModule(moduleUrl);
  } finally {
    URL.revokeObjectURL(moduleUrl);
  }
  return new AudioWorkletNode(context, "dalistener-pcm", {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
  });
}

function recognizedMeeting(label: string) {
  return /\b(zoom|google meet|microsoft teams|teams|webex)\b/i.test(label);
}

function displayTitle(stream: MediaStream): string {
  const raw = stream.getVideoTracks()[0]?.label || stream.getAudioTracks()[0]?.label || "";
  const opaque = !raw || /^(web-contents-media-stream|screen|window):\/\//i.test(raw) || /^[{(]?[a-f0-9-]{24,}[})]?$/i.test(raw);
  if (!opaque) return raw.replace(/^(Chrome|Edge) Tab\s*[-–—:]\s*/i, "").trim() || "Shared browser tab";
  const entered = window.prompt(
    "Chromium did not expose this tab's title. Enter a display name for this captured tab:",
    "Shared browser tab",
  );
  if (entered === null) throw new Error("Capture cancelled");
  return entered.trim() || "Shared browser tab";
}

function stopHandle(handle: CaptureHandle, closeSocket = true) {
  handle.processor.disconnect();
  handle.processor.port.onmessage = null;
  handle.source.disconnect();
  handle.sink.disconnect();
  handle.stream.getTracks().forEach(track => track.stop());
  if (closeSocket && handle.socket.readyState < WebSocket.CLOSING) handle.socket.close(1000, "Capture stopped");
  void handle.context.close();
}

export async function startBrowserCapture(browserAudioToken: string): Promise<{meetingId: string; title: string}> {
  const options = {
    audio: true,
    video: {width: {max: 16}, height: {max: 16}, frameRate: {max: 1}},
    preferCurrentTab: false,
    selfBrowserSurface: "exclude",
    surfaceSwitching: "exclude",
    systemAudio: "exclude",
  } as DisplayMediaStreamOptions;
  const stream = await navigator.mediaDevices.getDisplayMedia(options);
  const audioTrack = stream.getAudioTracks()[0];
  if (!audioTrack) {
    stream.getTracks().forEach(track => track.stop());
    throw new Error("The selected source did not provide audio. Select a Chromium tab and enable Share tab audio.");
  }
  let title: string;
  try {
    title = displayTitle(stream);
  } catch (error) {
    stream.getTracks().forEach(track => track.stop());
    throw error;
  }
  if (!recognizedMeeting(title) && !window.confirm(
    `“${title}” is not recognized as a typical meeting. Capture its audio anyway? OpenAI charges may apply.`,
  )) {
    stream.getTracks().forEach(track => track.stop());
    throw new Error("Capture cancelled");
  }

  const context = new AudioContext();
  await context.resume();
  const source = context.createMediaStreamSource(stream);
  const processor = await createPcmProcessor(context);
  const sink = context.createGain();
  sink.gain.value = 0;
  source.connect(processor);
  processor.connect(sink);
  sink.connect(context.destination);

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/v1/browser/audio?token=${encodeURIComponent(browserAudioToken)}`);
  socket.binaryType = "arraybuffer";
  const meetingId = await new Promise<string>((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error("DaListener did not acknowledge the shared tab within 25 seconds")), 25_000);
    socket.onopen = () => socket.send(JSON.stringify({
      type: "start", title, browser: navigator.userAgent, sample_rate: context.sampleRate, channels: 1,
    }));
    socket.onmessage = event => {
      const response = JSON.parse(String(event.data));
      if (response.type === "started") {
        window.clearTimeout(timeout);
        title = response.title || title;
        resolve(response.meeting_id);
      } else if (response.type === "error") {
        window.clearTimeout(timeout);
        reject(new Error(response.message || "DaListener rejected the shared tab"));
      }
    };
    socket.onerror = () => { window.clearTimeout(timeout); reject(new Error("Could not connect the shared tab to DaListener")); };
    socket.onclose = event => {
      if (!captures.size && event.code !== 1000) reject(new Error(event.reason || "The local audio bridge closed"));
    };
  }).catch(error => {
    stopHandle({stream, context, source, processor, sink, socket});
    throw error;
  });

  const handle = {stream, context, source, processor, sink, socket};
  captures.set(meetingId, handle);
  socket.onclose = event => {
    const current = captures.get(meetingId);
    if (!current) return;
    captures.delete(meetingId);
    stopHandle(current, false);
    if (event.code !== 1000) {
      window.dispatchEvent(new CustomEvent("dalistener:capture-error", {
        detail: event.reason || `The local audio bridge closed (${event.code})`,
      }));
    }
  };
  processor.port.onmessage = event => {
    if (socket.readyState !== WebSocket.OPEN) return;
    const samples = event.data as Float32Array;
    socket.send(samples.slice().buffer);
  };
  const ended = () => {
    const current = captures.get(meetingId);
    if (!current) return;
    captures.delete(meetingId);
    stopHandle(current);
  };
  stream.getTracks().forEach(track => track.addEventListener("ended", ended, {once: true}));
  return {meetingId, title};
}

export function stopBrowserCapture(meetingId: string): boolean {
  const handle = captures.get(meetingId);
  if (!handle) return false;
  captures.delete(meetingId);
  stopHandle(handle);
  return true;
}
