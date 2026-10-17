import Foundation
import HealthKit

/// Reads sleep and steps from Apple Health and sends one number per day to the Mac.
enum HealthSync {
    static let store = HKHealthStore()
    private static let sleepType = HKObjectType.categoryType(forIdentifier: .sleepAnalysis)!
    private static let stepType = HKObjectType.quantityType(forIdentifier: .stepCount)!

    static var enabled: Bool {
        get { UserDefaults.standard.bool(forKey: "healthEnabled") }
        set { UserDefaults.standard.set(newValue, forKey: "healthEnabled") }
    }

    static func connect() async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw HealthError.unavailable }
        try await store.requestAuthorization(toShare: [], read: [sleepType, stepType])
        enabled = true
    }

    /// Cheap to call often: only syncs every 3 hours.
    static func syncIfDue() async {
        guard enabled else { return }
        let last = UserDefaults.standard.double(forKey: "healthSyncedAt")
        guard Date().timeIntervalSince1970 - last > 3 * 3600 else { return }
        _ = await sync()
    }

    /// Returns a short description of what was sent.
    @discardableResult
    static func sync(days: Int = 7) async -> String {
        guard enabled, HKHealthStore.isHealthDataAvailable() else { return "Health not connected" }
        let cal = Calendar.current
        let end = Date()
        let start = cal.startOfDay(for: cal.date(byAdding: .day, value: -days, to: end) ?? end)

        let sleepSamples = (try? await fetchSleep(start: start.addingTimeInterval(-12 * 3600), end: end)) ?? []
        let sleep = sleepPerDay(sleepSamples).filter { $0.key >= Day.string(start) }
        for (day, seconds) in sleep {
            await Outbox.shared.logMetric(day: day, metric: "sleep", value: seconds)
        }
        let steps = (try? await fetchSteps(start: start, end: end)) ?? [:]
        for (day, count) in steps where count > 0 {
            await Outbox.shared.logMetric(day: day, metric: "steps", value: count)
        }
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "healthSyncedAt")

        let lastNight = sleep[Day.string(end)].map { String(format: "%.1fh", $0 / 3600) } ?? "no data"
        let today = steps[Day.string(end)].map { "\(Int($0))" } ?? "0"
        return "Last night: \(lastNight) · Steps today: \(today)"
    }

    // MARK: - Queries

    private static func fetchSleep(start: Date, end: Date) async throws -> [HKCategorySample] {
        try await withCheckedThrowingContinuation { cont in
            let predicate = HKQuery.predicateForSamples(withStart: start, end: end)
            let q = HKSampleQuery(sampleType: sleepType, predicate: predicate,
                                  limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, error in
                if let error {
                    cont.resume(throwing: error)
                } else {
                    cont.resume(returning: (samples as? [HKCategorySample]) ?? [])
                }
            }
            store.execute(q)
        }
    }

    private static func fetchSteps(start: Date, end: Date) async throws -> [String: Double] {
        try await withCheckedThrowingContinuation { cont in
            let q = HKStatisticsCollectionQuery(
                quantityType: stepType,
                quantitySamplePredicate: HKQuery.predicateForSamples(withStart: start, end: end),
                options: .cumulativeSum,
                anchorDate: start,
                intervalComponents: DateComponents(day: 1))
            q.initialResultsHandler = { _, results, error in
                if let error {
                    cont.resume(throwing: error)
                    return
                }
                var out: [String: Double] = [:]
                results?.enumerateStatistics(from: start, to: end) { stats, _ in
                    out[Day.string(stats.startDate)] = stats.sumQuantity()?.doubleValue(for: .count()) ?? 0
                }
                cont.resume(returning: out)
            }
            store.execute(q)
        }
    }

    /// Sums "asleep" time per night. A night belongs to the day you wake up.
    /// Overlapping samples (iPhone + Watch) are merged so nothing is counted twice.
    static func sleepPerDay(_ samples: [HKCategorySample]) -> [String: Double] {
        let asleep = Set(HKCategoryValueSleepAnalysis.allAsleepValues.map { $0.rawValue })
        var byDay: [String: [(start: Date, end: Date)]] = [:]
        for s in samples where asleep.contains(s.value) {
            // shift by 6h so a segment ending at 23:40 still counts for the next morning
            let day = Day.string(s.endDate.addingTimeInterval(6 * 3600))
            byDay[day, default: []].append((s.startDate, s.endDate))
        }
        var out: [String: Double] = [:]
        for (day, intervals) in byDay {
            let sorted = intervals.sorted { $0.start < $1.start }
            guard var cur = sorted.first else { continue }
            var total: TimeInterval = 0
            for iv in sorted.dropFirst() {
                if iv.start <= cur.end {
                    cur.end = max(cur.end, iv.end)
                } else {
                    total += cur.end.timeIntervalSince(cur.start)
                    cur = iv
                }
            }
            total += cur.end.timeIntervalSince(cur.start)
            out[day] = total
        }
        return out
    }

    enum HealthError: LocalizedError {
        case unavailable
        var errorDescription: String? { "Apple Health isn't available on this device." }
    }
}
