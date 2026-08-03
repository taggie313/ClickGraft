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
