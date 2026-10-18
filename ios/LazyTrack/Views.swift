import SwiftUI
import UIKit

// MARK: - Quick log

struct LogView: View {
    @State private var pending = 0
    @State private var flash: LogKind?
    @State private var counts: [String: Int] = [:]

    private let columns = [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)]

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVGrid(columns: columns, spacing: 12) {
                    ForEach(LogKind.allCases, id: \.self) { kind in
                        Button { tap(kind) } label: { tile(kind) }
                            .buttonStyle(.plain)
                    }
                }
                .padding()

                if pending > 0 {
                    Label("\(pending) waiting to sync with your Mac", systemImage: "icloud.slash")
                        .font(.footnote)
                        .foregroundStyle(.orange)
                        .padding(.bottom)
                }
            }
            .navigationTitle("Quick log")
            .refreshable { await Outbox.shared.flush(); await refresh() }
            .task { await refresh() }
        }
    }

    private func tile(_ kind: LogKind) -> some View {
        VStack(spacing: 6) {
            Text(kind.emoji).font(.system(size: 42))
            Text(kind.name).font(.headline)
            Text("\(counts[kind.rawValue, default: 0]) today")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, minHeight: 120)
        .background(flash == kind ? Color.yellow.opacity(0.45) : Color(.secondarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .animation(.easeOut(duration: 0.2), value: flash)
    }

    private func tap(_ kind: LogKind) {
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        flash = kind
        Task {
            await Outbox.shared.logEvent(kind.rawValue)
            await refresh()
            try? await Task.sleep(nanoseconds: 350_000_000)
            flash = nil
        }
    }

    private func refresh() async {
        counts = TodayCounter.load()
        pending = await Outbox.shared.pendingCount
    }
}

// MARK: - Food

struct FoodView: View {
    @State private var showCamera = false
    @State private var image: UIImage?
    @State private var result: FoodResult?
    @State private var message = ""
    @State private var countdown = 0
    @State private var logTask: Task<Void, Never>?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 18) {
                    if let image {
                        Image(uiImage: image)
                            .resizable()
                            .scaledToFit()
                            .frame(maxHeight: 320)
                            .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                    }
                    if let result { verdict(result) }
                    if !message.isEmpty {
                        Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }

                    Button { showCamera = true } label: {
                        Label("Snap your food", systemImage: "camera.fill")
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 8)
                    }
                    .buttonStyle(.borderedProminent)

                    Text("The photo is checked on your iPhone and then thrown away. Junk food gets logged automatically.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding()
            }
            .navigationTitle("Food")
            .fullScreenCover(isPresented: $showCamera) {
                CameraPicker { analyze($0) }.ignoresSafeArea()
            }
        }
    }

    @ViewBuilder
    private func verdict(_ r: FoodResult) -> some View {
        VStack(spacing: 8) {
            Text(r.junk ? "🍔 Junk food" : r.isFood ? "🥗 Looks fine" : "🤔 Not sure that's food")
                .font(.title2.bold())
            Text("\(r.prettyLabel) · \(Int(r.confidence * 100))% sure")
                .foregroundStyle(.secondary)
            if countdown > 0 {
                Button(role: .destructive) { cancelLog() } label: {
                    Text("Not junk, don't log (\(countdown))")
                }
                .buttonStyle(.bordered)
            } else if !r.junk {
                Button("It's junk actually") { logJunk(r, delay: 0) }
                    .buttonStyle(.bordered)
            }
        }
    }

    private func analyze(_ img: UIImage) {
        logTask?.cancel()
        countdown = 0
        image = img
        result = nil
        message = "Looking…"
        Task {
            let r = FoodClassifier.judge(await FoodClassifier.classify(img))
            result = r
            message = r == nil ? "Couldn't recognise anything." : ""
            if let r, r.junk { logJunk(r, delay: 5) }
        }
    }

    /// Waits `delay` seconds (so you can cancel a wrong guess), then logs.
    private func logJunk(_ r: FoodResult, delay: Int) {
        countdown = delay
        logTask = Task {
            while countdown > 0 {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                if Task.isCancelled { return }
                countdown -= 1
            }
            if Task.isCancelled { return }
            await Outbox.shared.logEvent("junk_food", note: r.prettyLabel, source: "food_photo")
            Notifier.send("🍔 Junk food logged", r.prettyLabel)
            message = "Logged junk food (\(r.prettyLabel))."
        }
    }

    private func cancelLog() {
        logTask?.cancel()
        countdown = 0
        message = "Not logged 👍"
    }
}

struct CameraPicker: UIViewControllerRepresentable {
    var onImage: (UIImage) -> Void
    @Environment(\.dismiss) var dismiss

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = UIImagePickerController.isSourceTypeAvailable(.camera) ? .camera : .photoLibrary
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraPicker
        init(_ parent: CameraPicker) { self.parent = parent }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            if let img = info[.originalImage] as? UIImage { parent.onImage(img) }
            parent.dismiss()
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            parent.dismiss()
        }
    }
}

// MARK: - Setup

struct SetupView: View {
    @AppStorage("serverURL") private var serverURL = ""
    @AppStorage("token") private var token = ""
    @AppStorage("deviceName") private var deviceName = "iphone"
    @ObservedObject private var gym = GymManager.shared
    @ObservedObject private var egg = EggLink.shared
    @State private var testResult = ""
    @State private var healthResult = ""
    @State private var healthOn = HealthSync.enabled

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://100.x.y.z:8042", text: $serverURL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Token", text: $token)
                    TextField("Device name", text: $deviceName)
                        .textInputAutocapitalization(.never)
                    Button("Test connection") {
                        Task {
                            testResult = "Testing…"
                            testResult = await Outbox.ping()
                            await Outbox.shared.flush()
                        }
                    }
                    if !testResult.isEmpty { Text(testResult).font(.footnote) }
                } header: {
                    Text("Your Mac")
                } footer: {
                    Text("Easiest: run ./lt phone on the Mac and scan the QR code with the iPhone Camera app. Tailscale must be on on both devices.")
                }

                Section {
                    Toggle("Keep my egg online", isOn: $egg.enabled)
                    Text(egg.status).font(.footnote)
                    if egg.relayed > 0 { Text("\(egg.relayed) requests passed on since the app started").font(.footnote).foregroundStyle(.secondary) }
                } header: {
                    Text("LazyTrack Egg (Bluetooth)")
                } footer: {
                    Text("When the egg has no WiFi, it goes online through your phone, even with the app closed. The first time, iOS asks to pair: tap Pair. Only the LazyTrack server is reached, nothing else.")
                }

                Section {
                    Button(gym.gymSet ? "📍 I'm at the gym, move it here" : "📍 I'm at the gym, save this place") {
                        gym.setGymHere()
                    }
                    if gym.gymSet {
                        Button("Remove gym", role: .destructive) { gym.removeGym() }
                    }
                    Text(gym.authText).font(.footnote)
                    if !gym.status.isEmpty { Text(gym.status).font(.footnote).foregroundStyle(.secondary) }
                } header: {
                    Text("Gym (automatic)")
                } footer: {
                    Text("Stay 30+ minutes and it's logged by itself, even if the app is closed. Needs location set to Always.")
                }

                Section {
                    Button(healthOn ? "Sync sleep & steps now" : "Connect Apple Health") {
                        Task {
                            do {
                                if !healthOn {
                                    try await HealthSync.connect()
                                    healthOn = true
                                }
                                healthResult = "Syncing…"
                                healthResult = await HealthSync.sync()
                            } catch {
                                healthResult = error.localizedDescription
                            }
                        }
                    }
                    if !healthResult.isEmpty { Text(healthResult).font(.footnote) }
                } header: {
                    Text("Sleep & steps")
                } footer: {
                    Text("Reads sleep and step count from the Health app (iPhone or Apple Watch). Syncs automatically every few hours.")
                }

                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("For each app you want to track (YouTube, Instagram, TikTok…):")
                        Text("1. Shortcuts → Automation → + → App → pick the app → **Is Opened** → Run Immediately")
                        Text("2. Action: **App opened** (LazyTrack), App name: YouTube")
                        Text("3. Same again with **Is Closed** → **App closed**, same name")
                    }
                    .font(.footnote)
                } header: {
                    Text("Screen time")
                }

                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Say **\"Log cigarette in LazyTrack\"** to Siri.")
                        Text("Or make a shortcut with **Log habit** (Cigarette), then pick it in Settings → Accessibility → Touch → Back Tap. Double-tap the back of your phone to log.")
                    }
                    .font(.footnote)
                } header: {
                    Text("Even lazier logging")
                }
            }
            .navigationTitle("Setup")
        }
    }
}
