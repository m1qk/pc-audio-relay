# Measuring end-to-end delay with your real devices

"End to end" means: a sound is played on the PC, and you hear the same sound from the iPhone. It includes the Windows audio path, the sender buffer, Opus, Wi-Fi, the phone's jitter buffer, and the iPhone speaker or headphones.

There are three ways, from quick to precise.

## 1. The page's own estimate (10 seconds, rough)

On the iPhone page open **Latency & stats**. It shows network round trip, receive buffer, sender queue, packet loss and concealed audio, and adds them into an **Estimated delay**. This covers capture to the phone's audio output only. It leaves out speaker or Bluetooth latency. Use it to watch the effect of changing buffers, not as the final number.

## 2. Click test with one microphone (recommended, 5 minutes)

You need a third device that can record, for example another phone with Voice Memos or a laptop with Audacity.

1. Make the click track on the PC:
   ```powershell
   .venv\Scripts\python tools\make_click_track.py -o click_track.wav
   ```
   One short click per second over a very quiet continuous tone. The quiet tone matters: without it the audio stream is silent between clicks, and the sender holds an isolated click back by about 30 ms, so you would read a slightly worse number than music or games really get.
2. Start the sender (set the device to the one your PC speakers use) and connect the iPhone.
3. Play `click_track.wav` on the PC through the **speakers the sender captures**. The PC speakers play it directly and the sender streams it to the iPhone.
4. Put the iPhone speaker and the PC speakers about the same distance from the recording device. Turn the iPhone volume up. Record 30 to 40 seconds.
5. Convert the recording to WAV if needed (Voice Memos gives .m4a):
   ```powershell
   winget install Gyan.FFmpeg
   ffmpeg -i recording.m4a -ac 1 recording.wav
   ```
6. Measure:
   ```powershell
   .venv\Scripts\python tools\measure_delay.py recording.wav --mode mic
   ```
   If the iPhone was farther from the microphone than the PC speakers, add `--phone-farther-by-m 0.5` (metres). Sound needs about 2.9 ms per metre and this subtracts it.

Output looks like:
```
clicks measured: 31
delay (median): 142.3 ms
mean 143.0 ms   min 138.1 ms   max 151.7 ms   spread (stdev) 3.2 ms
```
The **median** is your delay. A large spread means the Wi-Fi is jittery.

The tool was tested on synthetic recordings with known delays (38, 85.5 and 240 ms), recovered within 1.5 ms, including added noise.

**What this measures:** the difference between the PC speakers' sound and the iPhone's sound. The PC speakers have their own small delay (Windows mixer, plus a Bluetooth or HDMI monitor if that is what you use). If you watch video on the PC with sound only from the iPhone, that PC-side delay is not in play, so your real lip-sync offset is a little different. Note which output the PC used.

**Headphones:** the microphone cannot hear wired headphones, so test with the iPhone speaker. Bluetooth headphones add their own delay, often 100 to 250 ms. Measure them with the same recording method by holding the microphone next to the headphone, and compare against the speaker result to see what Bluetooth costs.

## 3. Two-channel line test (most precise)

For a sound card or interface with a stereo line input and the right cables:

1. Channel 1: PC line or headphone output (a splitter lets you also keep listening).
2. Channel 2: iPhone headphone output (USB-C to 3.5 mm adapter).
3. Record both channels while the click track plays and the sender streams it.
4. Run:
   ```powershell
   .venv\Scripts\python tools\measure_delay.py recording.wav --mode lines
   ```
   The offset between the channels is the delay, with no acoustic error.

## What to try, and what to expect

Measure these combinations and keep the numbers in a note:

| Test | Change |
|---|---|
| Baseline | defaults (sender 40 ms, receiver 60 ms) |
| Low delay | sender `--buffer 20`, receiver slider 0 |
| Safe | sender 100, receiver 120 |
| Output | iPhone speaker vs wired vs Bluetooth |
| Network | 5 GHz vs 2.4 GHz Wi-Fi, near vs far from the router |

The software floor I measured (Chrome on one machine, no Wi-Fi, no speaker): about 103 ms at sender 20 / receiver 0, 123 ms at 40 / 0, and 148 ms at the defaults of 40 / 60. Your real end-to-end number is that plus Wi-Fi and the phone's output, so expect it to be higher. Safari's buffering may differ from Chrome's, which is exactly why this measurement has to be made on your iPhone.

Stable first, then lower: reduce one buffer at a time until you hear crackles, then step back up one notch.

## Reading the sender's own counters

The window shows **Queued** (audio waiting in the sender) and **Dropped** (audio discarded to stop delay growing) while streaming. Queued should sit near your sender buffer. Rising Dropped means the sender could not keep pace, so check CPU load or the capture device.
