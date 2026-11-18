import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: ReceiverModel
    @State private var showScanner = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("PC Audio").font(.largeTitle.bold())

                statusPill
                Text(model.detail)
                    .font(.subheadline)
                    .foregroundStyle(model.state == .failed ? .red : .secondary)
                    .frame(minHeight: 40, alignment: .topLeading)

                Button(action: toggle) {
                    Text(model.isActive ? "Disconnect" : "Connect")
                        .font(.title3.weight(.bold))
                        .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .tint(model.isActive ? .red : .accentColor)

                VStack(alignment: .leading, spacing: 6) {
                    Text("Volume").font(.subheadline).foregroundStyle(.secondary)
                    HStack {
                        Image(systemName: "speaker.fill")
                        Slider(value: $model.volume, in: 0...1)
                        Image(systemName: "speaker.wave.3.fill")
                    }
                    Text("Playing on: \(model.routeName). The side volume buttons also work.")
                        .font(.footnote).foregroundStyle(.secondary)
                }

                GroupBox("PC address") {
                    VStack(alignment: .leading, spacing: 10) {
                        TextField("192.168.1.20:8765", text: $model.address)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .textFieldStyle(.roundedBorder)
                            .submitLabel(.go)
                            .onSubmit { model.connect() }
                        Button { showScanner = true } label: {
                            Label("Scan QR code", systemImage: "qrcode.viewfinder")
                        }
                        .buttonStyle(.bordered)
                    }
                }
                .disabled(model.isActive)
            }
            .padding(20)
        }
        .sheet(isPresented: $showScanner) {
            ZStack(alignment: .topTrailing) {
                QRScannerView { code in
                    showScanner = false
                    model.connect(to: code)
                }
                .ignoresSafeArea()
                Button("Cancel") { showScanner = false }
                    .padding(12).background(.regularMaterial, in: Capsule()).padding()
            }
        }
    }

    private var statusPill: some View {
        HStack(spacing: 10) {
            Circle().fill(color).frame(width: 12, height: 12)
            Text(model.state.label).font(.headline)
            Spacer()
        }
        .padding(14)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        switch model.state {
        case .connected: return .green
        case .connecting, .reconnecting: return .orange
        case .failed: return .red
        case .idle: return .gray
        }
    }

    private func toggle() {
        model.isActive ? model.disconnect() : model.connect()
    }
}
