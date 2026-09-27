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
//   ./tools/macvm run --bundle ~/VMs/macos12.bundle
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
                       platform: VZMacPlatformConfiguration) throws -> VZVirtualMachineConfiguration {
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

    // The guest needs the network to fetch HP Click; NAT is enough and needs no
    // privileges.
    let network = VZVirtioNetworkDeviceConfiguration()
    network.attachment = VZNATNetworkDeviceAttachment()
    config.networkDevices = [network]

    config.pointingDevices = [VZMacTrackpadConfiguration()]
    config.keyboards = [VZMacKeyboardConfiguration()]
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

final class Runner: NSObject, NSApplicationDelegate, VZVirtualMachineDelegate {
    let bundle: Bundle
    var vm: VZVirtualMachine!
    var window: NSWindow!

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
        let config = try! makeConfiguration(bundle, cpus: cpus, memoryGB: ram, platform: platform)
        vm = VZVirtualMachine(configuration: config)
        vm.delegate = self

        let view = VZVirtualMachineView(frame: NSRect(x: 0, y: 0, width: 1440, height: 900))
        view.virtualMachine = vm
        view.capturesSystemKeys = false
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
      macvm run --bundle <dir> [--cpus 4] [--ram 8]
    """)
    exit(2)
}
