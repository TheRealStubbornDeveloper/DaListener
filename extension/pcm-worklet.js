class DaListenerPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.target = Math.round(sampleRate / 10);
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels?.length) return true;
    const left = channels[0];
    const right = channels[1];
    for (let index = 0; index < left.length; index++) {
      this.samples.push(right ? (left[index] + right[index]) / 2 : left[index]);
    }
    while (this.samples.length >= this.target) {
      const chunk = new Float32Array(this.samples.splice(0, this.target));
      this.port.postMessage(chunk.buffer, [chunk.buffer]);
    }
    return true;
  }
}
registerProcessor("dalistener-pcm", DaListenerPcmProcessor);
