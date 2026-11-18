// The ONLY file that knows which WebRTC package is linked.
//
// * LiveKit's build (https://github.com/livekit/webrtc-xcframework, product "LiveKitWebRTC")
//   prefixes every class with "LK", e.g. LKRTCPeerConnection. This is the default.
// * Google's unprefixed build (e.g. https://github.com/stasel/WebRTC, product "WebRTC")
//   uses the plain RTC names, so no aliases are needed.
//
// To switch libraries, change the package in project.yml; nothing else has to change.

#if canImport(LiveKitWebRTC)
import LiveKitWebRTC

typealias RTCPeerConnection = LKRTCPeerConnection
typealias RTCPeerConnectionFactory = LKRTCPeerConnectionFactory
typealias RTCPeerConnectionDelegate = LKRTCPeerConnectionDelegate
typealias RTCConfiguration = LKRTCConfiguration
typealias RTCMediaConstraints = LKRTCMediaConstraints
typealias RTCSessionDescription = LKRTCSessionDescription
typealias RTCSignalingState = LKRTCSignalingState
typealias RTCIceConnectionState = LKRTCIceConnectionState
typealias RTCIceGatheringState = LKRTCIceGatheringState
typealias RTCPeerConnectionState = LKRTCPeerConnectionState
typealias RTCIceCandidate = LKRTCIceCandidate
typealias RTCMediaStream = LKRTCMediaStream
typealias RTCDataChannel = LKRTCDataChannel
typealias RTCRtpReceiver = LKRTCRtpReceiver
typealias RTCRtpTransceiverInit = LKRTCRtpTransceiverInit
typealias RTCAudioTrack = LKRTCAudioTrack
typealias RTCStatisticsReport = LKRTCStatisticsReport
typealias RTCAudioSessionConfiguration = LKRTCAudioSessionConfiguration
#elseif canImport(WebRTC)
import WebRTC
#else
#error("No WebRTC library found. Add the package declared in project.yml.")
#endif
