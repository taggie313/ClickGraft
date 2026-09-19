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
> HP Click for Mac was one of those until version 4.11.31. That translation is
> why it's slow to start and why clicks take a moment to register. HP released
> 4.11.31 in September 2026 built for Apple Silicon; if that's the one you have,
> ClickGraft will say there's nothing to do. *(1.5.7)*
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

**State — present, but only because Xcode is waiting for its licence** *(1.5.6)*.
Shown as a small line under the ✓ panel:

> Xcode on this Mac is waiting for its licence to be accepted, so ClickGraft is
> using the Command Line Tools instead. Nothing for you to do.

**Blocked — Xcode needs its licence, and there are no Command Line Tools to use
instead** *(1.5.6)*. Replaces this screen:

> **Xcode needs its licence accepted first**
>
> ClickGraft uses Apple's developer tools, and on this Mac they come from Xcode.
> Xcode has been updated, and until its new licence is accepted, macOS won't let
> anything use those tools, ClickGraft included. Nothing is wrong with ClickGraft
> or with HP Click, and nothing has been changed.
>
> **Open Xcode once.** It shows Apple's licence. Agree to it (macOS may ask for
> your Mac's password), then come back here and press Check again. You don't
> need to do anything else in Xcode.
>
> `Open Xcode`
>
> If you'd rather use Terminal, sudo xcodebuild -license accept does the same thing.

**Controls:** `Quit` · `Check again`

Why it exists: Xcode 27.0 updated itself on 15 Sep 2026 with its licence
unaccepted, every developer tool then refused to run, and ClickGraft said only
that it "couldn't start" and that reopening usually helps, which it cannot. Opening
Xcode is the instruction; the Terminal command is second, per the note below.
`Open Xcode` opens the Xcode that `xcode-select` points at, because acceptance is
per Xcode version.

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

When the report is made, the first dialog also asks for an optional address
*(1.5.5)*, before the description is written, so the preview is the whole report:

> Optional — where to reach you, to hear when this version is supported:
> *(you@example.com — or leave it blank)*

**Not selectable — too old for any version of ClickGraft** *(1.5.5)*:

> HP Click V4.7 — version 4.7.28 · Too old for any version of ClickGraft

**Not selectable — HP's own Apple Silicon build** *(1.5.7)*:

> HP Click — version 4.11.31 · Already runs natively on Apple Silicon. No copy needed

**Native block** — green, and with no report offer, because nothing is wrong:

> **HP Click 4.11.31 already runs natively on Apple Silicon.** HP released it
> built for your Mac's processor, so there is nothing for ClickGraft to do. Use it
> as it is. It doesn't need Rosetta, so it will keep working on future versions of
> macOS.
>
> One exception: HP Click 4.11.31 doesn't support *(the printers 4.8.117 accepts
> and it does not)*. If you print to one of those, keep HP Click 4.8.117, which
> does, and make a ClickGraft copy of it.

Recognised by the main executable and the Electron framework both carrying arm64
alongside x86_64; a ClickGraft copy has arm64 alone. The printer sentence is
computed from the app's own list against 4.8.117's and left out when it is empty.

**Too-old block** — shown instead of the report offer, because no report or update
can help:

> **HP Click 4.7.28 can't be made native, by any version of ClickGraft.**
> ClickGraft works by switching on the Apple Silicon code HP already builds into
> its app, and this version has none. A report or an update won't change that. A
> newer HP Click will: 4.8.117 is still free on HP's servers, and ClickGraft works
> with it. It accepts every printer HP Click 4.7.28 does.
>
> What ClickGraft found: *(the evidence, e.g. "It is built on Electron 8.2.3 …")*
>
> `Where to get 4.8.117`

The printer sentence is computed from their app against 4.8.117's list; if a
printer would be lost it is named instead, and if the list can't be read the
sentence is left out.

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

**Printer warning** *(1.5.8)* — orange, first on the screen, above everything
below. Shown only when the copy already at the output path supports printers the
new copy won't:

> **This replaces your copy made from HP Click 4.8.117. The new copy won't support
> the DesignJet T310 24-in, T320 24-in, T350 24-in, T720 24-in, T720 36-in, T750
> 24-in and T750 36-in.** HP Click 4.8.118, the one you chose, doesn't list them.
>
> If you print to one of those, keep the copy you have: press Back.
>
> Or make the new copy from HP Click 4.8.117 instead, which still supports them.
>
> `Where to get 4.8.117`
>
> ☐ Replace it anyway. I don't print to any of these.

`Create the copy` stays off until the box is ticked, and the tick is passed to the
backend, which refuses to replace without it. The "Or make…" line and its button
appear only when the old copy's version is one ClickGraft supports and differs
from the new one.

Why it exists: the copy's name is fixed, so every build replaces the one before
it, and this screen said only "Replacing". 4.8.118 reached people as a background
update of 4.8.117 and dropped the T310/T320/T350/T720/T750, so a T-series owner
making a new copy of "the HP Click I have", as anyone who wants 1.5.8's fixes
must, could swap a copy that prints to their plotter for one that can't. The
version comes from the old copy's Info.plist, where ClickGraft changes the bundle
identifier but never the version; the printers come from both apps' own
printersValidate.json, not from version numbers. If either list can't be read,
nothing is said about printers either way.

**Copy is open** *(1.5.8)* — orange, and `Create the copy` stays off:

> **HP Click (Apple Silicon) is open.** Quit it before you create the new copy.
> ClickGraft won't replace an app while it's running, and it won't quit it for
> you, in case it's in the middle of a print.
>
> `Check again`

**Section — Where things go:**

> **Reading from** /Applications/HP Click.app — opened for reading only, not changed
>
> **Creating** /Applications/HP Click (Apple Silicon).app — a new app; nothing is
> overwritten

When a copy is already there, **Replacing** instead of **Creating**, with one of
*(1.5.8)*:

> This replaces your copy made from HP Click 4.8.117 with one made from HP Click
> 4.8.118. Your original HP Click is still untouched.
>
> This replaces your copy made from HP Click 4.8.117 with a new one made from the
> same version. Your original HP Click is still untouched.

and, when its version can't be read or it isn't a ClickGraft copy, the older
wording:

> A copy is already here from a previous run. It will be replaced. Your original
> HP Click is still untouched.
>
> An app with this name is already here. It will be replaced. Your original HP
> Click is still untouched.

**Section — The main change:**

> **Replacing the Intel engine with the Apple Silicon one.** ClickGraft downloads
> the official Apple Silicon engine directly from its makers, checks it against a
> published fingerprint, and puts it in the copy. HP's own files — layout, colour,
> the print engine, your settings — are carried across untouched.

**Section — Small fixes to the copy:**

Each as *plain sentence first, filename second*. Since 1.5.8 a point is shown only
when the manifest for the chosen version patches its file (`fixes` in the plan,
from `FIX_FOR_PATH` in `clickgraft/agent.py`), so the list is four points for
4.8.117 and 4.8.118 and three for 4.10.42. No count is shown: five patches make
four points on 4.8.x, because two files carry one repair.

> **Stops HP's updater downloading its Intel version over your new app.** HP
> Click asks HP for an update each time it starts. Left alone it can download HP's
> Intel build — around 570 MB — and keep offering to restart and install it.
> ClickGraft stops it asking, and replaces the installer that would do the
> replacing. *(reworded in 1.5.8)*
> `app/node/main/app-updater.js`
>
> **Stops crash reports being sent unencrypted.** HP's build uploads them over an
> unencrypted connection. This turns that off.
> `app/package.json` *(1.5.8; before that the root `package.json`, whose copy of
> the setting HP's crash reporter never reads)*
>
> **Stops HP Click writing printer passwords into its log.** If you type SNMPv3
> printer passwords and press Return, HP Click 4.8 and 4.10 write them into its
> log. HP stopped this in 4.11.31; the copy makes the same change. *(1.5.8)*
> `app/bundle.js`
>
> **Fixes a bug in HP's code.** Two of HP's files have a mistake that makes the
> app report an error every time it starts — on Intel Macs too. ClickGraft
> repairs it. *(4.8.117 and 4.8.118 only, from 1.5.8)*
> `app/shared/constants.js`, `app/shared/industries.js`

Why they changed in 1.5.8:

- **Crash reports.** The sentence was not true until 1.5.8. HP's crash reporter
  reads `app/package.json` (`app/main.js` require()s it), and every earlier
  release turned the setting off in the root `package.json`, whose copy of the
  setting nothing reads — Electron reads that file for `main` and the version,
  not for this — so every copy made before 1.5.8 went on trying to send crash
  reports. The wording stays; the fix now matches it, and the build checks the
  value.
- **The updater.** The old sentence said the update would "quietly undo the whole
  thing". By the evidence it would more likely fail: Squirrel checks the update
  against the running app's own signature, the copy's is ad-hoc, and ClickGraft
  replaces HP's ShipIt with a stub in every copy anyway. What the lock certainly
  stops is the download and the restart bar, and neither is quiet.
- **Printer passwords.** HP's own 4.11.31 line, copied exactly. The sentence says
  "press Return" because that is the only time HP Click writes it: it doesn't
  happen just by setting up an SNMPv3 printer. It isn't a promise that the
  passwords are private anywhere else: lines already in the log stay there.
- **HP's bug.** 4.10.42's `index.html` never loads `shared/constants.js` as a
  script, so the error the point describes can't happen there, and 1.5.8 stopped
  patching those two files for 4.10.42. Up to 1.5.7 the point was shown for every
  version.

**Section — Support files added:**

> HP's Apple Silicon components expect two small libraries that HP forgot to
> include. ClickGraft downloads them from their official source and adds them to
> the copy. Without them the app would fail the first time it went online.

**Disclosure — "Show technical detail":** the exact patches, anchors, dylib names,
download URLs and SHA-256s. Unchanged from what's there now — someone who opens
this wants precision, not prose.

**Controls:** `Back` · `Create the copy` (off while the copy being replaced is
open, and until the printer box is ticked when there is one *(1.5.8)*)

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

**Controls:** `Show me the app` · `Open the log` · `Report a problem` ·
`Share how it went` · `Done`

**Share how it went — why it exists:** only failures ever reach us otherwise, so
a working install is invisible and "does this tool work" can only be answered
from an absence of complaints, which is not evidence. Framed as a favour asked
plainly, never as a nag: one button, no badge, no reminder, and "No thanks" is a
real answer. The same rules as the bug report apply — the exact text is shown
first, nothing is sent that has not been read, the home folder name is removed,
and there is no account, identifier, or way to link two submissions.

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

**The build refused to replace the copy** *(1.5.8)* — orange, not red, and no
report offer: nothing went wrong and the person can put it right. Review checks
both of these first, so this only appears when things changed after Review was
drawn.

> **Quit HP Click (Apple Silicon) first**
>
> HP Click (Apple Silicon) is open, so ClickGraft hasn't replaced it. Nothing has
> been downloaded or changed.
>
> **Quit HP Click (Apple Silicon), then press Try again.** ClickGraft won't quit
> it for you, in case it's in the middle of a print.
>
> **Your original HP Click was not changed.**
>
> `Back` · `Try again`

The copy can also be opened *during* the build, which takes about a minute — the
new copy is in the output folder only at the very end. `build.py` checks again in
its last step, immediately before it would delete the old bundle, and stops
instead; the copy it built is thrown away. The same screen appears, with the
second sentence replaced, because by then the download did happen:

> It was opened while the new copy was being made, so that new copy was thrown
> away rather than put in its place. Nothing here changed.

> **Check the printers first**
>
> The copy that is already here supports the DesignJet T310 24-in, … and T750
> 36-in, and the new one won't. ClickGraft hasn't replaced it. Nothing has been
> downloaded or changed.
>
> **Go back to see what would change.** If you don't print to any of them, you
> can tick the box there and replace it anyway.
>
> **Your original HP Click was not changed.**
>
> `Back`

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
