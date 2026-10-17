import AppIntents
import Foundation

enum LogKind: String, AppEnum, CaseIterable {
    case cigarette, joint, junk_food, alcohol, energy_drink, gym, walk, water

    var emoji: String {
        switch self {
        case .cigarette: return "🚬"
        case .joint: return "🌿"
        case .junk_food: return "🍔"
        case .alcohol: return "🍺"
        case .energy_drink: return "⚡"
        case .gym: return "🏋️"
        case .walk: return "🚶"
        case .water: return "💧"
        }
    }

    var name: String {
        switch self {
        case .cigarette: return "Cigarette"
        case .joint: return "Joint"
        case .junk_food: return "Junk food"
        case .alcohol: return "Alcohol"
        case .energy_drink: return "Energy drink"
        case .gym: return "Gym"
        case .walk: return "Walk"
        case .water: return "Water"
        }
    }

    static var typeDisplayRepresentation: TypeDisplayRepresentation = "Habit"
    static var caseDisplayRepresentations: [LogKind: DisplayRepresentation] = [
        .cigarette: "🚬 Cigarette",
        .joint: "🌿 Joint",
        .junk_food: "🍔 Junk food",
        .alcohol: "🍺 Alcohol",
        .energy_drink: "⚡ Energy drink",
        .gym: "🏋️ Gym",
        .walk: "🚶 Walk",
        .water: "💧 Water",
    ]
}

/// "Hey Siri, log cigarette in LazyTrack", Back Tap, Action Button, home screen shortcut...
struct LogHabitIntent: AppIntent {
    static var title: LocalizedStringResource = "Log habit"
    static var description = IntentDescription("Logs a habit in LazyTrack without opening the app.")
    static var openAppWhenRun: Bool = false

    @Parameter(title: "Habit")
    var kind: LogKind

    func perform() async throws -> some IntentResult & ProvidesDialog {
        await Outbox.shared.logEvent(kind.rawValue, source: "shortcut")
        return .result(dialog: "Logged \(kind.emoji) \(kind.name)")
    }
}

/// Shortcuts automation: "When YouTube is opened" -> App Opened (YouTube)
struct AppOpenedIntent: AppIntent {
    static var title: LocalizedStringResource = "App opened"
    static var description = IntentDescription("Use in a Shortcuts automation: When <app> is Opened. Type the app's name.")
    static var openAppWhenRun: Bool = false

    @Parameter(title: "App name")
    var app: String

    func perform() async throws -> some IntentResult {
        AppUsage.opened(app)
        await HealthSync.syncIfDue()  // piggyback: keeps sleep/steps fresh without opening the app
        await Outbox.shared.flush()
        return .result()
    }
}

/// Shortcuts automation: "When YouTube is closed" -> App Closed (YouTube)
struct AppClosedIntent: AppIntent {
    static var title: LocalizedStringResource = "App closed"
    static var description = IntentDescription("Use in a Shortcuts automation: When <app> is Closed. Same app name as 'App opened'.")
    static var openAppWhenRun: Bool = false

    @Parameter(title: "App name")
    var app: String

    func perform() async throws -> some IntentResult {
        if let span = AppUsage.closed(app) {
            await Outbox.shared.logActivity(app: app, start: span.start, end: span.end)
        }
        return .result()
    }
}

struct LazyTrackShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: LogHabitIntent(),
            phrases: [
                "Log \(\.$kind) in \(.applicationName)",
                "\(.applicationName) log \(\.$kind)",
            ],
            shortTitle: "Log habit",
            systemImageName: "plus.circle.fill")
    }
}
