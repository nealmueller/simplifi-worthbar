import AppKit
import Foundation

final class SimplifiWorthBarApp: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var refreshTimer: Timer?

    private let pythonPath = "/usr/bin/python3"
    private let appName = "SimplifiWorthBar"
    private let refreshInterval: TimeInterval = 300

    private let defaults = UserDefaults.standard
    private let displayModeKey = "SimplifiWorthBar.displayMode"

    private var statusMessage: String = "Initializing"

    private enum DisplayMode: String, CaseIterable {
        case compact
        case full
        case delta

        var title: String {
            switch self {
            case .compact: return "Compact ($1.2M +2%)"
            case .full: return "Full ($1,234,567 +2%)"
            case .delta: return "Delta Today (+$2.4K)"
            }
        }
    }

    private struct Snapshot: Decodable {
        let ok: Bool
        let source: String?
        let total: Double?
        let daily_percent: Double?
        let label: String?
        let error_code: String?
        let message: String?
    }

    private lazy var appSupportDir: URL = {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support")
            .appendingPathComponent(appName)
    }()

    private lazy var scriptPath: String = {
        appSupportDir.appendingPathComponent("get_networth_label.py").path
    }()

    private lazy var baselineFile: URL = {
        appSupportDir.appendingPathComponent("daily_baseline.json")
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "$..."

        refreshLabel()
        statusItem.menu = buildMenu()

        refreshTimer = Timer.scheduledTimer(withTimeInterval: refreshInterval, repeats: true) { [weak self] _ in
            self?.refreshLabel()
        }
    }

    @objc private func refreshNow() {
        refreshLabel()
    }

    @objc private func openSimplifi() {
        if let url = URL(string: "https://simplifi.quicken.com/") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func copyDiagnostics() {
        let output = runProcess(pythonPath, args: [scriptPath, "--diagnostics"]).stdout
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(output.isEmpty ? "No diagnostics output" : output, forType: .string)
    }

    @objc private func selectDisplayMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let mode = DisplayMode(rawValue: raw) else {
            return
        }
        defaults.set(mode.rawValue, forKey: displayModeKey)
        refreshLabel()
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }

    private func selectedDisplayMode() -> DisplayMode {
        let raw = defaults.string(forKey: displayModeKey) ?? DisplayMode.compact.rawValue
        return DisplayMode(rawValue: raw) ?? .compact
    }

    private func refreshLabel() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }

            let mode = self.selectedDisplayMode()
            let result = self.runProcess(self.pythonPath, args: [self.scriptPath, "--json"])

            let nextTitle: String
            var nextStatus = "Unavailable"

            if let data = result.stdout.data(using: .utf8),
               let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) {
                if snapshot.ok, let total = snapshot.total, let pct = snapshot.daily_percent {
                    nextTitle = self.renderTitle(mode: mode, total: total, dailyPercent: pct)
                    let src = snapshot.source ?? "unknown"
                    nextStatus = "OK (source: \(src))"
                } else {
                    let code = snapshot.error_code ?? "unavailable"
                    if code == "signin_required" {
                        nextTitle = "Sign In"
                        nextStatus = "Sign in to Simplifi in MenubarX, then refresh"
                    } else {
                        nextTitle = "$--"
                        let msg = snapshot.message ?? "Unknown error"
                        nextStatus = "Error: \(msg)"
                    }
                }
            } else {
                nextTitle = "$--"
                let err = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
                nextStatus = err.isEmpty ? "Unexpected script output" : err
            }

            DispatchQueue.main.async {
                self.statusMessage = nextStatus
                self.statusItem.button?.title = nextTitle
                self.statusItem.menu = self.buildMenu()
            }
        }
    }

    private func renderTitle(mode: DisplayMode, total: Double, dailyPercent: Double) -> String {
        switch mode {
        case .compact:
            return "\(formatCompactUSD(total)) \(formatSignedPercent(dailyPercent))"
        case .full:
            return "\(formatFullUSD(total)) \(formatSignedPercent(dailyPercent))"
        case .delta:
            let delta = updateAndComputeDailyDelta(currentTotal: total)
            return formatSignedDelta(delta)
        }
    }

    private func updateAndComputeDailyDelta(currentTotal: Double) -> Double {
        let date = currentDateYMD()

        if let data = try? Data(contentsOf: baselineFile),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let storedDate = obj["date"] as? String,
           let storedTotal = obj["total"] as? Double,
           storedDate == date {
            return currentTotal - storedTotal
        }

        // New day (or missing baseline): reset baseline to first seen value.
        try? FileManager.default.createDirectory(at: appSupportDir, withIntermediateDirectories: true)
        let payload: [String: Any] = ["date": date, "total": currentTotal]
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted]) {
            try? data.write(to: baselineFile, options: .atomic)
        }
        return 0.0
    }

    private func currentDateYMD() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter.string(from: Date())
    }

    private func formatCompactUSD(_ value: Double) -> String {
        let sign = value < 0 ? "-" : ""
        let absolute = abs(value)
        let shown: Double
        let suffix: String

        if absolute >= 1_000_000_000_000 {
            shown = absolute / 1_000_000_000_000
            suffix = "T"
        } else if absolute >= 1_000_000_000 {
            shown = absolute / 1_000_000_000
            suffix = "B"
        } else if absolute >= 1_000_000 {
            shown = absolute / 1_000_000
            suffix = "M"
        } else if absolute >= 1_000 {
            shown = absolute / 1_000
            suffix = "K"
        } else {
            shown = absolute
            suffix = ""
        }

        let number: String
        if suffix.isEmpty {
            number = String(format: "%.0f", shown)
        } else {
            number = String(format: "%.1f", shown).replacingOccurrences(of: #"\.0$"#, with: "", options: .regularExpression)
        }

        return "\(sign)$\(number)\(suffix)"
    }

    private func formatFullUSD(_ value: Double) -> String {
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.maximumFractionDigits = 0
        f.locale = Locale(identifier: "en_US")
        return f.string(from: NSNumber(value: value)) ?? String(format: "$%.0f", value)
    }

    private func formatSignedPercent(_ value: Double) -> String {
        let rounded = Int(value.rounded())
        let sign = rounded >= 0 ? "+" : ""
        return "\(sign)\(rounded)%"
    }

    private func formatSignedDelta(_ value: Double) -> String {
        let sign = value >= 0 ? "+" : "-"
        let compact = formatCompactUSD(abs(value)).replacingOccurrences(of: "$", with: "")
        return "\(sign)$\(compact)"
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()

        let refreshItem = NSMenuItem(title: "Refresh Now", action: #selector(refreshNow), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)

        let openItem = NSMenuItem(title: "Open Simplifi", action: #selector(openSimplifi), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)

        let diagItem = NSMenuItem(title: "Copy Diagnostics", action: #selector(copyDiagnostics), keyEquivalent: "d")
        diagItem.target = self
        menu.addItem(diagItem)

        menu.addItem(.separator())

        let modeHeader = NSMenuItem(title: "Display Mode", action: nil, keyEquivalent: "")
        modeHeader.isEnabled = false
        menu.addItem(modeHeader)

        let selected = selectedDisplayMode().rawValue
        for mode in DisplayMode.allCases {
            let item = NSMenuItem(title: mode.title, action: #selector(selectDisplayMode(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = mode.rawValue
            item.state = (mode.rawValue == selected) ? .on : .off
            menu.addItem(item)
        }

        menu.addItem(.separator())
        let statusItem = NSMenuItem(title: statusMessage, action: nil, keyEquivalent: "")
        statusItem.isEnabled = false
        menu.addItem(statusItem)

        menu.addItem(.separator())
        let quitItem = NSMenuItem(title: "Quit Simplifi WorthBar", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        return menu
    }

    private func runProcess(_ path: String, args: [String]) -> (stdout: String, stderr: String, exitCode: Int32) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: path)
        task.arguments = args

        let stdout = Pipe()
        let stderr = Pipe()
        task.standardOutput = stdout
        task.standardError = stderr

        do {
            try task.run()
            task.waitUntilExit()
        } catch {
            return ("", "Failed to run process: \(error)", 1)
        }

        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        let errData = stderr.fileHandleForReading.readDataToEndOfFile()
        let out = String(data: outData, encoding: .utf8) ?? ""
        let err = String(data: errData, encoding: .utf8) ?? ""
        return (out, err, task.terminationStatus)
    }
}

let app = NSApplication.shared
let delegate = SimplifiWorthBarApp()
app.delegate = delegate
app.run()
