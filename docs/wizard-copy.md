# Wizard copy — draft for review

Source of truth for every string the ClickGraft app shows. Written to be read by
someone who runs a print shop, not someone who writes software.

Mark it up freely — this is meant to be argued with before any of it reaches Swift.

---

## Who is reading this

Someone whose plotter is central to their livelihood, who has been told by a
stranger's app that it can fix HP Click. They are not worried about Electron
versions. They are worried about exactly three things:

1. **Will this break the thing I use to make money?**
2. **What is it actually doing to my computer?**
3. **How do I undo it if I hate it?**

Every screen answers at least one of those. If a sentence doesn't serve one of
them, it should probably go.

## Voice

- **Plain, specific, calm.** "Your HP Click is not changed" beats "non-destructive".
- **Concrete nouns.** "A second app appears next to your existing one", not
  "the output artifact is provisioned".
- **No hedging, no salesmanship.** State what happens. Don't say "safely" or
  "simply" — showing is what makes it feel safe.
- **Jargon budget: three terms, each glossed once.** "Apple Silicon", "Rosetta"
  and "the engine". Everything else gets said in ordinary words. Rosetta earns
  its place because a user who has already searched why Click is slow has met
  the word, and because it lets them verify the fix themselves.
- **Never blame the user.** If something can't proceed, the screen says what to
  do next.

### The one explanation everything rests on

Used in full on the welcome screen, in short form on the review screen:

> In 2020 Apple started replacing the Intel processors in Macs with its own,
> called Apple Silicon. Apps built for the older Intel chips still run: macOS
> translates them as they go, using a system called Rosetta. That translation is
> what costs the speed.
>
> HP already builds most of HP Click for Apple Silicon. The parts that do the
> real work — page layout, colour handling, the print engine — are compiled for
> your Mac's processor and are sitting inside the app right now. What HP ships
> alongside them is the wrong engine: an Intel one.
>
> ClickGraft makes a copy of your HP Click and puts the Apple Silicon engine into
> the copy. It doesn't rewrite HP's software. It swaps one component — one HP
> downloads rather than writes — and leaves everything else exactly as HP
> shipped it.

Why this works: it is true, it is checkable, and it reframes the tool from
"modifying HP's app" to "finishing the job HP left half-done."

---

## Screen 1 — Welcome

**Purpose:** answer "what is this and what will it do to my stuff" before asking
for anything.

**Heading:** ClickGraft

**Sub-heading:** Make HP Click run properly on your Mac

**Body:**

> In 2020 Apple started replacing the Intel processors in Macs with its own,
> called Apple Silicon. Your Mac still runs apps built for the older Intel chips
> by translating them as they go — that's Rosetta.
>
> HP Click for Mac is one of those. That translation is why it's slow to start
> and why clicks take a moment to register.
>
> HP already builds the important parts of HP Click for Apple Silicon — page
> layout, colour, the print engine. They're inside the app you have installed
> right now. They're just packaged with an Intel engine.
>
> ClickGraft makes a copy of your HP Click and puts the Apple Silicon engine
> into that copy.

**Reassurance block** — visually distinct, three lines:

> **Your HP Click is not modified.** It's opened for reading only, and left
> exactly as it is.
>
> **You end up with two apps.** Your original, and a new one beside it.
>
> **To undo everything, drag the new app to the Trash.** There is no uninstaller
> because there's nothing else to remove.

**Controls:** `Continue` · `Quit`

**Note:** no "Welcome to" — it wastes the most-read line on the screen. No
performance numbers here either; they read as a sales pitch before trust exists.
They belong on the Done screen, where they're a result rather than a claim.

---

## Screen 2 — What ClickGraft needs

**Purpose:** get the one dependency sorted without a terminal.

**Heading:** What ClickGraft needs

**Body:**

> ClickGraft uses a set of tools Apple ships for free, called the Command Line
> Tools. Most Macs used for design or print work already have them.

**State — present:**

> ✓ **Apple's Command Line Tools are installed.** Nothing to do.

**State — missing:**

> **Apple's Command Line Tools aren't installed yet.**
>
> They come from Apple, not from us. macOS will offer to install them the first
> time it needs them — accept, wait for it to finish, then come back here. It's a
> large download and can take several minutes.

**Controls:** `Back` · `Continue` (disabled until present) · `Check again`

**Disclosure — "What ClickGraft uses them for":**

> Two things: to read the app you already have, and to sign the copy it makes so
> macOS will run it. The list below is exactly what it looks for.
>
> *(tool → path table)*

**Note:** never print `xcode-select --install` as the primary instruction. A
terminal command in a wizard is a failure of the wizard.

---

## Screen 3 — Choose your HP Click

**Purpose:** make picking the wrong thing impossible.

**Heading:** Choose your HP Click

**Body:**

> Pick the HP Click you use now. ClickGraft reads it and leaves it alone.

**Each option shows:** app name · version · a short status line.

**Selectable:**

> HP Click — version 4.8.117 · Ready to copy

**Not selectable — already a ClickGraft copy:**

> HP Click (Apple Silicon) — This one was already made by ClickGraft. Choose your
> original instead.

**Not selectable — unsupported version:**

> HP Click — version 5.1.0 · ClickGraft doesn't know this version yet

**State — nothing found:**

> **No HP Click found in your Applications folder.**
>
> ClickGraft looks in Applications. If yours lives somewhere else, move it there
> and press Check again.

**Controls:** `Back` · `Continue` (disabled until a valid choice) · `Check again`

**Unsupported-version block:**

> ClickGraft only works with versions it has been tested against, because it
> needs to know exactly where to make its changes. Guessing would risk your app.
>
> You can send a report describing this version, and support can be added.
>
> `Create a report`

**Note:** show unsupported apps rather than hiding them. Someone who sees only
one of their three HP Clicks assumes the tool is broken. Showing them greyed with
a reason answers the question before it's asked.

---

## Screen 4 — Here's exactly what will happen

**Purpose:** the trust moment. Last screen before anything is written.

**Heading:** Here's exactly what will happen

**Body:**

> Nothing has been changed yet. Nothing will be, until you press the button
> below.

**Section — Where things go:**

> **Reading from** /Applications/HP Click.app — opened for reading only, not changed
>
> **Creating** /Applications/HP Click (Apple Silicon).app — a new app; nothing is
> overwritten

**Section — The main change:**

> **Replacing the Intel engine with the Apple Silicon one.** ClickGraft downloads
> the official Apple Silicon engine directly from its makers, checks it against a
> published fingerprint, and puts it in the copy. HP's own files — layout, colour,
> the print engine, your settings — are carried across untouched.

**Section — Four small fixes to the copy:**

Each as *plain sentence first, filename second*:

> **Stops HP's updater replacing your new app with the Intel version.** Without
> this, HP's automatic update would quietly undo the whole thing.
> `app/node/main/app-updater.js`
>
> **Stops crash reports being sent unencrypted.** HP's build uploads them over an
> unencrypted connection. This turns that off.
> `package.json`
>
> **Fixes a bug in HP's code.** Two of HP's files have a mistake that makes the
> app report an error every time it starts — on Intel Macs too. ClickGraft
> repairs it.
> `app/shared/constants.js`, `app/shared/industries.js`

**Section — Support files added:**

> HP's Apple Silicon components expect two small libraries that HP forgot to
> include. ClickGraft downloads them from their official source and adds them to
> the copy. Without them the app would fail the first time it went online.

**Disclosure — "Show technical detail":** the exact patches, anchors, dylib names,
download URLs and SHA-256s. Unchanged from what's there now — someone who opens
this wants precision, not prose.

**Controls:** `Back` · `Create the copy`

**Note:** the button says what it does. Not "Start", not "Build" — "Create the
copy" repeats the central reassurance at the exact moment of commitment.

---

## Screen 5 — Making your copy

**Purpose:** make a few minutes of waiting feel accounted for.

**Heading:** Making your copy

**Progress captions** — plain language, in order:

1. Checking your HP Click
2. Getting the Apple Silicon engine from its makers
3. Making a copy of your app
4. Fitting the new engine
5. Adding the support files
6. Making the small fixes
7. Signing the copy so macOS will run it
8. Checking the result

**Body under the bar:**

> This usually takes under a minute. Your original HP Click is not being touched.

**Disclosure — "Show detail":** the live technical log.

**Controls:** none until finished. No cancel button — see note.

**Note:** the reassurance is repeated *here* deliberately. This is the only screen
where the user is watching a progress bar and wondering what's happening to their
software. On cancel: the build is short and interrupting mid-write leaves a
half-made copy. If we add one later it must delete the partial copy and say so.

---

## Screen 6 — Done

**Heading:** Your Apple Silicon copy is ready

**Body:**

> **HP Click (Apple Silicon)** is in your Applications folder, next to your
> original.
>
> Everything checked out: it's built for your Mac's processor, it's signed, and it
> starts up correctly.

**Now the numbers land** — as a result, not a promise:

> On this Mac it starts about 11× faster than it did under Rosetta, and without
> the freezes.
>
> You can confirm it yourself: open Activity Monitor, find HP Click, and look at
> the Kind column. It now says Apple instead of Intel.

**Important block:**

> **Don't run both at once.** The two apps share your printers and settings, so
> opening one while the other is running makes the second one quit without saying
> anything. Quit one before opening the other.
>
> **Your original is untouched.** If anything about the new copy bothers you, drag
> it to the Trash and carry on as before.

**Controls:** `Show me the app` · `Open the log` · `Done`

**Note:** the can't-run-both warning is the single most likely support question.
It gets a heading, not a footnote.

---

## Error states

**Something went wrong during the build:**

> **The copy wasn't finished.**
>
> *(plain description of what failed)*
>
> **Your original HP Click was not changed.** Nothing was installed. You can try
> again, or send the log if it keeps happening.
>
> `Try again` · `Open the log` · `Back`

**Source app isn't what ClickGraft expected:**

> **This doesn't look like the HP Click ClickGraft was tested with.**
>
> It may have been updated, or already modified. ClickGraft won't guess — making
> changes in the wrong place could damage the app.

**Note:** every error screen states that the original is untouched. That is the
first thing a worried user wants to know, and it costs one line.

---

## Words to avoid

| Don't say | Say |
|---|---|
| Patch, patching | Change, fix |
| Binary, executable, bundle | App |
| ASAR archive | (don't mention it outside technical detail) |
| Electron runtime | The engine |
| arm64 / x86_64 | Apple Silicon / Intel |
| Dylib, library | Support file |
| Code signing | Signing, so macOS will run it |
| Repack, graft | Make a copy |

The product is called ClickGraft; the verb is never "graft".

---

## Decisions

1. **Performance numbers on Done — keep.** They're measured, and on the Done
   screen they read as a result rather than a claim.
2. **"Choose manually…" — not now.** Cut from screens 3 and its controls. If
   someone keeps HP Click outside /Applications we'll hear about it, and it's a
   small screen to add later.
3. **Crash-report note — keep.** It raises a concern the user didn't arrive with,
   which is exactly why it builds credit: unprompted candour about something
   unflattering to HP is what makes the rest of the plan believable.

4. **Name Rosetta — yes.** Used three times: once in the background paragraph,
   once on Welcome, once on Done. It costs a term but buys two things. Anyone who
   has already searched why HP Click is slow has met the word, so it connects this
   tool to a problem they've already named. And it makes the result checkable —
   Activity Monitor's Kind column is where they can see Intel become Apple for
   themselves, which is worth more than any number we print.
