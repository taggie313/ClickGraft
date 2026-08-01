// ClickGraft — native AppKit front end.
//
// Replaces both a tkinter wizard (Apple's system Tk 8.5.9 maps widgets and
// never paints them on current macOS) and a browser-served one (worked, but
// shipping a local web server as a Mac app is the wrong shape).
//
// All the real work stays in Python. This drives it by spawning
//   python3 -m clickgraft.cli agent ...
// and reading one JSON object per line, so there is no build logic here to
// drift out of step with the backend.
//
// Builds with the Command Line Tools alone:
//   swiftc -O -o ClickGraft ClickGraft.swift -framework AppKit

import AppKit
import Foundation

// MARK: - Backend

final class Agent {
    let resources: URL
    init(resources: URL) { self.resources = resources }

    private func process(_ args: [String]) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = ["-m", "clickgraft.cli", "agent"] + args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = resources.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"   // never dirty a signed bundle
        p.environment = env
        p.currentDirectoryURL = resources
        return p
    }

    /// One-shot call returning the first JSON object printed.
    func once(_ args: [String]) -> [String: Any]? {
        let p = process(args)
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        for line in String(decoding: data, as: UTF8.self).split(separator: "\n") {
            if let d = line.data(using: .utf8),
               let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                return obj
            }
        }
        return nil
    }

    /// Streaming call: `onEvent` fires on the main queue per JSON line.
    func stream(_ args: [String], onEvent: @escaping ([String: Any]) -> Void) {
        let p = process(args)
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        var buffer = Data()
        out.fileHandleForReading.readabilityHandler = { fh in
            buffer.append(fh.availableData)
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = buffer.subdata(in: buffer.startIndex..<nl)
                buffer.removeSubrange(buffer.startIndex...nl)
                if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                    DispatchQueue.main.async { onEvent(obj) }
                }
            }
        }
        p.terminationHandler = { _ in
            out.fileHandleForReading.readabilityHandler = nil
        }
        try? p.run()
    }
}

/// Top-anchored container. Without this, content shorter than the window
/// renders against the bottom edge, because AppKit's default coordinate
/// origin is bottom-left.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

// MARK: - Small view helpers

func label(_ text: String, size: CGFloat = 13, weight: NSFont.Weight = .regular,
           color: NSColor = .labelColor, mono: Bool = false) -> NSTextField {
    let f = NSTextField(wrappingLabelWithString: text)
    f.font = mono ? NSFont.monospacedSystemFont(ofSize: size, weight: weight)
                  : NSFont.systemFont(ofSize: size, weight: weight)
    f.textColor = color
    f.isSelectable = true
    f.setContentHuggingPriority(.defaultLow, for: .horizontal)
    return f
}

func vstack(_ views: [NSView], spacing: CGFloat = 10,
            align: NSLayoutConstraint.Attribute = .leading) -> NSStackView {
    let s = NSStackView(views: views)
    s.orientation = .vertical
    s.alignment = align
    s.spacing = spacing
    s.translatesAutoresizingMaskIntoConstraints = false
    return s
}

func hstack(_ views: [NSView], spacing: CGFloat = 10) -> NSStackView {
    let s = NSStackView(views: views)
    s.orientation = .horizontal
    s.spacing = spacing
    return s
}

func button(_ title: String, _ target: AnyObject, _ action: Selector,
            key: String = "") -> NSButton {
    let b = NSButton(title: title, target: target, action: action)
    b.bezelStyle = .rounded
    b.keyEquivalent = key
    return b
}

// MARK: - Controller

final class Wizard: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var agent: Agent!
    var content: NSView!

    var candidates: [[String: Any]] = []
    var picked: [String: Any]?
    var plan: [String: Any] = [:]
    var outputPath = ""
    var logPath = ""

    var progressBar: NSProgressIndicator?
    var progressLabel: NSTextField?
    var logView: NSTextView?

    // -- lifecycle ------------------------------------------------------
    func applicationDidFinishLaunching(_ note: Notification) {
        let exe = URL(fileURLWithPath: Bundle.main.bundlePath)
        agent = Agent(resources: exe.appendingPathComponent("Contents/Resources"))

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 660, height: 520),
                          styleMask: [.titled, .closable, .miniaturizable],
                          backing: .buffered, defer: false)
        window.title = "ClickGraft"
        window.center()

        content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = content
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        showRequirements()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    private func setBody(_ stack: NSStackView) {
        content.subviews.forEach { $0.removeFromSuperview() }
        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        let doc = FlippedView()
        doc.translatesAutoresizingMaskIntoConstraints = false
        doc.addSubview(stack)
        scroll.documentView = doc
        content.addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: content.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            doc.widthAnchor.constraint(equalTo: scroll.widthAnchor),
            stack.leadingAnchor.constraint(equalTo: doc.leadingAnchor, constant: 26),
            stack.trailingAnchor.constraint(equalTo: doc.trailingAnchor, constant: -26),
            stack.topAnchor.constraint(equalTo: doc.topAnchor, constant: 24),
            doc.bottomAnchor.constraint(equalTo: stack.bottomAnchor, constant: 24),
        ])
    }

    private func title(_ s: String) -> NSTextField { label(s, size: 20, weight: .semibold) }
    private func caption(_ s: String) -> NSTextField {
        label(s, size: 12, color: .secondaryLabelColor)
    }

    // -- 1. requirements -------------------------------------------------
    @objc func showRequirements() {
        guard let d = agent.once(["env"]),
              let env = d["env"] as? [String: Any] else {
            setBody(vstack([title("Couldn't start"),
                            caption("The ClickGraft backend did not respond.")]))
            return
        }
        candidates = d["candidates"] as? [[String: Any]] ?? []
        outputPath = d["default_output"] as? String ?? ""
        let ok = env["clt"] as? Bool ?? false

        var rows: [NSView] = [
            title("Requirements"),
            caption("ClickGraft needs one thing: Apple's Xcode Command Line Tools. "
                    + "They supply the signing tools and the Python it runs on."),
            label(ok ? "✓  Xcode Command Line Tools — installed"
                     : "✗  Xcode Command Line Tools — missing",
                  size: 13, weight: .medium,
                  color: ok ? .systemGreen : .systemRed),
        ]
        if !ok {
            rows.append(caption("Open Terminal and run  xcode-select --install  then reopen ClickGraft."))
        }
        if let tools = env["tools"] as? [String: String] {
            let detail = tools.sorted { $0.key < $1.key }
                .map { "\($0.key.padding(toLength: 20, withPad: " ", startingAt: 0))\($0.value.isEmpty ? "not found" : $0.value)" }
                .joined(separator: "\n")
            rows.append(label(detail, size: 11, color: .tertiaryLabelColor, mono: true))
        }
        let next = button(ok ? "Continue" : "Quit", self,
                          ok ? #selector(showChoose) : #selector(quit), key: "\r")
        next.keyEquivalent = "\r"
        rows.append(hstack([next]))
        setBody(vstack(rows, spacing: 14))
    }

    @objc func quit() { NSApp.terminate(nil) }

    // -- 2. choose -------------------------------------------------------
    @objc func showChoose() {
        var rows: [NSView] = [
            title("Choose your HP Click"),
            caption("Pick your original installation. ClickGraft never modifies it — "
                    + "it writes a separate copy."),
        ]
        if candidates.isEmpty {
            rows.append(label("No HP Click installation found in /Applications.",
                              color: .systemRed))
        }
        for (i, c) in candidates.enumerated() {
            let usable = c["usable"] as? Bool ?? false
            let name = c["name"] as? String ?? "?"
            let archs = (c["archs"] as? [String] ?? []).joined(separator: " / ")
            let ver = c["version"] as? String ?? ""
            let radio = NSButton(radioButtonWithTitle: usable ? "\(name)   —   \(ver)" : name,
                                 target: self, action: #selector(pick(_:)))
            radio.tag = i
            radio.isEnabled = usable
            var sub: [NSView] = [radio,
                                 label("      \(archs)", size: 11,
                                       color: .tertiaryLabelColor, mono: true)]
            if let why = c["why"] as? String, !why.isEmpty {
                sub.append(label("      \(why)", size: 11, color: .systemOrange))
            }
            rows.append(vstack(sub, spacing: 2))
        }
        let back = button("Back", self, #selector(showRequirements))
        let next = button("Continue", self, #selector(showReview), key: "\r")
        next.isEnabled = false
        nextButton = next
        rows.append(hstack([back, next]))
        setBody(vstack(rows, spacing: 12))

        // preselect the only usable candidate, if there is exactly one
        let usableIdx = candidates.indices.filter { candidates[$0]["usable"] as? Bool ?? false }
        if usableIdx.count == 1 {
            picked = candidates[usableIdx[0]]
            next.isEnabled = true
            for v in allRadios() where v.tag == usableIdx[0] { v.state = .on }
        }
    }

    var nextButton: NSButton?

    private func allRadios() -> [NSButton] {
        var found: [NSButton] = []
        func walk(_ v: NSView) {
            if let b = v as? NSButton, b.action == #selector(pick(_:)) { found.append(b) }
            v.subviews.forEach(walk)
        }
        walk(content)
        return found
    }

    @objc func pick(_ sender: NSButton) {
        picked = candidates[sender.tag]
        nextButton?.isEnabled = true
    }

    // -- 3. review -------------------------------------------------------
    @objc func showReview() {
        guard let src = picked?["path"] as? String,
              let d = agent.once(["plan", "--source", src, "--out", outputPath]),
              let p = d["plan"] as? [String: Any] else {
            setBody(vstack([title("Couldn't read the plan"),
                            caption((agent.once(["plan", "--source", picked?["path"] as? String ?? ""])?["error"] as? String) ?? "unknown error"),
                            hstack([button("Back", self, #selector(showChoose))])]))
            return
        }
        plan = p

        var rows: [NSView] = [
            title("Review the plan"),
            caption("Nothing is written until you approve this."),
            label("Read from    \(p["source"] as? String ?? "")\n"
                  + "Written to   \(p["output"] as? String ?? "")\n"
                  + "Version      HP Click \(p["app_version"] as? String ?? "")\n"
                  + "Runtime      Electron \(p["electron"] as? String ?? "") (arm64)",
                  size: 11, mono: true),
            label("Changes to the copy", size: 13, weight: .semibold),
        ]
        for x in p["patches"] as? [[String: Any]] ?? [] {
            rows.append(vstack([
                label("• " + (x["path"] as? String ?? ""), size: 11, mono: true),
                label("   " + (x["why"] as? String ?? ""), size: 11, color: .secondaryLabelColor),
            ], spacing: 1))
        }
        rows.append(label("Libraries added", size: 13, weight: .semibold))
        for x in p["dylibs"] as? [[String: Any]] ?? [] {
            let pre = (x["preload"] as? Bool ?? false) ? "   [preloaded]" : ""
            rows.append(vstack([
                label("• " + (x["name"] as? String ?? "") + pre, size: 11, mono: true),
                label("   " + (x["why"] as? String ?? ""), size: 11, color: .secondaryLabelColor),
            ], spacing: 1))
        }
        rows.append(label("Downloaded and checked", size: 13, weight: .semibold))
        for s in p["downloads"] as? [String] ?? [] {
            rows.append(label("• " + s, size: 11, color: .secondaryLabelColor))
        }
        let go = button("Build it", self, #selector(startBuild), key: "\r")
        rows.append(hstack([button("Back", self, #selector(showChoose)), go]))
        setBody(vstack(rows, spacing: 10))
    }

    // -- 4. build --------------------------------------------------------
    @objc func startBuild() {
        let bar = NSProgressIndicator()
        bar.isIndeterminate = false
        bar.minValue = 0; bar.maxValue = 1
        bar.translatesAutoresizingMaskIntoConstraints = false
        bar.widthAnchor.constraint(equalToConstant: 600).isActive = true
        progressBar = bar

        let msg = caption("Starting…")
        progressLabel = msg

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.heightAnchor.constraint(equalToConstant: 240).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 600).isActive = true
        let tv = NSTextView()
        tv.isEditable = false
        tv.font = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        scroll.documentView = tv
        logView = tv

        setBody(vstack([title("Building"), msg, bar, scroll], spacing: 12))

        agent.stream(["build", "--source", picked?["path"] as? String ?? "",
                      "--out", outputPath]) { [weak self] ev in
            self?.handle(ev)
        }
    }

    private func append(_ s: String) {
        guard let tv = logView else { return }
        tv.string += s + "\n"
        tv.scrollToEndOfDocument(nil)
    }

    private func handle(_ ev: [String: Any]) {
        switch ev["type"] as? String ?? "" {
        case "start":
            logPath = ev["log_path"] as? String ?? ""
        case "progress":
            let pct = ev["pct"] as? Double ?? 0
            let msg = ev["msg"] as? String ?? ""
            progressBar?.doubleValue = pct
            progressLabel?.stringValue = msg
            append(String(format: "%5.1f%%  %@", pct * 100, msg))
        case "done":
            showDone(ev)
        case "error":
            showError(ev["error"] as? String ?? "unknown error")
        default: break
        }
    }

    // -- 5. done / error --------------------------------------------------
    private func showDone(_ ev: [String: Any]) {
        let out = ev["output"] as? String ?? outputPath
        logPath = ev["log_path"] as? String ?? logPath
        var rows: [NSView] = [
            label("Done", size: 20, weight: .semibold, color: .systemGreen),
            caption("Created \(out)"),
        ]
        for (k, v) in (ev["results"] as? [String: String] ?? [:]).sorted(by: { $0.key < $1.key }) {
            rows.append(label("✓  \(k.replacingOccurrences(of: "_", with: " ")) — \(v)", size: 11))
        }
        rows.append(label("The two apps can't run at the same time. They share settings, so "
                          + "opening one while the other is running makes the second quit "
                          + "silently. Quit one before opening the other.",
                          size: 12, color: .secondaryLabelColor))
        rows.append(label("To undo this, drag the new app to the Trash. Your original was "
                          + "never changed.", size: 12, color: .secondaryLabelColor))
        rows.append(hstack([button("Show me the app", self, #selector(revealOutput)),
                            button("Open log", self, #selector(revealLog)),
                            button("Quit", self, #selector(quit), key: "\r")]))
        setBody(vstack(rows, spacing: 12))
    }

    private func showError(_ message: String) {
        var rows: [NSView] = [
            label("It didn't finish", size: 20, weight: .semibold, color: .systemRed),
            label(message, size: 12),
            caption("Your original app was not modified."),
        ]
        rows.append(hstack([button("Back", self, #selector(showReview)),
                            button("Open log", self, #selector(revealLog)),
                            button("Quit", self, #selector(quit))]))
        setBody(vstack(rows, spacing: 12))
    }

    @objc func revealOutput() { NSWorkspace.shared.selectFile(outputPath, inFileViewerRootedAtPath: "") }
    @objc func revealLog() {
        guard !logPath.isEmpty else { return }
        NSWorkspace.shared.selectFile(logPath, inFileViewerRootedAtPath: "")
    }
}

// MARK: - main

let app = NSApplication.shared
let delegate = Wizard()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
