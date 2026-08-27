// ClickGraft — native AppKit front end.
//
// All strings here come from docs/wizard-copy.md, which is the source of truth.
// If you change wording, change it there too — the copy was written for someone
// whose plotter is how they earn a living, not for a developer, and the
// reasoning behind each screen is recorded alongside it.
//
// No build logic lives here. The app spawns
//   python3 -m clickgraft.cli agent ...
// and reads one JSON object per line, so the UI cannot drift from the backend.
//
// Builds with the Command Line Tools alone:
//   swiftc -O -o ClickGraft ClickGraft.swift -framework AppKit

import AppKit
import Foundation

// MARK: - Backend bridge

final class Agent {
    let resources: URL
    init(resources: URL) { self.resources = resources }

    private func process(_ args: [String]) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = ["-m", "clickgraft.cli", "agent"] + args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = resources.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"      // never dirty a signed bundle
        p.environment = env
        p.currentDirectoryURL = resources
        return p
    }

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
               let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { return o }
        }
        return nil
    }

    func stream(_ args: [String], onEvent: @escaping ([String: Any]) -> Void) {
        let p = process(args)
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        var buf = Data()
        out.fileHandleForReading.readabilityHandler = { fh in
            buf.append(fh.availableData)
            while let nl = buf.firstIndex(of: 0x0A) {
                let line = buf.subdata(in: buf.startIndex..<nl)
                buf.removeSubrange(buf.startIndex...nl)
                if let o = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                    DispatchQueue.main.async { onEvent(o) }
                }
            }
        }
        p.terminationHandler = { _ in out.fileHandleForReading.readabilityHandler = nil }
        try? p.run()
    }
}

/// Content shorter than its scroll view renders against the BOTTOM unless the
/// document view is flipped — AppKit's origin is bottom-left.
final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

/// Layer-backed tinted panel. Re-resolves its colour when the system
/// appearance changes — a CGColor captured once would keep the old theme's.
final class PanelView: NSView {
    var tint: NSColor = .clear
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        layer?.cornerRadius = 9
        layer?.backgroundColor = tint.cgColor
    }
    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

// MARK: - Building blocks

enum UI {
    static let margin: CGFloat = 30
    static let width: CGFloat = 720

    static func text(_ s: String, size: CGFloat = 13, weight: NSFont.Weight = .regular,
                     color: NSColor = .labelColor, mono: Bool = false) -> NSTextField {
        let f = NSTextField(wrappingLabelWithString: s)
        f.font = mono ? .monospacedSystemFont(ofSize: size, weight: weight)
                      : .systemFont(ofSize: size, weight: weight)
        f.textColor = color
        f.isSelectable = true
        f.preferredMaxLayoutWidth = width - (margin * 2) - 16
        return f
    }

    static func title(_ s: String) -> NSTextField { text(s, size: 22, weight: .semibold) }
    static func subtitle(_ s: String) -> NSTextField {
        text(s, size: 14, color: .secondaryLabelColor)
    }
    static func body(_ s: String) -> NSTextField { text(s, size: 13) }
    static func small(_ s: String) -> NSTextField {
        text(s, size: 11.5, color: .secondaryLabelColor)
    }
    static func section(_ s: String) -> NSTextField {
        text(s, size: 11, weight: .bold, color: .secondaryLabelColor)
    }

    /// A tinted panel. Used for the reassurance blocks, which must read as a
    /// distinct promise rather than more prose.
    ///
    /// Deliberately not an NSBox: assigning an auto-layout stack to NSBox's
    /// contentView gives the box no intrinsic height, so it collapses to zero
    /// and its text draws on top of whatever is above it.
    static func panel(_ views: [NSView], tint: NSColor = NSColor.controlAccentColor
                        .withAlphaComponent(0.07)) -> NSView {
        let v = PanelView()
        v.tint = tint
        v.wantsLayer = true
        v.translatesAutoresizingMaskIntoConstraints = false
        let stack = vstack(views, spacing: 7)
        v.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: v.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: v.trailingAnchor, constant: -14),
            stack.topAnchor.constraint(equalTo: v.topAnchor, constant: 12),
            stack.bottomAnchor.constraint(equalTo: v.bottomAnchor, constant: -12),
            v.widthAnchor.constraint(equalToConstant: width - margin * 2),
        ])
        return v
    }

    /// "Point — explanation" on one line, bold lead. The pattern the copy deck
    /// uses for every reassurance and every listed change.
    static func point(_ lead: String, _ rest: String) -> NSTextField {
        let f = NSTextField(wrappingLabelWithString: "")
        let a = NSMutableAttributedString(
            string: lead, attributes: [.font: NSFont.systemFont(ofSize: 12.5, weight: .semibold),
                                       .foregroundColor: NSColor.labelColor])
        a.append(NSAttributedString(
            string: rest.isEmpty ? "" : " " + rest,
            attributes: [.font: NSFont.systemFont(ofSize: 12.5),
                         .foregroundColor: NSColor.secondaryLabelColor]))
        f.attributedStringValue = a
        f.isSelectable = true
        f.preferredMaxLayoutWidth = width - (margin * 2) - 34
        return f
    }

    static func vstack(_ v: [NSView], spacing: CGFloat = 12) -> NSStackView {
        let s = NSStackView(views: v)
        s.orientation = .vertical
        s.alignment = .leading
        s.spacing = spacing
        s.translatesAutoresizingMaskIntoConstraints = false
        return s
    }

    static func hstack(_ v: [NSView], spacing: CGFloat = 10) -> NSStackView {
        let s = NSStackView(views: v)
        s.orientation = .horizontal
        s.spacing = spacing
        return s
    }

    static func button(_ t: String, _ target: AnyObject, _ a: Selector,
                       primary: Bool = false) -> NSButton {
        let b = NSButton(title: t, target: target, action: a)
        b.bezelStyle = .rounded
        if primary { b.keyEquivalent = "\r" }
        return b
    }

    static func spacer() -> NSView {
        let v = NSView()
        v.setContentHuggingPriority(.init(1), for: .horizontal)
        return v
    }
}

/// Collapsible "Show technical detail". Built lazily so it reflects state at
/// the moment it is opened.
final class Disclosure: NSView {
    private let label: String
    private let provider: () -> String
    private var toggle: NSButton!
    private var shown: NSScrollView?

    init(label: String = "Show technical detail", provider: @escaping () -> String) {
        self.label = label
        self.provider = provider
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        toggle = NSButton(title: "▸ " + label, target: self, action: #selector(flip))
        toggle.bezelStyle = .inline
        toggle.isBordered = false
        toggle.contentTintColor = .secondaryLabelColor
        toggle.font = .systemFont(ofSize: 11.5)
        toggle.translatesAutoresizingMaskIntoConstraints = false
        addSubview(toggle)
        NSLayoutConstraint.activate([
            toggle.leadingAnchor.constraint(equalTo: leadingAnchor),
            toggle.topAnchor.constraint(equalTo: topAnchor),
            toggle.bottomAnchor.constraint(lessThanOrEqualTo: bottomAnchor),
        ])
        heightAnchor.constraint(greaterThanOrEqualToConstant: 20).isActive = true
    }
    required init?(coder: NSCoder) { nil }

    @objc private func flip() {
        if let s = shown {
            s.removeFromSuperview(); shown = nil
            toggle.title = "▸ " + label
            invalidateIntrinsicContentSize()
            return
        }
        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.borderType = .lineBorder
        let tv = NSTextView()
        tv.isEditable = false
        tv.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
        tv.string = provider()
        scroll.documentView = tv
        addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.widthAnchor.constraint(equalToConstant: UI.width - UI.margin * 2 - 10),
            scroll.topAnchor.constraint(equalTo: toggle.bottomAnchor, constant: 6),
            scroll.heightAnchor.constraint(equalToConstant: 170),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        shown = scroll
        toggle.title = "▾ Hide " + label.replacingOccurrences(of: "Show ", with: "")
    }
}

// MARK: - Wizard

final class Wizard: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var agent: Agent!
    var container: NSView!

    var candidates: [[String: Any]] = []
    var picked: [String: Any]?
    var plan: [String: Any] = [:]
    var outputPath = ""
    var logPath = ""
    var env: [String: Any] = [:]

    var continueButton: NSButton?
    var bar: NSProgressIndicator?
    var caption: NSTextField?
    var logText: NSTextView?
    var logBuffer = ""
    var lastError = ""
    var lastResults: [String: String] = [:]
    var outcome = "no build has been run"
    var updateURL = ""
    var updateBanner: NSView?
    var allowIntelHost = false

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ n: Notification) {
        agent = Agent(resources: URL(fileURLWithPath: Bundle.main.bundlePath)
                        .appendingPathComponent("Contents/Resources"))
        // Sized so the review screen — the longest, and the one the user most
        // needs to read all of — fits without scrolling on a laptop display.
        // Resizable because a shorter screen would otherwise clip it silently.
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: UI.width, height: 790),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.minSize = NSSize(width: UI.width, height: 420)
        window.title = "ClickGraft"
        window.center()
        container = NSView()
        window.contentView = container
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        showWelcome()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    @objc func quit() { NSApp.terminate(nil) }

    private func present(_ rows: [NSView], buttons: [NSView]) {
        container.subviews.forEach { $0.removeFromSuperview() }

        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        let doc = FlippedView()
        doc.translatesAutoresizingMaskIntoConstraints = false
        let body = UI.vstack(rows, spacing: 14)
        doc.addSubview(body)
        scroll.documentView = doc

        let barRow = UI.hstack(buttons)
        barRow.translatesAutoresizingMaskIntoConstraints = false

        // A hairline above the buttons: it marks where the readable area stops.
        // Without it a screen whose text runs past the fold looks like it simply
        // ended, and on the review screen that means someone presses the button
        // having read two-thirds of what they were promised.
        let hair = NSBox()
        hair.boxType = .separator
        hair.translatesAutoresizingMaskIntoConstraints = false

        container.addSubview(scroll)
        container.addSubview(hair)
        container.addSubview(barRow)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: container.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: hair.topAnchor),
            hair.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            hair.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            hair.bottomAnchor.constraint(equalTo: barRow.topAnchor, constant: -14),
            doc.widthAnchor.constraint(equalTo: scroll.widthAnchor),
            body.leadingAnchor.constraint(equalTo: doc.leadingAnchor, constant: UI.margin),
            body.trailingAnchor.constraint(equalTo: doc.trailingAnchor, constant: -UI.margin),
            body.topAnchor.constraint(equalTo: doc.topAnchor, constant: 26),
            doc.bottomAnchor.constraint(equalTo: body.bottomAnchor, constant: 20),
            barRow.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: UI.margin),
            barRow.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -UI.margin),
            barRow.bottomAnchor.constraint(equalTo: container.bottomAnchor, constant: -20),
        ])
    }

    // MARK: 1 — Welcome

    @objc func showWelcome() {
        let rows: [NSView] = [
            UI.title("ClickGraft"),
            UI.subtitle("Make HP Click run properly on your Mac"),
            UI.body("In 2020 Apple started replacing the Intel processors in Macs with its "
                    + "own, called Apple Silicon. Your Mac still runs apps built for the older "
                    + "Intel chips by translating them as they go — that's Rosetta."),
            UI.body("HP Click for Mac is one of those. That translation is why it's slow to "
                    + "start and why clicks take a moment to register."),
            UI.body("HP already builds the important parts of HP Click for Apple Silicon — "
                    + "page layout, colour, the print engine. They're inside the app you have "
                    + "installed right now. They're just packaged with an Intel engine."),
            UI.body("ClickGraft makes a copy of your HP Click and puts the Apple Silicon "
                    + "engine into that copy."),
            UI.panel([
                UI.point("Your HP Click is not modified.",
                         "It's opened for reading only, and left exactly as it is."),
                UI.point("You end up with two apps.", "Your original, and a new one beside it."),
                UI.point("To undo everything, drag the new app to the Trash.",
                         "There is no uninstaller because there's nothing else to remove."),
            ]),
        ]
        present(rows, buttons: [UI.button("Quit", self, #selector(quit)), UI.spacer(),
                                UI.button("Continue", self, #selector(showRequirements),
                                          primary: true)])

        // Checked in the background so a slow or blocked network never delays
        // the first screen. If it fails, nothing is said — an update notice is
        // not worth an error dialog.
        checkForUpdate { [weak self] up in
            guard let self = self, let up = up else { return }
            self.updateURL = up.url
            self.updateBanner?.removeFromSuperview()
            let here = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"

            // Tell people whether it matters. A tool that shouts equally about
            // a new icon and a broken build teaches them to ignore both, and
            // the one that mattered is the one they then miss.
            let lead: String, rest: String, action: String, tint: NSColor
            switch up.importance {
            case "optional":
                lead = "Version \(up.version) is available, but you don't need it."
                rest = (up.summary.isEmpty ? "Nothing about how ClickGraft works has changed."
                                           : up.summary)
                     + " You're on \(here), and it will keep working exactly as it does now."
                action = "Get it anyway"
                tint = NSColor.secondaryLabelColor.withAlphaComponent(0.08)
            case "important":
                lead = "Version \(up.version) fixes something that stops ClickGraft working."
                rest = (up.summary.isEmpty ? "Updating is strongly recommended." : up.summary)
                     + " You're on \(here)."
                action = "Get the update"
                tint = NSColor.systemOrange.withAlphaComponent(0.13)
            default:
                lead = "Version \(up.version) is available."
                rest = (up.summary.isEmpty
                        ? "Newer versions usually mean support for newer HP Click releases."
                        : up.summary) + " You're on \(here)."
                action = "Get the update"
                tint = NSColor.systemBlue.withAlphaComponent(0.10)
            }

            let b = UI.panel([
                UI.point(lead, rest),
                UI.button(action, self, #selector(self.openDownloadPage)),
            ], tint: tint)
            self.updateBanner = b
            if let stack = self.container.subviews.first(where: { $0 is NSScrollView })
                            .flatMap({ ($0 as? NSScrollView)?.documentView?.subviews.first as? NSStackView }) {
                stack.addArrangedSubview(b)
            }
        }
    }

    // MARK: 2 — Requirements

    @objc func showRequirements() {
        guard let d = agent.once(["env"]), let e = d["env"] as? [String: Any] else {
            present([UI.title("ClickGraft couldn't start"),
                     UI.body("The part of ClickGraft that does the work didn't respond. "
                             + "Reopening the app usually clears this.")],
                    buttons: [UI.spacer(), UI.button("Quit", self, #selector(quit), primary: true)])
            return
        }
        env = e
        candidates = d["candidates"] as? [[String: Any]] ?? []
        outputPath = d["default_output"] as? String ?? ""
        let ok = e["clt"] as? Bool ?? false
        // Default true: if an older backend omits the key, fail open rather
        // than blocking every user on a missing field.
        let silicon = e["apple_silicon"] as? Bool ?? true

        var rows: [NSView] = [
            UI.title("What ClickGraft needs"),
            UI.body("ClickGraft uses a set of tools Apple ships for free, called the Command "
                    + "Line Tools. Most Macs used for design or print work already have them."),
        ]
        if ok {
            rows.append(UI.panel([UI.point("Apple's Command Line Tools are installed.",
                                           "Nothing to do.")],
                                 tint: NSColor.systemGreen.withAlphaComponent(0.10)))
        } else {
            rows.append(UI.panel([
                UI.point("Apple's Command Line Tools aren't installed yet.", ""),
                UI.small("They come from Apple, not from us. macOS will offer to install them "
                         + "the first time it needs them — accept, wait for it to finish, then "
                         + "come back here. It's a large download and can take several minutes."),
            ], tint: NSColor.systemOrange.withAlphaComponent(0.12)))
        }
        if !silicon {
            let cb = NSButton(checkboxWithTitle:
                "Yes — I'm making this copy for a different Mac that has Apple Silicon",
                target: self, action: #selector(toggleIntelOverride(_:)))
            cb.state = allowIntelHost ? .on : .off
            rows.append(UI.panel([
                UI.point("This Mac has an Intel processor.",
                         "ClickGraft's whole job is putting the Apple Silicon engine into a "
                         + "copy of HP Click. That copy will not run on this Mac, and your "
                         + "HP Click here is already the right one for it."),
                UI.small("If that's a surprise: click the Apple menu, then About This Mac. "
                         + "Macs sold from about 2020 onward say Apple M1, M2, M3 and so on. "
                         + "This one says Intel."),
                UI.point("Building for another Mac is supported.",
                         "Everything except the final test-launch works here, because this "
                         + "Mac can't run the copy in order to check it. Tick the box and "
                         + "carry on — the copy will be made, just not tried out."),
                cb,
            ], tint: NSColor.systemOrange.withAlphaComponent(0.13)))
        }
        rows.append(Disclosure(label: "What ClickGraft uses them for") { [weak self] in
            let tools = (self?.env["tools"] as? [String: String]) ?? [:]
            let list = tools.sorted { $0.key < $1.key }
                .map { "\($0.key.padding(toLength: 20, withPad: " ", startingAt: 0))"
                     + "\($0.value.isEmpty ? "not found" : $0.value)" }
                .joined(separator: "\n")
            return "Two things: to read the app you already have, and to sign the copy it "
                 + "makes so macOS will run it.\n\n" + list
        })

        let next = UI.button("Continue", self, #selector(showChoose), primary: true)
        next.isEnabled = ok && (silicon || allowIntelHost)
        var buttons: [NSView] = [UI.button("Back", self, #selector(showWelcome))]
        if !ok { buttons.append(UI.button("Check again", self, #selector(showRequirements))) }
        buttons += [UI.spacer(), next]
        present(rows, buttons: buttons)
    }

    // MARK: 3 — Choose

    @objc func showChoose() {
        var rows: [NSView] = [
            UI.title("Choose your HP Click"),
            UI.body("Pick the HP Click you use now. ClickGraft reads it and leaves it alone."),
        ]

        if candidates.isEmpty {
            rows.append(UI.panel([
                UI.point("No HP Click found in your Applications folder.", ""),
                UI.small("ClickGraft looks in Applications. If yours lives somewhere else, "
                         + "move it there and press Check again."),
            ], tint: NSColor.systemOrange.withAlphaComponent(0.12)))
        }

        for (i, c) in candidates.enumerated() {
            let usable = c["usable"] as? Bool ?? false
            let name = (c["name"] as? String ?? "").replacingOccurrences(of: ".app", with: "")
            let ver = c["version"] as? String ?? ""
            let title = ver.isEmpty ? name : "\(name) — version \(ver)"
            let radio = NSButton(radioButtonWithTitle: title, target: self,
                                 action: #selector(pick(_:)))
            radio.tag = i
            radio.isEnabled = usable
            radio.font = .systemFont(ofSize: 13, weight: usable ? .medium : .regular)

            var sub: [NSView] = [radio]
            if usable {
                sub.append(UI.small("      Ready to copy"))
            } else if let why = c["why"] as? String, !why.isEmpty {
                sub.append(UI.small("      " + why))
            }
            rows.append(UI.vstack(sub, spacing: 2))
        }

        // Only an unrecognised *version* is worth explaining and reporting. An
        // already-made copy is greyed out with its own one-line reason; showing
        // this panel for it reads as "your app is unsupported", which it isn't.
        if candidates.contains(where: { ($0["reason"] as? String ?? "") == "unsupported" }) {
            rows.append(UI.panel([
                UI.small("ClickGraft only works with versions it has been tested against, "
                         + "because it needs to know exactly where to make its changes. "
                         + "Guessing would risk your app."),
                UI.small("You can send a report describing this version, and support can be "
                         + "added."),
                UI.button("Create a report", self, #selector(makeReport)),
            ], tint: NSColor.secondaryLabelColor.withAlphaComponent(0.07)))
        }

        let next = UI.button("Continue", self, #selector(showReview), primary: true)
        next.isEnabled = false
        continueButton = next
        present(rows, buttons: [UI.button("Back", self, #selector(showRequirements)),
                                UI.button("Check again", self, #selector(rescan)),
                                UI.spacer(), next])

        let usableIdx = candidates.indices.filter { candidates[$0]["usable"] as? Bool ?? false }
        if usableIdx.count == 1 {
            picked = candidates[usableIdx[0]]
            next.isEnabled = true
            radios().first { $0.tag == usableIdx[0] }?.state = .on
        }
    }

    @objc func toggleIntelOverride(_ b: NSButton) {
        allowIntelHost = (b.state == .on)
        showRequirements()          // redraw so Continue enables/disables
    }

    /// Re-read the Applications folder without leaving the Choose screen.
    @objc func rescan() {
        if let d = agent.once(["env"]) {
            candidates = d["candidates"] as? [[String: Any]] ?? []
            outputPath = d["default_output"] as? String ?? outputPath
        }
        picked = nil
        showChoose()
    }

    private func radios() -> [NSButton] {
        var out: [NSButton] = []
        func walk(_ v: NSView) {
            if let b = v as? NSButton, b.action == #selector(pick(_:)) { out.append(b) }
            v.subviews.forEach(walk)
        }
        walk(container)
        return out
    }

    @objc func pick(_ s: NSButton) {
        picked = candidates[s.tag]
        continueButton?.isEnabled = true
    }

    @objc func makeReport() {
        let bad = candidates.first { ($0["reason"] as? String ?? "") == "unsupported" }
        guard let path = bad?["path"] as? String else { return }
        let a = NSAlert()
        a.messageText = "Creating the report…"
        a.informativeText = "This looks at the app and writes a description of it. "
                          + "It takes a minute."
        a.addButton(withTitle: "OK")
        a.runModal()
        if let r = agent.once(["probe", "--source", path]),
           let report = r["report"] as? String {
            let dir = FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask)[0]
            let file = dir.appendingPathComponent("ClickGraft report.txt")
            try? report.write(to: file, atomically: true, encoding: .utf8)
            NSWorkspace.shared.selectFile(file.path, inFileViewerRootedAtPath: "")
        }
    }

    // MARK: 4 — Review

    @objc func showReview() {
        guard let src = picked?["path"] as? String,
              let d = agent.once(["plan", "--source", src, "--out", outputPath]),
              let p = d["plan"] as? [String: Any] else {
            present([UI.title("ClickGraft couldn't read that app"),
                     UI.body("It may have been updated or moved. Go back and choose again.")],
                    buttons: [UI.button("Back", self, #selector(showChoose)), UI.spacer()])
            return
        }
        plan = p
        let out = p["output"] as? String ?? ""
        let replacing = FileManager.default.fileExists(atPath: out)

        var rows: [NSView] = [
            UI.title("Here's exactly what will happen"),
            UI.body("Nothing has been changed yet. Nothing will be, until you press the "
                    + "button below."),

            UI.section("WHERE THINGS GO"),
            UI.point("Reading from", ""),
            UI.text(p["source"] as? String ?? "", size: 11, mono: true),
            UI.small("Opened for reading only, not changed."),
            UI.point(replacing ? "Replacing" : "Creating", ""),
            UI.text(out, size: 11, mono: true),
            // The build deletes an existing output bundle outright. Promising
            // "nothing is overwritten" while doing that is the one lie this
            // screen cannot afford, so a second run says what it really does.
            UI.small(replacing
                     ? "A copy is already here from a previous run. It will be replaced. "
                     + "Your original HP Click is still untouched."
                     : "A new app. Nothing is overwritten."),

            UI.section("THE MAIN CHANGE"),
            UI.point("Replacing the Intel engine with the Apple Silicon one.",
                     "ClickGraft downloads the official Apple Silicon engine directly from "
                     + "its makers, checks it against a published fingerprint, and puts it in "
                     + "the copy. HP's own files — layout, colour, the print engine, your "
                     + "settings — are carried across untouched."),

            // Not "\(patches.count) small fixes": the manifest's four patches
            // collapse into three explanations, because two of them repair the
            // same HP bug in two files. A number here would contradict the list.
            UI.section("SMALL FIXES TO THE COPY"),
            UI.point("Stops HP's updater replacing your new app with the Intel version.",
                     "Without this, HP's automatic update would quietly undo the whole thing."),
            UI.point("Stops crash reports being sent unencrypted.",
                     "HP's build uploads them over an unencrypted connection. This turns "
                     + "that off."),
            UI.point("Fixes a bug in HP's code.",
                     "Two of HP's files have a mistake that makes the app report an error "
                     + "every time it starts — on Intel Macs too. ClickGraft repairs it."),

            UI.section("SUPPORT FILES ADDED"),
            UI.body("HP's Apple Silicon components expect two small libraries that HP forgot "
                    + "to include. ClickGraft downloads them from their official source and "
                    + "adds them to the copy. Without them the app would fail the first time "
                    + "it went online."),

            Disclosure { [weak self] in self?.technicalPlan() ?? "" },
        ]
        rows.append(UI.panel([
            UI.point("Your HP Click is not modified.", "It is only read."),
        ]))

        present(rows, buttons: [UI.button("Back", self, #selector(showChoose)), UI.spacer(),
                                UI.button("Create the copy", self, #selector(startBuild),
                                          primary: true)])
    }

    private func technicalPlan() -> String {
        var out = "SOURCE   \(plan["source"] as? String ?? "")\n"
        out += "OUTPUT   \(plan["output"] as? String ?? "")\n"
        out += "VERSION  HP Click \(plan["app_version"] as? String ?? "")\n"
        out += "RUNTIME  Electron \(plan["electron"] as? String ?? "") (darwin-arm64)\n\nPATCHES\n"
        for p in plan["patches"] as? [[String: Any]] ?? [] {
            out += "  \(p["path"] as? String ?? "")\n      \(p["why"] as? String ?? "")\n"
        }
        out += "\nLIBRARIES\n"
        for d in plan["dylibs"] as? [[String: Any]] ?? [] {
            let pre = (d["preload"] as? Bool ?? false) ? "  [preloaded]" : ""
            out += "  \(d["name"] as? String ?? "")\(pre)\n      \(d["why"] as? String ?? "")\n"
        }
        out += "\nDOWNLOADS\n"
        for s in plan["downloads"] as? [String] ?? [] { out += "  \(s)\n" }
        return out
    }

    // MARK: 5 — Building

    @objc func startBuild() {
        let b = NSProgressIndicator()
        b.isIndeterminate = false
        b.minValue = 0; b.maxValue = 1
        b.translatesAutoresizingMaskIntoConstraints = false
        b.widthAnchor.constraint(equalToConstant: UI.width - UI.margin * 2 - 10).isActive = true
        bar = b

        let cap = UI.body("Checking your HP Click")
        caption = cap

        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.borderType = .lineBorder
        scroll.heightAnchor.constraint(equalToConstant: 150).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: UI.width - UI.margin * 2 - 10).isActive = true
        let tv = NSTextView()
        tv.isEditable = false
        tv.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
        scroll.documentView = tv
        logText = tv

        present([
            UI.title("Making your copy"),
            cap, b,
            UI.panel([UI.point("This usually takes under a minute.",
                               "Your original HP Click is not being touched.")]),
            UI.small("Detail"),
            scroll,
        ], buttons: [UI.spacer()])

        var args = ["build", "--source", picked?["path"] as? String ?? "",
                    "--out", outputPath]
        if allowIntelHost { args.append("--allow-intel-host") }
        agent.stream(args) { [weak self] ev in self?.handle(ev) }
    }

    /// The backend's messages are written for the log. These are written for
    /// someone watching a progress bar wondering what is happening to their app.
    private func friendlyCaption(_ pct: Double) -> String {
        switch pct {
        case ..<0.10:  return "Checking your HP Click"
        case ..<0.25:  return "Getting the Apple Silicon engine from its makers"
        case ..<0.35:  return "Making a copy of your app"
        case ..<0.50:  return "Fitting the new engine"
        case ..<0.62:  return "Adding the support files"
        case ..<0.75:  return "Making the small fixes"
        case ..<0.95:  return "Signing the copy so macOS will run it"
        default:       return "Checking the result"
        }
    }

    private func handle(_ ev: [String: Any]) {
        switch ev["type"] as? String ?? "" {
        case "start":
            logPath = ev["log_path"] as? String ?? ""
        case "progress":
            let pct = ev["pct"] as? Double ?? 0
            bar?.doubleValue = pct
            caption?.stringValue = friendlyCaption(pct)
            logBuffer += String(format: "%5.1f%%  %@\n", pct * 100, ev["msg"] as? String ?? "")
            logText?.string = logBuffer
            logText?.scrollToEndOfDocument(nil)
        case "done":  showDone(ev)
        case "error": showFailed(ev)
        default: break
        }
    }

    // MARK: 6 — Done

    private func showDone(_ ev: [String: Any]) {
        let out = ev["output"] as? String ?? outputPath
        outcome = (ev["results"] as? [String: String] ?? [:])["smoke_launch"]?
                    .hasPrefix("SKIPPED") == true
                  ? "the build finished; the test-launch was skipped because this Mac "
                  + "cannot run an Apple Silicon app"
                  : "the build finished and every check passed"
        lastResults = ev["results"] as? [String: String] ?? [:]
        let crossBuilt = lastResults["smoke_launch"]?.hasPrefix("SKIPPED") == true
        logPath = ev["log_path"] as? String ?? logPath
        let name = (out as NSString).lastPathComponent.replacingOccurrences(of: ".app", with: "")

        present([
            UI.text("Your Apple Silicon copy is ready", size: 22, weight: .semibold,
                    color: .systemGreen),
            UI.body("\(name) is in your Applications folder, next to your original."),
            UI.body(crossBuilt
                    ? "It's built for Apple Silicon and it's signed. It has not been "
                    + "started up, because this Mac can't run it — try it on the Mac you "
                    + "made it for."
                    : "Everything checked out: it's built for your Mac's processor, it's "
                    + "signed, and it starts up correctly."),
            // The numbers and the Activity Monitor tip are both about running
            // it, so neither belongs on a Mac that cannot. Saying "on this Mac
            // it starts 11× faster" one line under "this Mac can't run it" is
            // the same mistake as the sentence above, one paragraph later.
            UI.panel(crossBuilt
                ? [
                    UI.point("On the Mac you made it for, it should start about 11× faster "
                             + "than it does under Rosetta,", "and without the freezes."),
                    UI.small("You can confirm it over there: open Activity Monitor, find "
                             + "HP Click, and look at the Kind column. It should say Apple "
                             + "instead of Intel."),
                  ]
                : [
                    UI.point("On this Mac it starts about 11× faster than it did under Rosetta,",
                             "and without the freezes."),
                    UI.small("You can confirm it yourself: open Activity Monitor, find HP Click, "
                             + "and look at the Kind column. It now says Apple instead of Intel."),
                  ],
                tint: NSColor.systemGreen.withAlphaComponent(0.10)),
            UI.panel([
                UI.point("Don't run both at once.",
                         "The two apps share your printers and settings, so opening one while "
                         + "the other is running makes the second one quit without saying "
                         + "anything. Quit one before opening the other."),
                UI.point("Your original is untouched.",
                         "If anything about the new copy bothers you, drag it to the Trash and "
                         + "carry on as before."),
            ], tint: NSColor.systemOrange.withAlphaComponent(0.11)),
            // The checks above prove the bundle is sound; they cannot prove a
            // page came out of a plotter. This project has shipped to people in
            // six countries and heard back from none of them, so the only
            // evidence it works in a print shop is the evidence someone chooses
            // to send. Ask plainly, say what it costs, and leave it optional.
            UI.panel([
                UI.point("If it printed, say so.",
                         "There is no telemetry in here, so a note from you is the only way "
                         + "anyone learns whether this holds up on a real printer. Share how "
                         + "it went takes one sentence, you can read every word before it "
                         + "leaves, and it carries nothing that identifies you."),
            ], tint: NSColor.systemBlue.withAlphaComponent(0.10)),
            Disclosure(label: "Show what was checked") {
                (ev["results"] as? [String: String] ?? [:])
                    .sorted { $0.key < $1.key }
                    .map { "\($0.key.replacingOccurrences(of: "_", with: " ")): \($0.value)" }
                    .joined(separator: "\n")
            },
        ], buttons: [UI.button("Show me the app", self, #selector(revealOutput)),
                     UI.button("Open the log", self, #selector(revealLog)),
                     // Passing our checks is not the same as working. Nothing
                     // here has ever verified that a page actually prints, and
                     // the person best placed to tell us is standing at a
                     // plotter looking at a finished copy.
                     UI.button("Report a problem", self, #selector(sendReport)),
                     UI.button("Share how it went", self, #selector(shareResult)),
                     UI.spacer(),
                     UI.button("Done", self, #selector(quit), primary: true)])
    }

    // MARK: error

    /// Two genuinely different outcomes, which the first version of this screen
    /// conflated. If the build itself failed, nothing was installed and the red
    /// heading is right. If the build finished and only a *check* failed, the
    /// copy is sitting in Applications and usually works — telling that user
    /// "nothing was installed" is simply false, and sends them to support over
    /// an app they could be using.
    private func showFailed(_ ev: [String: Any]) {
        let message = ev["error"] as? String ?? ""
        let madeIt = (ev["output_exists"] as? Bool ?? false)
                     && (ev["stage"] as? String ?? "") == "verify"
        logPath = ev["log_path"] as? String ?? logPath
        lastError = message
        outcome = madeIt ? "the copy was made but a check did not pass"
                         : "the build did not finish"
        let out = ev["output"] as? String ?? outputPath

        var rows: [NSView]
        if madeIt {
            let name = (out as NSString).lastPathComponent
                .replacingOccurrences(of: ".app", with: "")
            rows = [
                UI.text("Your copy was made, but one check didn't pass",
                        size: 22, weight: .semibold, color: .systemOrange),
                UI.body("\(name) is in your Applications folder. ClickGraft builds the "
                        + "copy first and then checks it over, and it was the check that "
                        + "failed — not the copy."),
                UI.panel([
                    UI.point("Your original HP Click was not changed.",
                             "That hasn't been touched at any point."),
                    UI.point("The copy is most likely fine.", "Try opening it. If it "
                             + "starts and finds your printer, you're done."),
                    UI.point("If it doesn't work,", "drag it to the Trash and nothing "
                             + "about your Mac has changed."),
                ], tint: NSColor.systemOrange.withAlphaComponent(0.10)),
                UI.section("WHAT THE CHECK SAID"),
                UI.small(message),
                Disclosure(label: "Show detail") { [weak self] in self?.logBuffer ?? "" },
            ]
        } else {
            rows = [
                UI.text("The copy wasn't finished", size: 22, weight: .semibold,
                        color: .systemRed),
                UI.body(message),
                UI.panel([
                    UI.point("Your original HP Click was not changed.",
                             "Nothing was installed. You can try again, or send a report "
                             + "if it keeps happening."),
                ]),
                Disclosure(label: "Show detail") { [weak self] in self?.logBuffer ?? "" },
            ]
        }

        var buttons: [NSView] = [UI.button("Back", self, #selector(showReview)),
                                 UI.button("Send a report", self, #selector(sendReport))]
        if madeIt { buttons.append(UI.button("Open the copy", self, #selector(revealOutput))) }
        buttons += [UI.spacer(),
                    UI.button("Try again", self, #selector(startBuild), primary: !madeIt)]
        present(rows, buttons: buttons)
    }

    // MARK: - Reporting a problem

    static let reportURL = "https://clickgraft.elusive.net/report"
    static let appcastURL = "https://clickgraft.elusive.net/appcast.json"

    /// Everything the report will contain, assembled so it can be SHOWN to the
    /// user before it goes anywhere. Nothing is sent that they have not read.
    private func reportBody(note: String = "", printer: String = "",
                            kind: String = "problem") -> String {
        let pi = ProcessInfo.processInfo
        var out = "kind: \(kind)\n"
        out += "ClickGraft \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?")\n"
        out += "macOS \(pi.operatingSystemVersionString)\n"
        out += "arch: \(machineArch())"
        if let h = env["host"] as? [String: Any] {
            let silicon = h["apple_silicon"] as? Bool ?? true
            let translated = h["translated"] as? Bool ?? false
            out += "  (hardware: \(silicon ? "Apple Silicon" : "Intel")"
                 + (translated ? ", running under Rosetta" : "") + ")"
        }
        out += "\n"
        out += "outcome: \(outcome)\n"
        out += "source: \((picked?["path"] as? String).map(scrub) ?? "none")\n"
        out += "version: \(picked?["version"] as? String ?? "?")\n\n"

        // What the person says beats anything we can infer. A copy that builds
        // cleanly and then won't print looks identical to a perfect run from
        // in here, so their sentence is the only signal that exists.
        if !note.isEmpty {
            out += "what they said:\n\(scrub(note))\n\n"
        }
        if !lastError.isEmpty {
            out += "error:\n\(scrub(lastError))\n\n"
        }
        if !lastResults.isEmpty {
            out += "checks:\n"
            for (k, v) in lastResults.sorted(by: { $0.key < $1.key }) {
                out += "  \(k): \(v)\n"
            }
            out += "\n"
        }
        if !printer.isEmpty {
            out += "printer (included with permission):\n\(printer)\n\n"
        }
        out += "log:\n\(scrub(logBuffer))"
        return out
    }

    /// The home directory carries a real name often enough to matter. Nothing
    /// about a path under /Users/<someone> helps diagnose a build, so it goes.
    private func scrub(_ s: String) -> String {
        let home = NSHomeDirectory()
        let user = (home as NSString).lastPathComponent
        return s.replacingOccurrences(of: home, with: "~")
                .replacingOccurrences(of: "/Users/\(user)", with: "/Users/~")
    }

    private func machineArch() -> String {
        var si = utsname(); uname(&si)
        return withUnsafePointer(to: &si.machine) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
    }

    /// The other half of the picture. Only failures ever reach us otherwise, so
    /// a working install is invisible and "is this tool actually working" can
    /// only be answered from the absence of complaints — which is not evidence.
    @objc func shareResult() {
        let ask = NSAlert()
        ask.messageText = "Share how this went?"
        ask.informativeText = "Knowing that it worked is genuinely useful, and nobody "
            + "sends that in unprompted. This is what would be sent — you can read all "
            + "of it first, and it goes nowhere unless you press Send.\n\nNo account, "
            + "no identifier, nothing that says who or where you are, and no way to link "
            + "this to anything else you send."
        let wrap = NSView(frame: NSRect(x: 0, y: 0, width: 460, height: 104))
        let note = NSTextField(frame: NSRect(x: 0, y: 36, width: 460, height: 68))
        note.placeholderString = "Anything worth knowing? (optional)"
        note.usesSingleLineMode = false
        note.cell?.wraps = true
        note.cell?.isScrollable = false
        let inclPrinter = NSButton(checkboxWithTitle:
            "Include my printer's model and firmware", target: nil, action: nil)
        inclPrinter.frame = NSRect(x: 0, y: 6, width: 460, height: 22)
        inclPrinter.state = .off
        inclPrinter.toolTip = "Model, firmware version and paper sizes. Never the "
            + "printer's name, address, serial number or any password."
        wrap.addSubview(note)
        wrap.addSubview(inclPrinter)
        ask.accessoryView = wrap
        ask.addButton(withTitle: "Continue")
        ask.addButton(withTitle: "No thanks")
        ask.window.initialFirstResponder = note
        guard ask.runModal() == .alertFirstButtonReturn else { return }

        var printer = ""
        if inclPrinter.state == .on,
           let r = agent.once(["printerinfo"]), let t = r["text"] as? String {
            printer = t
        }
        let body = reportBody(note: note.stringValue, printer: printer, kind: "result")

        let a = NSAlert()
        a.messageText = "Send this?"
        a.informativeText = "Everything below, and nothing else. Your home folder name "
            + "has already been removed. If anything here bothers you, don't send it — "
            + "the app works exactly the same either way."
        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 460, height: 220))
        tv.string = body
        tv.isEditable = false
        tv.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
        let sc = NSScrollView(frame: NSRect(x: 0, y: 0, width: 460, height: 220))
        sc.hasVerticalScroller = true
        sc.documentView = tv
        a.accessoryView = sc
        a.addButton(withTitle: "Send")
        a.addButton(withTitle: "Cancel")
        if a.runModal() == .alertFirstButtonReturn { postReport(body) }
    }

    @objc func sendReport() {
        // Ask what's wrong first. From the Done screen the tool believes
        // everything worked, so without this the report says only "it worked"
        // and the reason they pressed the button is lost.
        let ask = NSAlert()
        ask.messageText = "What's the problem?"
        ask.informativeText = "In your own words. \"It prints nothing\", \"the page "
            + "comes out rotated\", \"it won't find my printer\" — whatever you'd say "
            + "out loud is exactly right. You can leave it blank if you'd rather."
        let wrap = NSView(frame: NSRect(x: 0, y: 0, width: 460, height: 104))
        let note = NSTextField(frame: NSRect(x: 0, y: 36, width: 460, height: 68))
        note.placeholderString = "What happened?"
        note.usesSingleLineMode = false
        note.cell?.wraps = true
        note.cell?.isScrollable = false

        // Off unless they turn it on. Model and firmware are the two things
        // that make a printing report actionable, but they are the user's
        // equipment, so they get asked rather than told.
        let inclPrinter = NSButton(checkboxWithTitle:
            "Include my printer's model and firmware", target: nil, action: nil)
        inclPrinter.frame = NSRect(x: 0, y: 6, width: 460, height: 22)
        inclPrinter.state = .off
        inclPrinter.toolTip = "Model, firmware version and paper sizes. Never the "
            + "printer's name, address, serial number or any password."
        wrap.addSubview(note)
        wrap.addSubview(inclPrinter)
        ask.accessoryView = wrap
        ask.addButton(withTitle: "Continue")
        ask.addButton(withTitle: "Cancel")
        ask.window.initialFirstResponder = note
        guard ask.runModal() == .alertFirstButtonReturn else { return }

        var printer = ""
        if inclPrinter.state == .on,
           let r = agent.once(["printerinfo"]), let t = r["text"] as? String {
            printer = t
        }
        let body = reportBody(note: note.stringValue, printer: printer)

        let a = NSAlert()
        a.messageText = "Send this to the ClickGraft developers?"
        a.informativeText = "This is everything that will be sent. Nothing else leaves "
            + "your Mac — no file names from your work, no printer details, no personal "
            + "information. Your home folder name has been removed. Read it first; if "
            + "anything in it bothers you, don't send it."
        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 460, height: 220))
        tv.string = body
        tv.isEditable = false
        tv.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
        let sc = NSScrollView(frame: NSRect(x: 0, y: 0, width: 460, height: 220))
        sc.hasVerticalScroller = true
        sc.documentView = tv
        a.accessoryView = sc
        a.addButton(withTitle: "Send")
        a.addButton(withTitle: "Copy instead")
        a.addButton(withTitle: "Cancel")

        switch a.runModal() {
        case .alertFirstButtonReturn:  postReport(body)
        case .alertSecondButtonReturn:
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(body, forType: .string)
            let d = NSAlert()
            d.messageText = "Copied"
            d.informativeText = "Paste it into an email or a GitHub issue whenever suits."
            d.runModal()
        default: break
        }
    }

    private func postReport(_ body: String) {
        guard let url = URL(string: Wizard.reportURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("text/plain; charset=utf-8", forHTTPHeaderField: "Content-Type")
        req.httpBody = body.data(using: .utf8)
        req.timeoutInterval = 20

        URLSession.shared.dataTask(with: req) { _, resp, err in
            DispatchQueue.main.async {
                let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                let ok = err == nil && (200...299).contains(code)
                let d = NSAlert()

                if ok {
                    d.messageText = "Report sent"
                    d.informativeText = "Thank you. There's nothing to follow up on — if "
                        + "you want a reply, open an issue on GitHub as well."
                    d.runModal()
                    return
                }

                // Say WHY. The first person this happened to could only tell us
                // "it failed", and the cause turned out to be a server-side 404
                // during a four-minute window — diagnosable in seconds if the
                // status code had been on screen. A failure the user can't
                // describe is a failure we can't fix.
                let why: String
                if let e = err {
                    why = "Your Mac couldn't reach the server: \(e.localizedDescription)"
                } else if code == 404 || code == 502 || code == 503 {
                    why = "The server answered \(code), which means the reporting service "
                        + "is down or being worked on. This is our problem, not yours, and "
                        + "trying later usually works."
                } else if code == 413 {
                    why = "The server answered 413: the report was too large to accept."
                } else if code == 429 {
                    why = "The server answered 429: too many reports too quickly. Waiting "
                        + "a minute will clear it."
                } else {
                    why = "The server answered \(code)."
                }

                d.messageText = "The report didn't go through"
                d.informativeText = why + "\n\nNothing was sent, and nothing on your Mac "
                    + "has changed. You can copy the report instead and paste it into a "
                    + "GitHub issue or an email — that reaches us just as well."
                d.addButton(withTitle: "Copy the report")
                d.addButton(withTitle: "Open GitHub issues")
                d.addButton(withTitle: "Close")

                switch d.runModal() {
                case .alertFirstButtonReturn:
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(body, forType: .string)
                case .alertSecondButtonReturn:
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(body, forType: .string)
                    if let u = URL(string: "https://github.com/taggie313/ClickGraft/issues/new") {
                        NSWorkspace.shared.open(u)
                    }
                default: break
                }
            }
        }.resume()
    }

    // MARK: - Updates

    /// Checked once at launch, quietly. A tool people run twice a year is
    /// exactly the kind that goes stale without anyone noticing, and a stale
    /// copy is how someone concludes their HP Click version is unsupported when
    /// it has been supported for months.
    struct Update {
        let version: String
        let url: String
        /// "optional" | "recommended" | "important". Anything unrecognised —
        /// including a server that has never heard of this field — becomes
        /// "recommended". An update whose importance cannot be read must never
        /// be presented as ignorable.
        let importance: String
        let summary: String
    }

    func checkForUpdate(_ done: @escaping (Update?) -> Void) {
        guard let url = URL(string: Wizard.appcastURL) else { return done(nil) }
        var req = URLRequest(url: url)
        req.timeoutInterval = 8
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data = data,
                  let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let latest = o["version"] as? String else { return DispatchQueue.main.async { done(nil) } }
            let here = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0"
            guard latest.compare(here, options: .numeric) == .orderedDescending else {
                return DispatchQueue.main.async { done(nil) }
            }
            // Walk every release NEWER than this one and take the most
            // serious. importance describes a release against the one before
            // it, so reading only the newest is wrong for anyone who skipped:
            // on 1.3.0, with an important 1.4.0 followed by a cosmetic 1.4.1,
            // the flat field alone would say "you don't need it" and bury the
            // release that mattered.
            //
            // Falls back to the flat field when there is no history — an older
            // server, or a hand-written appcast.
            let rank = ["optional": 0, "recommended": 1, "important": 2]
            func normalise(_ v: Any?) -> String {
                let s = (v as? String ?? "").lowercased()
                return rank[s] != nil ? s : "recommended"
            }

            var importance = normalise(o["importance"])
            var summary = o["summary"] as? String ?? ""
            var skipped = 0

            if let history = o["releases"] as? [[String: Any]] {
                var worst = "optional"
                var worstSummary = ""
                for r in history {
                    guard let v = r["version"] as? String,
                          v.compare(here, options: .numeric) == .orderedDescending
                    else { continue }
                    skipped += 1
                    let imp = normalise(r["importance"])
                    if rank[imp]! >= rank[worst]! {
                        worst = imp
                        let s = r["summary"] as? String ?? ""
                        if !s.isEmpty { worstSummary = s }
                    }
                }
                if skipped > 0 {
                    importance = worst
                    // Prefer the sentence belonging to the most serious
                    // release, not the newest one — that is the release the
                    // wording is about.
                    if !worstSummary.isEmpty { summary = worstSummary }
                    if skipped > 1 && worst != "optional" {
                        summary += " (\(skipped) releases since yours.)"
                    }
                }
            }

            DispatchQueue.main.async {
                done(Update(version: latest,
                            url: o["url"] as? String ?? "",
                            importance: importance,
                            summary: summary))
            }
        }.resume()
    }

    @objc func openDownloadPage() {
        if let u = URL(string: updateURL.isEmpty
                        ? "https://clickgraft.elusive.net/" : updateURL) {
            NSWorkspace.shared.open(u)
        }
    }

    @objc func revealOutput() {
        NSWorkspace.shared.selectFile(outputPath, inFileViewerRootedAtPath: "")
    }
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
