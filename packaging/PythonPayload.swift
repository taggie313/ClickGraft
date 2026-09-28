// ClickGraft — the interpreter it fetches when a Mac has none of its own.
//
// Split out of ClickGraft.swift for 1.8.2 so the installer can be compiled into
// a test harness as well as the app, and its cancellation behaviour tested
// deterministically rather than asserted against source text. The review that
// found F1 had to build exactly such a harness to reproduce it; the tests that
// close it should not have to rebuild that scaffolding each time.
//
// Foundation and CryptoKit only: nothing here touches AppKit, and keeping it
// that way is what lets a command-line harness link it.
//
// Compiled into the app by packaging/build_app.sh alongside ClickGraft.swift
// and BackendTransport.swift. It is a recorded source like any other
// packaging/*.swift, so the release tag fixes it.

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

    /// One attempt to install an interpreter, and everything that belongs to it.
    ///
    /// Cancellation used to be a process-global Boolean that `install()` reset.
    /// A cancelled attempt could still be inside `ditto` or `codesign` on its
    /// delegate queue; pressing Fetch again set that shared flag back to false,
    /// and the cancelled attempt's next check saw the NEW attempt's value and
    /// carried on to install and write `.pinned`. A review reproduced it by
    /// compiling this installer into a harness:
    ///
    ///     cancel-only: ORIGINAL: cancelled          MARKER: false
    ///     retry:       ORIGINAL: installed after cancel   MARKER: true
    ///
    /// So cancellation belongs to the attempt. The flag is one-way -- nothing
    /// un-cancels an Operation -- and a new attempt cannot reach an older one's
    /// state at all.
    final class Operation {
        /// Distinguishes attempts at callback-delivery time, so a late
        /// completion from a replaced attempt cannot clear the current one.
        let id: UInt64
        private let gate = NSLock()
        private var cancelledFlag = false
        private var committedFlag = false

        init(id: UInt64) { self.id = id }

        var isCancelled: Bool {
            gate.lock(); defer { gate.unlock() }
            return cancelledFlag
        }

        /// One-way. Refused once this attempt has been admitted to commit,
        /// because past that point the destination is being replaced and
        /// "cancelled" would be a lie.
        @discardableResult
        func cancel() -> Bool {
            gate.lock(); defer { gate.unlock() }
            if committedFlag { return false }
            cancelledFlag = true
            return true
        }

        /// The same lock decides cancel and commit, so exactly one wins.
        /// Returns false when this attempt was cancelled first.
        func admitCommit() -> Bool {
            gate.lock(); defer { gate.unlock() }
            if cancelledFlag { return false }
            committedFlag = true
            return true
        }

        var hasCommitted: Bool {
            gate.lock(); defer { gate.unlock() }
            return committedFlag
        }
    }

    private static var running: Fetcher?
    private static var lastOperationID: UInt64 = 0

    /// The attempt the coordinator currently owns, or nil. Main queue only.
    static var currentOperationID: UInt64? { running?.operation.id }

    /// Whether a cancelled attempt is still cleaning up. Retry stays unavailable
    /// until it finishes: the alternative is two workers contending for the
    /// install lock while the UI claims the first one stopped.
    private(set) static var isStopping = false

    /// Fetches, verifies and installs. Both closures are called on the main
    /// queue; `progress` runs 0...1. A second call while one is in flight, or
    /// while a cancelled one is still cleaning up, is ignored -- so a
    /// double-click cannot start two, and a retry cannot revive a cancelled
    /// attempt by resetting shared state.
    @discardableResult
    static func install(progress: @escaping (Double) -> Void,
                        done: @escaping (Result<String, Failure>) -> Void) -> UInt64? {
        guard let pin = pin else {
            DispatchQueue.main.async { done(.failure(.noPin)) }
            return nil
        }
        guard running == nil, !isStopping else { return nil }
        lastOperationID += 1
        let operation = Operation(id: lastOperationID)
        let f = Fetcher(pin: pin, operation: operation, progress: progress, done: done)
        running = f
        f.start()
        return operation.id
    }

    /// Stop the attempt in flight.
    ///
    /// Returns false when the attempt has already been admitted to commit, in
    /// which case it is finishing rather than stopping and the UI must say so
    /// instead of falsely acknowledging a cancellation.
    ///
    /// `running` is NOT cleared here. The attempt still owns its staging
    /// directory and install lock, and clearing it is what let a retry start a
    /// second worker against the first one's files. Ownership is released by
    /// the worker, after cleanup, through finishedStopping().
    @discardableResult
    static func cancel() -> Bool {
        guard let f = running else { return false }
        let accepted = f.operation.cancel()
        if accepted {
            isStopping = true
            f.stopTransfer()
        }
        return accepted
    }

    /// Whether a callback from `operation` should still be acted on.
    ///
    /// Main queue only. An attempt that has been replaced, or whose result has
    /// already been delivered, must not move the current attempt's progress bar
    /// or navigate its screen.
    fileprivate static func deliver(_ operation: Operation) -> Bool {
        if let current = running?.operation.id { return current == operation.id }
        // No current attempt: only the one just released may report a terminal
        // result, which is how a cancelled worker says its cleanup is done.
        return operation.id == lastOperationID
    }

    /// Called by the worker once it has cleaned up and released its lock.
    /// Only the attempt that owns the coordinator may release it.
    fileprivate static func finishedStopping(_ operation: Operation) {
        guard running?.operation.id == operation.id else { return }
        running = nil
        isStopping = false
    }

    /// Everything between "the bytes arrived" and "there is an interpreter".
    /// Runs off the main queue, throws Failure.
    static func accept(_ zip: URL, pin: Pin, operation: Operation,
                       progress: (Double) -> Void) throws -> String {
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
        // Asked of THIS attempt, never of a shared flag a later attempt can
        // reset. The defer clears staging either way.
        if operation.isCancelled { throw Failure.cancelled }
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

        // The marker is prepared in the VERIFIED STAGING TREE, before commit is
        // admitted. A marker inside staging is never a usable installation: it
        // becomes one only when staging becomes the destination, which means
        // there is no ordering where a half-published directory looks trusted.
        do {
            try pin.sha256.write(toFile: staging + "/.pinned",
                                 atomically: true, encoding: .utf8)
        } catch {
            throw Failure.broken(error.localizedDescription)
        }

        // The barrier. cancel() and admitCommit() take the same lock, so
        // exactly one wins. Cancelled first: nothing is moved, no marker
        // becomes live, and the attempt throws. Committed first: cancel()
        // answers false and the UI reports finishing rather than pretending it
        // stopped something it did not.
        guard operation.admitCommit() else { throw Failure.cancelled }

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
        // The marker travelled with staging, so there is nothing left to write:
        // it went live in the same rename that published the interpreter.
        guard fm.fileExists(atPath: dir + "/.pinned") else {
            throw Failure.broken("the installed interpreter carries no marker")
        }
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
    fileprivate final class Fetcher: NSObject, URLSessionDownloadDelegate {
        private let pin: Pin
        /// The attempt this worker belongs to. Everything it decides is asked
        /// of this object, never of shared state a later attempt can reach.
        let operation: Operation
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

        init(pin: Pin, operation: Operation, progress: @escaping (Double) -> Void,
             done: @escaping (Result<String, Failure>) -> Void) {
            self.pin = pin
            self.operation = operation
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

        /// Stop the transfer. NOT the whole attempt: the worker may be inside
        /// ditto or codesign, and it still owns its staging directory and the
        /// install lock. It releases the coordinator itself, after cleanup.
        ///
        /// `settled` is not set here. Doing that made the terminal result
        /// vanish, so nothing ever reported that cleanup had finished and the
        /// UI had to guess when a retry was safe.
        func stopTransfer() {
            task?.cancel()
        }

        private func settle(_ r: Result<String, Failure>) {
            guard claimSettle() else { return }
            session.finishTasksAndInvalidate()
            DispatchQueue.main.async {
                // Identity, not assumption. An old worker finishing late must
                // not clear a newer attempt's ownership -- `running = nil` used
                // to be unconditional, so a stale completion could hand the
                // coordinator away from the attempt that actually held it.
                PythonPayload.finishedStopping(self.operation)
                guard PythonPayload.deliver(self.operation) else { return }
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
            DispatchQueue.main.async {
                // Stale progress from a replaced attempt would drive the bar of
                // the one that replaced it.
                guard PythonPayload.deliver(self.operation) else { return }
                self.progress(f * 0.7)
            }
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
                let exe = try PythonPayload.accept(location, pin: pin,
                                                   operation: operation) { f in
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
