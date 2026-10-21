// Click Updater Fix — a window with one button, for the people who have this
// problem and no reason to own a terminal.
//
// HP Click 4.11.31 and earlier cannot run their own update helper: ShipIt's
// rpath names the directory above and the directory below the one holding
// Mantle.framework, so dyld kills it before main(). Squirrel registers it with
// launchd as "restart until it exits successfully", which it never does, so it
// restarts roughly every 2.3 seconds forever and logs a failure each time.
//
// The fix is two launchctl calls and deleting a log. No root, and nothing
// inside any application bundle is touched — patching ShipIt's rpath in place
// would break the app's code signature and macOS would then refuse to open it.
//
// Same logic as site/hpclick-update-loop.sh, which is the version for people
// who would rather read a script than trust an app.

import AppKit
import Foundation

let kLabel = "com.hp.hpclick.ShipIt"
let kCacheDir = NSHomeDirectory() + "/Library/Caches/" + kLabel
let kLogPath = kCacheDir + "/ShipIt_stderr.log"

// MARK: - running things

@discardableResult
func run(_ path: String, _ args: [String]) -> (out: String, err: String, code: Int32) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let o = Pipe(), e = Pipe()
    p.standardOutput = o; p.standardError = e
    do { try p.run() } catch { return ("", "\(error)", -1) }
    // Read before waiting: a pipe that fills while we block on exit deadlocks.
    let od = o.fileHandleForReading.readDataToEndOfFile()
    let ed = e.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (String(decoding: od, as: UTF8.self), String(decoding: ed, as: UTF8.self), p.terminationStatus)
}

func domainTarget() -> String { "gui/\(getuid())/\(kLabel)" }

// MARK: - what we found

enum Updater { case broken, works, absent, unknown }

struct Install {
    let name: String, version: String, state: Updater
}

struct Diagnosis {
    var installs: [Install] = []
    var jobRunning = false
    var crashes: Int?
    var blocked = false
    var logBytes: Int64 = 0

    var anyBroken: Bool { installs.contains { $0.state == .broken } }
    var anyWorking: Bool { installs.contains { $0.state == .works } }
    /// Something to do: it is looping now, there is a log to reclaim, or a
    /// broken build is present that will start looping when next opened.
    var actionable: Bool { jobRunning || logBytes > 0 || (anyBroken && !blocked) }
    var clean: Bool { !jobRunning && logBytes == 0 && blocked }
}

// MARK: - looking

enum Scanner {
    static func infoValue(_ app: String, _ key: String) -> String? {
        guard let d = NSDictionary(contentsOfFile: app + "/Contents/Info.plist") else { return nil }
        return d[key] as? String
    }

    /// Ask the helper itself. Run with no arguments it prints a usage line and
    /// quits, so this is safe — and unlike reading a version number it tests
    /// the actual fault. Judge by what it SAYS: a working ShipIt given no
    /// arguments still exits non-zero, so the exit code cannot tell "ran and
    /// complained" from "never started".
    static func updaterState(_ app: String) -> Updater {
        let candidates = [
            app + "/Contents/Frameworks/Squirrel.framework/Versions/A/ShipIt",
            app + "/Contents/Frameworks/Squirrel.framework/Resources/ShipIt",
        ]
        guard let bin = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) })
        else { return .absent }
        let r = run(bin, [])
        let text = r.out + r.err
        if text.contains("Library not loaded") { return .broken }
        if text.contains("Missing launchd job label") || text.contains("ShipIt quitting") { return .works }
        return .unknown
    }

    /// Stock HP Click only. Grafts carry com.hp.hpclick.arm64 and have the
    /// updater patched out, so flagging their ShipIt would be a false alarm.
    static func installs() -> [Install] {
        let fm = FileManager.default
        let names = (try? fm.contentsOfDirectory(atPath: "/Applications")) ?? []
        return names.sorted().compactMap { name in
            guard name.hasSuffix(".app") else { return nil }
            let app = "/Applications/" + name
            guard infoValue(app, "CFBundleIdentifier") == "com.hp.hpclick" else { return nil }
            return Install(name: name,
                           version: infoValue(app, "CFBundleShortVersionString") ?? "?",
                           state: updaterState(app))
        }
    }

    static func scan() -> Diagnosis {
        var d = Diagnosis()
        d.installs = installs()

        let printed = run("/bin/launchctl", ["print", domainTarget()])
        d.jobRunning = printed.code == 0
        if d.jobRunning {
            for line in printed.out.split(separator: "\n") where line.contains("successive crashes") {
                d.crashes = Int(line.split(separator: "=").last?
                    .trimmingCharacters(in: .whitespaces) ?? "")
                break
            }
        }
        let disabled = run("/bin/launchctl", ["print-disabled", "gui/\(getuid())"])
        d.blocked = disabled.out.contains("\"\(kLabel)\" => disabled")
            || disabled.out.contains("\"\(kLabel)\" => true")

        if let a = try? FileManager.default.attributesOfItem(atPath: kLogPath),
           let n = a[.size] as? NSNumber { d.logBytes = n.int64Value }
        return d
    }
}

// MARK: - changing things

enum Fixer {
    /// Returns bytes reclaimed.
    @discardableResult static func apply() -> Int64 {
        var freed: Int64 = 0
        if let a = try? FileManager.default.attributesOfItem(atPath: kLogPath),
           let n = a[.size] as? NSNumber { freed = n.int64Value }
        run("/bin/launchctl", ["bootout", domainTarget()])
        run("/bin/launchctl", ["disable", domainTarget()])
        for f in ["ShipIt_stderr.log", "ShipIt_stdout.log", "ShipItState.plist"] {
            try? FileManager.default.removeItem(atPath: kCacheDir + "/" + f)
        }
        return freed
    }

    static func undo() { run("/bin/launchctl", ["enable", domainTarget()]) }
}

// MARK: - presenting it

func humanBytes(_ b: Int64) -> String {
    let f = ByteCountFormatter()
    f.countStyle = .file
    return f.string(fromByteCount: b)
}

final class Controller: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let headline = NSTextField(labelWithString: "")
    let detail = NSTextField(wrappingLabelWithString: "")
    let list = NSTextField(wrappingLabelWithString: "")
    let action = NSButton(title: "", target: nil, action: nil)
    let undo = NSButton(title: "Undo", target: nil, action: nil)
    var d = Diagnosis()

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.regular)
        build()
        refresh()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func build() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 340),
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered, defer: false)
        window.title = "Click Updater Fix"
        window.center()

        headline.font = .systemFont(ofSize: 17, weight: .semibold)
        headline.lineBreakMode = .byWordWrapping
        headline.maximumNumberOfLines = 2
        detail.font = .systemFont(ofSize: 12)
        detail.textColor = .secondaryLabelColor
        list.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        list.textColor = .secondaryLabelColor

        action.bezelStyle = .rounded
        action.controlSize = .large
        action.keyEquivalent = "\r"
        action.target = self
        action.action = #selector(primary)

        undo.bezelStyle = .rounded
        undo.target = self
        undo.action = #selector(doUndo)

        let buttons = NSStackView(views: [undo, action])
        buttons.orientation = .horizontal
        buttons.spacing = 10

        let stack = NSStackView(views: [headline, detail, list, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.edgeInsets = NSEdgeInsets(top: 26, left: 26, bottom: 26, right: 26)
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = NSView()
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor),
            headline.widthAnchor.constraint(lessThanOrEqualToConstant: 500),
            detail.widthAnchor.constraint(lessThanOrEqualToConstant: 500),
            list.widthAnchor.constraint(lessThanOrEqualToConstant: 500),
        ])
        window.makeKeyAndOrderFront(nil)
    }

    func refresh() {
        d = Scanner.scan()

        if d.installs.isEmpty {
            list.stringValue = "No HP Click found in your Applications folder."
        } else {
            list.stringValue = d.installs.map { i in
                let note: String
                switch i.state {
                case .broken:  note = "affected"
                case .works:   note = "updater is fine (4.11.32 or newer)"
                case .absent:  note = "no updater"
                case .unknown: note = "could not tell"
                }
                return "  \(i.name)  —  \(i.version)  —  \(note)"
            }.joined(separator: "\n")
        }

        if d.jobRunning {
            headline.stringValue = "HP Click's updater is stuck in a loop."
            var bits = ["It is restarting about every 2 seconds and cannot ever succeed."]
            if let c = d.crashes { bits.append("It has failed \(c) times so far.") }
            if d.logBytes > 0 { bits.append("Its log file has reached \(humanBytes(d.logBytes)).") }
            detail.stringValue = bits.joined(separator: " ")
        } else if d.logBytes > 0 {
            headline.stringValue = "There is a \(humanBytes(d.logBytes)) log left behind."
            detail.stringValue = "The updater is not running right now, but its log is still "
                + "taking up space and it will start again next time HP Click is opened."
        } else if d.anyBroken && !d.blocked {
            headline.stringValue = "This Mac will start looping when you next open HP Click."
            detail.stringValue = "A version with the broken updater is installed. Stopping it now "
                + "prevents it, and changes nothing else."
        } else if d.clean || d.blocked {
            headline.stringValue = "All clear."
            var t = "Nothing is running, there is no log, and it cannot start again."
            if d.anyWorking {
                t += " Note that while the block is in place, the newer HP Click you also have "
                    + "cannot auto-update either — use Undo if you want that back."
            }
            detail.stringValue = t
        } else {
            headline.stringValue = "Nothing wrong here."
            detail.stringValue = "No sign of the stuck updater on this Mac."
        }

        action.title = d.actionable ? "Stop it" : "Re-check"
        undo.isHidden = !d.blocked
    }

    @objc func primary() {
        guard d.actionable else { refresh(); return }
        let freed = Fixer.apply()
        refresh()
        let a = NSAlert()
        a.messageText = "Done."
        var t = "The updater has been stopped and cannot start again.\n\nHP Click itself was not "
            + "changed and will open as usual."
        if freed > 0 { t = "Freed \(humanBytes(freed)).\n\n" + t }
        if d.anyBroken {
            t += "\n\nTo stop needing this at all, install HP Click 4.11.32 or newer from HP, "
                + "if it lists your printer."
        }
        a.informativeText = t
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window, completionHandler: nil)
    }

    @objc func doUndo() {
        Fixer.undo()
        refresh()
        let a = NSAlert()
        a.messageText = "Put back."
        a.informativeText = "HP Click may start its updater again. Nothing else was ever changed."
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window, completionHandler: nil)
    }
}

// --print renders exactly what the window would say, as text. It exists so the
// app can be checked without a screen -- by the tests, and by anyone who wants
// to know what it would do before letting it draw a window.
if CommandLine.arguments.contains("--print") {
    let d = Scanner.scan()
    print("installs:")
    if d.installs.isEmpty { print("  (none)") }
    for i in d.installs {
        let s: String
        switch i.state {
        case .broken: s = "broken"; case .works: s = "works"
        case .absent: s = "absent"; case .unknown: s = "unknown"
        }
        print("  \(i.name) | \(i.version) | \(s)")
    }
    print("jobRunning: \(d.jobRunning)")
    print("crashes: \(d.crashes.map(String.init) ?? "-")")
    print("blocked: \(d.blocked)")
    print("logBytes: \(d.logBytes)")
    print("actionable: \(d.actionable)")
    print("clean: \(d.clean)")
    exit(0)
}

let app = NSApplication.shared
let controller = Controller()
app.delegate = controller
app.run()
