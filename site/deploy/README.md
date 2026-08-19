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
| `setup-ct.sh` | One-time provisioning. Run on the PVE node. |
| `redeploy.sh` | Push the page + `dist/ClickGraft.zip` and restart. Run from a checkout. |
| `_common.sh` | Shared preflight. Refuses to report numbers it could not read. |
| `fetch-stats.sh` | Read the traffic numbers over SSH. `--html` also pulls the GoAccess report. |
| `docker-compose.yml` · `nginx.conf` | The stack. |
| `summary.sh` · `stats/run-goaccess.sh` | Traffic analysis, run inside the CT. |

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

The tunnel token lives only in `/opt/clickgraft/.env` on the CT, mode 600. It is
never committed and never copied off.

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

`redeploy.sh` now ships **content only** — `html/`, `collector/collector.py` and
`summary.sh` into `sites/clickgraft/` — and restarts nothing but ClickGraft's own
collector. A site deploy must never be able to take the other projects sharing
that nginx offline.

It also re-runs `watch/install-watch.sh` at the end. The watcher is a systemd
unit on the CT rather than a container, so nothing else would ever update it: it
spent four days announcing downloads while silently dropping every visitor,
because a site change renamed the assets it looked for and no deploy touched it.
