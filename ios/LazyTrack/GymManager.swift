import CoreLocation
import Foundation

/// Logs a gym visit automatically when you spend 30+ minutes inside the gym geofence, and tells the Mac when you
/// arrive and leave (Mochi works out while you are there).
/// iOS wakes the app for region enter/exit even when it's closed (needs "Always" location).
final class GymManager: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = GymManager()

    @Published var gymSet: Bool
    @Published var status = ""
    @Published var authText = ""

    private let lm = CLLocationManager()
    private var pendingSetGym = false
    private let minMinutes = 30.0
    private let radius: CLLocationDistance = 120

    override init() {
        gymSet = UserDefaults.standard.object(forKey: "gymLat") != nil
        super.init()
        lm.delegate = self
        updateAuthText()
    }

    func setGymHere() {
        pendingSetGym = true
        status = "Getting your location…"
        lm.desiredAccuracy = kCLLocationAccuracyBest
        switch lm.authorizationStatus {
        case .notDetermined:
            lm.requestAlwaysAuthorization()  // location is requested once you answer
        case .authorizedWhenInUse:
            lm.requestAlwaysAuthorization()  // asks once to upgrade to Always
            lm.requestLocation()
        case .authorizedAlways:
            lm.requestLocation()
        default:
            pendingSetGym = false
            status = "Location is off for LazyTrack. Turn it on in Settings → LazyTrack → Location → Always."
        }
    }

    func removeGym() {
        for r in lm.monitoredRegions { lm.stopMonitoring(for: r) }
        UserDefaults.standard.removeObject(forKey: "gymLat")
        UserDefaults.standard.removeObject(forKey: "gymLon")
        gymSet = false
        status = "Gym removed"
    }

    private func updateAuthText() {
        switch lm.authorizationStatus {
        case .authorizedAlways: authText = "Location: Always ✅"
        case .authorizedWhenInUse: authText = "Location: While Using ⚠️ set it to Always in Settings"
        case .denied, .restricted: authText = "Location: denied ❌ enable it in Settings"
        default: authText = "Location: not asked yet"
        }
    }

    // MARK: CLLocationManagerDelegate

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        updateAuthText()
        let s = manager.authorizationStatus
        if pendingSetGym && (s == .authorizedAlways || s == .authorizedWhenInUse) {
            manager.requestLocation()
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard pendingSetGym, let loc = locations.last else { return }
        pendingSetGym = false
        UserDefaults.standard.set(loc.coordinate.latitude, forKey: "gymLat")
        UserDefaults.standard.set(loc.coordinate.longitude, forKey: "gymLon")
        for r in manager.monitoredRegions { manager.stopMonitoring(for: r) }
        let region = CLCircularRegion(center: loc.coordinate, radius: radius, identifier: "gym")
        region.notifyOnEntry = true
        region.notifyOnExit = true
        manager.startMonitoring(for: region)
        // you're at the gym right now, so start the visit timer
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "gymEnteredAt")
        gymSet = true
        status = "Gym saved ✅ Visits of \(Int(minMinutes))+ min are logged automatically."
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if pendingSetGym {
            pendingSetGym = false
            status = "Couldn't get location: \(error.localizedDescription)"
        }
    }

    func locationManager(_ manager: CLLocationManager, didEnterRegion region: CLRegion) {
        guard region.identifier == "gym" else { return }
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "gymEnteredAt")
        Task { await Outbox.shared.atGym(true) }   // Mochi starts working out
    }

    func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) {
        guard region.identifier == "gym" else { return }
        Task { await Outbox.shared.atGym(false) }  // ...and stops
        let t = UserDefaults.standard.double(forKey: "gymEnteredAt")
        UserDefaults.standard.removeObject(forKey: "gymEnteredAt")
        guard t > 0 else { return }
        let minutes = (Date().timeIntervalSince1970 - t) / 60
        guard minutes >= minMinutes, minutes < 6 * 60 else { return }
        Task { await Outbox.shared.logEvent("gym", note: "\(Int(minutes)) min", source: "geofence") }
        Notifier.send("🏋️ Gym logged", "\(Int(minutes)) minutes. Nice work.")
    }
}
