import SwiftUI

@main
struct LazyTrackApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @State private var alert: String?

    init() {
        _ = GymManager.shared  // must exist at launch so iOS can deliver gym enter/exit in the background
        _ = EggLink.shared     // same for the egg: iOS relaunches the app in the background when the egg needs internet
        Notifier.requestAuth()
    }

    var body: some Scene {
        WindowGroup {
            RootView()   // LazyTrack itself (the same app as on the Mac); phone settings open from inside it
            .onOpenURL { url in
                if Settings.apply(setupURL: url) {
                    Task {
                        alert = await Outbox.ping()
                        await Outbox.shared.flush()
                    }
                }
            }
            .alert(alert ?? "", isPresented: Binding(get: { alert != nil }, set: { if !$0 { alert = nil } })) {
                Button("OK", role: .cancel) {}
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task {
                    await Outbox.shared.flush()
                    await HealthSync.syncIfDue()
                }
            }
        }
    }
}
