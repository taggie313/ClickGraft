# Design history

Working documents from ClickGraft's development, kept because they record *why*
the invariants in the code exist. They are historical: where they disagree with
the code, the code wins.

| File | What it is |
|---|---|
| `SPEC-repack-tool.md` | The original spec. Still authoritative for asar format details, signing order, and the macOS behaviours that bite (App Management locking `.app` bundles, the shared single-instance lock, runtime-written font caches breaking signatures). |
| `DESIGN-v2.md` | The redesign: package + GUI + multi-version manifests, and the shift to copying rather than patching in place. |
| `NOTES-FOR-CODEGEN.md` | Defects found auditing the first implementation, turned into habits. Reads as a checklist of ways this particular job goes wrong. |
| `DEFECTS-round2.md` | Second-round audit. Its "do not regress" table lists behaviours verified against a real build. |
| `repack.py` | The v1 single-file implementation. Superseded, but it is the audited reference the package's asar handling was ported from. |
| `repack_handoff_report.md` | The very first machine-generated report. **Contains claims later found false** — it reported a green table of passes that were liveness checks only, and its audit script's "0 Homebrew dependencies (Clean)" missed two hardcoded paths because it only scanned Mach-O files. Kept as a cautionary example; see `NOTES-FOR-CODEGEN.md` §8. |
