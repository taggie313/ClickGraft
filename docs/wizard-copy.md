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

**Disclosure — "If you can't install them on this Mac"** *(1.5.8)*. Shown only
while the tools are missing, under the orange panel. What the app shows, with the
*(1.5.9)* additions marked; "15" comes from the wizard's own `copiesNeedMacOS`:

> Installing these tools needs an administrator password. So does putting the
> copy into Applications, and making one downloads Apple's Apple Silicon engine
> and two small libraries from the internet. On a managed Mac all three are
> usually someone else's to allow. Three ways round it:
>
> 1. Check HP Click 4.11.31 first. HP's September 2026 version runs on Apple
>    Silicon by itself: no tools, no copy, nothing for ClickGraft to do. Download
>    it from HP's support page — but not if you print to the DesignJet T310,
>    T320, T350, T720 and T750, which only HP Click 4.8.117 supports.
> 2. Ask IT to make one copy for everyone. It is usually a smaller ask than
>    installing developer tools across the estate: they make the copy once, on a
>    Mac they administer, and what comes out is an ordinary app they can deploy
>    like any other. It needs nothing installed on the Macs that receive it — no
>    developer tools, no ClickGraft, no downloads — and nothing from the Mac that
>    made it, *but they do need macOS 15 or later (1.5.9)*. Forward them the
>    notes below.
> 3. Or make the copy on a Mac of your own that has the tools, with your version
>    of HP Click installed, and bring the app over. *This Mac needs macOS 15 or
>    later to open it, and so does a Mac with Apple Silicon to make it (1.5.9).*
>    Move it on a USB drive or a file share if you can: after AirDrop or a
>    download, macOS refuses to open it the first time, and you have to allow it
>    in System Settings, under Privacy & Security.
>
> For whoever does it, measured on macOS 27 (20 Sep 2026):
>
> - Deploy it as a package. …
> - Build that package on the Mac that made the copy. …
> - Don't re-sign it. …
> - The copy carries the HP Click version it was made from, so make it from the
>   version your printers need: the DesignJet T310, T320, T350, T720 and T750
>   need HP Click 4.8.117.
> - *(1.5.9)* It needs macOS 15 or later, and its Info.plist says so: macOS
>   won't open it on an older Mac. A Mac with Apple Silicon needs macOS 15 or
>   later to make it too. An Intel Mac can make it on the macOS it has, but
>   can't test-launch it (22 Sep 2026).

Why the macOS lines *(1.5.9)*: every copy now states macOS 15 as its minimum, so
routes 2 and 3 sent a managed Mac on macOS 12 to 14 to IT for an app macOS would
refuse to open. Until 1.5.9 route 3 was recorded here as "Any Mac with Apple
Silicon, these tools and your version of HP Click installed can make it", which
stopped being true for Apple Silicon Macs older than 15, where the build refuses.

The printer names come from the backend's own `printers_dropped`, not from a
fixed list, so they stay right when HP's list changes. Carriage widths are dropped
from every list the wizard shows: HP's own list names each width, so seven entries
are five plotters, and the widths bury the model numbers somebody is scanning for.

Why it exists: the missing-tools panel said only that macOS would offer to
install them, which is a dead end for anyone on a managed Mac, where that offer
ends at an administrator password they don't have. It is a disclosure rather than
a panel because it is the minority case, and the terminal command sits inside it
rather than on the screen, per the note below: it is what someone forwards to IT,
not what they are asked to type.

The same transfer caveat is now on the Intel-Mac panel, which already offered to
build for another Mac and never said how to get the copy there. *(1.5.9)* That
panel also says, under "Building for another Mac is supported":

> The Mac you make it for needs macOS 15 or later: macOS won't open the copy on
> anything older.

**Blocked — this Mac is older than any copy needs** *(1.5.9)*. On a Mac with Apple
Silicon older than macOS 15, replaces this screen, before anything runs
`/usr/bin/python3`: on a Mac without the Command Line Tools that alone brings up
macOS's offer to install them, a large download behind an administrator password,
for a copy that Mac could never open. The wizard checks macOS itself here
(`copiesNeedMacOS`), because the backend can't run yet; the test suite holds that
figure to what every stock version's own files need. Intel Macs aren't checked:
their copy is for another Mac.

> **ClickGraft needs macOS 15 or later**
>
> This Mac has macOS 14.6. The copy ClickGraft makes needs macOS 15 or later,
> because files HP ships inside HP Click, and the support files ClickGraft adds
> from Homebrew, are built for it. So ClickGraft can't make one on this Mac, and
> there's nothing to install for it.
>
> **Your HP Click is unchanged.** It works as it did.
>
> **Once this Mac is on macOS 15 or later,** open ClickGraft again and it can
> make the copy.
>
> *(blue)* **HP Click 4.11.31 may be the better answer.** It's HP's own Apple
> Silicon version, so it needs no copy, and HP lists it for macOS 12 to 26. It
> doesn't support the DesignJet T310, T320, T350, T720 and T750, so if you print
> to one of those, keep the HP Click you have.
>
> `Where to get 4.11.31`

**Controls:** `Back` · `Quit`

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

Only when it has looked: if Check again gets no answer, see *Choose — when Check
again gets no answer*, under *Since 1.5.9*.

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

**The printer sentence, when HP Click has printers configured** *(since the
capability matrix)*. The list above is eight models long and left the reader to
work out whether one of them was theirs. HP Click keeps the printers it is set up
for in its own `printers.json`, so the wizard can say it. Their printer is not
listed by the build they have:

> Except for your printer. The DesignJet T750 36-in isn't listed by HP Click
> 4.11.31, so keep HP Click 4.8.117 — which does list it — and make a ClickGraft
> copy of it.

Their printer is fine:

> Your DesignJet T1600dr is listed by HP Click 4.11.31, so there is nothing you
> need from ClickGraft.

Read through `printerinfo.collect()`, whose allowlist is why this is safe:
`productName` is a model, not a serial, an address, or a name the user typed. It
never leaves the Mac — it decides what this sentence says. With no printer
configured, the eight-model sentence above is shown unchanged.

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

**Which version it sends them to** *(since the capability matrix)*. This was
always 4.8.117, the oldest ClickGraft supports. That is right for a plotter only
4.8.117 lists, and needlessly roundabout for a shop whose printer HP's own
4.11.31 drives natively — they were told to fetch an older HP Click and make a
copy when they needed neither. The version is now chosen for the printers this Mac
is configured for, and the sentence about a copy follows it:

> **HP Click 4.6.47 can't be made native, by any version of ClickGraft.**
> ClickGraft works by switching on the Apple Silicon code HP already builds into
> its app, and this version has none. A report or an update won't change that. A
> newer HP Click will: 4.11.31 is still free on HP's servers, and HP builds it for
> Apple Silicon itself, so you won't need a copy at all. It lists your DesignJet
> T1600dr.

When no released HP Click lists one of their printers, it is named rather than
dropped:

> One thing it cannot do: it doesn't list the DesignJet T750 36-in, and no
> released HP Click does.

Without a configured printer, or without the recorded table, both panels keep the
wording above them exactly as it was.

**What is not claimed.** No HP Click below 12.0 has been launched on a Mac at its
floor by this project, so no panel says a version will run — only that HP lists it
and what it accepts. `floor_tested` is recorded as false in every row of the table
for that reason.

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
> the DesignJet T310, T320, T350, T720 and T750.** HP Click 4.8.118, the one you
> chose, doesn't list them.
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
this wants precision, not prose. *(1.5.9)* Each Homebrew line under DOWNLOADS
names the bottle and the macOS it is built for, as the backend writes it:
"libidn2.0.dylib from Homebrew's CDN (arm64_ventura bottle, for macOS 13.0 and
later), SHA-256 checked".

**Controls:** `Back` · `Create the copy` (off while the copy being replaced is
open, and until the printer box is ticked when there is one *(1.5.8)*; off when
this Mac's macOS is older than the copy needs *(1.5.9)*)

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

*(1.5.9)* That second sentence is said only when nothing was replaced
(`previous_copy: "none"`). After a rebuild, "carry on as before" was untrue: the
copy the owner used before had just been deleted. Instead:

- `previous_copy: "replaced"` — **Your original is untouched.** The copy this one
  replaced was removed once this one had passed its checks. If anything about
  the new copy bothers you, drag it to the Trash and use your original, which
  works as it always did. ClickGraft can make another copy whenever you like.
- `previous_copy: "aside"` (it passed, and the old one couldn't be deleted) —
  **Your original is untouched.** The copy this one replaced couldn't be removed.
  It's set aside, hidden, in the same folder, and ClickGraft will offer to remove
  it the next time you open it.

On an Intel Mac the second body sentence ends "…try it on the Mac you made it
for, which needs macOS 15 or later." *(1.5.9)*, the figure read back from the
copy's own Info.plist (`needs_macos`).

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

**Something went wrong during the build:** superseded twice — the "…if it
keeps happening" wording, which taught someone to retry nine times before sending
a report (see the comment in `showFailed`), and the "Nothing was installed"
promise, which was false once a build could replace a copy (1.5.9). The current screen, and every variant of the line about the
previous copy, is under *1.5.9 → The copy wasn't finished — changed* below.
Change it there, not here.

**Source app isn't what ClickGraft expected:**

> **This doesn't look like the HP Click ClickGraft was tested with.**
>
> It may have been updated, or already modified. ClickGraft won't guess — making
> changes in the wrong place could damage the app.

**This Mac's macOS is older than the copy needs** *(1.5.9)* — the backend
refuses with `stage: "macos_too_old"`, before anything is downloaded, and gives
`needs` and `this_mac`. The copy's minimum is the highest minimum any file in it
declares (15.0 for 4.8.117, 4.8.118 and 4.10.42 on 22 Sep 2026: HP's own
libmagic in all three and OpenSSL in 4.10.42, and the Homebrew support files),
and never lower than HP's own 12.0. It is written into the copy's Info.plist, so
a Mac too old for a copy made elsewhere gets macOS's own "requires macOS 15.0 or
later" rather than a crash at launch. Review can say it first: the plan's
`macos_floor` carries the same numbers and, when this Mac is too old, the same
`message`. The wizard has its own screen for it, and a panel on Review (see
*1.5.9* below); the backend's message is still written to stand alone, because
it is what `clickgraft build` prints and what a report carries:

> This Mac has macOS 14.6.1, and a copy of HP Click 4.10.42 would need macOS
> 15.0 or later, so ClickGraft has not made one. Nothing has been downloaded or
> written. That comes from files HP ships inside HP Click itself and the support
> files ClickGraft adds from Homebrew, which are built for macOS 15.0 or later.
>
> Your HP Click is unchanged and works as it did. Once this Mac is on macOS 15.0
> or later, ClickGraft can make the copy.

The "That comes from…" sentence names only what actually sets the floor: one of
the two, or both. When nothing inside does and HP's own Info.plist sets it, the
sentence is "That is the macOS HP Click 4.10.42 itself asks for." If the engine
ever sets it, that is only known once the copy is made, so the refusal comes at
the end of the build instead: the first two sentences become "…and the copy of
HP Click 4.10.42 that ClickGraft made needs macOS 26.0 or later. ClickGraft has
thrown it away rather than put it in place, and nothing here was replaced.", and
the reason "the files of the Apple Silicon engine ClickGraft puts in the copy".
A report is no help to this person, so the screen of its own has no report
offer.

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
its last step, immediately before it would set the old bundle aside (up to
1.5.8: delete it), and stops instead; the copy it built is thrown away. The same
screen appears, with the second sentence replaced, because by then the download
did happen:

> It was opened while the new copy was being made, so that new copy was thrown
> away rather than put in its place. Nothing here changed.

> **Check the printers first**
>
> The copy that is already here supports the DesignJet T310, T320, T350, T720 and
> T750, and the new one won't. ClickGraft hasn't replaced it. Nothing has been
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

## 1.5.9 — the copy being replaced, and the macOS a copy needs

Every string below is new or changed in 1.5.9.

**Why.** Up to 1.5.8 a second build deleted the copy already in Applications and
then renamed the new one into place, and checked the new one only after that. A
new copy that failed its checks had already cost the owner the one that worked,
and a rename that failed cost them both. Meanwhile the red screen said "Nothing
was installed, and nothing about your Mac is different from a minute ago", and
the orange one "drag it to the Trash and nothing about your Mac has changed".
Now the copy being replaced is set aside, hidden and never deleted, until the
new one has passed its checks, and put back when it doesn't. The backend says
what became of it in every result (`previous_copy`, `new_copy`, `check`), and
each screen says exactly that and nothing it can't know.

A new copy that fails its checks is **removed** when there is a previous copy to
put back in its place: the old one has been shown to work and the new one
hasn't. With no previous copy the new one **stays**, as before 1.5.9: it is all
there is, and until now every copy that failed a check still launched. What a
report needs — the check's message and the test launch's own logs — lives
outside the copy, so nothing is lost by removing it.

### Review — Screen 4

**When this Mac's macOS is older than the copy needs** — orange, first on the
screen, and `Create the copy` stays off:

> **This copy needs macOS 15 or later, and this Mac has macOS 14.6.** That's
> because files HP ships inside HP Click itself, and the support files ClickGraft
> adds from Homebrew, are built for macOS 15 or later.
>
> Your HP Click is unchanged and works as it did. Once this Mac is on macOS 15 or
> later, ClickGraft can make the copy.
>
> **HP Click 4.11.31 may be the better answer.** It's HP's own Apple Silicon
> version, so it needs no copy, and HP lists it for macOS 12 to 26. It doesn't
> support the DesignJet T310, T320, T350, T720 and T750, so if you print to one of
> those, keep the HP Click you have.
>
> `Where to get 4.11.31`

The reason sentence names only what sets the minimum, from the backend's
`reasons`: "That's because files HP ships inside HP Click itself are built for
macOS 15 or later.", "That's because the support files ClickGraft adds from
Homebrew are built for…", "That's because the files of the Apple Silicon engine
ClickGraft puts in the copy are built for…", or, when only HP's own Info.plist
sets it, "That's the macOS HP Click 4.10.42 itself asks for." The version is said
the way people say it: "15", or "15.4" when the minor number isn't 0.

The 4.11.31 paragraph appears only when HP lists it for this Mac (macOS 12 or
later; `alternative` in the plan). "12 to 26" is HP's own list as it stood on
22 Sep 2026 (`hp_lists_from`, `hp_lists_to`): it was "12 and later" until review,
which overstated it, since HP's page stops at 26. When 4.11.31 is already in Applications, "It's
already in your Applications folder." follows the first sentence and the button
goes. The printers come from that app's own list when it is installed, and
otherwise are the five HP dropped after 4.8.117. "HP lists it for" is HP's word:
4.11.31 carries the same minimum-15.0 libmagic and OpenSSL as 4.10.42 under its
12.0 Info.plist, so ClickGraft does not vouch for it on an older Mac.

**Where things go**, when a copy is already there — one new line under the
existing replace line:

> ClickGraft keeps the copy that's there until the new one has passed its checks,
> and puts it back if it doesn't.

**When a copy an earlier build set aside here is still waiting** (the plan's
`leftover`) — orange, near the top, and `Create the copy` stays off until it is
dealt with:

> **Your previous copy is still set aside from last time.** The last time
> ClickGraft replaced HP Click (Apple Silicon), it didn't finish, and your
> previous copy, made from HP Click 4.8.117, is still set aside, hidden, in the
> same folder. Decide what happens to it before ClickGraft makes another copy.
>
> `Decide now`

`Decide now` opens the screen below and comes back to Review. Why: a build sets
the copy at the path aside, so a build over a waiting one stacks a second on top
of it, and the two can only be undone newest first — which nothing on screen
explained. In review, two presses of `Put it back` in the order shown deleted the
owner's working copy. The backend refuses such a build too (`stage:
"leftover_pending"`), and the wizard then goes straight to the screen below.

### Your previous copy is still here — new screen, before Screen 2

Shown when `agent env` finds a copy a build set aside and never put back or
deleted, and from Review as above. Nothing else would ever mention it — it is
hidden in the same folder — so this screen does, and makes no choice for the
owner.

Worded from the backend's `state` for it, never from assumption. The first draft
said "the new copy, which was never fully checked" of whatever was at the path —
including a copy that had passed, and one that had failed — and its primary button
removed whatever was there. Since review the backend records, beside each copy it
sets aside, which copy it put in its place and what verify made of that one, and
only ever removes that copy to make room.

**Heading:** Your previous copy is still here

**`installed`** — the build stopped while it was checking the new copy (quit,
crashed or killed):

> The last time ClickGraft made a copy of HP Click, it stopped before it had
> finished checking the new one. Your previous copy, made from HP Click 4.8.117,
> was set aside first, so it's safe. It's hidden, in the same folder as the new
> one.
>
> **Put it back if you're not sure.** The new copy, made from HP Click 4.10.42,
> which never finished its checks, is removed, and your previous copy goes back
> where it was.
>
> **Keep the new copy** only if you've used it since and it works. Your previous
> copy is then deleted.

`Decide later` · `Keep the new copy` · **`Put it back`**

**`passed`** — the new copy passed, and deleting the old one failed:

> The last time ClickGraft made a copy of HP Click, the new copy passed its
> checks, but ClickGraft couldn't remove the one it replaced. Your previous copy,
> made from HP Click 4.8.117, is still here, hidden, in the same folder.
>
> **Remove it if the new copy works for you.** The new copy, made from HP Click
> 4.10.42, stays where it is. That's what ClickGraft would have done.
>
> **Put it back** if you'd rather go back to it. The new copy is then removed.

`Decide later` · `Put it back` · **`Remove it`**

**`failed`** — the new copy failed a check, and putting the old one back failed:

> The last time ClickGraft made a copy of HP Click, the new copy didn't pass its
> checks, and ClickGraft couldn't put your previous copy back in its place. Your
> previous copy, made from HP Click 4.8.117, is safe: it's hidden, in the same
> folder. What failed: the test launch.
>
> **Put it back.** The new copy, which didn't pass, is removed, and your previous
> copy goes back where it was. If HP Click (Apple Silicon) is open, quit it first.
>
> **Keep the new copy** only if you've used it since and it works. Your previous
> copy is then deleted.

`Decide later` · `Keep the new copy` · **`Put it back`**

**`missing`** — nothing is at the path (the build stopped between its renames):

> The last time ClickGraft made a copy of HP Click, it stopped part-way through
> replacing yours. Your previous copy, made from HP Click 4.8.117, was set aside
> first, so it's safe, but there's no copy in its place at the moment.
>
> **Put it back.** It goes back where it was, as it was.

`Decide later` · **`Put it back`**

**`other`** — something is at the path, but not the copy that build put there
(moved by hand, or made by a ClickGraft before 1.5.9), so nothing will remove it:

> ClickGraft set your previous copy, made from HP Click 4.8.117, aside while it
> was replacing it, and it's still here, hidden, in the same folder. The HP Click
> (Apple Silicon) in its place now isn't the one ClickGraft put there (it's made
> from HP Click 4.10.42), so ClickGraft won't remove it to make room.
>
> **To put your previous copy back,** move HP Click (Apple Silicon) out of that
> folder yourself first — to the Trash, say — then press Put it back.
>
> **If you don't need your previous copy,** delete it. The HP Click (Apple
> Silicon) in its place stays as it is.

`Decide later` · `Delete it` · **`Put it back`**

Every state ends the panel with **Your original HP Click was not changed.**, then,
small:

> Until you decide, ClickGraft won't replace HP Click (Apple Silicon) again.

then "Set aside at:" and the path, small and in monospace. "made from HP Click …"
is left out of either sentence when that copy's version can't be read. `Decide
later` carries on — to Screen 2, or back to Review — and it is asked again next
time.

`Keep the new copy` asks first:

> **Delete your previous copy?**
> It's deleted, not moved to the Trash, and can't be brought back. HP Click
> (Apple Silicon) and your original HP Click stay as they are.
>
> `Delete it` · `Cancel`

(`Keep the new copy`, `Remove it` and `Delete it` all ask this.)

After `Put it back` — here, or on a failure screen below:

> **Your previous copy is back**
> It's at /Applications/HP Click (Apple Silicon).app, as it was.

and if it can't be done, the backend's reason, then:

> **ClickGraft couldn't put it back**
> *(the reason)*
>
> Your previous copy is still safe, set aside where it was.

If the delete fails: **ClickGraft couldn't delete it** and the backend's reason.
Neither is said without an answer from the backend: see *Put it back, and deleting
a set-aside copy — without an answer*, under *Since 1.5.9*.

### This copy needs macOS 15 or later — new screen

Replaces the red screen for `stage: "macos_too_old"`. Not red and no report
offer: nothing went wrong, and a report can't change which macOS HP's files are
built for.

**Heading:** This copy needs macOS 15 or later

**Body:**

> This Mac has macOS 14.6, so ClickGraft hasn't made the copy. Nothing has been
> downloaded or changed.
>
> *(the reason sentence, as on Review)*

or, when the engine's own files set the minimum and that was only known once the
copy was made:

> This Mac has macOS 14.6. ClickGraft could only tell once the copy was made, so
> it has thrown that copy away. Nothing here was replaced.

**Panel** (orange):

> **Your HP Click is unchanged.** It works as it did.
>
> **Your previous copy hasn't been touched either.** *(only when there is one)*
>
> **Once this Mac is on macOS 15 or later,** ClickGraft can make the copy.

Then the 4.11.31 panel as on Review, in blue.

**Controls:** `Back` · `Quit`

### The new copy didn't pass its checks — replaces "Your copy was made, but one check didn't pass"

Every version says which check failed, as "What failed: …", from verify's
`check`:

| `check` | said as |
|---|---|
| `bundle` | the first look at the new copy |
| `architectures` | the check that it's built for Apple Silicon |
| `bundle_audit` | the check of every file in it |
| `flat_symbols` | the check for anything HP's code needs that's missing |
| `minimum_macos` | the check of which macOS it needs |
| `code_signature` | the check of its signature |
| `asar_integrity` | the check of HP's app files inside it |
| `update_locks` | the check that HP's updater can't replace it |
| `patch_outcomes` | the check of the small fixes |
| `smoke_launch` | the test launch |
| `resealed` | signing it again after the test launch |

**The previous copy has been put back** (`previous_copy: "restored"`) — orange:

> **The new copy didn't pass its checks**
>
> So your previous copy has been put back, as it was, and the new one has been
> removed. What failed: the test launch.
>
> **Your previous copy is back where it was.** Use it just as you did before.
>
> **Your original HP Click was not changed.** That hasn't been touched at any
> point.
>
> **Please send the report.** It says which check failed and why, which is usually
> enough to fix it. Check back here in a day or so: if a new ClickGraft solves
> it, the app will offer you the update itself.
>
> WHAT THE CHECK SAID — *(the message)*

**Controls:** `Back` · `Try again` · `Send a report`

**There was no previous copy** (`previous_copy: "none"`, `new_copy: "kept"`) —
orange:

> **The new copy didn't pass its checks**
>
> HP Click (Apple Silicon) is in your Applications folder, but ClickGraft couldn't
> confirm that it works. What failed: the test launch.
>
> **Your original HP Click was not changed.** That hasn't been touched at any
> point.
>
> **The new copy may still work.** Try opening it. If it starts and finds your
> printer, you're done.
>
> **If it doesn't,** drag it to the Trash.

(It said "There's nothing else to remove." until review: ClickGraft's download
cache stays in ~/.cache/clickgraft, and a failed test launch keeps its logs in
/private/tmp/cg-smoke-*, on purpose, for the report.)
>
> WHAT THE CHECK SAID — *(the message)*

**Controls:** `Back` · `Send a report` · `Open the copy` · `Try again`

**The previous copy couldn't be put back** (`previous_copy: "aside"`) — usually
because the new copy was opened in the seconds after the test launch — orange:

> **The new copy didn't pass its checks**
>
> ClickGraft couldn't put your previous copy back in its place, because the new
> copy is open. Your previous copy is safe. It has been set aside, hidden, in the
> same folder. What failed: the test launch.

("…in its place." when the reason is anything else; the backend's own words go
to the detail log.)
>
> **Put it back when you're ready.** Quit HP Click (Apple Silicon) if it's open,
> then press Put it back. ClickGraft will also offer to do it the next time you
> open it.
>
> **Your original HP Click was not changed.** That hasn't been touched at any
> point.

**Controls:** `Back` · `Send a report` · `Put it back`

### The copy wasn't finished — changed

The first point's second sentence now depends on `previous_copy`, and "Nothing
was installed, and nothing about your Mac is different from a minute ago" is
gone — the Electron download and support files stay in ClickGraft's cache, and
before 1.5.9 the copy being replaced could already be gone:

> **Your original HP Click was not changed.** *then one of:*
>
> - Nothing was put in your Applications folder. *(no copy was there, and none
>   is now)*
> - Your previous copy hasn't been touched either. *(the build stopped before it
>   moved anything — signing is its last step that can fail, and it works on a
>   separate copy)*
> - ClickGraft had started to put the new copy in place, so it has put your
>   previous copy back, as it was.
> - Your previous copy couldn't be put back in its place, but it's safe: it has
>   been set aside, hidden, in the same folder. Press Put it back to return it.

**Controls:** `Back` · `Try again` · `Put it back` (only in the last case) ·
`Send a report`

**Another ClickGraft is working in the same folder** (`stage: "busy"`) — the same
red screen, with the backend's words as the body:

> Another ClickGraft is making or checking a copy in /Applications right now.
> Wait for it to finish, then try again. Nothing here has been changed.

Why: two builds into one folder at once — two wizards open, or one beside a build
run by hand — could interleave so that the second one's rollback put a copy that
had failed its checks back over the owner's, and deleted that. One build, check
and settle at a time per folder since review.

### What a report says happened

The `outcome:` line of a report now follows the same state: "a check did not
pass (smoke_launch); the previous copy was put back", "a check did not pass
(smoke_launch); the previous copy could not be put back and is set aside", "the
copy was made but a check did not pass (smoke_launch)", "the build did not
finish; the previous copy was put back", "the build did not start: this Mac's
macOS is older than the copy needs", and "the copy was made, needed a newer macOS
than this Mac's, and was thrown away". Its `checks:` list now carries the checks
that passed before the one that failed.

---

## Since 1.5.9 — answers ClickGraft can't confirm, and a copy that changes under a build

Every string below is new or changed since 1.5.9.

**Why** (review, 22 Sep 2026). 1.5.9 could lose the backend's last line (3 of
2,000 idle runs, 5 of 2,000 with the CPU oversubscribed, and 200 of 200 when
that line was 300 KB), and the wizard then waited for ever on "Checking the
result". The new transport (`packaging/BackendTransport.swift`) always gives one
final answer, and when it has none to trust, that is an error with
`stage: "backend"`. Two screens then took that error for an answer: Choose's
Check again read it as an empty Applications folder and said "No HP Click
found", and Put it back said "Your previous copy is still safe, set aside where
it was" when nothing had confirmed it. And a copy swapped at the output path
between Review and the button was replaced on the strength of a tick given for
another.

**One name for the backend.** On screen it is always "the part of ClickGraft that
does the work", as Screen 2's "ClickGraft couldn't start" already said it ("The
part of ClickGraft that does the work didn't respond.") — never "backend",
"background task" or "output streams" (see *Words to avoid*).

### ClickGraft couldn't confirm the result — new screen

For `stage: "backend"` from a build: it couldn't be started, it stopped without a
final message, the message couldn't be read, or it exited with an error after
giving one. None of these says how far the build got, so this screen claims
nothing about the copy, the copy it replaces, or the original. Not red: nothing is
known to have failed.

**Heading:** ClickGraft couldn't confirm the result

> The part of ClickGraft that does the work stopped without saying how the build
> ended. So ClickGraft can't tell whether the new copy was put in place, or
> whether a copy that was already there was set aside.
>
> Press Check again. ClickGraft looks for anything that was set aside before it
> makes another copy.
>
> ▸ Show detail — *(the reason below, what stderr said last, then the progress
> log)*

**Controls:** `Send a report` · **`Check again`**

`Check again` starts from Screen 2 and looks for copies set aside afresh — even
one the person chose to Decide later on earlier — so what comes next is about
what is there now. The report's outcome: "the part of ClickGraft that does the
work stopped without a confirmed result; what it installed or set aside is
unknown".

**The reason**, first line of the detail and of the report's `error:`, then the
end of what it wrote to stderr (up to 16 KB):

- The part of ClickGraft that does the work couldn't be started: *(macOS's
  reason)*
- The part of ClickGraft that does the work stopped without giving an answer
  (exit status 1). — or "(it was stopped by signal 9)": a signal is said as one
- The part of ClickGraft that does the work sent an answer ClickGraft couldn't
  read (exit status 0).
- The part of ClickGraft that does the work gave its answer, then stopped with
  an error (exit status 3).
- The part of ClickGraft that does the work stopped, but something it started
  was still connected to it 2 seconds later, so ClickGraft can't be sure it heard
  the whole answer.

### The copy there changed — new screen

For `stage: "replacement_changed"`: the output path no longer holds what Review
showed. Review's answer carries a token for what was there, `Create the copy`
passes it back (`--expect-replacing`), and the build checks it before it
downloads anything and again just before it would replace anything. Something
outside ClickGraft made the change, so, like *The build refused to replace the
copy*: orange, and the button to press is `Check again`, not a report. Worded
from `change` and `during_build`:

**`removed`, since Review** — **HP Click (Apple Silicon) has gone**

> HP Click (Apple Silicon) was in your Applications folder when ClickGraft showed
> you what it would do, and it has gone since. ClickGraft hasn't made the copy.
> Nothing has been downloaded or changed.

**`removed`, during the build** — **HP Click (Apple Silicon) has gone**

> HP Click (Apple Silicon) was removed from your Applications folder while the new
> copy was being made. ClickGraft only does what it showed you, so it hasn't put
> the new copy there: it has thrown it away.

**`appeared`, since Review** — **HP Click (Apple Silicon) is there now**

> There was no HP Click (Apple Silicon) in your Applications folder when
> ClickGraft showed you what it would do, and there is one now. ClickGraft won't
> replace a copy it hasn't shown you, so it has left it alone. Nothing has been
> downloaded or changed.

**`appeared`, during the build** — **HP Click (Apple Silicon) is there now**

> HP Click (Apple Silicon) appeared in your Applications folder while the new copy
> was being made. ClickGraft won't replace a copy it hasn't shown you, so it has
> left it alone, and has thrown the new copy away.

**`changed`, since Review** — **HP Click (Apple Silicon) has changed**

> The HP Click (Apple Silicon) in your Applications folder isn't the one
> ClickGraft showed you: it has been replaced or changed since. ClickGraft won't
> replace a copy it hasn't shown you, so it has left it alone. Nothing has been
> downloaded or changed.

**`changed`, during the build** — **HP Click (Apple Silicon) has changed**

> HP Click (Apple Silicon) was replaced or changed while the new copy was being
> made, so it isn't the one ClickGraft showed you. ClickGraft won't replace a copy
> it hasn't shown you, so it has left it alone, and has thrown the new copy away.

**Panel** (orange):

> **Press Check again.** ClickGraft shows you what's there now, and changes
> nothing until you press Create the copy.
>
> **The HP Click (Apple Silicon) there now hasn't been touched.** *(not when it
> has gone)*
>
> **Your original HP Click was not changed.**

**Controls:** `Send a report` · **`Check again`** (Review, afresh)

Reports: "the copy it would replace was removed after Review; nothing was put in
its place", "a copy appeared at the output path during the build; it was left
alone", "the copy it would replace changed after Review; it was left alone", with
", and the new copy was thrown away" when it was during the build.

**`Try again`** on the failure screens now asks what is at the output path first.
When it is what Review showed, the build starts, as before. When it isn't —
usually a new copy that failed a check and stayed where there was none — Review
comes back, because the build would refuse a token given for an empty folder.

### Choose — when Check again gets no answer

Instead of "No HP Click found…", above the list it had (orange):

> **ClickGraft couldn't look again.** The part of ClickGraft that does the work
> stopped without an answer, so this list is from the last time it looked. Press
> Check again to try once more.
>
> ▸ Show technical detail

("…so nothing is listed." when there was no list to keep.)

### Put it back, and deleting a set-aside copy — without an answer

"Your previous copy is still safe, set aside where it was." now follows **ClickGraft
couldn't put it back** only when the part of ClickGraft that does the work answered
*and* said the copy is still there (`backup_exists: true`). With no answer to trust,
with no word either way, or with the copy gone from there, and for a delete with no
answer:

> **ClickGraft couldn't confirm what happened**
> It didn't get a clear answer about your previous copy, so it can't tell whether
> it was put back. It may be back in place, or still set aside, hidden, in the same
> folder. Press Check again to see where things stand.

For a delete: "…whether it was deleted. It may be gone, or still set aside,
hidden, in the same folder." The reason goes in a small box below, not in the
sentence. One button, `Check again`: the leftover screen behind the alert was
drawn before the attempt, so it starts again from Screen 2, as above.

### Two more check names

| `check` | said as |
|---|---|
| `launcher` | the check that it loads its support files |
| `verification` | the overall result of the checks |

`launcher` fails a copy whose start-up script doesn't load every support file the
copy needs (a `--no-preload` build, which is for diagnosis only); `verification`
is the checks reporting a failure without naming one.

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
| Backend, background task, agent | The part of ClickGraft that does the work |
| Output streams, pipe | (don't mention them) |

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
