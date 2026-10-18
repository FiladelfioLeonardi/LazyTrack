import SwiftUI
import WebKit
import Combine

/// The app's main screen: the real LazyTrack (the same pretty app as on the Mac, served by LazyTrack on your Mac
/// through Tailscale), full screen. The phone-only things (egg Bluetooth, gym, Apple Health, food scanner) run
/// natively around it; the page opens them with lazytrack-app://setup and lazytrack-app://food
/// (Settings → App → "iPhone app" on the page, shown only inside this app).
enum NativeSheet: String, Identifiable {
    case setup, food
    var id: String { rawValue }
}

struct WebHome: UIViewRepresentable {
    let url: URL
    @Binding var sheet: NativeSheet?
    @Binding var failed: String?

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        // the page's "Your egg" panel talks to the phone's Bluetooth link through this (window.webkit.messageHandlers.egg)
        cfg.userContentController.add(context.coordinator, name: "egg")
        cfg.applicationNameForUserAgent = "LazyTrackApp/1"   // the page knows it's inside the iPhone app
        cfg.allowsInlineMediaPlayback = true
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.isOpaque = false
        web.backgroundColor = .clear
        web.scrollView.backgroundColor = .clear
        web.scrollView.contentInsetAdjustmentBehavior = .never   // the page keeps clear of the home bar itself
        let refresh = UIRefreshControl()                          // pull down to reload
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.reload(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh
        context.coordinator.web = web
        web.load(URLRequest(url: url))
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let parent: WebHome
        weak var web: WKWebView?
        private var watch: AnyCancellable?

        init(_ parent: WebHome) {
            self.parent = parent
            super.init()
            let egg = EggLink.shared   // tell the page whenever the Bluetooth link changes
            watch = Publishers.CombineLatest4(egg.$status, egg.$enabled, egg.$relayed, egg.$linked)
                .debounce(for: .milliseconds(150), scheduler: RunLoop.main)
                .sink { [weak self] _ in self?.sendEgg() }
        }

        /// {"do": "get" | "on" | "off" | "forget" | "cmd", "cmd": "sing" | "find" | "sync"} from the page
        func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == "egg", let m = message.body as? [String: Any], let what = m["do"] as? String else { return }
            let egg = EggLink.shared
            switch what {
            case "on": egg.enabled = true
            case "off": egg.enabled = false
            case "forget": egg.forget()
            case "cmd":   // instant over Bluetooth; the page hears back whether it went (else it uses the server)
                let ok = egg.command(m["cmd"] as? String ?? "")
                web?.evaluateJavaScript("window.eggCmdSent && window.eggCmdSent(\(ok ? "true" : "false"))")
                return
            default: break
            }
            sendEgg()
        }

        private func sendEgg() {
            let egg = EggLink.shared
            let state: [String: Any] = ["enabled": egg.enabled, "status": egg.status, "relayed": egg.relayed,
                                        "linked": egg.linked, "known": egg.known]
            guard let data = try? JSONSerialization.data(withJSONObject: state), let json = String(data: data, encoding: .utf8) else { return }
            web?.evaluateJavaScript("window.eggNative && window.eggNative(\(json))")
        }

        @objc func reload(_ control: UIRefreshControl) {
            web?.reload()
            control.endRefreshing()
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let u = action.request.url else { return decisionHandler(.allow) }
            if u.scheme == "lazytrack-app" {                       // the page asks for a phone screen
                parent.sheet = NativeSheet(rawValue: u.host ?? "")
                return decisionHandler(.cancel)
            }
            if action.navigationType == .linkActivated, let host = u.host, host != parent.url.host {
                UIApplication.shared.open(u)                         // other websites open in Safari
                return decisionHandler(.cancel)
            }
            decisionHandler(.allow)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            parent.failed = error.localizedDescription
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            parent.failed = nil
            sendEgg()
        }

        // the page's confirm() / alert() (Forget this egg, Uninstall...): real iOS alerts
        private func present(_ a: UIAlertController) {
            let root = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first?.rootViewController
            var top = root
            while let p = top?.presentedViewController { top = p }
            top?.present(a, animated: true)
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
            a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
            present(a)
        }

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
            present(a)
        }

        // links that open a new window (target=_blank): open them here
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let u = action.request.url { webView.load(URLRequest(url: u)) }
            return nil
        }
    }
}

/// Picks what to show: LazyTrack itself once the phone is connected to your Mac, otherwise the setup.
struct RootView: View {
    @AppStorage("serverURL") private var serverURL = ""
    @AppStorage("token") private var token = ""
    @Environment(\.colorScheme) private var scheme
    @State private var sheet: NativeSheet?
    @State private var failed: String?
    @State private var reloadKey = 0

    private var homeURL: URL? {
        guard !serverURL.isEmpty else { return nil }
        let t = token.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? ""
        return URL(string: "\(serverURL)/?token=\(t)")
    }

    var body: some View {
        Group {
            if let url = homeURL {
                ZStack {
                    (scheme == .dark ? Color(red: 0.09, green: 0.08, blue: 0.12) : Color(red: 0.94, green: 0.92, blue: 1))
                        .ignoresSafeArea()
                    WebHome(url: url, sheet: $sheet, failed: $failed)
                        .id("\(url.absoluteString)#\(reloadKey)")
                        .ignoresSafeArea(edges: .bottom)
                    if let failed {
                        VStack(spacing: 12) {
                            Text("Can't reach LazyTrack on your Mac").font(.headline)
                            Text("Is the Mac on, with LazyTrack running, and Tailscale on on both? (\(failed))")
                                .font(.footnote).multilineTextAlignment(.center).foregroundStyle(.secondary)
                            Button("Try again") { self.failed = nil; reloadKey += 1 }.buttonStyle(.borderedProminent)
                            Button("Phone settings") { sheet = .setup }
                        }
                        .padding(24)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                        .padding(24)
                    }
                }
            } else {
                SetupView()   // not connected to the Mac yet
            }
        }
        .sheet(item: $sheet) { s in
            switch s {
            case .setup: SetupView()
            case .food: FoodView()
            }
        }
    }
}
