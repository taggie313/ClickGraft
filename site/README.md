# site/

The download page for <https://clickgraft.elusive.net>.

One self-contained `index.html`. No build step, no framework, no external
requests — deliberately, so it can be served from anywhere and so a reader on a
locked-down shop network gets the whole page or none of it, never half.

## Deploying

Upload two files to the web root:

```
index.html
ClickGraft.zip      <- from ./packaging/sign_and_notarize.sh, notarized and stapled
```

The download button links to `ClickGraft.zip` relative to the page, so as long
as they sit beside each other it works on any host — a static bucket, a plain
nginx directory, GitHub Pages.

**Ship the stapled zip, not `dist/ClickGraft.app` zipped by hand.** The notarize
script re-zips *after* stapling; a zip made before that carries no notarization
ticket, and a Mac that downloads it while offline shows the "unidentified
developer" warning the page promises won't appear.

## Before it goes up

- The GitHub URL is `taggie313/ClickGraft` in five places. If the repository
  lands somewhere else, change them all — a 404 from the "Source on GitHub" link
  costs more trust than the page builds.
- The performance table is real measured data from `benchmarks/`. If the numbers
  are ever re-measured, they change here too. They are the one claim on the page
  a reader can check, so they have to survive checking.
- `site/` carries no version number on purpose. The page describes what
  ClickGraft does, and the release it points at is whatever `ClickGraft.zip` is.

## Wording

The copy follows `docs/wizard-copy.md` — same voice, same audience, same
words-to-avoid table. Someone who reads the page and then opens the app should
feel like they are still being talked to by the same thing.
