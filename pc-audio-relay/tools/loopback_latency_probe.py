#!/usr/bin/env python3
"""Developer tool: measures the SOFTWARE pipeline delay on one machine.

Starts the sender with its click test signal, opens the real web receiver in
headless Chrome, and timestamps each click as it reaches the browser's audio
graph, using one clock for both ends. It covers: sender buffer + Opus + RTP +
the browser's jitter buffer + decode. It does NOT include Wi-Fi, the phone's
audio output, or a speaker/Bluetooth, so it is a floor. Use docs/LATENCY.md for
the real end-to-end number.

    python tools/loopback_latency_probe.py            # default settings matrix
Needs: playwright and a Chrome/Chromium binary (set CHROME_PATH if not on PATH).
"""
import asyncio
import os
import shutil
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from pc_audio_relay.app import RelayApp  # noqa: E402
from pc_audio_relay.config import Settings  # noqa: E402

WORKLET = """
async () => {
  const ac = new AudioContext(); await ac.resume();
  const code = `class D extends AudioWorkletProcessor {
    constructor() { super(); this.last = -1; }
    process(inputs) {
      const ch = inputs[0][0];
      if (ch) for (let i = 0; i < ch.length; i++) {
        if (Math.abs(ch[i]) > 0.25) {
          const t = currentTime + i / sampleRate;
          if (t - this.last > 0.3) { this.last = t; this.port.postMessage(t); }
        }
      }
      return true;
    }
  }
  registerProcessor('det', D);`;
  await ac.audioWorklet.addModule(URL.createObjectURL(new Blob([code], {type: 'text/javascript'})));
  const src = ac.createMediaStreamSource(document.getElementById('player').srcObject);
  const node = new AudioWorkletNode(ac, 'det');
  src.connect(node);
  window.__clicks = [];
  const ts = ac.getOutputTimestamp();
  const wall0 = performance.timeOrigin + ts.performanceTime, ctx0 = ts.contextTime;
  node.port.onmessage = (e) => window.__clicks.push(wall0 + (e.data - ctx0) * 1000);
  return true;
}
"""


async def measure(browser, sender_buffer_ms: int, receiver_buffer_ms: int, seconds: int = 14) -> list[float]:
    s = Settings(port=0, key="probe", capture_buffer_ms=sender_buffer_ms)
    import socket
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); s.port = sock.getsockname()[1]; sock.close()
    s.clamp()
    app = RelayApp(s, tone_mode="click")
    app.start()
    page = await browser.new_page()
    try:
        await page.goto(f"http://localhost:{s.port}/?k={s.key}")  # localhost is a secure context (AudioWorklet needs one)
        await page.evaluate(f"(() => {{ const j = document.getElementById('jb'); j.value = {receiver_buffer_ms}; j.dispatchEvent(new Event('input')); }})()")
        await page.click("#connectBtn")
        await page.wait_for_selector("#status[data-state=connected]", timeout=15000)
        await page.evaluate(WORKLET)
        await asyncio.sleep(seconds)
        clicks = await page.evaluate("window.__clicks")
        t0 = app.engine.t0_wall
        delays = []
        for c in clicks:
            k = round(c / 1000 - t0)            # which click this is (one per second)
            if k >= 3:                          # skip start-up
                delays.append(c - (t0 + k) * 1000)
        return delays
    finally:
        await page.close()
        app.stop()


async def main() -> None:
    chrome = os.environ.get("CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=chrome, headless=True,
                                           args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
        print(f"{'sender buffer':>14} {'receiver buffer':>16} {'clicks':>7} {'median':>9} {'min':>8} {'max':>8}")
        for sb, rb in [(20, 0), (40, 0), (40, 60), (80, 60), (160, 120)]:
            d = await measure(browser, sb, rb)
            if d:
                print(f"{sb:>11} ms {rb:>13} ms {len(d):>7} {statistics.median(d):>6.0f} ms {min(d):>5.0f} ms {max(d):>5.0f} ms")
            else:
                print(f"{sb:>11} ms {rb:>13} ms   no clicks detected")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
