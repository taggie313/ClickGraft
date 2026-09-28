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
//   ./packaging/build_app.sh

import AppKit
import CryptoKit
import Foundation

// MARK: - The interpreter

/// The Python the backend runs on, and where it comes from.
///
/// ClickGraft *is* Python: the app is a front end that spawns
/// `python3 -m clickgraft.cli agent`. /usr/bin/python3 is not a Python -- on
/// macOS 27 it is a 200,560-byte xcrun shim with 78 hard links, the same inode
/// as clang, lipo and otool, and /System/Library/Frameworks/Python.framework is
/// gone. On a Mac without Apple's Command Line Tools, running it is what brings
/// up macOS's offer to install them: a multi-gigabyte download behind an
/// administrator password, which on a managed Mac is someone else's to give.
/// ClickGraft could not start at all.
///
/// Bundling python.org's framework fixed that, and charged every user 17 MB of
/// every download for a problem most of them do not have: ClickGraft.zip went
/// 770 KB -> 18 MB. So the app carries the *pin* and fetches the framework
/// once, on the Macs that need one, into the user's own Application Support.
///
/// What makes the fetch defensible is not the transport. python-pin.json is a
/// recorded source: the release tag fixes the file, the file fixes the sha256,
/// and nothing is unpacked until the bytes match it. HTTPS decides whether the
/// download succeeds, never whether it is trusted.
enum PythonPayload {
    struct Pin {
        let version: String
        let url: URL
        let sha256: String
    }

    /// Resources/python-pin.json, which build_app.sh copies from packaging/.
    /// nil from a source checkout, where there is no app around this code and
    /// /usr/bin/python3 is right there and working.
    static let pin: Pin? = {
        guard let path = Bundle.main.path(forResource: "python-pin", ofType: "json"),
              let data = FileManager.default.contents(atPath: path),
              let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let version = o["version"] as? String,
              let sha = o["payload_zip_sha256"] as? String,
              let published = o["payload_url"] as? String,
              !sha.isEmpty
        else { return nil }
        // Overridable so the whole path -- fetch, hash, unpack, and each way it
        // refuses -- can be exercised against a local file, including before
        // one is published.
        let from = ProcessInfo.processInfo.environment["CLICKGRAFT_PYTHON_PAYLOAD_URL"]
            ?? published
        guard let url = URL(string: from) else { return nil }
        return Pin(version: version, url: url, sha256: sha.lowercased())
    }()

    /// Where an installed one lives. Keyed by version, so a new pin does not
    /// land on top of the framework the running app is using.
    static func home(_ pin: Pin) -> String {
        // Overridable because nothing else can move it: FileManager finds the
        // real Application Support whatever HOME says, so without this a test
        // would install over the copy the developer's own ClickGraft is using.
        if let root = ProcessInfo.processInfo.environment["CLICKGRAFT_PYTHON_HOME"] {
            return root + "/" + pin.version
        }
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory() + "/Library/Application Support")
        return base.path + "/ClickGraft/python/" + pin.version
    }

    /// The interpreter this Mac already fetched, or nil.
    ///
    /// Two things have to hold. The marker says an install finished: it is
    /// written last, after the unpack, the signature and the move all passed, so
    /// an interrupted install leaves a directory this refuses rather than runs.
    /// And the framework still has to be signed by whoever signed ClickGraft.
    ///
    /// The signature is checked on every launch and not just at install, because
    /// the marker on its own decides nothing an attacker could not decide too:
    /// it is a text file in a directory the user can write, and the value it
    /// holds is public -- it ships in Contents/Resources/python-pin.json and is
    /// published on the site. A review planted an empty framework and a marker
    /// copied from the pin, and the app accepted it. It also pointed out the
    /// reach: this is consulted BEFORE /usr/bin/python3, so a Mac with working
    /// developer tools that never needed a fetch would prefer the plant.
    ///
    /// 30 ms, measured on a 48 MB framework.
    static var installed: String? {
        guard let pin = pin else { return nil }
        let dir = home(pin)
        let exe = dir + "/Python.framework/Versions/Current/bin/python3"
        guard FileManager.default.isExecutableFile(atPath: exe),
              let stamp = try? String(contentsOfFile: dir + "/.pinned", encoding: .utf8),
              stamp.trimmingCharacters(in: .whitespacesAndNewlines) == pin.sha256,
              trusted(dir + "/Python.framework")
        else { return nil }
        return exe
    }

    /// The Team ID ClickGraft itself is signed with, or nil for a build that
    /// carries none -- a local build_app.sh output, or a source checkout.
    static let teamID: String? = {
        let text = stderr("/usr/bin/codesign", ["-dv", "--verbose=4", Bundle.main.bundlePath])
        for line in text.split(separator: "\n") where line.hasPrefix("TeamIdentifier=") {
            let value = line.dropFirst("TeamIdentifier=".count)
            return value == "not set" ? nil : String(value)
        }
        return nil
    }()

    /// Whether a framework is one ClickGraft should run.
    ///
    /// `--verify --strict` ALONE does not answer that. It checks code against
    /// its own designated requirement, so an ad-hoc seal satisfies it -- which
    /// means it establishes integrity and says nothing about who signed. The
    /// requirement below is what ties the interpreter to the same Developer ID
    /// as the app, and it is the difference between "these bytes are intact"
    /// and "these bytes are ours".
    ///
    /// Fail-SAFE, not fail-stuck: a false here makes `installed` nil, so the app
    /// falls through to /usr/bin/python3 or offers the fetch again. It never
    /// leaves someone with an app that refuses to start.
    static func trusted(_ framework: String) -> Bool {
        guard let team = teamID else {
            // Nothing to match against. Check the seal and accept it: an
            // unsigned ClickGraft is a development build, and refusing here
            // would break running from a checkout.
            return shell("/usr/bin/codesign", ["--verify", "--strict", framework]).status == 0
        }
        // The leading "=" matters: codesign -R reads its argument as a PATH to a
        // requirement file unless the text starts with one. Without it every
        // framework fails with "invalid requirement specification", including
        // ClickGraft's own -- which would have made this "hardening" a silent
        // refusal of the real interpreter, i.e. worse than not having it.
        let requirement = "=anchor apple generic and certificate leaf[subject.OU] = \"\(team)\""
        return shell("/usr/bin/codesign",
                     ["--verify", "--strict", "-R", requirement, framework]).status == 0
    }

    /// A tool's stderr, whole. shell() keeps one line on purpose; this is for
    /// reading output rather than reporting a failure.
    private static func stderr(_ tool: String, _ args: [String]) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        let err = Pipe()
        p.standardError = err
        p.standardOutput = Pipe()
        do { try p.run() } catch { return "" }
        let data = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(decoding: data, as: UTF8.self)
    }

    enum Failure: Error {
        case noPin
        case busy
        case cancelled
        case network(String)
        case mismatch(String, String)
        case unpack(String)
        case signature(String)
        case broken(String)

        /// docs/wizard-copy.md, "ClickGraft needs one more piece".
        var text: String {
            switch self {
            case .noPin:
                return "This copy of ClickGraft doesn't know what to fetch."
            case .busy:
                return "Another copy of ClickGraft is already fetching it. Wait for that "
                     + "one to finish, then try again."
            case .cancelled:
                // Only reached by --fetch-python, which has no Cancel; the
                // wizard has already settled and shown the offer again.
                return "Stopped."
            case .network(let why):
                return "The download didn't finish: \(why)"
            case .mismatch:
                return "What arrived isn't what this version of ClickGraft expects, so "
                     + "nothing was installed. Try again \u{2014} if it keeps happening, "
                     + "something between this Mac and the download is changing it."
            case .unpack(let why):
                // Named separately because the advice differs: a corrupt download
                // is worth retrying, a full disk is not.
                if why.contains("No space left on device") || why.contains("Disk quota") {
                    return "There isn't enough room on this Mac to unpack it. It needs about "
                         + "110 MB free while it installs, and 50 MB afterwards."
                }
                return "The download arrived but couldn't be unpacked: \(why)"
            case .signature(let why):
                return "The download arrived but macOS wouldn't vouch for it, so nothing "
                     + "was installed: \(why)"
            case .broken(let why):
                return "The download was fine but installing it failed: \(why)"
            }
        }

        /// For a report. Deliberately not on the screen: two hashes mean nothing
        /// to the person reading, and the sentence above says what to do.
        var detail: String? {
            if case .mismatch(let want, let got) = self {
                return "expected \(want)\ngot      \(got)"
            }
            return nil
        }
    }

    private static var running: Fetcher?

    /// Set by cancel(), cleared when a fetch starts.
    ///
    /// Cancel used to stop only the DOWNLOAD. accept() is called synchronously
    /// from the delegate callback and consulted nothing, so pressing Cancel
    /// during "Unpacking" still unpacked 49 MB, verified it and wrote .pinned
    /// for a fetch the person had backed out of -- while the button's own
    /// comment said nothing was left half-installed.
    private static let cancelLock = NSLock()
    private static var cancelling = false
    static var isCancelling: Bool {
        cancelLock.lock(); defer { cancelLock.unlock() }
        return cancelling
    }
    private static func setCancelling(_ value: Bool) {
        cancelLock.lock(); defer { cancelLock.unlock() }
        cancelling = value
    }

    /// Fetches, verifies and installs. Both closures are called on the main
    /// queue; `progress` runs 0...1. A second call while one is in flight is
    /// ignored, so a double-click cannot start two.
    static func install(progress: @escaping (Double) -> Void,
                        done: @escaping (Result<String, Failure>) -> Void) {
        guard let pin = pin else {
            DispatchQueue.main.async { done(.failure(.noPin)) }
            return
        }
        guard running == nil else { return }
        setCancelling(false)
        let f = Fetcher(pin: pin, progress: progress, done: done)
        running = f
        f.start()
    }

    /// Stop a fetch in flight. Answers nothing back: the caller asked for this
    /// and has already moved on to another screen.
    static func cancel() {
        setCancelling(true)
        running?.cancel()
        running = nil
    }

    /// Everything between "the bytes arrived" and "there is an interpreter".
    /// Runs off the main queue, throws Failure.
    static func accept(_ zip: URL, pin: Pin, progress: (Double) -> Void) throws -> String {
        let fm = FileManager.default
        let got = try sha256(zip)
        guard got == pin.sha256 else { throw Failure.mismatch(pin.sha256, got) }
        progress(0.35)

        let dir = home(pin)
        let parent = (dir as NSString).deletingLastPathComponent
        try? fm.createDirectory(atPath: parent, withIntermediateDirectories: true)

        // One installer at a time, across PROCESSES.
        //
        // The in-process guards (running, settled) do nothing about two copies of
        // ClickGraft, or --fetch-python run twice by a deployment script, or a
        // script racing the person at the keyboard. A review reproduced that in 2
        // of 14 staggered double-launches: both unpacked into the SAME staging
        // path, and what got installed was a tree with one of ditto's .BC.* temp
        // files left in it -- so codesign --verify failed on the installed
        // framework ever after, while .pinned said it had passed. The second
        // check the whole design rests on was void for that install, permanently,
        // because nothing re-runs it.
        let lock = Lock(parent + "/.installing")
        defer { lock.release() }
        if let problem = lock.problem {
            throw Failure.broken("ClickGraft can't write to \(parent): \(problem)")
        }
        guard lock.taken else { throw Failure.busy }

        // A staging directory nobody else can be using, even so: the lock is
        // advisory and a stale one must not wedge the app forever. Unpacking
        // beside the destination keeps the final move on one filesystem.
        let staging = parent + "/.staging-" + UUID().uuidString
        // Cleared on EVERY exit, not only the failures this function remembers to
        // name. An interrupted fetch used to leave 49 MB behind for good.
        defer { try? fm.removeItem(atPath: staging) }
        sweepStale(parent, keeping: staging)

        // ditto, not unzip: it is what made the archive, and it keeps the
        // symlinks a framework is built out of. Like codesign below it is a real
        // binary in /usr/bin -- one hard link, not one of the 78-link xcrun
        // shims -- so neither needs a developer tool to be installed.
        let unpacked = shell("/usr/bin/ditto", ["-x", "-k", zip.path, staging])
        guard unpacked.status == 0 else {
            throw Failure.unpack(unpacked.problem ?? "ditto exited \(unpacked.status)")
        }
        let framework = staging + "/Python.framework"
        guard fm.fileExists(atPath: framework) else {
            throw Failure.unpack("it did not contain an interpreter")
        }
        // Checked here and again below: these are the two points after which
        // giving up stops being free. The defer clears staging either way.
        if isCancelling { throw Failure.cancelled }
        progress(0.7)

        // The second check. The sha256 says these are the bytes the release
        // named; this says macOS's own verifier finds the seal intact, so a bad
        // unpack or a damaged archive is caught even when the bytes hashed right.
        //
        // trusted(), not a bare --verify --strict: that checks code against its
        // own designated requirement, so an ad-hoc seal satisfies it. The
        // requirement inside trusted() names ClickGraft's own Team ID, which is
        // what makes this a check on provenance rather than only on integrity.
        guard trusted(framework) else {
            let why = shell("/usr/bin/codesign", ["--verify", "--strict", framework])
            throw Failure.signature(why.problem
                ?? "it is not signed by whoever signed ClickGraft")
        }
        progress(0.85)
        if isCancelling { throw Failure.cancelled }

        // Replace without a window where `dir` exists but holds nothing usable.
        // The old code removed `dir` and then moved staging in, so a move that
        // failed after the remove -- a network home, a file locked by something
        // else -- destroyed a working interpreter and left nothing in its place.
        let previous = parent + "/.replaced-" + UUID().uuidString
        var displaced = false
        if fm.fileExists(atPath: dir) {
            do {
                try fm.moveItem(atPath: dir, toPath: previous)
                displaced = true
            } catch {
                throw Failure.broken(error.localizedDescription)
            }
        }
        do {
            try fm.moveItem(atPath: staging, toPath: dir)
        } catch {
            // Put back what was working before giving up.
            if displaced { try? fm.moveItem(atPath: previous, toPath: dir) }
            throw Failure.broken(error.localizedDescription)
        }
        if displaced { try? fm.removeItem(atPath: previous) }

        let exe = dir + "/Python.framework/Versions/Current/bin/python3"
        guard fm.isExecutableFile(atPath: exe) else {
            throw Failure.broken("what came out has no python3 in it")
        }
        // Last, after everything above passed: the marker the next launch reads.
        do { try pin.sha256.write(toFile: dir + "/.pinned", atomically: true, encoding: .utf8) }
        catch { throw Failure.broken(error.localizedDescription) }
        progress(1.0)
        return exe
    }

    /// Leftovers from a run that was killed between unpacking and installing.
    /// Each is ~49 MB, so they are swept rather than left for someone to find.
    private static func sweepStale(_ parent: String, keeping: String) {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: parent) else { return }
        for name in names where name.hasPrefix(".staging-") || name.hasPrefix(".replaced-") {
            let path = parent + "/" + name
            if path == keeping { continue }
            try? fm.removeItem(atPath: path)
        }
    }

    /// One installer at a time, across processes, via flock(2).
    ///
    /// The first version of this was an O_EXCL lock file holding the owner's pid,
    /// with liveness and age rules to take over a stale one. A review compiled
    /// that class verbatim and ran 400 barrier-synchronised double-launches
    /// against a lock naming a dead pid: 16 of them had BOTH processes believe
    /// they held it. The takeover was removeItem-then-create, so the second
    /// process deleted the lock the first had just created with O_EXCL and then
    /// created its own. The lock whose entire purpose was to stop two installers
    /// let two through, in exactly the case -- a crashed or force-quit fetch --
    /// the takeover rules existed for.
    ///
    /// flock has no such path. The kernel owns the lock, releases it when the
    /// file descriptor closes OR the process dies, and LOCK_NB means a second
    /// holder is told no rather than waiting. There is nothing to take over, so
    /// there is no takeover to get wrong: no pid to parse, no liveness probe, no
    /// age rule, and the lock file itself is never removed (removing it is what
    /// reintroduces the race).
    final class Lock {
        private var fd: Int32 = -1
        private(set) var taken = false
        /// The lock could not be opened at all -- an unwritable directory, a
        /// read-only volume, a path that is a file. Distinguished from "busy",
        /// because telling someone whose home is unwritable to wait for another
        /// copy to finish is advice that can never come true, and they will
        /// retry for ever.
        private(set) var problem: String?

        init(_ path: String) {
            fd = open(path, O_CREAT | O_RDWR, 0o644)
            guard fd >= 0 else {
                problem = String(cString: strerror(errno))
                return
            }
            if flock(fd, LOCK_EX | LOCK_NB) == 0 {
                taken = true
                return
            }
            // EWOULDBLOCK is the ordinary "someone else has it"; anything else
            // is a real failure and is worth saying out loud.
            if errno != EWOULDBLOCK {
                problem = String(cString: strerror(errno))
            }
            close(fd)
            fd = -1
        }

        func release() {
            guard fd >= 0 else { return }
            if taken { _ = flock(fd, LOCK_UN) }
            close(fd)
            fd = -1
        }
    }

    /// Streamed: the payload is 16 MB and reading it whole to hash it would
    /// hold all of it in memory for no reason.
    static func sha256(_ file: URL) throws -> String {
        guard let h = FileHandle(forReadingAtPath: file.path) else {
            throw Failure.unpack("the download could not be reopened")
        }
        defer { try? h.close() }
        var digest = SHA256()
        while true {
            let chunk = h.readData(ofLength: 1 << 20)
            if chunk.isEmpty { break }
            digest.update(data: chunk)
        }
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }

    /// status, and the most informative line of stderr if there was one.
    ///
    /// Not simply the LAST line, which is what this did first. ditto reports the
    /// cause and then its own summary, so a full disk printed
    ///   .../_sha1.cpython-313-darwin.so: No space left on device
    ///   ditto: Couldn't read pkzip signature.
    /// and the user was shown the second one -- telling someone whose disk is
    /// full that the download is corrupt, which sends them to fetch 17 MB again,
    /// indefinitely. Prefer a line that names a cause.
    private static func shell(_ tool: String, _ args: [String])
        -> (status: Int32, problem: String?) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        let err = Pipe()
        p.standardError = err
        p.standardOutput = Pipe()
        do { try p.run() } catch { return (-1, error.localizedDescription) }
        let data = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let lines = String(decoding: data, as: UTF8.self)
            .split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        // errno strings macOS actually hands back for the failures worth telling
        // apart. A line carrying one of these is the cause; anything else is a
        // summary, and the last line is usually the summary.
        let causes = ["No space left on device", "Permission denied", "Read-only file system",
                      "Operation not permitted", "Disk quota exceeded",
                      "No such file or directory", "Input/output error"]
        if let cause = lines.first(where: { line in causes.contains { line.contains($0) } }) {
            return (p.terminationStatus, cause)
        }
        return (p.terminationStatus, lines.last)
    }

    /// Holds the URLSession alive for the length of one download, and does the
    /// work in its delegate callbacks.
    private final class Fetcher: NSObject, URLSessionDownloadDelegate {
        private let pin: Pin
        private let progress: (Double) -> Void
        private let done: (Result<String, Failure>) -> Void
        private var session: URLSession!
        // Written by cancel() on the main queue, read and written by the
        // delegate callbacks on URLSession's queue, so it needs a lock rather
        // than luck.
        private let gate = NSLock()
        private var settledFlag = false
        private var settled: Bool {
            get { gate.lock(); defer { gate.unlock() }; return settledFlag }
            set { gate.lock(); defer { gate.unlock() }; settledFlag = newValue }
        }

        /// Claims the right to answer, once. Returns false if someone already has.
        private func claimSettle() -> Bool {
            gate.lock(); defer { gate.unlock() }
            if settledFlag { return false }
            settledFlag = true
            return true
        }

        init(pin: Pin, progress: @escaping (Double) -> Void,
             done: @escaping (Result<String, Failure>) -> Void) {
            self.pin = pin
            self.progress = progress
            self.done = done
            super.init()
            let cfg = URLSessionConfiguration.ephemeral
            cfg.timeoutIntervalForRequest = 60
            cfg.timeoutIntervalForResource = 900
            // Said, not left to a default. The site's own figures depend on
            // telling this apart from a person downloading ClickGraft: the
            // access log is classified by user agent, and one fetch per
            // tool-less Mac counted as a download would inflate the one number
            // worth having (site/deploy/visitor-classify.awk, and the path rule
            // in summary.sh as a second line of defence).
            let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"]
                as? String ?? "0"
            cfg.httpAdditionalHeaders = ["User-Agent": "ClickGraft/\(version) (interpreter)"]
            session = URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
        }

        private var task: URLSessionDownloadTask?

        func start() {
            let t = session.downloadTask(with: pin.url)
            task = t
            t.resume()
        }

        /// Cancelling makes didCompleteWithError fire, which would otherwise
        /// report "cancelled" to a screen that has already been replaced, so
        /// settle first and let that callback find itself a no-op.
        func cancel() {
            settled = true
            task?.cancel()
            session.invalidateAndCancel()
        }

        private func settle(_ r: Result<String, Failure>) {
            guard claimSettle() else { return }
            session.finishTasksAndInvalidate()
            DispatchQueue.main.async {
                PythonPayload.running = nil
                self.done(r)
            }
        }

        // Downloading is most of the wait, so it gets most of the bar.
        func urlSession(_ s: URLSession, downloadTask: URLSessionDownloadTask,
                        didWriteData bytesWritten: Int64,
                        totalBytesWritten: Int64,
                        totalBytesExpectedToWrite: Int64) {
            guard totalBytesExpectedToWrite > 0 else { return }
            let f = Double(totalBytesWritten) / Double(totalBytesExpectedToWrite)
            DispatchQueue.main.async { self.progress(f * 0.7) }
        }

        func urlSession(_ s: URLSession, task: URLSessionTask,
                        didCompleteWithError error: Error?) {
            if let e = error { settle(.failure(.network(e.localizedDescription))) }
        }

        func urlSession(_ s: URLSession, downloadTask: URLSessionDownloadTask,
                        didFinishDownloadingTo location: URL) {
            // The file is deleted the moment this returns, so everything that
            // reads it has to happen here rather than after.
            if let http = downloadTask.response as? HTTPURLResponse,
               http.statusCode != 200 {
                settle(.failure(.network("the download answered \(http.statusCode)")))
                return
            }
            do {
                let exe = try PythonPayload.accept(location, pin: pin) { f in
                    DispatchQueue.main.async { self.progress(0.7 + f * 0.3) }
                }
                settle(.success(exe))
            } catch let f as Failure {
                settle(.failure(f))
            } catch {
                settle(.failure(.broken(error.localizedDescription)))
            }
        }
    }
}

// MARK: - Backend bridge

/// Which of Apple's developer tools the backend runs with.
///
/// /usr/bin/python3, clang, lipo and the rest are stubs that forward to whatever
/// `xcode-select` points at. When that is Xcode and Xcode has been updated,
/// every one of them refuses to run -- exit 69, "You have not agreed to the Xcode
/// license agreements" -- until the new licence is accepted. Xcode 27.0 did
/// exactly that to a Mac on 15 Sep 2026, the week it shipped, and ClickGraft
/// then said only that it "couldn't start" and that reopening usually helps,
/// which it cannot.
///
/// The stubs honour DEVELOPER_DIR, and the Command Line Tools carry no licence
/// gate: with the licence still unaccepted, DEVELOPER_DIR pointed at them ran
/// Python and git normally. So when the Command Line Tools are also installed,
/// ClickGraft uses them and carries on. When they are not, it says what is
/// actually wrong and how to clear it.
enum Toolchain {
    enum State { case normal, commandLineTools, xcodeLicenceNeeded }

    /// Overridable so both outcomes can be exercised on a Mac whose licence is
    /// already accepted: point CLICKGRAFT_PYTHON at a stand-in that prints the
    /// licence message and exits 69, and CLICKGRAFT_CLT_DIR somewhere empty.
    /// Where the interpreter came from. Named rather than implied, because
    /// which one it is decides what the Requirements screen can say and whether
    /// there is anything to fetch.
    enum Source {
        case override(String)     // CLICKGRAFT_PYTHON, for tests
        case bundled(String)      // carried inside the app
        case fetched(String)      // fetched once into Application Support
        case system               // /usr/bin/python3, with tools behind it
        case none                 // nothing to run: PythonPayload has the answer

        /// What to run, or nil when there is nothing.
        var path: String? {
            switch self {
            case .override(let p), .bundled(let p), .fetched(let p): return p
            case .system: return "/usr/bin/python3"
            case .none: return nil
            }
        }

        /// For --fetch-python and the report. Not for a screen: the wizard never
        /// tells anyone which interpreter it found.
        var name: String {
            switch self {
            case .override: return "CLICKGRAFT_PYTHON"
            case .bundled: return "bundled in the app"
            case .fetched: return "fetched by ClickGraft"
            case .system: return "/usr/bin/python3"
            case .none: return "none"
            }
        }
    }

    private static var cachedSource: Source?
    static var source: Source {
        if let c = cachedSource { return c }
        let s = resolveSource()
        cachedSource = s
        return s
    }

    /// The path to run. "/usr/bin/python3" for `.none` as well, so every caller
    /// that only wants a path keeps working; ask `needsPython` before starting
    /// anything that has to succeed.
    static var python: String { source.path ?? "/usr/bin/python3" }

    /// Nothing on this Mac can run the backend, and one can be fetched.
    static var needsPython: Bool {
        if case .none = source { return PythonPayload.pin != nil }
        return false
    }

    private static func resolveSource() -> Source {
        if let o = ProcessInfo.processInfo.environment["CLICKGRAFT_PYTHON"] {
            return .override(o)
        }
        // An app that carries one. build_app.sh stopped bundling the framework
        // in 1.8.0 -- it was 17 MB of every download for a problem most Macs do
        // not have -- but CLICKGRAFT_BUNDLE_PYTHON=1 still builds one that does,
        // which is what an IT department deploying to Macs with no internet
        // wants. Checked first so such a build never reaches the network.
        let bundled = Bundle.main.bundlePath
            + "/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
        if FileManager.default.isExecutableFile(atPath: bundled) { return .bundled(bundled) }

        if let fetched = PythonPayload.installed { return .fetched(fetched) }

        // /usr/bin/python3 last, and only when something is behind it. Asking
        // the filesystem rather than running it is the whole point: running a
        // shim with nothing behind it is what raises macOS's offer to install
        // the Command Line Tools, and sparing that offer to someone who does
        // not need it is why ClickGraft fetches an interpreter at all.
        //
        // Running from a source checkout lands here, where /usr/bin/python3 is
        // right there and working, so development needs no build step.
        if systemPythonWorks() { return .system }
        return .none
    }

    /// Whether /usr/bin/python3 can actually run -- established without running
    /// it blind. The shim forwards to whatever `xcode-select` points at, so if
    /// no developer directory holds a python3 there is nothing to forward to and
    /// nothing worth provoking. Once one exists, running it is safe and settles
    /// the licence question too.
    ///
    /// xcode-select itself is a real binary (one hard link, not one of the 78
    /// that share the shim's inode), so asking it costs nothing.
    static func systemPythonWorks() -> Bool {
        // Set on a Mac that HAS the tools, to reach the path taken by one that
        // does not. The same reason CLICKGRAFT_CLT_DIR exists: the outcome worth
        // testing is the one the test machine cannot be in.
        if ProcessInfo.processInfo.environment["CLICKGRAFT_NO_SYSTEM_PYTHON"] == "1" {
            return false
        }
        let fm = FileManager.default
        let dirs = [cltDir, selectedDeveloperDir()].compactMap { $0 }
        guard dirs.contains(where: { fm.isExecutableFile(atPath: $0 + "/usr/bin/python3") })
        else { return false }
        // The literal path, never Toolchain.python: that resolves through this,
        // and asking it here would recurse forever.
        if runPython(at: "/usr/bin/python3", [:]).status == 0 { return true }
        // It ran and refused. The one refusal worth surviving is Xcode's
        // outstanding licence, which probe() clears by pointing the shim at the
        // Command Line Tools instead; state/developerDir do that for every
        // later call, so agreeing here is agreeing with them.
        return fm.isExecutableFile(atPath: cltDir + "/usr/bin/python3")
            && runPython(at: "/usr/bin/python3", ["DEVELOPER_DIR": cltDir]).status == 0
    }

    /// `xcode-select -p`, or nil when it names nothing.
    static func selectedDeveloperDir() -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        p.arguments = ["-p"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        let path = String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path
    }

    static var cltDir: String {
        ProcessInfo.processInfo.environment["CLICKGRAFT_CLT_DIR"]
            ?? "/Library/Developer/CommandLineTools"
    }

    private static var cached: State?
    static func refresh() { cached = nil; cachedSource = nil }
    static var state: State {
        if let c = cached { return c }
        let s = probe()
        cached = s
        return s
    }
    /// DEVELOPER_DIR for the backend, or nil to leave the Mac's own choice alone.
    static var developerDir: String? { state == .commandLineTools ? cltDir : nil }

    private static func run(_ extra: [String: String]) -> (status: Int32, stderr: String) {
        return runPython(at: python, extra)
    }

    private static func runPython(at path: String, _ extra: [String: String])
        -> (status: Int32, stderr: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = ["-c", ""]
        var env = ProcessInfo.processInfo.environment
        // Even to ask "does this run?". `python3 -c ""` imports encodings, and
        // an interpreter allowed to write .pyc puts them inside the framework
        // -- which breaks its code signature, so trusted() then refuses the
        // interpreter ClickGraft itself just installed and the next launch
        // fetches 17 MB again. For ever.
        //
        // Found by running the shipped 1.8.0 against the live site: the app
        // fetched an interpreter, probe() ran it one screen later, and asking
        // the same app about it again answered "fetching". Agent.process sets
        // this for the same reason; this path did not, and this path runs first.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for (k, v) in extra { env[k] = v }
        p.environment = env
        let err = Pipe()
        p.standardError = err
        p.standardOutput = Pipe()
        do { try p.run() } catch { return (-1, "") }
        let data = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    private static func probe() -> State {
        let first = run([:])
        // Only the licence is handled here. Missing tools altogether make the
        // stub offer to install them, which the requirements screen already
        // explains; that path is deliberately left as it was.
        guard first.status != 0, first.stderr.lowercased().contains("license") else {
            return .normal
        }
        if FileManager.default.isExecutableFile(atPath: cltDir + "/usr/bin/python3"),
           run(["DEVELOPER_DIR": cltDir]).status == 0 {
            return .commandLineTools
        }
        return .xcodeLicenceNeeded
    }

    /// The Xcode whose licence is outstanding: the one `xcode-select` points at,
    /// not whichever Xcode Launch Services finds first. Acceptance is per Xcode
    /// version, so opening a different copy would not clear it.
    static func selectedXcode() -> URL? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
        p.arguments = ["-p"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let path = String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let r = path.range(of: ".app/Contents/Developer") else { return nil }
        return URL(fileURLWithPath: String(path[..<r.lowerBound]) + ".app")
    }
}

final class Agent {
    let resources: URL
    init(resources: URL) { self.resources = resources }

    private func process(_ args: [String]) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Toolchain.python)
        p.arguments = ["-m", "clickgraft.cli", "agent"] + args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = resources.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"      // never dirty a signed bundle
        // Inherited by everything the backend runs -- clang, lipo, otool,
        // install_name_tool -- which sit behind the same licence gate.
        if let dev = Toolchain.developerDir { env["DEVELOPER_DIR"] = dev }
        p.environment = env
        p.currentDirectoryURL = resources
        return p
    }

    /// One request, one reply, waited for. In practice never nil: with no
    /// reply to trust it is an error with stage "backend" (BackendTransport
    /// .swift), so check the reply's "type" or the key you need, not only
    /// whether there is one. 1.5.9 returned nil there; once this returned the
    /// error instead, Choose's Check again took it for an empty Applications
    /// folder and said "No HP Click found" (22 Sep 2026 review).
    func once(_ args: [String]) -> [String: Any]? {
        let completed = DispatchSemaphore(value: 0)
        var result: [String: Any] = [:]
        // Delivered on a global queue: the caller is the main thread, blocked
        // below, so a reply sent to the main queue would never arrive.
        BackendStream(process: process(args), singleReply: true,
                      deliveryQueue: DispatchQueue.global()) { event in
            result = event
            completed.signal()
        }.start()
        completed.wait()
        return result
    }

    func stream(_ args: [String], onEvent: @escaping ([String: Any]) -> Void) {
        BackendStream(process: process(args), onEvent: onEvent).start()
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
        // A panel insets its content by 14 a side, so the room inside it is
        // narrower than the room outside. text() assumes the outside width, and a
        // label laid out for 644pt inside a 632pt panel measures its own height
        // for fewer lines than it draws -- the overflow is clipped, mid-sentence,
        // and the app looks like it forgot how to finish a word. Seen on Choose,
        // 27 Sep 2026: "so there is nothing you need from" and then nothing.
        // Fixed here rather than in every caller, so it stays fixed.
        for case let label as NSTextField in views {
            label.preferredMaxLayoutWidth = width - (margin * 2) - 30
        }
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
        let text = provider()
        tv.string = text
        // Tall enough for the text, up to what the screen can spare. At a flat
        // 170 the "can't install them" advice ended mid-sentence, which reads
        // as the app being broken rather than as something to scroll.
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).reduce(0) {
            $0 + max(1, Int(ceil(Double($1.count) / 96.0)))
        }
        let height = min(430.0, max(170.0, Double(lines) * 13.5 + 16))
        scroll.documentView = tv
        addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.widthAnchor.constraint(equalToConstant: UI.width - UI.margin * 2 - 10),
            scroll.topAnchor.constraint(equalTo: toggle.bottomAnchor, constant: 6),
            scroll.heightAnchor.constraint(equalToConstant: height),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        shown = scroll
        toggle.title = "▾ " + label
    }
}

// MARK: - Wizard

final class Wizard: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var agent: Agent!
    var container: NSView!

    var candidates: [[String: Any]] = []
    /// agent.capability_overview(): which HP Click this Mac and printer need, the
    /// whole measured table, and where HP still serves each build. nil when the
    /// backend has no table, in which case the reference screen is not offered --
    /// people who already have the app installed do not go back to a website to
    /// read this, which is the whole reason it is in here.
    var capabilityOverview: [String: Any]?
    var picked: [String: Any]?
    var plan: [String: Any] = [:]
    var outputPath = ""
    // Whether outputPath is ~/Applications rather than /Applications, because
    // this account may not write to the latter. The Review screen has to say
    // so: a copy landing in the home folder with no explanation reads as the
    // tool choosing the wrong place, not as the only place it is allowed.
    var outputPerUser = false
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
    var updateDownloadURL = ""
    var updateBanner: NSView?
    /// The result of the launch check, kept so screens other than the first can
    /// use it. The unsupported-version panel is the one that needs it: the
    /// person seeing that panel is disproportionately someone whose copy predates
    /// support for the HP Click they have.
    var latestUpdate: Update?
    var allowIntelHost = false

    // The copy's name is fixed, so every build replaces the one made before it.
    // Review asks the backend what that copy is (1.5.8), and these carry its
    // answer to the button: nothing is replaced while the old copy is open, and
    // a copy that supports printers the new one won't is replaced only after a
    // deliberate tick, which is then passed on as --accept-printer-loss.
    var createButton: NSButton?
    var replacedIsOpen = false
    var replaceLosesPrinters = false
    var acceptPrinterLoss = false
    // Review's macOS check (1.5.9): this Mac is older than the copy would
    // need, so the button stays off. The backend refuses too.
    var macTooOld = false

    // Since 1.5.9 a build sets the copy it replaces aside, hidden, until the
    // new one has passed its checks. `leftovers` is any the backend found from
    // a run that was stopped in between (agent env); `asidePath` is the one a
    // failed build here could not put back. Both are the owner's working copy,
    // so nothing here deletes or restores one without being asked.
    var leftovers: [[String: Any]] = []
    var leftoverDeferred = false
    var asidePath = ""
    // The leftover at the output path Review is about to build into, if any;
    // the button stays off until it is dealt with.
    var pendingLeftover: [String: Any]?
    // Why Choose's Check again got no list, for the Choose screen it redraws.
    var rescanProblem: String?

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
            UI.body("HP Click for Mac was one of those until version 4.11.31. That "
                    + "translation is why it's slow to start and why clicks take a moment "
                    + "to register. HP released 4.11.31 in September 2026 built for Apple "
                    + "Silicon; if that's the one you have, ClickGraft will say there's "
                    + "nothing to do."),
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
                         "There is no uninstaller. If ClickGraft had to fetch a small "
                         + "program to do its work, that stays in your Library folder, "
                         + "under Application Support, in a folder named ClickGraft."),
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
            self.latestUpdate = up
            self.updateURL = up.url
            self.updateDownloadURL = up.download
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

    // MARK: 2a — Fetching the interpreter

    /// Why the last fetch failed, so the screen can come back with it.
    var pythonProblem: PythonPayload.Failure?

    /// This Mac has nothing the backend can run on, so nothing has run yet.
    ///
    /// Before Requirements, which reports what the backend found and therefore
    /// cannot be drawn until there is a backend. docs/wizard-copy.md,
    /// "ClickGraft needs one more piece".
    @objc func showNeedPython() {
        var rows: [NSView] = [UI.title("ClickGraft needs one more piece")]
        if let problem = pythonProblem {
            var inner: [NSView] = [UI.point(problem.text, "")]
            if let detail = problem.detail {
                inner.append(Disclosure { detail })
            }
            rows.append(UI.panel(inner, tint: NSColor.systemOrange.withAlphaComponent(0.13)))
        }
        rows += [
            UI.body("The part of ClickGraft that does the work needs a small program this "
                    + "Mac doesn't have. ClickGraft can fetch it now: 17 MB, once, and "
                    + "never again."),
            UI.panel([
                UI.point("It goes in your own Library folder, alongside your other app "
                         + "settings.",
                         "Nothing is installed into macOS and nobody is asked for an "
                         + "administrator password."),
                UI.point("ClickGraft knows what it should receive.",
                         "This version records a fingerprint of the exact file, checks what "
                         + "arrives against it, and installs nothing unless they match."),
            ], tint: NSColor.systemGreen.withAlphaComponent(0.10)),
            // Not "the same place ClickGraft itself came from": plenty of people
            // download the app from its GitHub releases page, and telling them
            // the fetch comes from where they got it would be false for exactly
            // the readers most likely to check.
            UI.small("It comes from clickgraft.elusive.net, ClickGraft's own site. "
                     + "The file and its fingerprint are published there too, so the "
                     + "check ClickGraft makes is one you can repeat."),
            Disclosure(label: "Why this Mac and not others") { """
                Macs set up for software development already have this program, and \
                ClickGraft uses the one that's there. Most Macs used for design or print \
                work don't, and asking macOS for it means a download of several gigabytes \
                behind an administrator password — for one small piece of it.

                So ClickGraft fetches that piece instead. If you'd rather not download \
                anything, installing Apple's Command Line Tools also works: ClickGraft will \
                find them next time it opens.
                """ },
        ]
        present(rows, buttons: [
            UI.button("Quit", self, #selector(quit)),
            UI.spacer(),
            UI.button(pythonProblem == nil ? "Fetch it" : "Try again",
                      self, #selector(fetchPython), primary: true),
        ])
    }

    /// Stop the fetch and go back to the offer. PythonPayload tears the download
    /// down and clears its staging directory; nothing is left half-installed.
    @objc func cancelFetchPython() {
        PythonPayload.cancel()
        pythonProblem = nil
        showNeedPython()
    }

    @objc func fetchPython() {
        pythonProblem = nil
        let b = NSProgressIndicator()
        b.isIndeterminate = false
        b.minValue = 0; b.maxValue = 1
        b.translatesAutoresizingMaskIntoConstraints = false
        b.widthAnchor.constraint(equalToConstant: UI.width - UI.margin * 2 - 10).isActive = true
        bar = b
        let cap = UI.body("Fetching")
        caption = cap
        // A way out. Without one the only exit during a 17 MB download on a slow
        // or metered connection was the window's close button, which quits the
        // app (applicationShouldTerminateAfterLastWindowClosed) in the middle of
        // unpacking -- the exact kill that used to strand 49 MB on the disk.
        present([UI.title("ClickGraft needs one more piece"), b, cap],
                buttons: [UI.button("Cancel", self, #selector(cancelFetchPython)),
                          UI.spacer()])

        PythonPayload.install(progress: { [weak self] f in
            self?.bar?.doubleValue = f
            // The same four words the copy promises, driven off the one number
            // the fetch actually knows.
            self?.caption?.stringValue =
                f < 0.7 ? "Fetching"
                : f < 0.75 ? "Checking what arrived"
                : f < 0.9 ? "Unpacking"
                : "Checking macOS is happy with it"
        }, done: { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success:
                // Straight on: they asked for one thing and it happened, so a
                // screen saying so would be a step that only reports itself.
                Toolchain.refresh()
                self.showRequirements()
            case .failure(let problem):
                self.pythonProblem = problem
                self.showNeedPython()
            }
        })
    }

    // MARK: 2 — Requirements

    /// The macOS every copy ClickGraft can make needs, for the one screen that
    /// comes before the backend has run: 15.0 for 4.8.117, 4.8.118 and 4.10.42
    /// (22 Sep 2026). HP's own libmagic declares it in all three, and Homebrew
    /// publishes three of the four support libraries only for macOS 15 and
    /// later (clickgraft/macos_floor.py). Review and the build work out each
    /// copy's own minimum and have the last word; this only spares someone on
    /// macOS 12 to 14 the Command Line Tools, which /usr/bin/python3 offers to
    /// install the moment anything runs it -- a large download, behind an
    /// administrator password, for a copy their Mac could never open.
    /// tests/test_rollback_and_signing.py checks it against every stock HP
    /// Click it can find, so it can't drift above what a copy needs.
    static let copiesNeedMacOS = OperatingSystemVersion(majorVersion: 15, minorVersion: 0, patchVersion: 0)

    /// copiesNeedMacOS the way people say it: "15".
    var copiesNeedName: String {
        macName("\(Wizard.copiesNeedMacOS.majorVersion).\(Wizard.copiesNeedMacOS.minorVersion)")
    }

    /// Apple Silicon, asked of the kernel rather than the backend, which can't
    /// run yet here. True under Rosetta too: hw.optional.arm64 describes the
    /// Mac, not the process.
    static var hostIsAppleSilicon: Bool {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        return sysctlbyname("hw.optional.arm64", &value, &size, nil, 0) == 0 && value == 1
    }

    @objc func showRequirements() {
        // Before anything runs python3: on a Mac without the Command Line Tools
        // that alone brings up macOS's offer to install them. On an Intel Mac
        // the copy is for another Mac, whose macOS the Intel panel below speaks
        // to, so only Apple Silicon is checked.
        if Wizard.hostIsAppleSilicon
            && !ProcessInfo.processInfo.isOperatingSystemAtLeast(Wizard.copiesNeedMacOS) {
            showMacTooOldForAnyCopy()
            return
        }
        // Probed afresh each time, so Check again notices an accepted licence.
        Toolchain.refresh()
        // Before Toolchain.state, which runs the interpreter to probe it: with
        // nothing behind /usr/bin/python3 that run IS macOS's offer to install
        // the Command Line Tools, and not raising it is the point.
        if Toolchain.needsPython {
            showNeedPython()
            return
        }
        if Toolchain.state == .xcodeLicenceNeeded {
            showXcodeLicence()
            return
        }
        guard let d = agent.once(["env"]), let e = d["env"] as? [String: Any] else {
            present([UI.title("ClickGraft couldn't start"),
                     UI.body("The part of ClickGraft that does the work didn't respond. "
                             + "Reopening the app usually clears this.")],
                    buttons: [UI.spacer(), UI.button("Quit", self, #selector(quit), primary: true)])
            return
        }
        env = e
        candidates = d["candidates"] as? [[String: Any]] ?? []
        capabilityOverview = d["capabilities"] as? [String: Any]
        outputPath = d["default_output"] as? String ?? ""
        outputPerUser = d["output_per_user"] as? Bool ?? false
        leftovers = d["leftovers"] as? [[String: Any]] ?? []
        if !leftovers.isEmpty && !leftoverDeferred {
            showLeftover()
            return
        }
        // Default true: if an older backend omits the key, fail open rather
        // than blocking every user on a missing field.
        let silicon = e["apple_silicon"] as? Bool ?? true

        var rows: [NSView] = [
            UI.title("What ClickGraft needs"),
            // Not "Apple's Command Line Tools" any more, and this screen said so
            // for a while after it stopped being true. What it showed was worse
            // than out of date: e["clt"] comes from check_clt(), whose list of
            // required tools is now empty, so it is true on every Mac -- and the
            // screen told a Mac with no developer tools at all that Apple's were
            // installed and there was nothing to do.
            UI.body("Nothing you have to install. ClickGraft needs a Mac with Apple Silicon "
                    + "and your own copy of HP Click, and it brings the rest itself."),
            // Scoped to the one fact it is entitled to assert. Unscoped, it read
            // "Everything ClickGraft needs is here" directly above the orange
            // panel telling an Intel Mac the copy will not run on it, and above
            // a Choose screen that may find no HP Click at all. The panel it
            // replaced was scoped the same way ("the Command Line Tools are
            // installed"), so the contradiction was new.
            UI.panel([UI.point("Nothing to install.",
                               "ClickGraft brings everything it needs.")],
                     tint: NSColor.systemGreen.withAlphaComponent(0.10)),
        ]
        // Said, not hidden: the tools list in the detail below will name the
        // Command Line Tools rather than Xcode, and a report should be able to
        // explain why.
        if Toolchain.state == .commandLineTools {
            rows.append(UI.small("Xcode on this Mac is waiting for its licence to be "
                                 + "accepted, so ClickGraft is using the Command Line "
                                 + "Tools instead. Nothing for you to do."))
        }
        do {
            // Kept, and now always reachable. It used to appear only when the
            // Command Line Tools were missing, which no longer happens -- and
            // what it says is the most useful thing on the screen for anyone on
            // a Mac they do not administer, which is who it was written for.
            rows.append(Disclosure(label: "If this is a Mac you don't administer") { [weak self] in
                let dropped = (self?.candidates.first {
                    ($0["reason"] as? String) == "hp_native"
                }?["printers_dropped"] as? [String]) ?? []
                let models = dropped.isEmpty
                    ? "DesignJet T310, T320, T350, T720 and T750"
                    : (self?.printerList(dropped) ?? "")
                let tSeries = "the " + models
                let floor = self?.copiesNeedName ?? "15"
                return """
                Putting the copy into Applications needs an administrator password, and \
                making one downloads Apple's Apple Silicon engine and two small libraries \
                from the internet. On a managed Mac both are usually someone else's to \
                allow. Three ways round it:

                1. Check HP Click 4.11.31 first. HP's September 2026 version runs on Apple \
                Silicon by itself: no tools, no copy, nothing for ClickGraft to do. Download \
                it from HP's support page — but not if you print to \(tSeries), which only \
                HP Click 4.8.117 supports.

                2. Ask IT to make one copy for everyone. They make the copy once, on a Mac \
                they administer, and what comes out is an ordinary app they can deploy like \
                any other. It needs nothing installed on the Macs that receive it — no \
                ClickGraft, no downloads — and nothing from the Mac that made it, but they do \
                need macOS \(floor) or later. Forward them the notes below.

                3. Or make the copy on a Mac of your own, with your version \
                of HP Click installed, and bring the app over. This Mac needs macOS \(floor) \
                or later to open it, and so does a Mac with Apple Silicon to make it. Move it \
                on a USB drive or a file share if you can: after AirDrop or a download, macOS \
                refuses to open it the first time, and you have to allow it in System \
                Settings, under Privacy & Security.

                ---
                For whoever does it, measured on macOS 27 (20 Sep 2026):

                • Deploy it as a package. An app installed from a package carries no \
                quarantine flag and opens normally; the same app sent by AirDrop or \
                downloaded is blocked until someone allows it in System Settings.

                • Build that package on the Mac that made the copy. A copy that was \
                itself downloaded carries its quarantine flag through the package and into \
                every installed file.

                • Don't re-sign it. The copy needs the entitlements ClickGraft gives it; \
                a re-sign that drops them leaves an app that starts without its print \
                engine. If you re-sign anyway, run ClickGraft's check on the result — it \
                catches this.

                • The copy carries the HP Click version it was made from, so make it from \
                the version your printers need: the \(models) need HP Click 4.8.117.

                • It needs macOS \(floor) or later, and its Info.plist says so: macOS won't \
                open it on an older Mac. A Mac with Apple Silicon needs macOS \(floor) or later \
                to make it too. An Intel Mac can make it on the macOS it has, but can't \
                test-launch it (22 Sep 2026).
                """
            })
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
                UI.small("The Mac you make it for needs macOS \(copiesNeedName) or later: "
                         + "macOS won't open the copy on anything older."),
                UI.small("Move the finished app on a USB drive or a file share if you can. "
                         + "After AirDrop or a download, macOS refuses to open it the first "
                         + "time, because the copy is signed by the Mac that made it rather "
                         + "than by Apple: open System Settings, go to Privacy & Security, "
                         + "and allow it there."),
                cb,
            ], tint: NSColor.systemOrange.withAlphaComponent(0.13)))
        }
        // "them" used to mean the Command Line Tools, which the body above named.
        // It no longer does, and a label with no antecedent is how a screen goes
        // quietly wrong: it read "What ClickGraft uses them for" above a
        // paragraph that had stopped mentioning any them.
        rows.append(Disclosure(label: "What ClickGraft uses on this Mac") { [weak self] in
            let tools = (self?.env["tools"] as? [String: String]) ?? [:]
            let list = tools.sorted { $0.key < $1.key }
                .map { "\($0.key.padding(toLength: 20, withPad: " ", startingAt: 0))"
                     + "\($0.value.isEmpty ? "not found" : $0.value)" }
                .joined(separator: "\n")
            return "No developer tools. Reading your app, rewriting the copy's support "
                 + "files and signing it are all done by ClickGraft itself. What it uses "
                 + "are three programs that come with macOS, and an interpreter to run "
                 + "on, which on this Mac is \(Toolchain.source.name).\n\n" + list
        })

        let next = UI.button("Continue", self, #selector(showChoose), primary: true)
        // No longer gated on e["clt"]: with nothing required from Apple's tools
        // that value is true on every Mac, so gating on it only ever looked like
        // a check. What can still stop a copy being made is the processor.
        next.isEnabled = silicon || allowIntelHost
        let buttons: [NSView] = [UI.button("Back", self, #selector(showWelcome)),
                                 UI.spacer(), next]
        present(rows, buttons: buttons)
    }

    /// This Mac's macOS is older than any copy ClickGraft can make needs
    /// (copiesNeedMacOS). Said here, before the Command Line Tools, and with
    /// HP's own Apple Silicon version, which HP lists for this Mac.
    func showMacTooOldForAnyCopy() {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        let here = "\(v.majorVersion).\(v.minorVersion)"
            + (v.patchVersion == 0 ? "" : ".\(v.patchVersion)")
        let needs = copiesNeedName
        var rows: [NSView] = [
            UI.title("ClickGraft needs macOS \(needs) or later"),
            UI.body("This Mac has macOS \(here). The copy ClickGraft makes needs macOS "
                    + "\(needs) or later, because files HP ships inside HP Click, and the "
                    + "support files ClickGraft adds from Homebrew, are built for it. So "
                    + "ClickGraft can't make one on this Mac, and there's nothing to install "
                    + "for it."),
            UI.panel([
                UI.point("Your HP Click is unchanged.", "It works as it did."),
                UI.point("Once this Mac is on macOS \(needs) or later,",
                         "open ClickGraft again and it can make the copy."),
            ], tint: NSColor.systemOrange.withAlphaComponent(0.12)),
        ]
        // The same facts agent.native_alternative gives Review, which can't be
        // asked yet: HP lists 4.11.31 for macOS 12 to 26, and ClickGraft itself
        // needs 12.
        let alt = nativeAlternative(["version": "4.11.31", "hp_lists_from": "12.0",
                                     "hp_lists_to": "26.0"])
        if !alt.isEmpty {
            rows.append(UI.panel(alt, tint: NSColor.systemBlue.withAlphaComponent(0.10)))
        }
        present(rows, buttons: [UI.button("Back", self, #selector(showWelcome)), UI.spacer(),
                                UI.button("Quit", self, #selector(quit), primary: true)])
    }

    /// Xcode was updated and its licence is unaccepted, and there are no Command
    /// Line Tools to fall back on. Not "couldn't start": that told people to
    /// reopen the app, which changes nothing.
    ///
    /// Opening Xcode is the instruction, not Terminal. A command in a wizard is
    /// a failure of the wizard; it is offered second, for people who prefer it.
    func showXcodeLicence() {
        var steps: [NSView] = [
            UI.point("Open Xcode once.",
                     "It shows Apple's licence. Agree to it (macOS may ask for your "
                     + "Mac's password), then come back here and press Check again. "
                     + "You don't need to do anything else in Xcode."),
        ]
        if Toolchain.selectedXcode() != nil {
            steps.append(UI.button("Open Xcode", self, #selector(openSelectedXcode)))
        }
        steps.append(UI.small("If you'd rather use Terminal, sudo xcodebuild -license "
                              + "accept does the same thing."))
        present([
            UI.title("Xcode needs its licence accepted first"),
            UI.body("ClickGraft uses Apple's developer tools, and on this Mac they come "
                    + "from Xcode. Xcode has been updated, and until its new licence is "
                    + "accepted, macOS won't let anything use those tools, ClickGraft "
                    + "included. Nothing is wrong with ClickGraft or with HP Click, and "
                    + "nothing has been changed."),
            UI.panel(steps, tint: NSColor.systemOrange.withAlphaComponent(0.12)),
        ], buttons: [UI.button("Quit", self, #selector(quit)), UI.spacer(),
                     UI.button("Check again", self, #selector(showRequirements), primary: true)])
    }

    @objc func openSelectedXcode() {
        if let u = Toolchain.selectedXcode() { NSWorkspace.shared.open(u) }
    }

    // MARK: 2a — A previous copy left set aside (1.5.9)

    /// A build sets the copy it replaces aside, hidden, until the new one has
    /// passed its checks, then deletes it -- or puts it back if the new one
    /// fails. Quit or crash in between, or a delete or put-back that fails,
    /// and the owner's previous copy stays set aside, where nothing else would
    /// ever mention it. Neither choice is made for them.
    ///
    /// Worded from the backend's `state` for it (build._leftover_state), never
    /// from assumption. The first version of this screen said "never fully
    /// checked" of whatever was at the path, including a copy that had passed
    /// and one that had failed, and its "Put it back" removed whatever was
    /// there -- once, after two interrupted builds, the owner's own copy
    /// (found in review, 22 Sep 2026). Now the backend only removes the copy
    /// that build put there, and says when something else is there instead.
    @objc func showLeftover() {
        guard let item = leftovers.first else { finishLeftover(); return }
        let version = item["version"] as? String ?? ""
        let current = item["current_version"] as? String ?? ""
        let there = item["restores_to"] as? String ?? ""
        let name = (there as NSString).lastPathComponent.replacingOccurrences(of: ".app", with: "")
        let exists = item["restores_to_exists"] as? Bool ?? false
        // An older backend sends no state: treat a copy there as unknown.
        let state = item["state"] as? String ?? (exists ? "other" : "missing")
        let check = item["check"] as? String ?? ""
        let checkName = Wizard.checkNames[check] ?? (check.isEmpty ? "one of the checks" : check)
        let made = version.isEmpty ? "" : ", made from HP Click \(version),"
        let newMade = current.isEmpty ? "" : ", made from HP Click \(current),"

        let body: String
        var points: [NSView] = []
        var buttons: [NSView] = [UI.button("Decide later", self, #selector(deferLeftover)),
                                 UI.spacer()]
        switch state {
        case "passed":
            body = "The last time ClickGraft made a copy of HP Click, the new copy passed its "
                + "checks, but ClickGraft couldn't remove the one it replaced. Your previous "
                + "copy\(made) is still here, hidden, in the same folder."
            points = [
                UI.point("Remove it if the new copy works for you.",
                         "The new copy\(newMade) stays where it is. That's what ClickGraft "
                         + "would have done."),
                UI.point("Put it back", "if you'd rather go back to it. The new copy is then "
                         + "removed."),
            ]
            buttons += [UI.button("Put it back", self, #selector(restoreLeftover)),
                        UI.button("Remove it", self, #selector(discardLeftover), primary: true)]
        case "failed":
            body = "The last time ClickGraft made a copy of HP Click, the new copy didn't "
                + "pass its checks, and ClickGraft couldn't put your previous copy back in "
                + "its place. Your previous copy\(made) is safe: it's hidden, in the same "
                + "folder. What failed: \(checkName)."
            points = [
                UI.point("Put it back.", "The new copy, which didn't pass, is removed, and "
                         + "your previous copy goes back where it was. If \(name) is open, "
                         + "quit it first."),
                UI.point("Keep the new copy", "only if you've used it since and it works. "
                         + "Your previous copy is then deleted."),
            ]
            buttons += [UI.button("Keep the new copy", self, #selector(discardLeftover)),
                        UI.button("Put it back", self, #selector(restoreLeftover), primary: true)]
        case "missing":
            body = "The last time ClickGraft made a copy of HP Click, it stopped part-way "
                + "through replacing yours. Your previous copy\(made) was set aside first, so "
                + "it's safe, but there's no copy in its place at the moment."
            points = [UI.point("Put it back.", "It goes back where it was, as it was.")]
            buttons += [UI.button("Put it back", self, #selector(restoreLeftover), primary: true)]
        case "other":
            body = "ClickGraft set your previous copy\(made) aside while it was replacing it, "
                + "and it's still here, hidden, in the same folder. The \(name) in its place "
                + "now isn't the one ClickGraft put there"
                + (current.isEmpty ? "" : " (it's made from HP Click \(current))")
                + ", so ClickGraft won't remove it to make room."
            points = [
                UI.point("To put your previous copy back,", "move \(name) out of that folder "
                         + "yourself first — to the Trash, say — then press Put it back."),
                UI.point("If you don't need your previous copy,", "delete it. The \(name) "
                         + "in its place stays as it is."),
            ]
            buttons += [UI.button("Delete it", self, #selector(discardLeftover)),
                        UI.button("Put it back", self, #selector(restoreLeftover), primary: true)]
        default:    // "installed": the build stopped while it was checking
            body = "The last time ClickGraft made a copy of HP Click, it stopped before it "
                + "had finished checking the new one. Your previous copy\(made) was set aside "
                + "first, so it's safe. It's hidden, in the same folder as the new one."
            points = [
                UI.point("Put it back if you're not sure.",
                         "The new copy\(current.isEmpty ? "," : newMade) which never finished "
                         + "its checks, is removed, "
                         + "and your previous copy goes back where it was."),
                UI.point("Keep the new copy", "only if you've used it since and it works. "
                         + "Your previous copy is then deleted."),
            ]
            buttons += [UI.button("Keep the new copy", self, #selector(discardLeftover)),
                        UI.button("Put it back", self, #selector(restoreLeftover), primary: true)]
        }
        points.append(UI.point("Your original HP Click was not changed.", ""))

        present([
            UI.title("Your previous copy is still here"),
            UI.body(body),
            UI.panel(points, tint: NSColor.systemOrange.withAlphaComponent(0.12)),
            // Why "Decide later" costs something: a build refuses while this
            // is waiting (build.LeftoverPendingError), so a second one can
            // never be set aside on top of it.
            UI.small("Until you decide, ClickGraft won't replace \(name) again."),
            UI.small("Set aside at:"),
            UI.text(item["path"] as? String ?? "", size: 11, mono: true),
        ], buttons: buttons)
    }

    /// Where the wizard goes once a leftover is dealt with or put off:
    /// Requirements when it was found at launch, Review when a build was
    /// refused because of it.
    var afterLeftover: (() -> Void)?

    private func finishLeftover() {
        let then = afterLeftover
        afterLeftover = nil
        if let then = then { then() } else { showRequirements() }
    }

    @objc func deferLeftover() {
        leftoverDeferred = true
        finishLeftover()
    }

    @objc func restoreLeftover() {
        guard let path = leftovers.first?["path"] as? String else { return }
        putBack(path) { [weak self] in self?.finishLeftover() }
    }

    @objc func discardLeftover() {
        guard let item = leftovers.first, let path = item["path"] as? String else { return }
        let there = ((item["restores_to"] as? String ?? "") as NSString).lastPathComponent
            .replacingOccurrences(of: ".app", with: "")
        let a = NSAlert()
        a.messageText = "Delete your previous copy?"
        a.informativeText = "It's deleted, not moved to the Trash, and can't be brought "
            + "back. \(there.isEmpty ? "The copy in its place" : there) and your original "
            + "HP Click stay as they are."
        a.addButton(withTitle: "Delete it")
        a.addButton(withTitle: "Cancel")
        guard a.runModal() == .alertFirstButtonReturn else { return }
        let r = agent.once(["discard-previous", "--backup", path]) ?? [:]
        guard (r["type"] as? String) == "discarded" else {
            if (r["stage"] as? String) != "backend", let why = r["error"] as? String {
                tell("ClickGraft couldn't delete it", why)
            } else {
                unconfirmed(r, whether: "it was deleted",
                            maybe: "It may be gone, or still set aside, hidden, in the same folder.")
            }
            return
        }
        finishLeftover()
    }

    /// Put a set-aside copy back, say how it went, then carry on with `then`.
    private func putBack(_ path: String, then: @escaping () -> Void) {
        let r = agent.once(["restore-previous", "--backup", path]) ?? [:]
        guard (r["type"] as? String) == "restored" else {
            // "Still safe, set aside where it was" only when the answer says
            // so. Every refusal carries backup_exists (agent._settle_leftover
            // and _settle_locked); a missing field was read as true until
            // review (22 Sep 2026), so the two refusals that say nothing was
            // moved -- another build holds the folder, and "not a copy
            // ClickGraft set aside" -- claimed it for a path that can hold
            // nothing, after the copy was deleted in the Finder or put back
            // from a second window.
            if (r["stage"] as? String) != "backend", let why = r["error"] as? String,
               r["backup_exists"] as? Bool == true {
                tell("ClickGraft couldn't put it back",
                     why + "\n\nYour previous copy is still safe, set aside where it was.")
            } else {
                unconfirmed(r, whether: "it was put back",
                            maybe: "It may be back in place, or still set aside, hidden, in the "
                            + "same folder.")
            }
            return
        }
        let out = r["output"] as? String ?? ""
        tell("Your previous copy is back",
             "It's at \(out), as it was.")
        then()
    }

    /// No answer ClickGraft can trust about a set-aside copy. Until review
    /// (22 Sep 2026) this said "Your previous copy is still safe, set aside
    /// where it was" whatever happened, though the part of ClickGraft that
    /// does the work may have stopped after moving it. So say it isn't known,
    /// keep the diagnostic out of the sentence, and look again: the leftover
    /// screen behind this alert was drawn before the attempt, and may be wrong.
    private func unconfirmed(_ r: [String: Any], whether: String, maybe: String) {
        let a = NSAlert()
        a.messageText = "ClickGraft couldn't confirm what happened"
        a.informativeText = "It didn't get a clear answer about your previous copy, so it "
            + "can't tell whether \(whether). \(maybe) Press Check again to see where "
            + "things stand."
        let detail = (r["error"] as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !detail.isEmpty {
            let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 460, height: 90))
            tv.string = detail
            tv.isEditable = false
            tv.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
            tv.textColor = .secondaryLabelColor
            let sc = NSScrollView(frame: NSRect(x: 0, y: 0, width: 460, height: 90))
            sc.hasVerticalScroller = true
            sc.documentView = tv
            a.accessoryView = sc
        }
        a.addButton(withTitle: "Check again")
        a.runModal()
        checkAgain()
    }

    /// Start again from Requirements after a result ClickGraft couldn't
    /// confirm, looking afresh for any copy set aside -- even one put off with
    /// Decide later earlier on: the run that gave no answer may have set
    /// another aside since, and what follows has to be about what is there now.
    @objc func checkAgain() {
        leftoverDeferred = false
        afterLeftover = nil
        showRequirements()
    }

    private func tell(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.addButton(withTitle: "OK")
        a.runModal()
    }

    // MARK: 3 — Choose

    @objc func showChoose() {
        var rows: [NSView] = [
            UI.title("Choose your HP Click"),
            UI.body("Pick the HP Click you use now. ClickGraft reads it and leaves it alone."),
        ]

        // Said once, for the Check again that just failed.
        let problem = rescanProblem
        rescanProblem = nil
        if let problem = problem {
            rows.append(UI.panel([
                UI.point("ClickGraft couldn't look again.",
                         "The part of ClickGraft that does the work stopped without an "
                         + "answer, so "
                         + (candidates.isEmpty ? "nothing is listed. " : "this list is from the "
                            + "last time it looked. ")
                         + "Press Check again to try once more."),
                Disclosure { problem },
            ], tint: NSColor.systemOrange.withAlphaComponent(0.12)))
        } else if candidates.isEmpty {
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

        // HP's own Apple Silicon build. 4.11.31 (17 Sep 2026) was the first:
        // every binary carries arm64 and it runs untranslated, so there is
        // nothing to graft. Said plainly and in green, because it is good news,
        // and without the report offer an unknown version gets -- there is
        // nothing wrong with it to report.
        //
        // The printer line is the one reason to still want ClickGraft: HP took
        // the T310/T320/T350/T720/T750 out in 4.8.118 and has not put them back.
        // Computed from the app's own list against 4.8.117's, not asserted.
        if let native = candidates.first(where: { ($0["reason"] as? String ?? "") == "hp_native" }) {
            let ver = native["version"] as? String ?? ""
            let theirs = ver.isEmpty ? "This HP Click" : "HP Click \(ver)"
            let ref = native["reference_version"] as? String ?? "4.8.117"
            var parts: [NSView] = [
                UI.point("\(theirs) already runs natively on Apple Silicon.",
                         "HP released it built for your Mac's processor, so there is "
                         + "nothing for ClickGraft to do. Use it as it is. It doesn't need "
                         + "Rosetta, so it will keep working on future versions of macOS."),
            ]
            // The printer sentence, said about THEIR printer when HP Click has
            // one configured. The dropped list is eight models long and left the
            // reader to work out whether one of them was theirs; the advice knows,
            // because agent.capability_advice asked HP Click's own printers.json.
            let advice = native["advice"] as? [String: Any]
            let mine = advice?["printers"] as? [String] ?? []
            let better = advice?["recommend"] as? String
            if !mine.isEmpty, let better = better, better != ver {
                parts.append(UI.small("Except for your printer. \(printerList(mine)) "
                    + (mine.count == 1 ? "isn't" : "aren't") + " listed by \(theirs), so keep "
                    + "HP Click \(better) — which does list "
                    + (mine.count == 1 ? "it" : "them") + " — and make a ClickGraft copy of it."))
            } else if !mine.isEmpty {
                parts.append(UI.small("Your \(printerList(mine)) is listed by \(theirs), so "
                    + "there is nothing you need from ClickGraft."))
            } else if let dropped = native["printers_dropped"] as? [String], !dropped.isEmpty {
                parts.append(UI.small("One exception: \(theirs) doesn't support the "
                    + printerList(dropped) + ". If you print to one of those, "
                    + "keep HP Click \(ref), which does, and make a ClickGraft copy of it."))
            }
            rows.append(UI.panel(parts, tint: NSColor.systemGreen.withAlphaComponent(0.10)))
        }

        // An HP Click whose own printing code has no arm64 in it. Not "unknown
        // yet": no ClickGraft release can graft it, so neither an update nor a
        // report is the answer, and offering either sends someone to wait for
        // support that cannot arrive. The first report from such a version --
        // 4.7.28, Electron 8 -- came in through exactly that panel.
        //
        // The printer sentence is computed from their app against 4.8.117's list,
        // not asserted, because "you lose nothing by moving" is the one thing a
        // person who deliberately kept an old HP Click needs to be true.
        if let old = candidates.first(where: { ($0["reason"] as? String ?? "") == "cannot_graft" }) {
            let ver = old["version"] as? String ?? ""
            let theirs = ver.isEmpty ? "this HP Click" : "HP Click \(ver)"
            // Which version to send them to. This was always 4.8.117, the oldest
            // ClickGraft supports -- correct for a plotter only 4.8.117 lists, and
            // needlessly roundabout for a T1600 shop, who can run HP's own
            // 4.11.31 natively and needs no copy at all. The advice picks by the
            // printers this Mac is configured for; without it, the old answer.
            let advice = old["advice"] as? [String: Any]
            let mine = advice?["printers"] as? [String] ?? []
            let suggested = advice?["recommend"] as? String
            let ref = suggested ?? (old["reference_version"] as? String ?? "4.8.117")
            // Three states, not two. needs_graft false means "no copy needed",
            // which on an Intel Mac is true of every release and says nothing
            // about who compiled it -- reading it as "HP ships this natively"
            // told an Intel Mac with a T750 that 4.8.117, an Intel-only build,
            // was built for Apple Silicon.
            let needsCopy = (advice?["needs_graft"] as? Bool) ?? true
            let recIsNative = (advice?["recommend_hp_native"] as? Bool) ?? false
            var printers = ""
            if !mine.isEmpty {
                let unlisted = advice?["unlisted"] as? [String] ?? []
                if unlisted.isEmpty {
                    printers = " It lists your \(printerList(mine))."
                } else {
                    printers = " One thing it cannot do: it doesn't list "
                        + printerList(unlisted) + ", and no released HP Click does."
                }
            } else if let lost = old["printers_lost"] as? [String] {
                printers = lost.isEmpty
                    ? " It accepts every printer \(theirs) does."
                    : " Check one thing first: \(ref) no longer lists "
                      + lost.joined(separator: ", ") + ", which \(theirs) does."
            }
            var parts: [NSView] = [
                UI.point("\(theirs) can't be made native, by any version of ClickGraft.",
                         "ClickGraft works by switching on the Apple Silicon code HP already "
                         + "builds into its app, and this version has none. A report or an "
                         + "update won't change that. A newer HP Click will: \(ref) is still "
                         + "free on HP's servers, and "
                         + (recIsNative
                            ? "HP builds it for Apple Silicon itself, so you won't need a copy at all."
                            : needsCopy
                              ? "ClickGraft works with it."
                              : "it runs natively on this Mac, so you won't need a copy at all.")
                         + printers),
            ]
            if let found = old["blockers"] as? [String], !found.isEmpty {
                parts.append(UI.small("What ClickGraft found: " + found.joined(separator: " ")))
            }
            parts.append(UI.button("Where to get \(ref)", self, #selector(openVersionsPage)))
            rows.append(UI.panel(parts, tint: NSColor.systemOrange.withAlphaComponent(0.12)))
        }

        // Only an unrecognised *version* is worth explaining and reporting. An
        // already-made copy is greyed out with its own one-line reason; showing
        // this panel for it reads as "your app is unsupported", which it isn't.
        if candidates.contains(where: { ($0["reason"] as? String ?? "") == "unsupported" }) {
            // Lead with the update when there is one, because for this panel it
            // is usually the answer rather than a suggestion.
            //
            // Support for an HP Click version ships inside a ClickGraft release:
            // 1.3.3 carried one manifest, and every HP Click published after it
            // looks unsupported to that copy forever. So the person most likely
            // to be reading this is someone one download away from it working,
            // and the old wording -- "only works with versions it has been tested
            // against", then a report button -- reads as a dead end and sends
            // them away. Measured on the live site: installs three and four
            // releases behind were still checking for updates daily, and not one
            // unsupported-version report had ever been submitted.
            let here = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
            if let up = latestUpdate {
                rows.append(UI.panel([
                    UI.point("ClickGraft \(up.version) is available, and may already support this.",
                             "You're on \(here). Support for a new HP Click version arrives in a "
                             + "ClickGraft update, so a version this copy doesn't recognise is "
                             + "often one a newer copy does. Update, then run it again."),
                    UI.button("Get the update", self, #selector(openDownloadPage)),
                    UI.small("If the new version doesn't recognise it either, send a report from "
                             + "there and support can be added."),
                ], tint: NSColor.systemBlue.withAlphaComponent(0.10)))
            } else {
                rows.append(UI.panel([
                    UI.small("ClickGraft only works with versions it has been tested against, "
                             + "because it needs to know exactly where to make its changes. "
                             + "Guessing would risk your app."),
                    UI.small("You can send a report describing this version, and support can be "
                             + "added."),
                    UI.button("Create a report", self, #selector(makeReport)),
                ], tint: NSColor.secondaryLabelColor.withAlphaComponent(0.07)))
            }
        }

        let next = UI.button("Continue", self, #selector(showReview), primary: true)
        next.isEnabled = false
        continueButton = next
        var choiceButtons: [NSView] = [UI.button("Back", self, #selector(showRequirements)),
                                       UI.button("Check again", self, #selector(rescan))]
        // Offered only when there is a table to show, so the button never leads
        // to an empty screen.
        if capabilityOverview?["table"] != nil {
            choiceButtons.append(UI.button("Which one do I need?", self,
                                           #selector(showVersionReference)))
        }
        choiceButtons.append(UI.spacer())
        choiceButtons.append(next)
        present(rows, buttons: choiceButtons)

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
    ///
    /// Only an `env` reply is a look at the folder. Anything else is kept
    /// from the list: in review (22 Sep 2026) a reply that wasn't one emptied
    /// it, and Choose then said "No HP Click found in your Applications
    /// folder" of a folder nobody had looked in.
    @objc func rescan() {
        let d = agent.once(["env"]) ?? [:]
        if (d["type"] as? String) == "env" {
            candidates = d["candidates"] as? [[String: Any]] ?? []
            capabilityOverview = d["capabilities"] as? [String: Any]
            outputPath = d["default_output"] as? String ?? outputPath
            outputPerUser = d["output_per_user"] as? Bool ?? outputPerUser
            rescanProblem = nil
        } else {
            rescanProblem = d["error"] as? String ?? ""
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
        // The address is asked for here, before the description is written, so
        // that the preview below is the whole report including it. Asking on the
        // preview itself would send a line the person never saw.
        let a = NSAlert()
        a.messageText = "Creating the report…"
        a.informativeText = "This looks at the app and writes a description of it. "
                          + "It takes a minute."
        let wrap = NSView(frame: NSRect(x: 0, y: 0, width: 460, height: 52))
        let (contactLabel, contact) = contactField(
            label: "Optional \u{2014} where to reach you, to hear when this version is supported:",
            y: 4)
        wrap.addSubview(contactLabel)
        wrap.addSubview(contact)
        a.accessoryView = wrap
        a.addButton(withTitle: "Continue")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = contact
        guard a.runModal() == .alertFirstButtonReturn else { return }
        let contactValue = contact.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)

        if let r = agent.once(["probe", "--source", path]),
           let probe = r["report"] as? String {
            let report = reportHeader(kind: "unsupported-version")
                + contactLines(contactValue, wants: "to hear when this version is supported")
                + (contactValue.isEmpty ? "\n" : "")
                + scrub(probe)
            let dir = FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask)[0]
            let file = dir.appendingPathComponent("ClickGraft report.txt")
            try? report.write(to: file, atomically: true, encoding: .utf8)
            NSWorkspace.shared.selectFile(file.path, inFileViewerRootedAtPath: "")

            // Then ask for it. Saving the file to the Desktop and stopping there
            // put the whole burden on the user to find somewhere to send it, and
            // an unsupported version is precisely the case where their copy is
            // the only thing that can close the gap: HP does not publish most of
            // these, so if nobody sends one, that version stays unsupported for
            // everyone on it. Shown in full first, like every other report.
            let offer = NSAlert()
            offer.messageText = "Send this description to the ClickGraft developers?"
            offer.informativeText = "ClickGraft doesn't know your version of HP Click. "
                + "This describes it — version numbers, file sizes and fingerprints — "
                + "and it is what makes supporting that version possible. HP does not "
                + "publish every build it ships, so for some versions a description "
                + "from someone who has one is the only way it can ever be added.\n\n"
                + "No file names from your work, no printer details, and nothing "
                + "identifying that you did not type yourself. Your home folder name has "
                + "been removed. It is on your Desktop either way — read it first, and "
                + "don't send it if anything in it bothers you."
                + (contactValue.isEmpty ? ""
                   : "\n\nYour address is in there because you entered it. It will be used "
                   + "to reply about this version and for nothing else.")
            let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 460, height: 220))
            tv.string = report
            tv.isEditable = false
            tv.font = .monospacedSystemFont(ofSize: 10, weight: .regular)
            let sc = NSScrollView(frame: NSRect(x: 0, y: 0, width: 460, height: 220))
            sc.hasVerticalScroller = true
            sc.documentView = tv
            offer.accessoryView = sc
            offer.addButton(withTitle: "Send it")
            offer.addButton(withTitle: "Not now")
            if offer.runModal() == .alertFirstButtonReturn {
                postReport(report)
            }
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
        // What is at the output path, as the backend read it: the version the
        // copy was made from, whether it is open, and any printers it supports
        // that the new copy won't. FileManager stays as a fallback so an
        // unreadable answer still says "Replacing" rather than "Creating".
        let existing = p["replacing"] as? [String: Any]
        let replacing = existing != nil || FileManager.default.fileExists(atPath: out)
        let newVersion = p["app_version"] as? String ?? ""
        let oldVersion = existing?["version"] as? String ?? ""
        let madeByUs = existing?["made_by_clickgraft"] as? Bool ?? false
        let lost = existing?["printers_lost"] as? [String] ?? []
        let openPids = existing?["open_pids"] as? [Int] ?? []
        let appName = (out as NSString).lastPathComponent
            .replacingOccurrences(of: ".app", with: "")
        replacedIsOpen = !openPids.isEmpty
        replaceLosesPrinters = !lost.isEmpty
        acceptPrinterLoss = false        // ticked afresh each time this screen is drawn
        let theirs = madeByUs && !oldVersion.isEmpty
            ? "your copy made from HP Click \(oldVersion)" : "the copy that is already there"

        var rows: [NSView] = [
            UI.title("Here's exactly what will happen"),
            UI.body("Nothing has been changed yet. Nothing will be, until you press the "
                    + "button below."),
        ]

        // First of all, because nothing below it can happen. The copy needs
        // the highest macOS any file in it declares (15.0 for every supported
        // version on 22 Sep 2026: HP's own libmagic, and Homebrew's support
        // files), and a Mac older than that would get a copy that fails at
        // launch. The backend refuses too; saying so here spares a download.
        let floor = p["macos_floor"] as? [String: Any] ?? [:]
        macTooOld = (floor["this_mac_ok"] as? Bool) == false
        if macTooOld {
            let needs = floor["needs"] as? String ?? ""
            var parts: [NSView] = [
                UI.point("This copy needs macOS \(macName(needs)) or later, and this Mac has "
                         + "macOS \(floor["this_mac"] as? String ?? "?").",
                         floorReason(floor["reasons"] as? [String] ?? [], needs: needs)),
                UI.small("Your HP Click is unchanged and works as it did. Once this Mac is on "
                         + "macOS \(macName(needs)) or later, ClickGraft can make the copy."),
            ]
            parts += nativeAlternative(floor["alternative"] as? [String: Any])
            rows.append(UI.panel(parts, tint: NSColor.systemOrange.withAlphaComponent(0.14)))
        }

        // A copy an earlier build set aside here and never put back or deleted.
        // The build refuses until the owner has decided (build.py,
        // LeftoverPendingError): a second one set aside on top of it could
        // only be undone in the right order, which nothing on screen explains.
        pendingLeftover = p["leftover"] as? [String: Any]
        if let item = pendingLeftover {
            let v = item["version"] as? String ?? ""
            rows.append(UI.panel([
                UI.point("Your previous copy is still set aside from last time.",
                         "The last time ClickGraft replaced \(appName), it didn't finish, and "
                         + (v.isEmpty ? "your previous copy" : "your previous copy, made from "
                            + "HP Click \(v),")
                         + " is still set aside, hidden, in the same folder. Decide what "
                         + "happens to it before ClickGraft makes another copy."),
                UI.button("Decide now", self, #selector(decidePendingLeftover)),
            ], tint: NSColor.systemOrange.withAlphaComponent(0.14)))
        }

        // First on the screen, because it is the one thing here that can cost
        // someone their plotter. HP took the T310/T320/T350/T720/T750 out in
        // 4.8.118 and shipped that as a background update of 4.8.117, so a
        // T-series owner rebuilding "the HP Click I have" can be rebuilding from
        // a version that no longer supports what they print to. Computed from
        // both apps' own printer lists, never from version numbers, and the
        // button stays off until the box is ticked.
        if !lost.isEmpty {
            let chosen = newVersion.isEmpty ? "The HP Click you chose"
                                            : "HP Click \(newVersion), the one you chose,"
            var parts: [NSView] = [
                UI.point("This replaces \(theirs). The new copy won't support the "
                         + printerList(lost) + ".",
                         "\(chosen) doesn't list them."),
                UI.small("If you print to one of those, keep the copy you have: press Back."),
            ]
            let supported = (env["versions"] as? [String] ?? []).contains(oldVersion)
            if madeByUs && supported && oldVersion != newVersion {
                parts.append(UI.small("Or make the new copy from HP Click \(oldVersion) "
                                      + "instead, which still supports them."))
                parts.append(UI.button("Where to get \(oldVersion)", self,
                                       #selector(openVersionsPage)))
            }
            let tick = NSButton(checkboxWithTitle:
                "Replace it anyway. I don't print to any of these.",
                target: self, action: #selector(togglePrinterLoss(_:)))
            tick.state = .off
            parts.append(tick)
            rows.append(UI.panel(parts, tint: NSColor.systemOrange.withAlphaComponent(0.14)))
        }

        // build.py sets the old copy aside and deletes it once the new one has
        // passed its checks, so it can't be open. Quitting it is left to the
        // person, because it may be in the middle of a print; the backend
        // refuses too, in case it is opened after this screen was drawn.
        if !openPids.isEmpty {
            rows.append(UI.panel([
                UI.point("\(appName) is open.",
                         "Quit it before you create the new copy. ClickGraft won't replace "
                         + "an app while it's running, and it won't quit it for you, in case "
                         + "it's in the middle of a print."),
                UI.button("Check again", self, #selector(showReview)),
            ], tint: NSColor.systemOrange.withAlphaComponent(0.14)))
        }

        // A second run replaces the copy that is there. Promising "nothing is
        // overwritten" while doing that is the one lie this screen cannot
        // afford, so it says what it really does, and names what it is
        // replacing. Since 1.5.9 the old copy is kept until the new one has
        // passed its checks, and put back if it doesn't, and the line under
        // this one says so -- until then it was deleted before the new one
        // was checked at all.
        let replaceLine: String
        if !replacing {
            replaceLine = "A new app. Nothing is overwritten."
        } else if madeByUs && !oldVersion.isEmpty {
            replaceLine = "This replaces your copy made from HP Click \(oldVersion) with "
                + (oldVersion == newVersion ? "a new one made from the same version. "
                                            : "one made from HP Click \(newVersion). ")
                + "Your original HP Click is still untouched."
        } else if existing != nil && !madeByUs {
            replaceLine = "An app with this name is already here. It will be replaced. "
                + "Your original HP Click is still untouched."
        } else {
            replaceLine = "A copy is already here from a previous run. It will be replaced. "
                + "Your original HP Click is still untouched."
        }

        rows += [
            UI.section("WHERE THINGS GO"),
            UI.point("Reading from", ""),
            UI.text(p["source"] as? String ?? "", size: 11, mono: true),
            UI.small("Opened for reading only, not changed."),
            UI.point(replacing ? "Replacing" : "Creating", ""),
            UI.text(out, size: 11, mono: true),
            UI.small(replaceLine),
            replacing
                ? UI.small("ClickGraft keeps the copy that's there until the new one has passed "
                           + "its checks, and puts it back if it doesn't.")
                : UI.spacer(),

            // Only when the fallback is actually in play. Saying "this is just
            // for you" on a normal /Applications build would invent a
            // limitation that is not there.
            outputPerUser
                ? UI.small("This is your own Applications folder, not the one at the top "
                           + "level of the disk. ClickGraft is using it because this account "
                           + "cannot write to that one, which usually needs an administrator. "
                           + "The copy works exactly the same and appears in Launchpad and "
                           + "Spotlight — but it will be available only to you, and other "
                           + "people who sign in to this Mac will not see it.")
                : UI.spacer(),

            UI.section("THE MAIN CHANGE"),
            UI.point("Replacing the Intel engine with the Apple Silicon one.",
                     "ClickGraft downloads the official Apple Silicon engine directly from "
                     + "its makers, checks it against a published fingerprint, and puts it in "
                     + "the copy. HP's own files — layout, colour, the print engine, your "
                     + "settings — are carried across untouched."),

            // Not a count: the points come from the backend's "fixes", one per HP
            // problem this version's manifest patches, not one per file. On 4.8.x
            // five patches make four points, because constants.js and industries.js
            // repair the same HP bug; on 4.10.42 three make three, because its
            // index.html never loads constants.js and so has no such bug to fix.
            UI.section("SMALL FIXES TO THE COPY"),
        ]
        let fixes = p["fixes"] as? [String] ?? []
        for f in Wizard.fixPoints where fixes.contains(f.id) {
            rows.append(UI.point(f.lead, f.rest))
        }
        rows += [
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

        let create = UI.button("Create the copy", self, #selector(startBuild), primary: true)
        createButton = create
        updateCreateButton()
        present(rows, buttons: [UI.button("Back", self, #selector(showChoose)), UI.spacer(),
                                create])
    }

    /// Review's small fixes, in the order shown. Each appears only when the
    /// backend lists its id for this version (clickgraft/agent.py FIX_FOR_PATH),
    /// so a point can't claim a fix the copy doesn't get.
    static let fixPoints: [(id: String, lead: String, rest: String)] = [
        // Not "would quietly undo the whole thing": the copy's own signature
        // would probably make HP's installer refuse, and ClickGraft replaces
        // that installer anyway. What the lock really stops is the download and
        // the restart bar, and neither of those is quiet. See the manifest's
        // "why" for app/node/main/app-updater.js.
        ("updater",
         "Stops HP's updater downloading its Intel version over your new app.",
         "HP Click asks HP for an update each time it starts. Left alone it can download "
         + "HP's Intel build — around 570 MB — and keep offering to restart and install it. "
         + "ClickGraft stops it asking, and replaces the installer that would do the "
         + "replacing."),
        ("crash_reports",
         "Stops crash reports being sent unencrypted.",
         "HP's build uploads them over an unencrypted connection. This turns that off."),
        ("snmp_log",
         "Stops HP Click writing printer passwords into its log.",
         "If you type SNMPv3 printer passwords and press Return, HP Click 4.8 and 4.10 "
         + "write them into its log. HP stopped this in 4.11.31; the copy makes the same "
         + "change."),
        ("startup_error",
         "Fixes a bug in HP's code.",
         "Two of HP's files have a mistake that makes the app report an error every time "
         + "it starts — on Intel Macs too. ClickGraft repairs it."),
    ]

    /// "DesignJet T310 24-in, T320 24-in and T750 36-in": the brand once rather
    /// than seven times, but only when every name carries it.
    /// HP's list names every carriage width, so seven entries are five plotters:
    /// "T720 24-in, T720 36-in" and so on. Nobody checking whether their printer
    /// is in the list needs the width — they know which one is on the floor — and
    /// the long form buries the model numbers that matter.
    private func printerList(_ names: [String]) -> String {
        let brand = "HP DesignJet "
        let allBrand = names.allSatisfy { $0.hasPrefix(brand) }
        var shown: [String] = []
        for name in names {
            var model = allBrand ? String(name.dropFirst(brand.count)) : name
            // " 24-in" / " 36-in" and the occasional "-in" spelling variants.
            if let r = model.range(of: #" \d+ ?-?in(ch)?$"#, options: .regularExpression) {
                model = String(model[model.startIndex..<r.lowerBound])
            }
            if !shown.contains(model) { shown.append(model) }
        }
        let joined = shown.count > 1
            ? shown.dropLast().joined(separator: ", ") + " and " + (shown.last ?? "")
            : (shown.first ?? "")
        return allBrand ? "DesignJet " + joined : joined
    }

    @objc func togglePrinterLoss(_ b: NSButton) {
        acceptPrinterLoss = (b.state == .on)
        updateCreateButton()
    }

    private func updateCreateButton() {
        createButton?.isEnabled = !macTooOld && !replacedIsOpen && pendingLeftover == nil
            && (!replaceLosesPrinters || acceptPrinterLoss)
    }

    @objc func decidePendingLeftover() {
        guard let item = pendingLeftover else { return }
        leftovers = [item]
        afterLeftover = { [weak self] in self?.showReview() }
        showLeftover()
    }

    /// "15.0" -> "15", "15.4" -> "15.4": how people say a macOS version.
    private func macName(_ v: String) -> String {
        let parts = v.split(separator: ".")
        return parts.count == 2 && parts[1] == "0" ? String(parts[0]) : v
    }

    /// One plain sentence saying what sets the copy's minimum macOS, from the
    /// backend's phrases (clickgraft/macos_floor.py floor_reasons). Every one
    /// but "HP Click itself" is plural files, so one verb fits them all.
    private func floorReason(_ reasons: [String], needs: String) -> String {
        let parts = reasons.filter { $0 != "HP Click itself" }
        if parts.isEmpty {
            if reasons.isEmpty { return "" }
            let v = picked?["version"] as? String ?? ""
            return "That's the macOS \(v.isEmpty ? "HP Click" : "HP Click \(v)") itself asks for."
        }
        return "That's because " + parts.joined(separator: ", and ")
            + (parts.count > 1 ? "," : "") + " are built for macOS \(macName(needs)) or later."
    }

    /// HP's own Apple Silicon version, when the backend says HP lists it for
    /// this Mac (agent.native_alternative). Its printer gap is said every
    /// time: the T-series owners it would strand are the people most likely to
    /// still be making copies.
    private func nativeAlternative(_ alt: [String: Any]?) -> [NSView] {
        guard let alt = alt, let ver = alt["version"] as? String else { return [] }
        let from = macName(alt["hp_lists_from"] as? String ?? "12.0")
        // HP's list, as HP gives it: "12 to 26" on 22 Sep 2026, not "and later".
        let upTo = (alt["hp_lists_to"] as? String).map { " to " + macName($0) } ?? " and later"
        let native = candidates.first { ($0["reason"] as? String) == "hp_native" }
        let haveIt = (native?["version"] as? String) == ver
        let dropped = native?["printers_dropped"] as? [String] ?? []
        let models = dropped.isEmpty ? "DesignJet T310, T320, T350, T720 and T750"
                                     : printerList(dropped)
        var out: [NSView] = [
            UI.point("HP Click \(ver) may be the better answer.",
                     "It's HP's own Apple Silicon version, so it needs no copy, and HP lists "
                     + "it for macOS \(from)\(upTo)"
                     + (from == "12" ? ", which was checked here on macOS 12: it opens." : ".")
                     + (haveIt ? " It's already in your Applications folder." : "")
                     + " It doesn't support the \(models), so if you print to one of those, "
                     + "keep the HP Click you have."),
        ]
        if !haveIt {
            out.append(UI.button("Where to get \(ver)", self, #selector(openVersionsPage)))
        }
        return out
    }

    private func technicalPlan() -> String {
        var out = "SOURCE   \(plan["source"] as? String ?? "")\n"
        out += "OUTPUT   \(plan["output"] as? String ?? "")\n"
        if let r = plan["replacing"] as? [String: Any] {
            let v = r["version"] as? String ?? ""
            out += "REPLACES HP Click \(v.isEmpty ? "(version unreadable)" : v)"
                + ((r["made_by_clickgraft"] as? Bool ?? false) ? ", a ClickGraft copy" : "")
                + "\n"
            if let lost = r["printers_lost"] as? [String], !lost.isEmpty {
                out += "         loses: \(lost.joined(separator: ", "))\n"
            }
        }
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
        if acceptPrinterLoss { args.append("--accept-printer-loss") }
        // What Review showed at the output path. The printer-loss tick and the
        // "Replacing" line were given for that copy; the backend refuses, as
        // "replacement_changed", if something else is there by now.
        if let token = plan["replacing_token"] as? String, !token.isEmpty {
            args += ["--expect-replacing", token]
        }
        agent.stream(args) { [weak self] ev in self?.handle(ev) }
    }

    /// Try again after a failure. Straight into another build when the output
    /// path still holds what Review showed; Review again when it doesn't --
    /// typically a new copy that failed a check and stayed where there was
    /// none, which the token Review gave would now be refused for.
    @objc func tryAgain() {
        guard let src = picked?["path"] as? String,
              let fresh = agent.once(["plan", "--source", src, "--out", outputPath])?["plan"]
                as? [String: Any],
              let token = fresh["replacing_token"] as? String,
              token == plan["replacing_token"] as? String else {
            showReview()
            return
        }
        startBuild()
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
        // The copy's own LSMinimumSystemVersion, as the backend read it back.
        let needs = macName(ev["needs_macos"] as? String ?? "")
        let forMac = needs.isEmpty ? "the Mac you made it for"
                                   : "the Mac you made it for, which needs macOS \(needs) or later"

        // What became of the copy this one replaced (1.5.9). Until then this
        // said "drag it to the Trash and carry on as before" after a rebuild
        // too, when "before" -- the copy it replaced -- had just been deleted.
        let original: NSView
        switch ev["previous_copy"] as? String ?? "" {
        case "replaced":
            original = UI.point("Your original is untouched.",
                                "The copy this one replaced was removed once this one had "
                                + "passed its checks. If anything about the new copy bothers "
                                + "you, drag it to the Trash and use your original, which works "
                                + "as it always did. ClickGraft can make another copy whenever "
                                + "you like.")
        case "aside":
            original = UI.point("Your original is untouched.",
                                "The copy this one replaced couldn't be removed. It's set aside, "
                                + "hidden, in the same folder, and ClickGraft will offer to "
                                + "remove it the next time you open it.")
        default:
            original = UI.point("Your original is untouched.",
                                "If anything about the new copy bothers you, drag it to the "
                                + "Trash and carry on as before.")
        }

        present([
            UI.text("Your Apple Silicon copy is ready", size: 22, weight: .semibold,
                    color: .systemGreen),
            UI.body("\(name) is in your Applications folder, next to your original."),
            UI.body(crossBuilt
                    ? "It's built for Apple Silicon and it's signed. It has not been "
                    + "started up, because this Mac can't run it — try it on \(forMac)."
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
                original,
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
    ///
    /// 1.5.9: what each screen says about the copy being replaced comes from
    /// the backend's `previous_copy` and `new_copy`, never from assumption. Up
    /// to 1.5.8 the red screen promised "nothing about your Mac is different
    /// from a minute ago" and the orange one "drag it to the Trash and nothing
    /// about your Mac has changed", while build.py had already deleted the copy
    /// the new one replaced. Now that copy is set aside until the new one
    /// passes, and put back when it doesn't, and each screen says which of
    /// those happened.
    private func showFailed(_ ev: [String: Any]) {
        let stage = ev["stage"] as? String ?? ""
        if stage == "backend" {
            // No answer to trust (BackendTransport.swift): the build may have
            // got as far as setting the old copy aside or installing the new
            // one, or not started. Nothing here can say which, so nothing
            // here claims it, not even about the original.
            lastError = ev["error"] as? String ?? ""
            outcome = "the part of ClickGraft that does the work stopped without a confirmed "
                + "result; what it installed or set aside is unknown"
            lastResults = [:]
            present([
                UI.title("ClickGraft couldn't confirm the result"),
                UI.body("The part of ClickGraft that does the work stopped without saying how "
                        + "the build ended. So ClickGraft can't tell whether the new copy was "
                        + "put in place, or whether a copy that was already there was set "
                        + "aside."),
                UI.body("Press Check again. ClickGraft looks for anything that was set aside "
                        + "before it makes another copy."),
                Disclosure(label: "Show detail") { [weak self] in
                    guard let self = self else { return "" }
                    return self.lastError
                        + (self.logBuffer.isEmpty ? "" : "\n\n" + self.logBuffer)
                },
            ], buttons: [UI.button("Send a report", self, #selector(sendReport)), UI.spacer(),
                         UI.button("Check again", self, #selector(checkAgain), primary: true)])
            return
        }
        if stage == "replacement_changed" {
            showReplacementChanged(ev)
            return
        }
        if stage == "in_use" || stage == "printers_lost" {
            showNotReplaced(ev)
            return
        }
        if stage == "macos_too_old" {
            showTooOld(ev)
            return
        }
        if stage == "leftover_pending" {
            // Review says so first; this is for one that appeared after it
            // was drawn. Nothing was fetched or changed, so go straight to the
            // choice, and back to Review once it is made.
            leftovers = ev["leftovers"] as? [[String: Any]] ?? []
            leftoverDeferred = false
            afterLeftover = { [weak self] in self?.showReview() }
            showLeftover()
            return
        }
        let message = ev["error"] as? String ?? ""
        let previous = ev["previous_copy"] as? String ?? ""
        let newCopy = ev["new_copy"] as? String ?? ""
        let check = ev["check"] as? String ?? ""
        let checkName = Wizard.checkNames[check] ?? (check.isEmpty ? "one of the checks" : check)
        let verifyFailed = stage == "verify"
        asidePath = previous == "aside" ? (ev["previous_path"] as? String ?? "") : ""
        logPath = ev["log_path"] as? String ?? logPath
        lastError = message
        lastResults = ev["results"] as? [String: String] ?? [:]
        let checkNote = check.isEmpty ? "" : " (\(check))"
        switch (verifyFailed, previous) {
        case (true, "restored"):
            outcome = "a check did not pass\(checkNote); the previous copy was put back"
        case (true, "aside"):
            outcome = "a check did not pass\(checkNote); the previous copy could not be put "
                    + "back and is set aside"
        case (true, _):
            outcome = "the copy was made but a check did not pass\(checkNote)"
        case (false, "restored"):
            outcome = "the build did not finish; the previous copy was put back"
        case (false, "aside"):
            outcome = "the build did not finish; the previous copy could not be put back "
                    + "and is set aside"
        default:
            outcome = "the build did not finish"
        }
        let out = ev["output"] as? String ?? outputPath
        let name = (out as NSString).lastPathComponent.replacingOccurrences(of: ".app", with: "")
        let original = UI.point("Your original HP Click was not changed.",
                                "That hasn't been touched at any point.")
        let report = UI.point("Please send the report.",
                              "It says which check failed and why, which is usually enough "
                              + "to fix it. Check back here in a day or so: if a new "
                              + "ClickGraft solves it, the app will offer you the update itself.")

        var rows: [NSView]
        var buttons: [NSView] = [UI.button("Back", self, #selector(showReview))]
        if verifyFailed && previous == "restored" {
            // The new copy failed and the old one, which worked, is back.
            rows = [
                UI.text("The new copy didn't pass its checks", size: 22, weight: .semibold,
                        color: .systemOrange),
                UI.body("So your previous copy has been put back, as it was, and the new "
                        + "one has been removed. What failed: \(checkName)."),
                UI.panel([
                    UI.point("Your previous copy is back where it was.",
                             "Use it just as you did before."),
                    original,
                    report,
                ], tint: NSColor.systemOrange.withAlphaComponent(0.10)),
                UI.section("WHAT THE CHECK SAID"),
                UI.small(message),
                Disclosure(label: "Show detail") { [weak self] in self?.logBuffer ?? "" },
            ]
            buttons.append(UI.button("Try again", self, #selector(tryAgain)))
            buttons += [UI.spacer(),
                        UI.button("Send a report", self, #selector(sendReport), primary: true)]
        } else if verifyFailed && previous == "aside" {
            // It failed, and the old one could not go back: usually because the
            // new copy was opened in the seconds after the test launch. The
            // backend's own words are in the detail below, not in this sentence.
            let newIsOpen = (ev["restore_reason"] as? String) == "open"
            rows = [
                UI.text("The new copy didn't pass its checks", size: 22, weight: .semibold,
                        color: .systemOrange),
                UI.body("ClickGraft couldn't put your previous copy back in its place"
                        + (newIsOpen ? ", because the new copy is open." : ".")
                        + " Your previous copy is safe. It has been set aside, hidden, in "
                        + "the same folder. What failed: \(checkName)."),
                UI.panel([
                    UI.point("Put it back when you're ready.",
                             "Quit \(name) if it's open, then press Put it back. ClickGraft "
                             + "will also offer to do it the next time you open it."),
                    original,
                ], tint: NSColor.systemOrange.withAlphaComponent(0.10)),
                UI.section("WHAT THE CHECK SAID"),
                UI.small(message),
                Disclosure(label: "Show detail") { [weak self] in self?.logBuffer ?? "" },
            ]
            buttons.append(UI.button("Send a report", self, #selector(sendReport)))
            buttons += [UI.spacer(),
                        UI.button("Put it back", self, #selector(putBackAside), primary: true)]
        } else if verifyFailed && newCopy != "removed" && (ev["output_exists"] as? Bool ?? false) {
            // No previous copy, so the new one stays: it is all there is, and a
            // copy that failed a check has so far always still launched.
            rows = [
                UI.text("The new copy didn't pass its checks", size: 22, weight: .semibold,
                        color: .systemOrange),
                UI.body("\(name) is in your Applications folder, but ClickGraft couldn't "
                        + "confirm that it works. What failed: \(checkName)."),
                UI.panel([
                    original,
                    UI.point("The new copy may still work.", "Try opening it. If it starts "
                             + "and finds your printer, you're done."),
                    UI.point("If it doesn't,", "drag it to the Trash."),
                ], tint: NSColor.systemOrange.withAlphaComponent(0.10)),
                UI.section("WHAT THE CHECK SAID"),
                UI.small(message),
                Disclosure(label: "Show detail") { [weak self] in self?.logBuffer ?? "" },
            ]
            buttons.append(UI.button("Send a report", self, #selector(sendReport)))
            buttons.append(UI.button("Open the copy", self, #selector(revealOutput)))
            buttons += [UI.spacer(), UI.button("Try again", self, #selector(tryAgain))]
        } else {
            let previousLine: String
            switch previous {
            case "none":
                previousLine = (ev["output_exists"] as? Bool ?? false)
                    ? "" : "Nothing was put in your Applications folder."
            case "untouched":
                previousLine = "Your previous copy hasn't been touched either."
            case "restored":
                previousLine = "ClickGraft had started to put the new copy in place, so it has "
                    + "put your previous copy back, as it was."
            case "aside":
                previousLine = "Your previous copy couldn't be put back in its place, but it's "
                    + "safe: it has been set aside, hidden, in the same folder. Press Put it "
                    + "back to return it."
            default:
                previousLine = ""
            }
            rows = [
                UI.text("The copy wasn't finished", size: 22, weight: .semibold,
                        color: .systemRed),
                UI.body(message),
                UI.panel([
                    UI.point("Your original HP Click was not changed.", previousLine),
                    // "…if it keeps happening" was in this panel, and it cost us the
                    // one failure report we have. Someone in Indonesia hit an
                    // unwritable /Applications and retried NINE times before sending
                    // anything, because the screen told them retrying was the normal
                    // response. A failure that repeats is not more informative than
                    // the first one; it is the same report, later.
                    UI.point("Please send the report.",
                             "It says which version of HP Click you have and where the "
                             + "build stopped. That is usually enough to fix it — the "
                             + "last report like this turned into a fix the same day. "
                             + "Check back here in a day or so: if a new ClickGraft "
                             + "solves it, the app will offer you the update itself."),
                ]),
                Disclosure(label: "Show detail") { [weak self] in self?.logBuffer ?? "" },
            ]
            // On a hard failure the report is the primary action, not "Try
            // again". Retrying an unwritable folder or an unreadable bundle
            // produces the same failure with no new information, and the button
            // that looks like the answer is the one people press.
            buttons.append(UI.button("Try again", self, #selector(tryAgain)))
            buttons.append(UI.spacer())
            if previous == "aside" {
                buttons.append(UI.button("Put it back", self, #selector(putBackAside)))
            }
            buttons.append(UI.button("Send a report", self, #selector(sendReport), primary: true))
        }
        present(rows, buttons: buttons)
    }

    /// verify's check names (clickgraft/verify.py VerifyError.check), as a
    /// person would say them. The key itself goes into the report.
    static let checkNames: [String: String] = [
        "bundle": "the first look at the new copy",
        "architectures": "the check that it's built for Apple Silicon",
        "bundle_audit": "the check of every file in it",
        "flat_symbols": "the check for anything HP's code needs that's missing",
        "minimum_macos": "the check of which macOS it needs",
        "code_signature": "the check of its signature",
        "asar_integrity": "the check of HP's app files inside it",
        "update_locks": "the check that HP's updater can't replace it",
        "patch_outcomes": "the check of the small fixes",
        "smoke_launch": "the test launch",
        "resealed": "signing it again after the test launch",
        "launcher": "the check that it loads its support files",
        "verification": "the overall result of the checks",
    ]

    @objc func putBackAside() {
        guard !asidePath.isEmpty else { return }
        putBack(asidePath) { [weak self] in
            self?.asidePath = ""
            self?.showReview()
        }
    }

    /// This Mac's macOS is older than the copy needs (1.5.9). Not the red
    /// screen and no report offer: nothing went wrong, and a report can't
    /// change which macOS the files inside HP Click are built for. Usually
    /// refused before any download; `after_build` is the rarer case where the
    /// engine's own files set the minimum, known only once the copy is made.
    private func showTooOld(_ ev: [String: Any]) {
        let needs = ev["needs"] as? String ?? ""
        let thisMac = ev["this_mac"] as? String ?? "?"
        let afterBuild = ev["after_build"] as? Bool ?? false
        let previous = ev["previous_copy"] as? String ?? ""
        logPath = ev["log_path"] as? String ?? logPath
        lastError = ev["error"] as? String ?? ""
        outcome = afterBuild
            ? "the copy was made, needed a newer macOS than this Mac's, and was thrown away"
            : "the build did not start: this Mac's macOS is older than the copy needs"

        var unchanged: [NSView] = [
            UI.point("Your HP Click is unchanged.", "It works as it did."),
        ]
        if previous == "untouched" {
            unchanged.append(UI.point("Your previous copy hasn't been touched either.", ""))
        }
        unchanged.append(UI.point("Once this Mac is on macOS \(macName(needs)) or later,",
                                  "ClickGraft can make the copy."))
        var rows: [NSView] = [
            UI.title("This copy needs macOS \(macName(needs)) or later"),
            UI.body(afterBuild
                ? "This Mac has macOS \(thisMac). ClickGraft could only tell once the copy "
                  + "was made, so it has thrown that copy away. Nothing here was replaced."
                : "This Mac has macOS \(thisMac), so ClickGraft hasn't made the copy. "
                  + "Nothing has been downloaded or changed."),
        ]
        let reason = floorReason(ev["reasons"] as? [String] ?? [], needs: needs)
        if !reason.isEmpty { rows.append(UI.body(reason)) }
        rows.append(UI.panel(unchanged, tint: NSColor.systemOrange.withAlphaComponent(0.12)))
        let alt = nativeAlternative(ev["alternative"] as? [String: Any])
        if !alt.isEmpty {
            rows.append(UI.panel(alt, tint: NSColor.systemBlue.withAlphaComponent(0.10)))
        }
        present(rows, buttons: [UI.button("Back", self, #selector(showReview)), UI.spacer(),
                                UI.button("Quit", self, #selector(quit), primary: true)])
    }

    /// The copy at the output path isn't the one Review showed (agent.py,
    /// "replacement_changed"): it has gone, one has appeared, or it was
    /// replaced or edited -- since Review, or while the copy was being made.
    /// Something outside ClickGraft did that, so, like the screen below, not
    /// red and no report as the button to press: the answer is a fresh
    /// Review. Until the 22 Sep 2026 review this came as a failed build, "The
    /// copy wasn't finished", with Send a report as the main button.
    private func showReplacementChanged(_ ev: [String: Any]) {
        let out = ev["output"] as? String ?? outputPath
        let name = (out as NSString).lastPathComponent.replacingOccurrences(of: ".app", with: "")
        let change = ev["change"] as? String ?? "changed"
        let duringBuild = ev["during_build"] as? Bool ?? false
        let gone = (ev["previous_copy"] as? String) == "gone"
        logPath = ev["log_path"] as? String ?? logPath
        lastError = ev["error"] as? String ?? ""
        lastResults = [:]
        let when = duringBuild ? "during the build" : "after Review"
        switch change {
        case "removed":
            outcome = "the copy it would replace was removed \(when); nothing was put in its place"
        case "appeared":
            outcome = "a copy appeared at the output path \(when); it was left alone"
        default:
            outcome = "the copy it would replace changed \(when); it was left alone"
        }
        if duringBuild { outcome += ", and the new copy was thrown away" }

        let title: String
        let body: String
        let notShown = "ClickGraft won't replace a copy it hasn't shown you, so it has left it alone"
        switch (change, duringBuild) {
        case ("removed", false):
            title = "\(name) has gone"
            body = "\(name) was in your Applications folder when ClickGraft showed you what it "
                + "would do, and it has gone since. ClickGraft hasn't made the copy. Nothing "
                + "has been downloaded or changed."
        case ("removed", true):
            title = "\(name) has gone"
            body = "\(name) was removed from your Applications folder while the new copy was "
                + "being made. ClickGraft only does what it showed you, so it hasn't put the "
                + "new copy there: it has thrown it away."
        case ("appeared", false):
            title = "\(name) is there now"
            body = "There was no \(name) in your Applications folder when ClickGraft showed you "
                + "what it would do, and there is one now. \(notShown). Nothing has been "
                + "downloaded or changed."
        case ("appeared", true):
            title = "\(name) is there now"
            body = "\(name) appeared in your Applications folder while the new copy was being "
                + "made. \(notShown), and has thrown the new copy away."
        case (_, false):
            title = "\(name) has changed"
            body = "The \(name) in your Applications folder isn't the one ClickGraft showed "
                + "you: it has been replaced or changed since. \(notShown). Nothing has been "
                + "downloaded or changed."
        default:
            title = "\(name) has changed"
            body = "\(name) was replaced or changed while the new copy was being made, so it "
                + "isn't the one ClickGraft showed you. \(notShown), and has thrown the new "
                + "copy away."
        }

        var points: [NSView] = [
            UI.point("Press Check again.", "ClickGraft shows you what's there now, and "
                     + "changes nothing until you press Create the copy."),
        ]
        if !gone {
            points.append(UI.point("The \(name) there now hasn't been touched.", ""))
        }
        points.append(UI.point("Your original HP Click was not changed.", ""))
        present([
            UI.title(title),
            UI.body(body),
            UI.panel(points, tint: NSColor.systemOrange.withAlphaComponent(0.12)),
        ], buttons: [UI.button("Send a report", self, #selector(sendReport)), UI.spacer(),
                     UI.button("Check again", self, #selector(showReview), primary: true)])
    }

    /// The backend refused because of the copy it would replace: it is open, or
    /// it supports printers the new copy won't and nobody ticked the box. Review
    /// checks both first, so this is what happens when things change after that
    /// screen was drawn. Not the red failure screen, and no report offer:
    /// nothing went wrong, and the person can put it right themselves.
    ///
    /// `during_build` (1.5.8) is the copy being opened during the build rather
    /// than before it: build.py checks again in its last step, just before it
    /// would set the old bundle aside (1.5.9; up to 1.5.8, delete it). Then the
    /// download did happen and the new copy was built and discarded, so this
    /// screen must not say otherwise.
    private func showNotReplaced(_ ev: [String: Any]) {
        let out = ev["output"] as? String ?? outputPath
        let name = (out as NSString).lastPathComponent.replacingOccurrences(of: ".app", with: "")
        lastError = ev["error"] as? String ?? ""
        let open = (ev["stage"] as? String ?? "") == "in_use"
        let duringBuild = ev["during_build"] as? Bool ?? false
        outcome = open ? "the build did not start: the copy it would replace is open"
                       : "the build did not start: replacing the copy would lose printers"
        if open && duringBuild { outcome = "the copy it would replace was open, so it was left alone" }
        let lost = ev["printers_lost"] as? [String] ?? []

        let rows: [NSView] = [
            UI.title(open ? "Quit \(name) first" : "Check the printers first"),
            UI.body(open
                    ? "\(name) is open, so ClickGraft hasn't replaced it. "
                    + (duringBuild
                       ? "It was opened while the new copy was being made, so that new copy "
                       + "was thrown away rather than put in its place. Nothing here changed."
                       : "Nothing has been downloaded or changed.")
                    : "The copy that is already here supports the " + printerList(lost)
                    + ", and the new one won't. ClickGraft hasn't replaced it. Nothing has "
                    + "been downloaded or changed."),
            UI.panel([
                open
                    ? UI.point("Quit \(name), then press Try again.",
                               "ClickGraft won't quit it for you, in case it's in the middle "
                               + "of a print.")
                    : UI.point("Go back to see what would change.",
                               "If you don't print to any of them, you can tick the box there "
                               + "and replace it anyway."),
                UI.point("Your original HP Click was not changed.", ""),
            ], tint: NSColor.systemOrange.withAlphaComponent(0.12)),
        ]
        present(rows, buttons: open
                ? [UI.button("Back", self, #selector(showReview)), UI.spacer(),
                   UI.button("Try again", self, #selector(tryAgain), primary: true)]
                : [UI.spacer(), UI.button("Back", self, #selector(showReview), primary: true)])
    }

    // MARK: - Reporting a problem

    static let reportURL = "https://clickgraft.elusive.net/report"
    static let appcastURL = "https://clickgraft.elusive.net/appcast.json"
    static let versionsURL = "https://clickgraft.elusive.net/#versions"

    /// Everything the report will contain, assembled so it can be SHOWN to the
    /// user before it goes anywhere. Nothing is sent that they have not read.
    private func reportBody(note: String = "", printer: String = "",
                            contact: String = "", kind: String = "problem") -> String {
        var out = reportHeader(kind: kind)
        out += "outcome: \(outcome)\n"
        out += "source: \((picked?["path"] as? String).map(scrub) ?? "none")\n"
        out += "version: \(picked?["version"] as? String ?? "?")\n\n"
        out += contactLines(contact, wants: "to be told when this is fixed")

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

    /// The lines every report starts with, whatever kind it is. "kind:" must stay
    /// first: the collector files a report by the start of its body.
    ///
    /// Shared because the unsupported-version report used to be the bare probe
    /// output, with no ClickGraft or macOS version at all. The first one ever
    /// received (4.7.28, 14 Sep 2026) could only be placed by reading the access
    /// log for the user agent that sent it.
    private func reportHeader(kind: String) -> String {
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
        if Toolchain.state == .commandLineTools {
            out += "developer tools: Command Line Tools (Xcode licence not accepted)\n"
        }
        // Which interpreter ran the build. Since 1.8.0 there are four
        // possibilities and they behave differently -- a fetched one is a
        // different 3.13.9 from whatever Apple's tools provide -- so a report
        // that does not say which is missing the first thing to ask about.
        out += "interpreter: \(Toolchain.source.name)\n"
        return out
    }

    /// Only if they typed one. Everything else in a report is scrubbed of
    /// anything identifying; this is the one field that is personal by
    /// definition, so it exists only when someone has deliberately filled it
    /// in, and it is visible in the preview they approve before sending.
    ///
    /// NOT scrubbed: scrub() strips /Users/<name>, and an address like
    /// name@users.example would be mangled by a careless pattern. It is
    /// checked for a newline instead, so it cannot forge extra report fields.
    private func contactLines(_ contact: String, wants: String) -> String {
        guard !contact.isEmpty else { return "" }
        return "contact: \(contact.replacingOccurrences(of: "\n", with: " "))\n"
             + "  (they asked \(wants))\n\n"
    }

    /// The optional address field, identical wherever a report is written, so
    /// the promise attached to it cannot drift between two copies.
    private func contactField(label: String, y: CGFloat) -> (NSTextField, NSTextField) {
        let l = NSTextField(labelWithString: label)
        l.frame = NSRect(x: 0, y: y + 26, width: 460, height: 18)
        l.font = .systemFont(ofSize: 11)
        l.textColor = .secondaryLabelColor
        let f = NSTextField(frame: NSRect(x: 0, y: y, width: 460, height: 22))
        f.placeholderString = "you@example.com \u{2014} or leave it blank"
        f.toolTip = "Used only to reply about this report: to ask a question, "
            + "or to tell you when it is fixed. Never added to a mailing list, "
            + "never used for anything else, never given to anyone."
        return (l, f)
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
            + "sends that in unprompted. This is what would be sent \u{2014} you can read all "
            + "of it first, and it goes nowhere unless you press Send.\n\nNo account, "
            + "no identifier, and no way to link this to anything else you send. The only "
            + "thing recorded about where you are is the two-letter country code "
            + "Cloudflare puts on the request."
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
        let wrap = NSView(frame: NSRect(x: 0, y: 0, width: 460, height: 156))
        let note = NSTextField(frame: NSRect(x: 0, y: 88, width: 460, height: 62))
        note.placeholderString = "What happened?"
        note.usesSingleLineMode = false
        note.cell?.wraps = true
        note.cell?.isScrollable = false

        // Off unless they turn it on. Model and firmware are the two things
        // that make a printing report actionable, but they are the user's
        // equipment, so they get asked rather than told.
        let inclPrinter = NSButton(checkboxWithTitle:
            "Include my printer's model and firmware", target: nil, action: nil)
        inclPrinter.frame = NSRect(x: 0, y: 58, width: 460, height: 22)
        inclPrinter.state = .off
        inclPrinter.toolTip = "Model, firmware version and paper sizes. Never the "
            + "printer's name, address, serial number or any password."
        // Optional, and the only field in this report that is personal by
        // definition. Without it a report is a dead end in one direction: the
        // Indonesian failure told us exactly what was wrong, it was fixed the
        // same day, and there was no way to tell them. Blank is a perfectly
        // good answer and the label says so before it says anything else.
        let (contactLabel, contact) = contactField(
            label: "Optional \u{2014} where to reach you, if you would like an answer:", y: 4)
        wrap.addSubview(note)
        wrap.addSubview(inclPrinter)
        wrap.addSubview(contactLabel)
        wrap.addSubview(contact)
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
        let contactValue = contact.stringValue
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let body = reportBody(note: note.stringValue, printer: printer,
                              contact: contactValue)

        let a = NSAlert()
        a.messageText = "Send this to the ClickGraft developers?"
        // The old wording promised "no personal information" flatly. With a
        // contact field that would be false the moment someone uses it, and a
        // privacy promise that is false in one case is worth less than none.
        a.informativeText = "This is everything that will be sent. Nothing else leaves "
            + "your Mac — no file names from your work, no printer details, and nothing "
            + "identifying that you did not type yourself. Your home folder name has "
            + "been removed. Read it first; if anything in it bothers you, don't send it."
            + (contactValue.isEmpty ? ""
               : "\n\nYour address is in there because you entered it. It will be used "
               + "to reply about this report and for nothing else — no mailing list, no "
               + "newsletter, and it is not passed to anyone.")
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
                    // Since 1.5.0 a report can carry an address, and telling the
                    // person who just typed one that there is nothing to follow up
                    // on contradicts the field they filled in.
                    d.informativeText = body.contains("\ncontact: ")
                        ? "Thank you. You'll hear back at the address you gave."
                        : "Thank you. There's nothing to follow up on — if you want a "
                          + "reply, open an issue on GitHub as well."
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
        /// The zip itself. The appcast has published this all along and the app
        /// only ever opened `url`, so "Get the update" landed people on the
        /// front page with the download still to find. Empty when an older
        /// server does not send it, and `openDownloadPage` falls back to `url`.
        let download: String
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
                            download: o["download"] as? String ?? "",
                            importance: importance,
                            summary: summary))
            }
        }.resume()
    }

    /// Prefers the zip over the landing page. The banner has already said what
    /// the update is and why it matters, so the next thing wanted is the file,
    /// not a page to read and then find a button on. Falls back to the page when
    /// the appcast carries no download, and to the site when there is no appcast
    /// at all -- a broken update button is worse than a slow one.
    /// The site's version table: both of HP's download locations for each
    /// supported build, and which printers each one drops. Not HP's DMG
    /// directly -- a 571 MB download that starts on a click, with no word about
    /// the choice between versions, is the wrong first thing to hand someone.
    /// The measured version table, in the app rather than on the website.
    ///
    /// Everything a user needs to KNOW -- which version their printer needs, what
    /// macOS each one wants, where to get one HP will not serve -- used to live only
    /// on the site. Somebody who already installed the app does not go back to a
    /// website, so it went unread by exactly the people it was written for.
    ///
    /// Read from the same measurements as the site and the recommendation, so the
    /// three cannot disagree. Monospaced because it is a table and alignment is the
    /// only thing making it readable; UI.text already does mono, so nothing new.
    @objc func showVersionReference() {
        guard let caps = capabilityOverview,
              let table = caps["table"] as? [[String: Any]] else {
            showChoose()
            return
        }
        let mine = caps["your_printers"] as? [String] ?? []
        let macos = caps["your_macos"] as? String ?? ""
        let pick = caps["recommended"] as? String ?? ""
        let unlisted = caps["unlisted"] as? [String] ?? []

        var rows: [NSView] = [UI.title("Which HP Click you should run")]

        // What the answer was worked out from, so it can be argued with rather
        // than taken on trust.
        var about = macos.isEmpty ? "" : "This Mac: macOS \(macos)."
        if !mine.isEmpty {
            about += (about.isEmpty ? "" : "  ") + "Your printer: " + printerList(mine) + "."
        }
        if !about.isEmpty { rows.append(UI.subtitle(about)) }

        if !pick.isEmpty {
            var says: [NSView] = [UI.point("Run HP Click \(pick).",
                                           (caps["why"] as? String) ?? "")]
            if (caps["needs_graft"] as? Bool) == true {
                says.append(UI.small("ClickGraft makes the Apple Silicon copy of it. "
                                     + "Choose it on the previous screen once it is installed."))
            }
            if !unlisted.isEmpty {
                says.append(UI.small("It does not list " + printerList(unlisted)
                                     + ", and no released HP Click does."))
            }
            says.append(UI.button("Get \(pick) from HP", self,
                                  #selector(openRecommendedDownload)))
            rows.append(UI.panel(says, tint: NSColor.systemBlue.withAlphaComponent(0.10)))
        }

        // Padded by hand, not with a format width: %@ silently ignores field
        // widths in CFString formatting, so "%-9@" compiles, runs, and produces a
        // table with every column jammed against the next. Measured, not assumed.
        func pad(_ text: String, _ n: Int) -> String {
            text.count >= n ? text + " "
                            : text + String(repeating: " ", count: n - text.count)
        }
        func line(_ mark: String, _ v: String, _ floor: String,
                  _ printers: String, _ story: String) -> String {
            mark + pad(v, 9) + pad(floor, 12) + pad(printers, 9) + story
        }
        var lines = [line("  ", "version", "needs macOS", "printers", "Apple Silicon")]
        for row in table {
            let v = row["version"] as? String ?? "?"
            let native = (row["hp_native"] as? Bool) == true
            let graft = row["graftable"] as? Bool
            let story = native ? "HP builds it"
                      : graft == true ? "ClickGraft can copy it"
                      : graft == false ? "cannot be copied" : "unknown"
            lines.append(line(v == pick ? "\u{2192} " : "  ", v,
                              row["declared_floor"] as? String ?? "?",
                              String(describing: row["printers"] ?? "?"), story))
        }
        rows.append(UI.text(lines.joined(separator: "\n"), size: 11.5, mono: true))
        rows.append(UI.small("\u{201C}Needs macOS\u{201D} is the minimum each build declares, "
                             + "which is what macOS enforces when you open it. No build below "
                             + "macOS 12 has been started on a Mac that old by this project, so "
                             + "those rows are read from the build and not tested."))

        present(rows, buttons: [UI.button("Back", self, #selector(showChoose)), UI.spacer(),
                                UI.button("Done", self, #selector(showChoose), primary: true)])
    }

    /// Hand over the download rather than the website. The .zip is the application
    /// itself and is the only form some of these were ever published in -- 4.8.118
    /// was never a .dmg, and 4.10.42's was removed from HP's page.
    @objc func openRecommendedDownload() {
        guard let caps = capabilityOverview,
              let pick = caps["recommended"] as? String,
              let table = caps["table"] as? [[String: Any]],
              let row = table.first(where: { ($0["version"] as? String) == pick }),
              let dl = row["download"] as? [String: Any],
              let zip = dl["zip"] as? String, let u = URL(string: zip) else { return }
        NSWorkspace.shared.open(u)
    }

    @objc func openVersionsPage() {
        if let u = URL(string: Wizard.versionsURL) { NSWorkspace.shared.open(u) }
    }

    @objc func openDownloadPage() {
        let target = !updateDownloadURL.isEmpty ? updateDownloadURL
                   : !updateURL.isEmpty         ? updateURL
                   : "https://clickgraft.elusive.net/"
        if let u = URL(string: target) { NSWorkspace.shared.open(u) }
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

// The one thing ClickGraft does without opening a window.
//
// It is here because the wizard is otherwise the only way to reach the code that
// downloads and unpacks an interpreter, which makes the part of ClickGraft that
// fetches something from the internet the only part no test can drive.
// tests/test_python_payload.py drives this.
//
// It also answers the managed estate the Requirements screen talks through: one
// command per Mac, no window, no administrator password, and nothing left to
// download by the time anyone opens the app.
//
//   ClickGraft.app/Contents/MacOS/ClickGraft --fetch-python [--force]
if CommandLine.arguments.contains("--fetch-python") {
    func say(_ line: String) { print(line); fflush(stdout) }
    func fail(_ line: String) {
        FileHandle.standardError.write(Data((line + "\n").utf8))
    }

    if !CommandLine.arguments.contains("--force"), let have = Toolchain.source.path {
        say("nothing to fetch: \(Toolchain.source.name) (\(have))")
        exit(0)
    }
    guard PythonPayload.pin != nil else {
        fail("this build names no interpreter to fetch (no python-pin.json)")
        exit(2)
    }

    var code: Int32 = 1
    var step = ""
    PythonPayload.install(progress: { f in
        let now = f < 0.7 ? "fetching" : f < 0.75 ? "checking what arrived"
                : f < 0.9 ? "unpacking" : "checking the signature"
        if now != step { step = now; say(now) }
    }, done: { result in
        switch result {
        case .success(let exe):
            say("ok: \(exe)")
            code = 0
        case .failure(let why):
            fail(why.text)
            if let detail = why.detail { fail(detail) }
            code = 1
        }
        CFRunLoopStop(CFRunLoopGetMain())
    })
    // install() answers on the main queue, which is only drained while this runs.
    CFRunLoopRun()
    exit(code)
}

let app = NSApplication.shared
let delegate = Wizard()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
