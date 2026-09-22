# site/deploy

Hosting for <https://clickgraft.elusive.net>: nginx serving one static page and
one download, behind a Cloudflare tunnel, in a Docker-capable LXC container on
Proxmox.

The specific host and container id live in `deploy.env`, which is gitignored —
copy `deploy.env.example` and fill it in. They describe a private network and
are no use to anyone else.

Follows the house pattern from `elusive-web`: unprivileged Debian CT with
`nesting=1,keyctl=1`, Docker, `/opt/<name>`, an nginx + `cloudflared` compose
pair, and deploys that go **through the PVE host** rather than ssh'ing into the
CT.

## Files

| | |
|---|---|
| `deploy.env.example` | Copy to `deploy.env` (gitignored) and set your host/CT. |
| `setup-ct.sh` | How CT 117 was provisioned. History: the site is served by edge now. |
| `redeploy.sh` | Push the page + `dist/ClickGraft.zip` and restart. Run from a checkout. |
| `publish_site.py` | Run in the CT by `redeploy.sh`: switches `html/` in one step and keeps what it replaced. `--restore` puts a kept copy back; `--check` only checks a staged tree. |
| `_common.sh` | Shared preflight. Refuses to report numbers it could not read. |
| `fetch-stats.sh` | Read the traffic numbers over SSH. `--html` also pulls the GoAccess report. |
| `summary.sh` | Traffic analysis, run inside the container. |
| `visitor-classify.awk` | Who counts as a visitor. `summary.sh` and the watcher both load it. |

Nothing publishes a port. The tunnel is the only way in, so the site stays
unreachable even if the CT ends up with a routable address.

## Deploying

```bash
./site/deploy/redeploy.sh
```

It refuses to run without `dist/ClickGraft.zip`, and that has to be the
**stapled** zip `packaging/sign_and_notarize.sh` writes *after* notarizing. A
hand-made zip carries no ticket, and the page's promise about no security
warnings becomes false.

`packaging/check_release.py --artifact` then checks that ZIP against the tag
its own version names, not against HEAD. A page-only commit after a release
deploys; a ZIP not built from the tagged sources does not. A page-only
redeploy still needs that release's ZIP in `dist/`.

ZIPs up to and including 1.5.9 were built before they carried a record of their
sources, and are refused unless the one version is named. Until 1.6.0 ships,
every redeploy is:

```bash
CLICKGRAFT_ALLOW_LEGACY_ARTIFACT=1.5.9 ./site/deploy/redeploy.sh
```

After publishing, it checks that edge serves the page, the appcast and the
download it has just published, and that the public site does too. A page that
merely says ClickGraft is not enough: the one it replaced says that as well.

The tunnel, and the token it runs on, belong to edge: both are managed from
`~/JoshCode/elusive-edge`, and nothing in this directory reads or writes them.

## Traffic

```bash
./site/deploy/fetch-stats.sh
```

The question is "how much interest is there", and answering it does not require
knowing who anyone is:

- **No full addresses are ever written.** nginx truncates the client address
  before logging — IPv4 to its `/24`, IPv6 to its `/48`. Enough to tell two
  visitors apart on a given day, not enough to point at a person.
- **Query strings are dropped.** Campaign tags and referrer junk collect there
  and none of it is worth keeping.
- **No cookies, no JavaScript, no third party.** The page makes zero external
  requests, and the CSP enforces that.
- **Kept, not rotated.** A few lines a day; rotation only split the history
  across two files and made auditing an individual hit harder. `fetch-stats.sh`
  still reads `clickgraft-access.log.1` as well, because one rotation happened
  before this was turned off and those lines are real.
- The footer of the page says all of this in plain words.

The report is deliberately not served. `fetch-stats.sh` pulls it over SSH.

## Gotchas, both found the hard way

**Don't log to `access.log`.** The nginx image ships
`/var/log/nginx/access.log` as a symlink to `/dev/stdout`, and a volume mounted
over that directory inherits the symlink — so the file GoAccess reads stays
permanently empty while everything looks fine. Hence `clickgraft-access.log`.

**`$server_protocol` is already `HTTP/1.1`.** Writing `HTTP/$server_protocol`
produces `HTTP/HTTP/1.1`, which GoAccess parses as nothing at all.


## When a tool says it cannot reach the host

The PVE nodes are on a tailnet, so an unreachable host is an ordinary condition
here, not a crisis. Every read tool refuses rather than printing zeros:

```
✗ cannot reach root@… over SSH.
  Tailscale is not running on this Mac…
  NOTHING WAS READ. This is not "no traffic" …
```

That wording exists because the opposite once happened. `ssh` failed with its
stderr discarded, the log came back empty, and the summary printed a clean table
of zeros — indistinguishable from a quiet day. "No visitors" and "I could not
look" are opposite facts and must never render the same way.

`healthcheck.sh` has the mirror image of the problem: if every check returns
HTTP 000 it now tests whether this machine can reach the internet at all, so a
local outage is not reported as the site being down.

## This directory no longer describes a container

ClickGraft is served by the shared **edge** host (CT 136); see
`~/JoshCode/elusive-edge`. `nginx.conf`, `docker-compose.yml` and `stats/` were
deleted rather than left behind: config that is no longer authoritative invites
someone to edit it and wonder why nothing changed. Routing for this site lives
in edge's `nginx/conf.d/clickgraft.conf`, and the collector runs there as
`clickgraft-report`.

`redeploy.sh` now ships **content only** — `html/`, `collector/collector.py`,
`summary.sh`, `visitor-classify.awk` and `publish_site.py` into
`sites/clickgraft/` — and restarts nothing but ClickGraft's own collector. A
site deploy must never be able to take the other projects sharing that nginx
offline.

It is also the **only** deploy of the collector's code. Edge owns the
`clickgraft-report` service, its mount and the `/report` route, but keeps no
copy of `collector.py`, and its `redeploy.sh` names clickgraft in `PROJECT_OWNED`
so it will not ship one. It used to: that copy sat three weeks behind this one,
and an edge deploy would have put the old collector back. A change to the
collector ships from here and nowhere else.

It also re-runs `watch/install-watch.sh` at the end. The watcher is a systemd
unit on the CT rather than a container, so nothing else would ever update it: it
spent four days announcing downloads while silently dropping every visitor,
because a site change renamed the assets it looked for and no deploy touched it.

## Recovering a publication

`publish_site.py` checks the staged appcast against the staged download, then
exchanges `html/` in one step under a lock. Emptying it and copying the new
files in, as deploys used to, left every file missing for seconds: measured on
22 Sep 2026, a reader missed 11,654 of 17,392 reads that way and none in 150
exchanges. nginx sees the exchange because edge mounts the parent `sites/`
directory. If `html/` itself were mounted, nginx would stay on the old one, and
`redeploy.sh`'s check of what edge serves would fail.

If the filesystem refuses the one-step exchange, a `⚠` line says so and the
files are moved one at a time instead, each missing for a moment. CT 136 is on
ZFS, which supports the exchange from OpenZFS 2.2, but it has not yet been run
there: watch for that line on the first deploy.

What it replaced (`html/`, `collector.py`, `summary.sh`,
`visitor-classify.awk`) is kept together as `.previous-<UTC time>-<id>` in
`/opt/edge/sites/clickgraft`, and `ls` lists them oldest first. The newest ten
are kept. 8 to 10 Sep 2026 had five releases and nine commits to `site/` in
three days, so rolling back a bad release can mean reaching past several later
deploys. Each copy is under 1 MB. To put one back, inside the container:

```sh
python3 /opt/edge/sites/clickgraft/publish_site.py --restore \
  /opt/edge/sites/clickgraft/.previous-SELECTED /opt/edge/sites/clickgraft
```

Then restart only `clickgraft-report` if collector code changed, and run the full
healthcheck. The restore keeps the site it displaces as another `.previous-*`.
Never restart shared nginx or the tunnel to roll back ClickGraft content.

If publishing fails after the switch, the previous site is put back and the
upload is removed. If putting it back fails too, nothing is deleted. The `✗`
lines say where the previous site is and give the exact command that restores
it, using the copy of `publish_site.py` kept with it. So read what a `✗` said
before removing an `.incoming-*` directory: it may hold the previous site. A
refused or interrupted upload leaves neither `.incoming-*` in the site
directory nor `/tmp/clickgraft-stage-*` on the PVE node.
