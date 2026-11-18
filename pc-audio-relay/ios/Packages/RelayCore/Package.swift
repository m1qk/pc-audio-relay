// swift-tools-version:5.9
import PackageDescription

// Pure logic (no UIKit, no WebRTC) so it builds and tests anywhere with `swift test`.
let package = Package(
    name: "RelayCore",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "RelayCore", targets: ["RelayCore"])],
    targets: [
        .target(name: "RelayCore"),
        .testTarget(name: "RelayCoreTests", dependencies: ["RelayCore"]),
    ]
)
