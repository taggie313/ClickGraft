# docs

| File | What it is |
|---|---|
| `wizard-copy.md` | Source of truth for every string the app shows, plus the reasoning behind each screen. Change wording here first, then in `packaging/ClickGraft.swift`. |

## Where the rest of the reasoning lives

There is no design-history folder, on purpose. The working documents from
building this tool described a GUI and a flow that no longer exist, and a stale
design doc is worse for a contributor than no design doc: it reads as current
and quietly disagrees with the code.

What mattered survived where it belongs.

- **Why an invariant exists** — in a comment next to the invariant. The
  post-conditions in `clickgraft/asar.py`, and the macOS behaviours that cost
  someone an afternoon, are annotated in place.
- **What the project will and will not accept** — `CONTRIBUTING.md`, including
  why the scope is narrow and the one line that does not move.
- **How a new HP Click version gets supported** — `CONTRIBUTING.md`. `probe`
  does most of the work.
- **How the site is hosted** — `site/deploy/README.md`.
