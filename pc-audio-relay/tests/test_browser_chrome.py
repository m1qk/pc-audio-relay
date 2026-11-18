"""Drives the real web receiver in headless Chrome against the real sender.

Skipped automatically when Playwright or a Chrome binary is unavailable
(set CHROME_PATH to point at one). Uses the synthetic tone source, so it
verifies signaling, WebRTC, Opus, stereo decode and reconnect behaviour, not
WASAPI capture or Safari-specific behaviour.
"""
import asyncio
import os
import shutil
import time

import pytest

pytest.importorskip("playwright")
from playwright.async_api import async_playwright  # noqa: E402

from pc_audio_relay.app import RelayApp  # noqa: E402
from pc_audio_relay.config import Settings  # noqa: E402
from tests.helpers import free_port  # noqa: E402

CHROME = os.environ.get("CHROME_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium binary found")

STEREO_JS = """
async () => {
  const ac = new AudioContext(); await ac.resume();
  const stream = document.getElementById('player').srcObject;
  const sp = ac.createChannelSplitter(2);
  ac.createMediaStreamSource(stream).connect(sp);
  const mk = (ch) => { const a = ac.createAnalyser(); a.fftSize = 8192; sp.connect(a, ch); return a; };
  const aL = mk(0), aR = mk(1);
  await new Promise(r => setTimeout(r, 1500));
  const peak = (a) => { const d = new Float32Array(a.frequencyBinCount); a.getFloatFrequencyData(d);
    let m = -1e9, mi = 0; d.forEach((v, i) => { if (v > m) { m = v; mi = i; } });
    return mi * ac.sampleRate / a.fftSize; };
  return { L: peak(aL), R: peak(aR) };
}
"""


@pytest.fixture
async def env():
    s = Settings(port=free_port(), key="abc123", capture_buffer_ms=40).clamp()
    app = RelayApp(s, tone_mode="tone")
    app.start()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(executable_path=CHROME, headless=True,
                                       args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
    yield app, browser
    await browser.close()
    await pw.stop()
    app.stop()


def page_url(app, key=None):
    k = app.settings.key if key is None else key
    return f"http://{app.address.ip}:{app.settings.port}/?k={k}"


async def state(page):
    return await page.get_attribute("#status", "data-state")


async def wait_state(page, want, timeout=20):
    await page.wait_for_selector(f"#status[data-state={want}]", timeout=timeout * 1000)


async def test_connects_and_decodes_stereo_in_a_real_browser(env):
    app, browser = env
    page = await browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(page_url(app))
    assert await page.is_hidden("#resumeBtn"), "resume button must be hidden until needed"
    await page.click("#connectBtn")
    await wait_state(page, "connected", 15)
    await asyncio.sleep(2.5)
    st = await page.evaluate("window.__pcaudio")
    assert st["packets"] > 80 and st["bytes"] > 10000
    assert (st["lossPct"] or 0) < 1 and (st["concealedPct"] or 0) < 2
    freqs = await page.evaluate(STEREO_JS)
    assert abs(freqs["L"] - 440) < 15, freqs
    assert abs(freqs["R"] - 880) < 20, freqs
    assert (await page.inner_text("#connectBtn")) == "Disconnect"
    assert errors == []


async def test_disconnect_hangs_up_on_the_server(env):
    app, browser = env
    page = await browser.new_page()
    await page.goto(page_url(app))
    await page.click("#connectBtn")
    await wait_state(page, "connected", 15)
    await page.click("#connectBtn")  # Disconnect
    await wait_state(page, "idle", 5)
    deadline = time.time() + 5
    while time.time() < deadline and app.snapshot()["clients"]:
        await asyncio.sleep(0.1)
    assert app.snapshot()["clients"] == []


async def test_wrong_key_shows_clear_error_and_does_not_retry(env):
    app, browser = env
    page = await browser.new_page()
    await page.goto(page_url(app, key="wrong"))
    await page.click("#connectBtn")
    await wait_state(page, "error", 10)
    assert "pairing key" in (await page.inner_text("#detail")).lower()
    await asyncio.sleep(2.5)
    assert await state(page) == "error"
    assert app.server.rejected_offers == 1, "must not hammer the server with a bad key"
    assert app.server.offers_received == 0
    assert (await page.inner_text("#connectBtn")) == "Connect"


async def test_page_recovers_when_server_drops_the_session(env):
    app, browser = env
    page = await browser.new_page()
    await page.goto(page_url(app))
    await page.click("#connectBtn")
    await wait_state(page, "connected", 15)
    first = app.snapshot()["clients"][0]["sessionId"]
    t0 = time.time()
    asyncio.run_coroutine_threadsafe(app.server._close_all("test: server dropped"), app._loop).result(5)
    deadline = time.time() + 30
    while time.time() < deadline:
        clients = app.snapshot()["clients"]
        if clients and clients[0]["sessionId"] != first and clients[0]["connectionState"] == "connected":
            break
        await asyncio.sleep(0.2)
    else:
        pytest.fail(f"page did not reconnect; state={await state(page)}")
    print(f"\n   reconnect after server-side drop took {time.time() - t0:.1f}s")
    await wait_state(page, "connected", 5)


async def test_page_recovers_when_sender_is_restarted(env):
    app, browser = env
    page = await browser.new_page()
    await page.goto(page_url(app))
    await page.click("#connectBtn")
    await wait_state(page, "connected", 15)
    t0 = time.time()
    app.stop()                      # sender closed...
    await asyncio.sleep(3)
    await wait_state(page, "reconnecting", 30)
    assert "reach the PC" in (await page.inner_text("#detail")) or "lost" in (await page.inner_text("#detail")).lower() \
        or "stopped" in (await page.inner_text("#detail")).lower()
    app.start()                     # ...and started again (same port + key)
    await wait_state(page, "connected", 40)
    print(f"\n   sender stop+start: page was playing again {time.time() - t0:.1f}s after the stop")
