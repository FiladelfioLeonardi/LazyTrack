import SwiftUI

@main
struct LazyTrackApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @State private var alert: String?

    init() {
        _ = GymManager.shared  // must exist at launch so iOS can deliver gym enter/exit in the background
        Notifier.requestAuth()
    }

    var body: some Scene {
        WindowGroup {
            TabView {
                LogView().tabItem { Label("Log", systemImage: "plus.circle.fill") }
                FoodView().tabItem { Label("Food", systemImage: "fork.knife") }
                SetupView().tabItem { Label("Setup", systemImage: "gearshape.fill") }
            }
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
