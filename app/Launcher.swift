// Launcher.swift — DocLens.app 启动器: 拉起 PyInstaller 后端 + WKWebView 原生窗口
import Cocoa
import WebKit

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var serverProc: Process?
    var pollTimer: Timer?
    let port = 8766

    func applicationDidFinishLaunching(_ n: Notification) {
        let res = Bundle.main.resourcePath  // .../DocLens.app/Contents/Resources
        let backend = "\(res!)/backend/server"

        // 1. 找空闲端口改为固定 8766(简单起见); 若被占用则尝试复用(已在运行)
        // 2. 启动后端
        let p = Process()
        p.executableURL = URL(fileURLWithPath: backend)
        p.arguments = ["--port", "\(port)",
                       "--html", "\(res!)/web/index.html",
                       "--ocr-bin", "\(res!)/bin/ocr_bin"]
        p.environment = ProcessInfo.processInfo.environment
        let logPath = NSHomeDirectory() + "/Library/Logs/DocLens.log"
        if let logH = FileHandle(forWritingAtPath: logPath) {
            logH.seekToEndOfFile()
            p.standardOutput = logH
            p.standardError = logH
        }
        do { try p.run(); serverProc = p }
        catch { fatalError("backend start failed: \(error)") }

        // 3. 窗口 + WebView
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "DocLens — 文档扫描"
        window.center()
        let cfg = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.navigationDelegate = self
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)

        // 4. 轮询后端就绪后加载页面
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] t in
            guard let self = self else { t.invalidate(); return }
            if self.serverReady() {
                t.invalidate()
                self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/")!))
            }
        }
    }

    func serverReady() -> Bool {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(port)/api/config")!)
        req.timeoutInterval = 1.5
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            if let h = resp as? HTTPURLResponse, h.statusCode == 200 { ok = true }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return ok
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ n: Notification) {
        serverProc?.terminate()
    }
}

let app = NSApplication.shared
let dlg = AppDelegate()
app.delegate = dlg
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
