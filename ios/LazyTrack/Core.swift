import Foundation
import UserNotifications

// MARK: - Settings (shared with @AppStorage in SetupView via the same keys)

enum Settings {
    private static var d: UserDefaults { .standard }

    static var serverURL: String {
        get { d.string(forKey: "serverURL") ?? "" }
        set { d.set(newValue, forKey: "serverURL") }
    }
    static var token: String {
        get { d.string(forKey: "token") ?? "" }
        set { d.set(newValue, forKey: "token") }
    }
    static var deviceName: String {
        let n = d.string(forKey: "deviceName") ?? ""
        return n.isEmpty ? "iphone" : n
    }
    static var isConfigured: Bool { !serverURL.isEmpty }

    /// Handles lazytrack://setup?url=http%3A%2F%2F100.x.y.z%3A8042&token=abc (the QR from `./lt phone`)
    static func apply(setupURL: URL) -> Bool {
        guard setupURL.scheme == "lazytrack", setupURL.host == "setup",
              let items = URLComponents(url: setupURL, resolvingAgainstBaseURL: false)?.queryItems,
              let server = items.first(where: { $0.name == "url" })?.value, !server.isEmpty
        else { return false }
        serverURL = server.hasSuffix("/") ? String(server.dropLast()) : server
        token = items.first(where: { $0.name == "token" })?.value ?? ""
        return true
    }

    /// A pasted LazyTrack link (http://100.x.y.z:8042/?token=abc, what Settings → Your phone shows on the Mac):
    /// splits it into the server address and the token. Returns false if it isn't one.
    static func applyPasted(_ text: String) -> Bool {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let u = URL(string: t), u.scheme == "lazytrack" { return apply(setupURL: u) }
        guard let c = URLComponents(string: t), let scheme = c.scheme, scheme.hasPrefix("http"), let host = c.host,
              let tok = c.queryItems?.first(where: { $0.name == "token" })?.value, !tok.isEmpty
        else { return false }
        serverURL = "\(scheme)://\(host)" + (c.port.map { ":\($0)" } ?? "")
        token = tok
        return true
    }
}

// MARK: - Outbox: every request is saved first, then sent. Offline? It waits and retries.

struct PendingRequest: Codable {
    let path: String
    let body: Data
}

actor Outbox {
    static let shared = Outbox()

    private var queue: [PendingRequest] = []
    private var flushing = false
    private let fileURL: URL

    init() {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        fileURL = dir.appendingPathComponent("outbox.json")
        if let data = try? Data(contentsOf: fileURL),
           let saved = try? JSONDecoder().decode([PendingRequest].self, from: data) {
            queue = saved
        }
    }

    var pendingCount: Int { queue.count }

    func send(_ path: String, _ payload: [String: Any]) async {
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else { return }
        queue.append(PendingRequest(path: path, body: body))
        save()
        await flush()
    }

    func flush() async {
        guard !flushing, Settings.isConfigured else { return }
        flushing = true
        defer { flushing = false }
        while let next = queue.first {
            guard await Self.post(next) else { break }  // Mac unreachable: try again later
            queue.removeFirst()
            save()
        }
    }

    private func save() {
        if let data = try? JSONEncoder().encode(queue) {
            try? data.write(to: fileURL, options: .atomic)
        }
    }

    private static func request(_ path: String) -> URLRequest? {
        guard let url = URL(string: Settings.serverURL + path) else { return nil }
        var req = URLRequest(url: url, timeoutInterval: 8)
        if !Settings.token.isEmpty { req.setValue(Settings.token, forHTTPHeaderField: "X-Token") }
        return req
    }

    private static func post(_ r: PendingRequest) async -> Bool {
        guard var req = request(r.path) else { return false }
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = r.body
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if code == 400 { return true }  // malformed, will never succeed: drop it
            return (200..<300).contains(code)
        } catch {
            return false
        }
    }

    static func ping() async -> String {
        guard Settings.isConfigured, let req = request("/api/ping") else { return "❌ No Mac address set" }
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            switch (resp as? HTTPURLResponse)?.statusCode ?? 0 {
            case 200: return "✅ Connected to your Mac"
            case 401: return "❌ Wrong token"
            case let c: return "❌ HTTP \(c)"
            }
        } catch {
            return "❌ \(error.localizedDescription)\nIs the Mac awake, LazyTrack running and Tailscale on?"
        }
    }

    // MARK: helpers

    func logEvent(_ kind: String, note: String = "", source: String = "phone") async {
        TodayCounter.add(kind)
        await send("/api/event", ["kind": kind, "device": Settings.deviceName, "source": source,
                                  "note": note, "ts": Date().timeIntervalSince1970])
    }

    func logActivity(app: String, start: Date, end: Date) async {
        await send("/api/activity", ["device": Settings.deviceName, "app": app, "category": "auto",
                                     "start": start.timeIntervalSince1970, "end": end.timeIntervalSince1970])
    }

    func logMetric(day: String, metric: String, value: Double) async {
        await send("/api/metric", ["day": day, "metric": metric, "value": value, "device": Settings.deviceName])
    }
}

// MARK: - Small helpers

enum Day {
    private static let formatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
    static func string(_ date: Date) -> String { formatter.string(from: date) }
}

/// Local "x today" counters for the buttons, so they work offline too.
enum TodayCounter {
    private static var key: String { "counts." + Day.string(Date()) }
    static func load() -> [String: Int] {
        (UserDefaults.standard.dictionary(forKey: key) as? [String: Int]) ?? [:]
    }
    static func add(_ kind: String) {
        var d = load()
        d[kind, default: 0] += 1
        UserDefaults.standard.set(d, forKey: key)
    }
}

/// Remembers when an app was opened (from the Shortcuts automation) to compute time spent.
enum AppUsage {
    static func opened(_ app: String) {
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "open." + app.lowercased())
    }
    static func closed(_ app: String) -> (start: Date, end: Date)? {
        let key = "open." + app.lowercased()
        let t = UserDefaults.standard.double(forKey: key)
        UserDefaults.standard.removeObject(forKey: key)
        guard t > 0 else { return nil }
        let start = Date(timeIntervalSince1970: t), end = Date()
        let seconds = end.timeIntervalSince(start)
        guard seconds > 2, seconds < 6 * 3600 else { return nil }  // ignore glitches / missed "closed"
        return (start, end)
    }
}

enum Notifier {
    static func requestAuth() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }
    static func send(_ title: String, _ body: String) {
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = body
        c.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }
}
