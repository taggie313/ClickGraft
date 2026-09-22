// How the wizard hears from the part of ClickGraft that does the work: one
// run of `python3 -m clickgraft.cli agent ...`, read as one JSON object per
// line on stdout.
//
// A file of its own because 1.5.9 got this wrong twice, and both ways left the
// wizard waiting for ever with no button to press:
//
// - It read stdout with a readabilityHandler and removed that handler in the
//   process's terminationHandler. Exit and the last bytes in the pipe race,
//   and the exit sometimes won, taking the final "done" or "error" with it.
//   Measured on 1.5.9 in the 22 Sep 2026 review: lost in 3 of 2,000 idle runs
//   and 5 of 2,000 with the CPU oversubscribed, and in 200 of 200 runs whose
//   final line was 300 KB. The wizard then sat on "Checking the result".
// - stderr was a Pipe nobody read. A pipe holds 64 KB; past that the backend
//   blocks on its next write and never exits. 250 KB of warnings hung a
//   one-shot request, and with it the main thread that was waiting on it.
//
// So a run here ends only once the process has exited AND both pipes have
// reached end-of-file, both pipes are always drained, and every run delivers
// exactly one final event: the backend's own "done" or "error", or an error
// with stage "backend" saying why there is no answer to trust. The wizard's
// "ClickGraft couldn't confirm the result" screen is that last case. Its
// wording is user-facing (docs/wizard-copy.md): the reason goes on screen and
// into reports, so it never says "backend".
import Foundation

/// Own one backend run through process exit AND EOF on both pipes. All state
/// lives on one queue; UI events are delivered in order on `deliveryQueue`.
/// The exit callback never removes a reader before its final bytes arrive.
final class BackendStream {
    private let process: Process
    private let singleReply: Bool
    private let deliveryQueue: DispatchQueue
    private let onEvent: ([String: Any]) -> Void
    private let queue = DispatchQueue(label: "ClickGraft.backend-stream")
    private let stdout = Pipe()
    private let stderr = Pipe()
    private var readers: [DispatchSourceRead] = []
    private var buffer = Data()
    private var diagnostic = Data()
    private var ended = 0
    private var exited = false
    private var finished = false
    private var terminal: [String: Any]?
    private var protocolProblem = false
    /// The longest line accepted. The backend's longest real lines are about
    /// 3 KB (22 Sep 2026: `env` 2,889 bytes, `plan` 3,021, `probe` 1,230, from
    /// a stock 4.10.42); this is only a bound, so a runaway line without a
    /// newline cannot grow in memory until the wizard is killed. Past it the
    /// run's answer is void, as for any line that isn't a JSON object.
    private let lineLimit = 1024 * 1024
    /// How much of stderr is kept for the screen and the report: the end,
    /// because a Python traceback is printed last. 16 KB holds several whole
    /// tracebacks and still reads in the report's preview; the rest is read
    /// and dropped, so the backend can never block writing it.
    private let diagnosticLimit = 16 * 1024
    /// How long to wait for EOF once the process has exited. Something it
    /// started can inherit a pipe and hold it open indefinitely, and waiting
    /// for that would hang the wizard as surely as 1.5.9 did. Measured 22 Sep
    /// 2026 with a stand-in backend: the last EOF came before the exit in
    /// 1,000 of 1,000 idle runs, and at most 0.42 ms after it in 1,000 runs
    /// with the CPU oversubscribed twice over. 2 s is thousands of times that,
    /// and short enough that nobody is left looking at a stalled screen.
    private let eofGrace: TimeInterval = 2

    init(process: Process, singleReply: Bool = false, deliveryQueue: DispatchQueue = .main,
         onEvent: @escaping ([String: Any]) -> Void) {
        self.singleReply = singleReply
        self.deliveryQueue = deliveryQueue
        self.process = process
        self.onEvent = onEvent
    }

    func start() {
        queue.async { self.launch() }
    }

    private func launch() {
        process.standardOutput = stdout
        process.standardError = stderr
        process.terminationHandler = { _ in
            self.queue.async {
                self.exited = true
                self.completeIfReady()
                self.queue.asyncAfter(deadline: .now() + self.eofGrace) {
                    if !self.finished {
                        self.finish(error: "The part of ClickGraft that does the work stopped, "
                            + "but something it started was still connected to it "
                            + "\(Int(self.eofGrace)) seconds later, so ClickGraft can't be "
                            + "sure it heard the whole answer.")
                    }
                }
            }
        }
        do {
            try process.run()
        } catch {
            stdout.fileHandleForWriting.closeFile()
            stderr.fileHandleForWriting.closeFile()
            stdout.fileHandleForReading.closeFile()
            stderr.fileHandleForReading.closeFile()
            finish(error: "The part of ClickGraft that does the work couldn't be started: "
                + error.localizedDescription)
            return
        }
        stdout.fileHandleForWriting.closeFile()
        stderr.fileHandleForWriting.closeFile()
        watch(stdout.fileHandleForReading, isOutput: true)
        watch(stderr.fileHandleForReading, isOutput: false)
    }

    private func watch(_ handle: FileHandle, isOutput: Bool) {
        let fd = handle.fileDescriptor
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
        source.setCancelHandler { handle.closeFile() }
        source.setEventHandler {
            var bytes = [UInt8](repeating: 0, count: 8192)
            // Dispatch signals readable bytes or EOF, so a single read cannot
            // wait for a buffer to fill. Each pipe gets its own source.
            let count = read(fd, &bytes, bytes.count)
            if count > 0 {
                let data = Data(bytes.prefix(count))
                if isOutput {
                    self.consume(data)
                } else {
                    self.diagnostic.append(data)
                    if self.diagnostic.count > self.diagnosticLimit {
                        self.diagnostic.removeFirst(self.diagnostic.count - self.diagnosticLimit)
                    }
                }
            } else if count == 0 || (count < 0 && errno != EINTR) {
                if count < 0 { self.protocolProblem = true }
                source.setEventHandler {}
                source.cancel()
                self.ended += 1
                if isOutput && !self.buffer.isEmpty {
                    self.line(self.buffer)
                    self.buffer.removeAll()
                }
                self.completeIfReady()
            }
        }
        readers.append(source)
        source.resume()
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let row = Data(buffer[..<newline])
            buffer.removeSubrange(buffer.startIndex...newline)
            line(row)
        }
        if buffer.count > lineLimit {
            protocolProblem = true
            buffer.removeAll()
        }
    }

    private func line(_ data: Data) {
        guard !data.isEmpty else { return }
        guard data.count <= lineLimit,
              let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = event["type"] as? String else {
            protocolProblem = true
            return
        }
        guard terminal == nil else { protocolProblem = true; return }
        if singleReply || type == "done" || type == "error" {
            terminal = event
        } else {
            deliveryQueue.async { self.onEvent(event) }
        }
    }

    private func completeIfReady() {
        guard exited && ended == 2 else { return }
        finish(error: nil)
    }

    /// How the process ended, for the diagnostic: a signal as a signal. Only
    /// called once it has exited; a Process that never ran raises if asked.
    private func howItEnded() -> String {
        process.terminationReason == .uncaughtSignal
            ? "it was stopped by signal \(process.terminationStatus)"
            : "exit status \(process.terminationStatus)"
    }

    private func finish(error: String?) {
        guard !finished else { return }
        finished = true
        process.terminationHandler = nil
        for reader in readers {
            reader.setEventHandler {}
            reader.cancel()
        }
        readers.removeAll()
        let event: [String: Any]
        if error == nil, !protocolProblem, let result = terminal,
           (result["type"] as? String == "error" || process.terminationStatus == 0) {
            event = result
        } else {
            let reason: String
            if let error = error {
                reason = error
            } else if protocolProblem {
                reason = "The part of ClickGraft that does the work sent an answer ClickGraft "
                    + "couldn't read (\(howItEnded()))."
            } else if terminal != nil {
                reason = "The part of ClickGraft that does the work gave its answer, then "
                    + "stopped with an error (\(howItEnded()))."
            } else {
                reason = "The part of ClickGraft that does the work stopped without giving "
                    + "an answer (\(howItEnded()))."
            }
            let detail = String(decoding: diagnostic, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            event = ["type": "error", "stage": "backend",
                     "error": reason + (detail.isEmpty ? "" : "\n\n" + detail)]
        }
        deliveryQueue.async { self.onEvent(event) }
    }
}
