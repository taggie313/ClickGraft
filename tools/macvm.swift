// tools/macvm.swift — a macOS guest, from a restore image, without a GUI to click.
//
// Built to answer one question the project has been hedging on since the
// capability matrix went in: HP Click declares LSMinimumSystemVersion 12.0 while
// its own libcrypto, libssl and libmagic declare 15.0, and nobody has ever
// started one on a Mac that old. The site says so. capabilities.py says so. The
// decision not to stamp 15.0 onto a frozen copy rests on it.
//
// UTM can make such a VM, but its New VM sheet is modal and cannot be driven in
// the background, and a static check is not an answer: scanning the SDK headers
// for symbols newer than macOS 12 missed _strchrnul, which is the exact symbol
// that caused this class of crash here before. A method that cannot see the one
// case it exists for is not evidence.
//
// So: create, install, run. Everything after the guest boots is an ordinary
// window that background control can reach.
//
//   swiftc -O -o tools/macvm tools/macvm.swift -framework Virtualization -framework AppKit
//   codesign --force -s - --entitlements tools/macvm.entitlements tools/macvm
//
//   ./tools/macvm install --ipsw <path.ipsw> --bundle ~/VMs/macos12.bundle [--disk 64] [--ram 8]
//   ./tools/macvm run --bundle ~/VMs/macos12.bundle [--disk2 <raw.img>] [--control <file>]
//
// Needs com.apple.security.virtualization; ad-hoc signing carries it fine for
// local use.

import AppKit
import Foundation
import Virtualization

// MARK: - bundle layout

struct Bundle {
    let root: URL
    var disk: URL { root.appendingPathComponent("Disk.img") }
    var aux: URL { root.appendingPathComponent("AuxiliaryStorage") }
    var hardwareModel: URL { root.appendingPathComponent("HardwareModel") }
    var machineIdentifier: URL { root.appendingPathComponent("MachineIdentifier") }
}

func die(_ message: String) -> Never {
    FileHandle.standardError.write(("macvm: " + message + "\n").data(using: .utf8)!)
    exit(1)
}

func arg(_ flag: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: flag), i + 1 < a.count else { return nil }
    return a[i + 1]
}

// MARK: - configuration shared by install and run

/// The guest's hardware. Built the same way in both modes: a VM restored with one
/// hardware model and booted with another does not boot, and the failure is a
/// black screen rather than a message.
func makeConfiguration(_ bundle: Bundle, cpus: Int, memoryGB: UInt64,
                       platform: VZMacPlatformConfiguration,
                       extraDisk: URL? = nil) throws -> VZVirtualMachineConfiguration {
    let config = VZVirtualMachineConfiguration()
    config.platform = platform
    config.cpuCount = cpus
    config.memorySize = memoryGB * 1024 * 1024 * 1024
    config.bootLoader = VZMacOSBootLoader()

    let display = VZMacGraphicsDeviceConfiguration()
    display.displays = [VZMacGraphicsDisplayConfiguration(widthInPixels: 1440,
                                                          heightInPixels: 900,
                                                          pixelsPerInch: 80)]
    config.graphicsDevices = [display]

    let attachment = try VZDiskImageStorageDeviceAttachment(url: bundle.disk, readOnly: false)
    config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: attachment)]

    // A second, read-only disk is how files get INTO the guest. Synthetic input
    // reaches a macOS guest badly: the pointer never receives move events, so menu
    // tracking hit-tests against a stale position and every menu-bar click opens
    // the Apple menu, and a Command-Shift chord arrives as something else again.
    // Handing the guest a volume it mounts by itself needs no typing at all --
    // and read-only means the host can still read the image while the guest has it.
    if let extra = extraDisk {
        let ro = try VZDiskImageStorageDeviceAttachment(url: extra, readOnly: true)
        config.storageDevices.append(VZVirtioBlockDeviceConfiguration(attachment: ro))
    }

    // The guest needs the network to fetch HP Click; NAT is enough and needs no
    // privileges.
    let network = VZVirtioNetworkDeviceConfiguration()
    network.attachment = VZNATNetworkDeviceAttachment()
    config.networkDevices = [network]

    // USB, not the Mac trackpad/keyboard: VZMacTrackpadConfiguration needs a
    // macOS 13 or newer GUEST, and a macOS 12 guest given one sees no pointing
    // device at all -- Setup Assistant sits on "connect a mouse" forever, which
    // looks like a hung VM rather than a misconfiguration.
    config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
    config.keyboards = [VZUSBKeyboardConfiguration()]
    config.audioDevices = []

    try config.validate()
    return config
}

// MARK: - install

func install() {
    guard let ipswPath = arg("--ipsw"), let bundlePath = arg("--bundle") else {
        die("install needs --ipsw <path> and --bundle <dir>")
    }
    let cpus = Int(arg("--cpus") ?? "4") ?? 4
    let ram = UInt64(arg("--ram") ?? "8") ?? 8
    let diskGB = UInt64(arg("--disk") ?? "64") ?? 64

    let bundle = Bundle(root: URL(fileURLWithPath: (bundlePath as NSString).expandingTildeInPath))
    let ipsw = URL(fileURLWithPath: (ipswPath as NSString).expandingTildeInPath)

    if FileManager.default.fileExists(atPath: bundle.root.path) {
        die("\(bundle.root.path) already exists — delete it or choose another --bundle")
    }
    try! FileManager.default.createDirectory(at: bundle.root, withIntermediateDirectories: true)

    print("reading the restore image…")
    let sema = DispatchSemaphore(value: 0)
    var image: VZMacOSRestoreImage?
    var loadError: Error?
    VZMacOSRestoreImage.load(from: ipsw) { result in
        switch result {
        case .success(let got): image = got
        case .failure(let e): loadError = e
        }
        sema.signal()
    }
    sema.wait()
    if let e = loadError { die("could not read \(ipsw.path): \(e.localizedDescription)") }
    guard let image = image else { die("no restore image") }
    guard let requirements = image.mostFeaturefulSupportedConfiguration else {
        die("this Mac cannot virtualise that restore image")
    }
    let v = image.operatingSystemVersion
    let versionText = "macOS \(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
    print("  \(versionText), build \(image.buildVersion)")
    print("  wants at least \(requirements.minimumSupportedCPUCount) CPU and "
          + "\(requirements.minimumSupportedMemorySize / 1024 / 1024 / 1024) GB")

    let cpuCount = max(cpus, requirements.minimumSupportedCPUCount)
    let memory = max(ram * 1024 * 1024 * 1024, requirements.minimumSupportedMemorySize)
        / 1024 / 1024 / 1024

    // The disk is created sparse: it reports its full size and occupies what is
    // written, so a 64 GB guest does not cost 64 GB on the host up front.
    FileManager.default.createFile(atPath: bundle.disk.path, contents: nil)
    let handle = try! FileHandle(forWritingTo: bundle.disk)
    try! handle.truncate(atOffset: diskGB * 1024 * 1024 * 1024)
    try! handle.close()

    let platform = VZMacPlatformConfiguration()
    platform.hardwareModel = requirements.hardwareModel
    platform.auxiliaryStorage = try! VZMacAuxiliaryStorage(creatingStorageAt: bundle.aux,
                                                           hardwareModel: requirements.hardwareModel,
                                                           options: [])
    platform.machineIdentifier = VZMacMachineIdentifier()
    // Kept beside the disk: restoring with one hardware model and booting with
    // another gives a black screen, not an error.
    try! platform.hardwareModel.dataRepresentation.write(to: bundle.hardwareModel)
    try! platform.machineIdentifier.dataRepresentation.write(to: bundle.machineIdentifier)

    let config = try! makeConfiguration(bundle, cpus: cpuCount, memoryGB: memory, platform: platform)
    let vm = VZVirtualMachine(configuration: config)

    print("installing \(versionText) into \(bundle.root.path)")
    print("  \(cpuCount) CPU, \(memory) GB RAM, \(diskGB) GB disk")

    let installer = VZMacOSInstaller(virtualMachine: vm, restoringFromImageAt: ipsw)
    var done = false
    var failure: Error?
    // Progress is the only sign of life for twenty minutes; print it on one line
    // so a log of this is readable afterwards.
    let observer = installer.progress.observe(\.fractionCompleted, options: [.new]) { p, _ in
        let pct = Int(p.fractionCompleted * 100)
        FileHandle.standardError.write("\r  \(pct)% ".data(using: .utf8)!)
    }
    installer.install { result in
        if case .failure(let e) = result { failure = e }
        done = true
    }
    while !done && RunLoop.current.run(mode: .default, before: .distantFuture) {}
    observer.invalidate()
    FileHandle.standardError.write("\n".data(using: .utf8)!)
    if let e = failure { die("install failed: \(e.localizedDescription)") }
    print("installed. Now: macvm run --bundle \(bundle.root.path)")
}

// MARK: - run

// MARK: - control channel
//
// Synthetic input reaches a macOS guest badly from outside. Events posted to the
// app by the usual automation paths arrive without the pointer ever MOVING, so
// the guest's menu tracking hit-tests a stale position and every menu-bar click
// opens the Apple menu; Command chords do not arrive as chords at all; and two
// separate clicks are never close enough together to be a double-click. All three
// were measured here against a macOS 12.4 guest, and between them they make a
// guest unusable for anything past a text field.
//
// So macvm builds the events itself and hands them to the view: a move before
// every press, a real clickCount, and flagsChanged around modifiers. Commands are
// read a line at a time from a file, which needs no port and no privileges:
//
//   move  <x> <y>            pointer, in GUEST display points, origin top-left
//   click <x> <y> [count]    count 2 is a double-click
//   key   <keyCode> [flags]  flags: c=command s=shift a=option t=control
//   text  <string>           one keystroke per character
//   sleep <ms>
//
// The file is truncated as it is read, so appending a line runs it.
final class Control {
    let path: String
    weak var view: VZVirtualMachineView?
    private var timer: Timer?

    init(path: String, view: VZVirtualMachineView) {
        self.path = path
        self.view = view
        FileManager.default.createFile(atPath: path, contents: Data())
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.drain()
        }
    }

    private func drain() {
        guard let data = FileManager.default.contents(atPath: path), !data.isEmpty,
              let text = String(data: data, encoding: .utf8) else { return }
        try? Data().write(to: URL(fileURLWithPath: path))
        for line in text.split(separator: "\n") {
            run(String(line))
        }
    }

    /// Guest display point -> window point. Computed from the view's live bounds
    /// rather than assumed, because the window is resizable and the guest's
    /// framebuffer is letterboxed inside it.
    private func windowPoint(_ gx: Double, _ gy: Double) -> NSPoint {
        guard let view = view else { return .zero }
        let b = view.bounds
        let guestW = 1440.0, guestH = 900.0
        let scale = min(b.width / guestW, b.height / guestH)
        let drawnW = guestW * scale, drawnH = guestH * scale
        let originX = b.minX + (b.width - drawnW) / 2
        let originY = b.minY + (b.height - drawnH) / 2
        // AppKit's y grows upward; the guest's grows downward.
        let vx = originX + gx * scale
        let vy = originY + (guestH - gy) * scale
        return view.convert(NSPoint(x: vx, y: vy), to: nil)
    }

    private func flags(_ spec: String) -> NSEvent.ModifierFlags {
        var f: NSEvent.ModifierFlags = []
        if spec.contains("c") { f.insert(.command) }
        if spec.contains("s") { f.insert(.shift) }
        if spec.contains("a") { f.insert(.option) }
        if spec.contains("t") { f.insert(.control) }
        return f
    }

    private func mouse(_ type: NSEvent.EventType, _ p: NSPoint, _ count: Int) {
        guard let view = view, let window = view.window else { return }
        guard let e = NSEvent.mouseEvent(with: type, location: p, modifierFlags: [],
                                         timestamp: ProcessInfo.processInfo.systemUptime,
                                         windowNumber: window.windowNumber, context: nil,
                                         eventNumber: 0, clickCount: count, pressure: 1) else { return }
        switch type {
        case .leftMouseDown: view.mouseDown(with: e)
        case .leftMouseUp: view.mouseUp(with: e)
        default: view.mouseMoved(with: e)
        }
    }

    private func key(_ code: UInt16, _ f: NSEvent.ModifierFlags, characters: String?) {
        guard let view = view, let window = view.window else { return }
        let chars = characters ?? ""
        func make(_ type: NSEvent.EventType) -> NSEvent? {
            NSEvent.keyEvent(with: type, location: .zero, modifierFlags: f,
                             timestamp: ProcessInfo.processInfo.systemUptime,
                             windowNumber: window.windowNumber, context: nil,
                             characters: chars, charactersIgnoringModifiers: chars,
                             isARepeat: false, keyCode: code)
        }
        // The guest needs to see the modifier go down as its own event, or the
        // chord arrives as a bare keypress.
        if !f.isEmpty, let fc = NSEvent.keyEvent(with: .flagsChanged, location: .zero,
                                                 modifierFlags: f, timestamp: ProcessInfo.processInfo.systemUptime,
                                                 windowNumber: window.windowNumber, context: nil,
                                                 characters: "", charactersIgnoringModifiers: "",
                                                 isARepeat: false, keyCode: modifierKeyCode(f)) {
            view.flagsChanged(with: fc)
        }
        if let d = make(.keyDown) { view.keyDown(with: d) }
        if let u = make(.keyUp) { view.keyUp(with: u) }
        if !f.isEmpty, let fc = NSEvent.keyEvent(with: .flagsChanged, location: .zero,
                                                 modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                                 windowNumber: window.windowNumber, context: nil,
                                                 characters: "", charactersIgnoringModifiers: "",
                                                 isARepeat: false, keyCode: modifierKeyCode(f)) {
            view.flagsChanged(with: fc)
        }
    }

    private func modifierKeyCode(_ f: NSEvent.ModifierFlags) -> UInt16 {
        if f.contains(.command) { return 55 }
        if f.contains(.shift) { return 56 }
        if f.contains(.option) { return 58 }
        if f.contains(.control) { return 59 }
        return 0
    }

    private func run(_ line: String) {
        let parts = line.split(separator: " ").map(String.init)
        guard let verb = parts.first else { return }
        switch verb {
        case "move":
            guard parts.count >= 3, let x = Double(parts[1]), let y = Double(parts[2]) else { return }
            mouse(.mouseMoved, windowPoint(x, y), 0)
        case "click":
            guard parts.count >= 3, let x = Double(parts[1]), let y = Double(parts[2]) else { return }
            let count = parts.count > 3 ? (Int(parts[3]) ?? 1) : 1
            let p = windowPoint(x, y)
            mouse(.mouseMoved, p, 0)
            for n in 1...max(1, count) {
                mouse(.leftMouseDown, p, n)
                mouse(.leftMouseUp, p, n)
            }
        case "key":
            guard parts.count >= 2, let code = UInt16(parts[1]) else { return }
            key(code, flags(parts.count > 2 ? parts[2] : ""), characters: nil)
        case "text":
            let body = String(line.dropFirst(verb.count).drop(while: { $0 == " " }))
            for ch in body { typeCharacter(ch) }
        case "sleep":
            if parts.count >= 2, let ms = Double(parts[1]) {
                Thread.sleep(forTimeInterval: ms / 1000)
            }
        default: break
        }
    }

    /// US-layout key codes. Only what a path and a URL need; anything else is
    /// better handed over as a file than typed.
    private func typeCharacter(_ ch: Character) {
        let lower: [Character: UInt16] = [
            "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
            "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32,
            "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
            "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
            "-": 27, "=": 24, "[": 33, "]": 30, "\\": 42, ";": 41, "'": 39,
            ",": 43, ".": 47, "/": 44, "`": 50, " ": 49
        ]
        let shifted: [Character: UInt16] = [
            ":": 41, "_": 27, "+": 24, "{": 33, "}": 30, "|": 42, "\"": 39,
            "<": 43, ">": 47, "?": 44, "~": 50, "!": 18, "@": 19, "#": 20, "$": 21,
            "%": 23, "^": 22, "&": 26, "*": 28, "(": 25, ")": 29
        ]
        if let code = lower[ch] {
            key(code, [], characters: String(ch))
        } else if ch.isUppercase, let code = lower[Character(ch.lowercased())] {
            key(code, [.shift], characters: String(ch))
        } else if let code = shifted[ch] {
            key(code, [.shift], characters: String(ch))
        }
        Thread.sleep(forTimeInterval: 0.03)
    }
}

final class Runner: NSObject, NSApplicationDelegate, VZVirtualMachineDelegate {
    let bundle: Bundle
    var vm: VZVirtualMachine!
    var window: NSWindow!
    var control: Control?

    init(bundle: Bundle) { self.bundle = bundle }

    func applicationDidFinishLaunching(_ note: Notification) {
        let hardware = VZMacHardwareModel(dataRepresentation: try! Data(contentsOf: bundle.hardwareModel))!
        let identifier = VZMacMachineIdentifier(dataRepresentation: try! Data(contentsOf: bundle.machineIdentifier))!
        let platform = VZMacPlatformConfiguration()
        platform.hardwareModel = hardware
        platform.machineIdentifier = identifier
        platform.auxiliaryStorage = VZMacAuxiliaryStorage(url: bundle.aux)

        let cpus = Int(arg("--cpus") ?? "4") ?? 4
        let ram = UInt64(arg("--ram") ?? "8") ?? 8
        let extra = arg("--disk2").map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
        let config = try! makeConfiguration(bundle, cpus: cpus, memoryGB: ram, platform: platform,
                                            extraDisk: extra)
        vm = VZVirtualMachine(configuration: config)
        vm.delegate = self

        let view = VZVirtualMachineView(frame: NSRect(x: 0, y: 0, width: 1440, height: 900))
        view.virtualMachine = vm
        view.capturesSystemKeys = false
        if let controlPath = arg("--control") {
            control = Control(path: (controlPath as NSString).expandingTildeInPath, view: view)
        }
        window = NSWindow(contentRect: view.frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "macvm — " + bundle.root.lastPathComponent
        window.contentView = view
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        vm.start { result in
            if case .failure(let e) = result {
                FileHandle.standardError.write("macvm: start failed: \(e.localizedDescription)\n"
                                                .data(using: .utf8)!)
            }
        }
    }

    func guestDidStop(_ virtualMachine: VZVirtualMachine) { NSApp.terminate(nil) }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        FileHandle.standardError.write("macvm: guest stopped: \(error.localizedDescription)\n"
                                        .data(using: .utf8)!)
        NSApp.terminate(nil)
    }
}

func run() {
    guard let bundlePath = arg("--bundle") else { die("run needs --bundle <dir>") }
    let bundle = Bundle(root: URL(fileURLWithPath: (bundlePath as NSString).expandingTildeInPath))
    guard FileManager.default.fileExists(atPath: bundle.disk.path) else {
        die("no disk at \(bundle.disk.path) — run install first")
    }
    let app = NSApplication.shared
    let runner = Runner(bundle: bundle)
    app.delegate = runner
    app.setActivationPolicy(.regular)
    app.run()
}

// MARK: - entry

switch CommandLine.arguments.dropFirst().first {
case "install": install()
case "run": run()
default:
    print("""
    macvm — a macOS guest without a GUI to click through

      macvm install --ipsw <path.ipsw> --bundle <dir> [--cpus 4] [--ram 8] [--disk 64]
      macvm run --bundle <dir> [--cpus 4] [--ram 8] [--disk2 <raw.img>] [--control <file>]
    """)
    exit(2)
}
