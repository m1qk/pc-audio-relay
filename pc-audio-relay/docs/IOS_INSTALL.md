# Installing the native iPhone receiver

The native app is a SwiftUI app in `ios/`. **It has never been compiled or run.** I could only check its syntax (a Swift grammar parse found zero errors), and I could run none of its logic. Treat the first build as a debugging session; the likely trouble spots are listed at the bottom.

You do not need this to try the project. The Safari receiver works first. You need the native app mainly for reliable **screen-locked playback**, which Safari may not give you.

## Why Windows alone can't build it

iPhone apps can only be built with Apple's Xcode, which runs only on macOS. There is no supported way to build an iOS app on Windows. You have three real options.

### Option A: a cloud Mac builds it, you sign and install from Windows (no Mac needed)

This repo includes `.github/workflows/ios-unsigned-ipa.yml`. GitHub's hosted Mac builds the app and gives you an unsigned `.ipa` file.

1. Put the project in a GitHub repository (a private one is fine). GitHub's hosted Macs use a free-minutes allowance; one build is a few minutes.
2. Edit `ios/project.yml` and change `PRODUCT_BUNDLE_IDENTIFIER` from `com.example.pcaudioreceiver` to something unique, such as `com.yourname.pcaudio`.
3. On GitHub: **Actions, "iOS receiver (unsigned IPA)", Run workflow.** When it finishes, download the artifact `PCAudioReceiver-unsigned-ipa`.
4. Sign and install it from Windows with a sideloading tool such as **Sideloadly** or **AltStore**, using a free Apple ID:
   * Install the web (not Microsoft Store) versions of iTunes and iCloud, which Sideloadly needs on Windows.
   * Connect the iPhone by USB and tap **Trust**.
   * On the iPhone: **Settings, Privacy & Security, Developer Mode, on**, then restart.
   * Drag the `.ipa` into Sideloadly, enter your Apple ID, click Start.
   * On the iPhone: **Settings, General, VPN & Device Management**, tap your Apple ID, **Trust**.

Limits of a free Apple ID, from Sideloadly's FAQ: the app expires after **7 days** and must be reinstalled; at most **3** sideloaded apps at once; Apple limits new App IDs to 10 per 7 days. I could not confirm in that FAQ that Sideloadly signs an unsigned `.ipa` as opposed to one prepared another way; it is widely used that way, but verify on first use.

### Option B: borrow or rent a Mac

1. Install a current Xcode (one that supports the iOS version on your iPhone; an iPhone 17 Pro ships with iOS 26, so you need Xcode 26 or newer).
2. `brew install xcodegen`, then in the `ios` folder run `xcodegen generate` and open `PCAudioReceiver.xcodeproj`.
3. Select the app target, **Signing & Capabilities**, choose your Apple ID as the Team, and set a unique bundle ID.
4. Plug in the iPhone, select it, press Run. Same Developer Mode and Trust steps as above.

### Option C: paid Apple Developer Program

About US$99 per year (check Apple for the current price). Apps last a year instead of 7 days and you can use TestFlight. You still need a Mac or the cloud build to produce the app.

## What needs signing or approval

| Item | Needed? |
|---|---|
| Apple ID | Yes (free is enough to start) |
| Unique bundle ID | Yes |
| Background audio | **Already configured.** `UIBackgroundModes = audio` is an Info.plist setting, not a restricted entitlement, so a free account can use it. |
| Local Network permission | **Already configured.** `NSLocalNetworkUsageDescription` is set. iOS shows a prompt the first time the app talks to your PC; tap **Allow**. If you tapped Don't Allow: Settings, Privacy & Security, Local Network, PC Audio, on. |
| Camera | `NSCameraUsageDescription` set (QR scanning only). You can type the address instead. |
| HTTP to the PC | `NSAllowsLocalNetworking` set so plain HTTP to local addresses is allowed. |
| Bonjour | Not used, so no `NSBonjourServices` entry. |

## How it works (for debugging)

* `RelayCore` (Swift package): parses the QR address, adds `stereo=1` to the Opus SDP, reconnect backoff. Has unit tests: `cd ios/Packages/RelayCore && swift test` on a Mac. The workflow runs them first.
* `PeerSession`: one receive-only WebRTC connection (no ICE servers), signaling over `docs/PROTOCOL.md`.
* `AudioSessionController`: playback audio category (no microphone), interruptions, route changes, lock-screen pause command.
* `ReceiverModel`: Connect and Disconnect, automatic reconnects with backoff, network-change and foreground reconnects.
* Behaviour choices: unplugging headphones **stops** playback (so the speaker doesn't suddenly blast) and shows a message; after a phone call or other interruption the app reconnects automatically.

## WebRTC library

Default: LiveKit's `webrtc-xcframework` (product `LiveKitWebRTC`, classes prefixed `LKRTC`). Its releases track recent Chromium WebRTC (M150-based builds in the last releases I could see) and the package was active within the past week when I checked. I did not pick Google-unprefixed `stasel/WebRTC` as the default because the registry page I could read listed its latest release as M126 from June 2024, though that page may be out of date.

All library-specific type names live in `WebRTCShim.swift`. To use the other library, change the package in `project.yml`; nothing else changes.

## Things most likely to need a fix on the first build

Everything here is a guess I could not check, in rough order of likelihood:

1. **Package version.** `from: "150.7871.02"` in `project.yml` comes from tags I saw on GitHub's release list. SwiftPM may reject version strings with leading zeros. If so, add the package in Xcode (File, Add Package Dependencies) and pick the newest tag, or use `branch: main`.
2. **ObjC method names** under the `LK` prefix (for example `RTCAudioSessionConfiguration.webRTC()` and `setWebRTC(_:)`) may differ slightly in newer builds. The compiler will say exactly which.
3. **Audio session.** libwebrtc normally sets a voice-chat session. `prepare()` swaps in plain playback. On first run check that **no microphone prompt appears** and that audio keeps playing with the screen locked.
4. **Remote volume** uses `track.source.volume`. If volume changes do nothing, that's the line to look at.
5. **Swift concurrency warnings** in `ReceiverModel` and `AudioSessionController` (`MainActor.assumeIsolated`), which is why the deployment target is iOS 17.
