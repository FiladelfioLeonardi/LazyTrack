import Foundation
import CoreBluetooth
import UIKit

/// The LazyTrack Egg goes online through this phone when it has no WiFi, like a smartwatch.
///
/// The egg sends a request over Bluetooth ({"m": method, "u": url, "h": headers, "b": body}), the phone sends it
/// to the LazyTrack server and passes the answer back ("R<status>\n<body>"). It also gives the egg the time
/// ("T<unix time>"). Messages are cut into Bluetooth-sized pieces: first byte "C" = more follows, "E" = last.
///
/// Safety: the link is encrypted (iOS asks to pair once), and the phone only forwards to the LazyTrack server
/// (https, *.supabase.co), so nothing else can use your phone's internet. The egg brings its own login.
/// Works in the background: iOS wakes the app when the egg needs something (Background Modes: bluetooth-central).
final class EggLink: NSObject, ObservableObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    static let shared = EggLink()
    static let service = CBUUID(string: "6C740001-8F3B-4A2E-9D1C-3B5A7E0F4C21")
    static let txUUID = CBUUID(string: "6C740002-8F3B-4A2E-9D1C-3B5A7E0F4C21")   // egg -> phone (notify)
    static let rxUUID = CBUUID(string: "6C740003-8F3B-4A2E-9D1C-3B5A7E0F4C21")   // phone -> egg (write)
    static let allowedHostSuffix = ".supabase.co"

    @Published var status = "Off"
    @Published var relayed = 0
    @Published var enabled: Bool {
        didSet {
            UserDefaults.standard.set(enabled, forKey: "eggLink")
            enabled ? start() : stop()
        }
    }

    private var central: CBCentralManager!
    private var egg: CBPeripheral?
    private var rx: CBCharacteristic?
    private var inbox = Data()
    private var outbox: [Data] = []
    private var timeSent = Date.distantPast
    private var paired = false
    private var pairFailed = false   // turned away: wait a bit between tries (the egg may not be in pairing mode)
    private var connectedAt = Date.distantPast

    override init() {
        enabled = UserDefaults.standard.object(forKey: "eggLink") as? Bool ?? true
        super.init()
        // the restore key lets iOS relaunch the app in the background for the egg
        central = CBCentralManager(delegate: self, queue: nil,
                                   options: [CBCentralManagerOptionRestoreIdentifierKey: "lazytrack.egg"])
    }

    private var savedEgg: UUID? {
        get { UserDefaults.standard.string(forKey: "eggID").flatMap(UUID.init) }
        set { UserDefaults.standard.set(newValue?.uuidString, forKey: "eggID") }
    }

    // MARK: finding and keeping the egg

    func start() {
        guard enabled, central.state == .poweredOn else { return }
        if let p = egg, p.state == .connected || p.state == .connecting { return }
        if let id = savedEgg, let p = central.retrievePeripherals(withIdentifiers: [id]).first {
            connect(p)   // a known egg: iOS waits for it as long as it takes, even in the background
        } else if let p = central.retrieveConnectedPeripherals(withServices: [Self.service]).first {
            connect(p)
        } else {
            status = "Looking for your egg…"
            central.scanForPeripherals(withServices: [Self.service])
        }
    }

    func stop() {
        central.stopScan()
        if let p = egg { central.cancelPeripheralConnection(p) }
        egg = nil
        rx = nil
        status = "Off"
    }

    private func connect(_ p: CBPeripheral) {
        egg = p
        p.delegate = self
        status = "Connecting to your egg…"
        central.connect(p)
    }

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        switch c.state {
        case .poweredOn: start()
        case .unauthorized: status = "Allow Bluetooth for LazyTrack in Settings"
        case .poweredOff: status = "Bluetooth is off"
        default: break
        }
    }

    func centralManager(_ c: CBCentralManager, willRestoreState dict: [String: Any]) {
        if let p = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral])?.first {
            egg = p
            p.delegate = self
        }
    }

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral, advertisementData: [String: Any], rssi: NSNumber) {
        c.stopScan()
        connect(p)
    }

    func centralManager(_ c: CBCentralManager, didConnect p: CBPeripheral) {
        savedEgg = p.identifier
        connectedAt = Date()
        status = "Connected, setting up…"
        p.discoverServices([Self.service])
    }

    func centralManager(_ c: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        status = "Couldn't connect, trying again"
        if enabled { c.connect(p) }
    }

    func centralManager(_ c: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
        if !paired && Date().timeIntervalSince(connectedAt) < 20 { pairFailed = true }   // the egg turned us away
        rx = nil
        paired = false
        inbox = Data()
        outbox = []
        if pairFailed {
            pairFailed = false
            status = "Not paired. On the egg: Info → C (Phone) → B, then confirm the same number on both."
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in
                guard let self, self.enabled else { return }
                c.connect(p)
            }
            return
        }
        status = enabled ? "Egg out of reach, waiting for it" : "Off"
        if enabled { c.connect(p) }   // reconnects by itself when the egg is back
    }

    // MARK: setting up the link

    func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        guard let s = p.services?.first(where: { $0.uuid == Self.service }) else { return }
        p.discoverCharacteristics([Self.txUUID, Self.rxUUID], for: s)
    }

    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor s: CBService, error: Error?) {
        for ch in s.characteristics ?? [] {
            if ch.uuid == Self.rxUUID { rx = ch }
            if ch.uuid == Self.txUUID {
                p.setNotifyValue(true, for: ch)
                p.readValue(for: ch)   // an encrypted read: makes iOS pair now (the Pair pop-up the first time)
            }
        }
    }

    func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor ch: CBCharacteristic, error: Error?) {
        if let error {
            status = "Pairing needed: \(error.localizedDescription)"
            return
        }
        if ch.isNotifying && status != "Paired. The egg goes online through this phone." {
            status = "Connected, pairing…"
        }
    }

    // MARK: relaying

    func peripheral(_ p: CBPeripheral, didUpdateValueFor ch: CBCharacteristic, error: Error?) {
        guard ch.uuid == Self.txUUID else { return }
        if let error {   // not paired: the egg only accepts new phones in pairing mode (with the 6-digit code)
            pairFailed = true
            status = "Pairing didn't work, trying again… If it keeps saying this: iPhone Settings → Bluetooth → ⓘ LazyTrack Egg → Forget This Device."
            savedEgg = nil
            central.cancelPeripheralConnection(p)
            return
        }
        if !paired {   // the first answer (to the encrypted read): the link is encrypted now
            paired = true
            status = "Paired. The egg goes online through this phone."
            sendTime()   // the egg's clock
        }
        guard let v = ch.value, let kind = v.first else { return }
        inbox.append(v.dropFirst())
        guard kind == UInt8(ascii: "E") else { return }
        let msg = inbox
        inbox = Data()
        relay(msg)
    }

    private func relay(_ msg: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: msg) as? [String: Any],
              let method = obj["m"] as? String, let u = obj["u"] as? String, let url = URL(string: u),
              url.scheme == "https", let host = url.host, host.hasSuffix(Self.allowedHostSuffix)
        else {
            reply(400, Data("not allowed".utf8))
            return
        }
        var req = URLRequest(url: url, timeoutInterval: 15)
        req.httpMethod = method
        for (k, v) in obj["h"] as? [String: String] ?? [:] { req.setValue(v, forHTTPHeaderField: k) }
        if let b = obj["b"] as? String { req.httpBody = Data(b.utf8) }
        // woken in the background: ask iOS for a few more seconds to finish this request
        var bg = UIBackgroundTaskIdentifier.invalid
        bg = UIApplication.shared.beginBackgroundTask { UIApplication.shared.endBackgroundTask(bg) }
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
            DispatchQueue.main.async {
                self.reply(code, data ?? Data())
                self.relayed += 1
                UIApplication.shared.endBackgroundTask(bg)
            }
        }.resume()
    }

    private func reply(_ code: Int, _ body: Data) {
        if Date().timeIntervalSince(timeSent) > 300 { sendTime() }   // keeps the egg's clock right (also if pairing came late)
        var d = Data("R\(code)\n".utf8)
        d.append(body)
        send(d)
    }

    private func sendTime() {
        timeSent = Date()
        send(Data("T\(Int(Date().timeIntervalSince1970))".utf8))
    }

    private func send(_ msg: Data) {
        guard let p = egg else { return }
        let size = max(19, p.maximumWriteValueLength(for: .withoutResponse) - 1)   // a piece + its 1-byte header
        var i = msg.startIndex
        repeat {
            let end = msg.index(i, offsetBy: size, limitedBy: msg.endIndex) ?? msg.endIndex
            var frame = Data([end == msg.endIndex ? UInt8(ascii: "E") : UInt8(ascii: "C")])
            frame.append(msg[i..<end])
            outbox.append(frame)
            i = end
        } while i < msg.endIndex
        pump()
    }

    private func pump() {
        guard let p = egg, let rx else { return }
        while !outbox.isEmpty && p.canSendWriteWithoutResponse {
            p.writeValue(outbox.removeFirst(), for: rx, type: .withoutResponse)
        }
    }

    func peripheralIsReady(toSendWriteWithoutResponse p: CBPeripheral) { pump() }
}
