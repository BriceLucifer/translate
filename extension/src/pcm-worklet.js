// AudioWorkletProcessor：多声道混成单声道，Float32 -> int16 PCM 发给主线程。
class PcmCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const ch0 = input[0];
    const len = ch0.length;
    const pcm = new Int16Array(len);

    if (input.length === 1) {
      for (let i = 0; i < len; i++) {
        const s = Math.max(-1, Math.min(1, ch0[i]));
        pcm[i] = s * 0x7fff;
      }
    } else {
      const ch1 = input[1];
      for (let i = 0; i < len; i++) {
        const s = Math.max(-1, Math.min(1, (ch0[i] + ch1[i]) / 2));
        pcm[i] = s * 0x7fff;
      }
    }

    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}

registerProcessor('pcm-capture', PcmCaptureProcessor);
