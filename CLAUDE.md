# Working on ClickGraft

## Attribution

Do not add "Generated with Claude Code", "Co-authored-by: Claude", or any similar
tag to release notes, PR bodies, issues, or commit messages in this repository.
This applies regardless of the default behaviour configured elsewhere.

## Never distribute

Two patches exist for personal use and are deliberately **not** in the shipped
tool or in any manifest:

- the ink-level workaround
- the advertising removal

Both stay out of distribution because publishing them invites HP to lock the app
against the graft itself, which would cost every user the thing that actually
matters. If a task seems to call for adding either to `manifests/` or to the
build, it is wrong — ask first.

## Shipping a release

In order. Skipping the rebuild once notarised a stale binary that no longer
matched the source.

1. Bump `VERSION` in `packaging/build_app.sh`.
2. Write `packaging/release.json`. `importance: important` is reserved for
   "fixes something that stops the tool working" — it is the only word left
   that means it.
3. `python3 packaging/check_release.py` — the patch guard, the whole suite and
   a throwaway build. Read every skip it lists; two are environmental (a
   case-sensitive filesystem, and Linux for the site's atomic swap).
4. **Commit everything that ships** — `clickgraft/`, `manifests/`,
   `packaging/*.swift`, `build_app.sh`, `AppIcon.icns`, LICENSE, NOTICE — and
   change none of it again before the tag. The artifact gate compares the
   shipped zip against the sources the tag committed, so a later edit to any
   of them invalidates the release rather than the working tree.
5. `rm -rf dist/ && ./packaging/build_app.sh` — build fresh, never reuse `dist/`.
6. `./packaging/sign_and_notarize.sh`
7. `git tag -a vX.Y.Z` and push the tag **before** deploying: the appcast's
   release history is built from tags, reading each tag's `release.json`, and
   the gate resolves the zip's version to that tag.
8. `python3 packaging/check_release.py --artifact dist/ClickGraft.zip` — the
   same check `redeploy.sh` runs: version, recorded sources, payload,
   signature, stapling, Gatekeeper.
9. `./site/deploy/redeploy.sh` — its healthcheck lists the GitHub release as
   "not created yet"; that is the next step, not a failure. A `✗` there is real.
10. `gh release create vX.Y.Z dist/ClickGraft.zip` with a title of the form
    `ClickGraft X.Y.Z — short tag line`.

Site-only redeploys between releases are fine: the gate checks the zip against
its own tag, not against HEAD. Zips released before 1.6.0 carry no source
record, so redeploying one needs
`CLICKGRAFT_ALLOW_LEGACY_ARTIFACT=<version> ./site/deploy/redeploy.sh`,
naming that exact version.

The download is published as `ClickGraft-<version>.zip`, with `ClickGraft.zip`
left as a **symlink** to it. A symlink and not a redirect, because a 302 would
put two lines in the access log for one download and the counters in
`summary.sh` and `clickgraft-watch.sh` count 200s on a path.

## Deploying

`site/deploy/deploy.env` (gitignored) holds `PVE_HOST` as a **node name**, not
an address. It is only an entry point: `require_host` asks the cluster which
node actually holds CT 136 and follows it, and a name is what the cluster
answers with. The container has already migrated once.

`~/JoshCode/elusive-edge` owns the shared nginx and the tunnel. A site deploy
must never be able to take the other projects on that host offline.

## Reports

Nothing is sent from the app that the user has not read first. This is written
into `site/deploy/collector/collector.py`'s docstring and stated on the site; it
is the reason the reporting is trustworthy. Do not add a silent submit.

The only field in a report that identifies anyone is the optional contact
address, and it exists only when someone types it. It is used to reply about
that report and nothing else.

## Tests

`python3 -m pytest` needs a **stock** HP Click 4.8.117 in `/Applications` —
`find_stock_bundle()` locates it by reading `CFBundleShortVersionString` and
rejects anything with an arm64 slice, because that is a ClickGraft output
rather than a build source. Without one the suite skips rather than fails.

## Checking your work

`site/deploy/healthcheck.sh` prints `✗` lines and a `FAILURES ABOVE` summary.
Read all of its output. Grepping it for `✓` has already hidden a real failure
where the site advertised one version and served another.
