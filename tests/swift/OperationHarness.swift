// A harness that drives PythonPayload's real cancellation logic.
//
// The review that found F1 had to compile the installer into a temporary
// executable to reproduce it, because the behaviour cannot be reached from
// pytest and a source-text assertion cannot tell a guard from an `if`. This is
// that harness, kept, so the acceptance cases run against the production types
// instead of being re-scaffolded each time.
//
// It links packaging/PythonPayload.swift unmodified. Nothing here reimplements
// the logic under test; the cases only drive Operation and read its answers.

import Foundation

func expect(_ label: String, _ got: Bool, _ want: Bool) -> Bool {
    let ok = got == want
    print("\(ok ? "ok  " : "FAIL") \(label): got \(got), want \(want)")
    return ok
}

var failures = 0

// --- C3: cancel and commit contend; exactly one wins -----------------------
//
// Run both from separate threads against a barrier so neither ordering is
// privileged, many times, and require the two answers to always disagree.
do {
    var cancelWins = 0, commitWins = 0, both = 0, neither = 0
    for _ in 0..<2000 {
        let op = PythonPayload.Operation(id: 1)
        let gate = DispatchSemaphore(value: 0)
        var cancelled = false, committed = false
        let group = DispatchGroup()
        DispatchQueue.global().async(group: group) {
            gate.wait(); cancelled = op.cancel()
        }
        DispatchQueue.global().async(group: group) {
            gate.wait(); committed = op.admitCommit()
        }
        gate.signal(); gate.signal()
        group.wait()
        if cancelled && committed { both += 1 }
        else if cancelled { cancelWins += 1 }
        else if committed { commitWins += 1 }
        else { neither += 1 }
    }
    print("C3 contention over 2000 rounds: cancel=\(cancelWins) commit=\(commitWins) "
          + "both=\(both) neither=\(neither)")
    // Only the safety property is asserted here. WHICH side wins a genuine race
    // is up to the scheduler -- one run gave 1998/2, the next 2000/0 -- so
    // requiring both to appear made the test flaky rather than strict. The two
    // orderings are covered deterministically below by releasing one side
    // first, which exercises the same lock without depending on luck.
    if !expect("C3 never both", both == 0, true) { failures += 1 }
    if !expect("C3 never neither", neither == 0, true) { failures += 1 }
    if !expect("C3 exactly one winner every round",
               cancelWins + commitWins == 2000, true) { failures += 1 }
}

// --- C3a: both orderings, forced rather than hoped for ---------------------
//
// Same lock, same two calls; only the order is fixed. This is what makes the
// racing loop above meaningful -- it shows the barrier answers correctly
// whichever side reaches it first, which a scheduler-dependent race cannot.
do {
    var cancelFirstWins = 0, commitFirstWins = 0
    for _ in 0..<500 {
        let a = PythonPayload.Operation(id: 100)
        if a.cancel() && !a.admitCommit() { cancelFirstWins += 1 }
        let b = PythonPayload.Operation(id: 101)
        if b.admitCommit() && !b.cancel() { commitFirstWins += 1 }
    }
    if !expect("C3a cancel-first always wins", cancelFirstWins == 500, true) { failures += 1 }
    if !expect("C3a commit-first always wins", commitFirstWins == 500, true) { failures += 1 }
}

// --- C3b: once committed, cancellation is refused --------------------------
do {
    let op = PythonPayload.Operation(id: 2)
    if !expect("commit admitted", op.admitCommit(), true) { failures += 1 }
    if !expect("cancel refused after commit", op.cancel(), false) { failures += 1 }
    if !expect("still not cancelled", op.isCancelled, false) { failures += 1 }
}

// --- C3c: once cancelled, commit is refused, and cancel is one-way ---------
do {
    let op = PythonPayload.Operation(id: 3)
    if !expect("cancel accepted", op.cancel(), true) { failures += 1 }
    if !expect("commit refused after cancel", op.admitCommit(), false) { failures += 1 }
    if !expect("cancel is idempotent", op.cancel(), true) { failures += 1 }
    if !expect("still cancelled", op.isCancelled, true) { failures += 1 }
}

// --- C4: a newer attempt cannot clear an older one's cancellation ----------
//
// This is the defect itself. With the old process-global flag, creating the
// second Operation reset the state the first one was about to read.
do {
    let first = PythonPayload.Operation(id: 10)
    _ = first.cancel()
    let second = PythonPayload.Operation(id: 11)
    if !expect("new attempt starts uncancelled", second.isCancelled, false) { failures += 1 }
    if !expect("old attempt is STILL cancelled", first.isCancelled, true) { failures += 1 }
    if !expect("old attempt cannot commit", first.admitCommit(), false) { failures += 1 }
    if !expect("new attempt can commit", second.admitCommit(), true) { failures += 1 }
}

print(failures == 0 ? "ALL PASSED" : "\(failures) FAILURE(S)")
exit(failures == 0 ? 0 : 1)
