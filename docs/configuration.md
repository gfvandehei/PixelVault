# Configuration Reference

Every environment variable PixelVault reads, what it does, and **what actually goes wrong when it
is set badly**. This is the complete list — if a variable is not here, the application does not
read it.

Configuration is loaded once, at import, by [`src/pixelvault/config.py`](../src/pixelvault/config.py).
Nothing re-reads the environment later, so **every change here needs a restart**, and a value that
is wrong is wrong for the life of the process rather than for one request.

Related documents:

- [`.env.example`](../.env.example) — a copy-and-fill template of the same variables.
- [`upload_operations.md`](upload_operations.md) — how the upload settings behave under load, and
  the per-hop limits (Cloudflare, nginx, Gunicorn) they have to agree with.
- [`upload_protocol.md`](upload_protocol.md) — the wire contract `UPLOAD_CHUNK_SIZE` feeds into.

---

## 1. How settings are loaded

```
.env / .env.prod / compose environment:  →  os.environ  →  config.py (import time)  →  app.config
```

Three consequences worth knowing before you debug a setting that "isn't taking effect":

1. **The compose files enumerate variables explicitly.** A variable added to `.env.prod` but not
   listed in `docker/prod.docker-compose.yml` never reaches the container. This is the most common
   way a correct value has no effect in production.
2. **`config.py` binds module-level constants at import.** Some are captured further still — as
   default argument values, or copied into `app.config` — so nothing short of a restart moves them.
3. **Unset is not the same as empty for the mail settings.** An empty `SMTP_HOST` selects the
   console backend; a wrong one selects SMTP and fails at send time.

The app validates the mail settings at boot and refuses to start on an incoherent combination
(§5.3). Everything else is trusted and will fail later, in the way described in its row.

---

## 2. Core & security

| Variable | Default | Required |
|---|---|---|
| `SECRET_KEY` | random per process | **yes, in production** |
| `HTTPS` | `false` | no |
| `FLASK_DEBUG` | `false` | no |
| `PORT` | `5000` | no |
| `ENV_FILE` | — | no |

**`SECRET_KEY`** — signs the session cookie. The default is `os.urandom(32).hex()`, generated
fresh on every process start, which means: with two Gunicorn workers, each worker signs with a
*different* key, so a logged-in user is randomly logged out depending on which worker answers, and
every restart logs everyone out. Set it. A leaked key is worse than a rotating one — anyone holding
it can forge a session cookie for any user, including an admin, without touching a password.
Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`.

**`HTTPS`** — set `true` when the app is served over TLS. It turns on `Secure` on the session
cookie and adds an HSTS header. Left `false` behind real HTTPS, the session cookie is willing to
travel over plaintext, so a single downgraded request leaks it. Set `true` on a deployment that is
*not* fully HTTPS and browsers will discard the cookie entirely — you get an app that cannot hold a
login, which is the confusing failure this variable produces.

**`FLASK_DEBUG`** — `true` enables the Werkzeug debugger. In production that is a remote code
execution console attached to any traceback. Never `true` on a reachable host. Only read by
`app.py` (the dev entry point); Gunicorn does not consult it.

**`PORT`** — the port `app.py` binds when run directly. Gunicorn's bind comes from its own command
line, so changing this does not move the production listener.

**`ENV_FILE`** — path to the `.env` file `app.py` loads via python-dotenv. Unset means dotenv's
default search. Not used in the Docker images, which pass real environment variables instead.

---

## 3. Storage & database

| Variable | Default | Required |
|---|---|---|
| `UPLOAD_FOLDER` | `./uploads` | no (yes in production) |
| `DATABASE_URL` | `sqlite:///pixelvault.db` | no (yes in production) |
| `DATA_DIRECTORY` | — | no |
| `FLASK_TEMPLATES_FOLDER` | `<repo>/templates` | no |
| `FLASK_STATIC_FOLDER` | `<repo>/static` | no |

**`UPLOAD_FOLDER`** — where uploaded media, thumbnails, and the `partials/` sub-directory of
in-flight `.part` files live. **This is the user data.** Point it at a path outside the container's
writable layer and back it up; a relative default inside a container means every uploaded photo
disappears with `docker compose down`. Changing it on a running deployment does not move existing
files, so every previously stored photo 404s at `/media/<filename>` while the database still lists
it.

**`DATABASE_URL`** — SQLAlchemy URI. For an absolute SQLite path you need **four** slashes:
`sqlite:////data/pixelvault.db`. Three slashes is a *relative* path resolved against the process's
working directory, which is the classic way to end up with a second, empty database and an app that
appears to have lost every account. The upload subsystem assumes SQLite semantics (WAL pragmas in
`extensions.py`); another backend will work but is untested.

**`DATA_DIRECTORY`** — the instance directory the database is placed in. Consumed by the compose
files and by `scripts/migrate_heic.py` (which derives `DATABASE_URL` from it when that is unset),
not by `config.py` directly. Setting it without `DATABASE_URL` in an environment that does not do
that derivation leaves the database at the default relative path.

**`FLASK_TEMPLATES_FOLDER` / `FLASK_STATIC_FOLDER`** — override the template and static roots.
Only useful when the package is installed somewhere other than the repo checkout; wrong values
surface as `TemplateNotFound` on the first page render.

---

## 4. Uploads

Full operational treatment, including the per-hop limits these must agree with, is in
[`upload_operations.md`](upload_operations.md).

| Variable | Default | Required |
|---|---|---|
| `MAX_UPLOAD_MB` | `500` | no |
| `UPLOAD_CHUNK_SIZE` | `8388608` (8 MiB) | no |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | no |
| `MAX_CONCURRENT_UPLOADS_PER_USER` | `10` | no |
| `MAX_INFLIGHT_UPLOAD_MB_PER_USER` | `2048` | no |

**`MAX_UPLOAD_MB`** — becomes `MAX_CONTENT_LENGTH`. It bounds a single request body *and* is
checked against the declared total size when a chunked upload is initialised — without that second
check, chunking would leave this bounding only one 8 MiB slice. Set below what users actually
upload and large files are refused with a 413; set very high and it stops being a meaningful
defence, since the per-user quotas below become the only thing bounding disk.

**`UPLOAD_CHUNK_SIZE`** — the slice size handed to the client at `init`. It must stay **well under
the smallest request-body cap in front of the app** — Cloudflare's free plan rejects bodies over
100 MB at the edge, and the origin never sees the request, so an oversized chunk presents as a
browser stall with nothing in any server log. Larger chunks mean fewer round trips but more data
re-sent when one fails; smaller means more database writes, since every accepted chunk commits an
`UPDATE`.

**`UPLOAD_SESSION_TTL_HOURS`** — how long a partial upload stays resumable before the sweep
reclaims its row and `.part` file. Too short and a phone that loses signal during a large video
cannot resume, so the whole transfer restarts. Too long and abandoned partials accumulate on disk
against the per-user quota, so a user who abandons uploads can lock themselves out of starting new
ones for the length of the TTL.

**`MAX_CONCURRENT_UPLOADS_PER_USER` / `MAX_INFLIGHT_UPLOAD_MB_PER_USER`** — the load-bearing
defence against a client filling the disk. They must not be reasoned about as "rate limiting": the
rate limiter lives in `memory://` per worker and resets on every deploy, so it bounds nothing
durable. These are DB-backed and do. The byte cap is deliberately tighter than
`MAX_CONCURRENT_UPLOADS_PER_USER × MAX_UPLOAD_MB` (5 GB at the defaults) — that product is the
worst case the session count alone would allow, and shrinking it is the entire point. Raise them
and the worst case per user rises with them.

---

## 5. Mail & invites

Invites are the app's only outbound side effect. Transport lives in
[`src/pixelvault/mailer.py`](../src/pixelvault/mailer.py) and is deliberately relay-agnostic:
moving between Gmail, Postmark, SES, or a relay on localhost is an edit to this section and no code
change.

| Variable | Default | Required |
|---|---|---|
| `PUBLIC_BASE_URL` | — | **yes, once SMTP is configured** |
| `MAIL_ENABLED` | `true` | no |
| `SMTP_HOST` | — | no |
| `SMTP_PORT` | `587` | no |
| `SMTP_USERNAME` | — | no |
| `SMTP_PASSWORD` | — | no |
| `SMTP_SECURITY` | `starttls` | no |
| `MAIL_FROM` | — | **yes, once SMTP is configured** |
| `MAIL_FROM_NAME` | `PixelVault` | no |
| `MAIL_TIMEOUT_SECONDS` | `10` | no |
| `ADMIN_CONTACT` | falls back to `MAIL_FROM` | no |
| `INVITE_TTL_HOURS` | `72` | no |
| `INVITE_RESEND_COOLDOWN_SECONDS` | `60` | no |

### 5.1 Which backend you get

`build_mailer()` picks exactly one, in this order:

| Condition | Backend | Behaviour |
|---|---|---|
| `MAIL_ENABLED=false` | `NullMailer` | Messages are discarded. Invites are still issued and still usable via the admin copy-link button. |
| `SMTP_HOST` set | `SMTPMailer` | Real delivery. |
| otherwise | `ConsoleMailer` | The whole message, link included, is written to the application log. |

`MAIL_ENABLED=false` is checked **first** on purpose: it is an explicit instruction to stop sending,
so it has to win on a host whose `.env` still carries working credentials.

`ConsoleMailer` is the no-configuration default, which is what lets a dev checkout complete an
invite flow with no relay — you read the link out of the log and open it. It is also the one place
in the codebase that logs a message body; everywhere else that is forbidden, because the invite
token in the body is a bearer credential that creates an account.

### 5.2 The variables

**`PUBLIC_BASE_URL`** — the canonical external origin invite links are built from, e.g.
`https://photos.example.com`. Deliberately *not* derived from the request (`url_for(_external=True)`):
behind Cloudflare → nginx → Gunicorn the reconstructed URL is only as trustworthy as the forwarded
headers, so an attacker-controlled `Host` header on the add-email request could otherwise mint an
invite pointing at a domain they control — and the recipient would have no way to tell. Set it to
the origin users actually type. A trailing slash is stripped automatically. Point it at the wrong
host and every invite email is undeliverable in practice: the link resolves somewhere else.

**`MAIL_ENABLED`** — `false` turns off delivery without turning off invites. The right setting for
an operator who does not want to run a relay and is happy to hand links over by other means.

**`SMTP_HOST`** — the relay. Empty means no relay, which is a supported configuration, not an
error. Wrong or unreachable and every invite send fails; the invite itself still exists (the row is
committed before the send is attempted) and the admin panel shows the failure with a **Resend**
button.

**`SMTP_PORT`** — 587 for STARTTLS, 465 for implicit TLS, 25 for an unauthenticated local relay.
A port that disagrees with `SMTP_SECURITY` is the most common mail misconfiguration: 465 with
`starttls` hangs until `MAIL_TIMEOUT_SECONDS` (the server is waiting for a TLS handshake while the
client sends plaintext), and 587 with `ssl` fails the handshake immediately.

**`SMTP_USERNAME` / `SMTP_PASSWORD`** — blank username means no `AUTH` command at all, for a relay
that authenticates by network location. The password is the one value here that is **not** stripped
of surrounding whitespace, because Gmail app passwords are usually pasted with their grouping
spaces and a password may legitimately end in one.

**`SMTP_SECURITY`** — `starttls` | `ssl` | `none`. With `starttls` the connection is upgraded
*before* `AUTH`, so credentials never cross a plaintext socket. `none` sends everything, including
the password, in the clear — only defensible for a relay on `localhost`. An unrecognised value is a
boot failure, not a send-time one.

**`MAIL_FROM`** — envelope and header sender. Empty with SMTP configured is a boot failure, because
every relay rejects a message with no sender and the failure would otherwise appear only when the
first invite is sent. See the Gmail profile below for why this is not free to choose.

**`MAIL_FROM_NAME`** — display name beside the address. Cosmetic.

**`MAIL_TIMEOUT_SECONDS`** — socket timeout for the entire SMTP conversation, connect included.
Sends happen synchronously inside the admin's request and production runs 2 workers × 4 threads, so
this is what stands between a relay that accepts a TCP connection and then goes quiet and one of
only eight threads being held for the OS default — minutes, not seconds. Raise it and you trade
thread availability for patience with a slow relay; lower it much below 10 and legitimately slow
handshakes start failing.

**`ADMIN_CONTACT`** — the address shown to share-link visitors who have no account, so they know
who to ask for an invitation. Registration is link-only, so without a usable value here that page
is a dead end. Defaults to `MAIL_FROM`.

**`INVITE_TTL_HOURS`** — link lifetime, measured from issue or resend. Long values are friendlier
to someone who checks mail weekly, and are also a longer window in which a link sitting in a
forwarded message, a shared mailbox, or a mail archive is still usable by whoever finds it.

**`INVITE_RESEND_COOLDOWN_SECONDS`** — minimum gap between two sends of one invite. Not an
anti-annoyance measure: an invite is mail *this server sends to a third party on request*, so a
resend button with no cooldown is a mail-bomb primitive pointed at whatever address was typed in,
and the sending domain's standing with the relay is what pays for it. Setting it to `0` removes
that protection.

### 5.3 Boot-time validation

`create_app()` refuses to start on a mail configuration that would only fail later, when an admin
has already promised someone an email:

| Combination | Result |
|---|---|
| `SMTP_HOST` set, `MAIL_FROM` empty | `RuntimeError` at boot |
| `SMTP_HOST` set, `PUBLIC_BASE_URL` empty | `RuntimeError` at boot |
| `SMTP_HOST` set, `SMTP_SECURITY` not one of the three modes | `RuntimeError` at boot |
| No `SMTP_HOST` at all | **Boots normally** on `ConsoleMailer` |
| `MAIL_ENABLED=false`, anything else | **Boots normally** on `NullMailer` |

The last two rows are the point: a dev checkout with no mail configuration must still start. The
checks fire only once someone has begun configuring a relay and stopped halfway.

### 5.4 Gmail profile

Gmail is the first relay this app was deployed against. Nothing in the code knows that — these are
settings, and switching relays is a `.env` edit.

```ini
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=<16-character app password>
MAIL_FROM=you@gmail.com
```

Four things to know before you use it:

1. **`SMTP_PASSWORD` must be an app password, not the account password.** Google rejects the
   account password over SMTP outright. Generating one **requires 2-Step Verification to be enabled
   on the account first** — the app-password page does not exist otherwise, which is why this looks
   like a missing feature rather than a prerequisite.
2. **`MAIL_FROM` must be the `SMTP_USERNAME` account.** Google rewrites a `From` header it does not
   own, so a different address does not fail loudly — it is silently replaced, and your carefully
   chosen sender never reaches anyone.
3. **Roughly 500 messages per day** on a consumer account. Ample for invites; the ceiling to
   remember if this mailer is later reused for notifications.
4. **Deliverability is the real limitation.** Mail from a `@gmail.com` sender for a domain with no
   matching SPF/DKIM records lands in spam noticeably more often than mail from a domain that
   authenticates its own sending. The copy-link fallback in the admin panel exists partly for this.
   If invites routinely go missing, that is the signal to move to a transactional provider
   (Postmark, SES, Resend) on your own domain — which is a change to this section, not to any code.

---

## 6. Reverse proxy

| Variable | Default | Required |
|---|---|---|
| `TRUSTED_PROXY_COUNT` | `1` | no |

**`TRUSTED_PROXY_COUNT`** — the number of proxies that append to `X-Forwarded-For` before a request
reaches the app, handed to `ProxyFix`. Cloudflare → nginx → app is **2**, *if* your nginx uses
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`.

Setting it **higher than the true hop count is a security hole, not a tuning mistake**: the
client's own `X-Forwarded-For` header then survives into the region `ProxyFix` trusts, so a caller
can name any address it likes and choose which rate-limit bucket it is charged under — which is to
say, it can have a fresh bucket per request. **When unsure, set it too low.** The cost of too low
is that everyone behind the proxy shares one bucket; the cost of too high is that the limiter can
be bypassed entirely.

Rate limits on authenticated routes key on user id rather than address (see `rate_limit_key` in
`extensions.py`), so this matters most for the pre-auth routes: login, and the invite links.

### 6.1 The hop count is only true if the hops cannot be skipped

`TRUSTED_PROXY_COUNT` describes a chain. It says nothing about whether a caller is obliged to
*use* that chain, and a correct value protects nothing if the app can also be reached directly.
That was the actual bug in [#42](https://github.com/gfvandehei/PixelVault/issues/42): the value
was right, and `docker/prod.docker-compose.yml` published the origin on `0.0.0.0:5000`, so a
caller could go around nginx and hand its own `X-Forwarded-For` straight to `ProxyFix` — which,
trusting one hop, read it as the client address. The header is not even parsed as an address, so
`X-Forwarded-For: bucket-0001` is a valid rate-limit identity and the next request can be
`bucket-0002`. Login's 20/hour, the only brute-force defence in the app (there is no lockout and
no captcha), stops existing at that point.

The app container now publishes on `127.0.0.1:5000` only. Two things follow that are easy to get
wrong:

1. **The reverse proxy must run on the same host as the container.** It reaches the app at
   `http://127.0.0.1:5000`. If your proxy is on a different machine, do not go back to
   `0.0.0.0` — bind the published port to the specific private interface the proxy connects
   from (`10.0.0.5:5000:5000`), so the origin is still unreachable from everywhere else.
2. **A host firewall is not a substitute.** Docker publishes a port with a DNAT rule in
   iptables' `DOCKER` chain, which is consulted before the `INPUT` chain `ufw` and `firewalld`
   write into. `ufw deny 5000` on a `0.0.0.0`-published container port denies nothing. This is
   why the fix is the binding itself and not a firewall rule.

To confirm the origin is closed, from a machine that is not the VPS:

```bash
curl -sS --max-time 5 http://YOUR_VPS_IP:5000/login   # must fail to connect
```

and from the VPS itself, that the proxy's upstream is still there:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/login   # 200
```

### 6.2 Recommended: recover the real client IP from Cloudflare

Closing the origin fixes the bypass but leaves the other half of #42 in place. Through the
intended chain, `x_for=1` makes `ProxyFix` take the entry nginx wrote, which behind Cloudflare is
**Cloudflare's edge address, not the visitor's**. That is the safe direction — nobody can choose
it — but every visitor then shares a single rate-limit bucket, so the 20/hour login limit is
effectively global and one busy visitor locks out everyone else.

The fix belongs in the VPS nginx config, not in the app: have nginx replace `$remote_addr` with
Cloudflare's `CF-Connecting-IP`, but **only for connections that actually came from Cloudflare** —
`set_real_ip_from` entries for every published Cloudflare range, refreshed from
`https://www.cloudflare.com/ips-v4` and `ips-v6`. Trusting the header unconditionally would
reintroduce exactly the bug this section is about, one layer up: any direct caller could then set
`CF-Connecting-IP` itself. With `real_ip_header CF-Connecting-IP` and `real_ip_recursive on`,
nginx's `$proxy_add_x_forwarded_for` starts from the visitor's address and
`TRUSTED_PROXY_COUNT=1` is the correct value for the app.

Writing that config is tracked in
[#28](https://github.com/gfvandehei/PixelVault/issues/28), which owns the VPS-side nginx hop.
`conf/nginx.conf` in this repo is the *bundled container* nginx and is not the file serving
production — see the note in [upload_operations.md §2](upload_operations.md#note-on-the-nginx-hop).

---

## 7. Admin bootstrap

| Variable | Default | Required |
|---|---|---|
| `ADMIN_USERNAME` | — | no |
| `ADMIN_EMAIL` | — | no |
| `ADMIN_PASSWORD` | — | no |

Used by `flask --app app create-admin` and by `scripts/create_admin.py`. **`create_app()` also
seeds an admin at startup whenever `ADMIN_EMAIL` is non-empty**, which is what the test container
relies on — and is worth knowing before you leave these set on a long-lived deployment, since the
password sits in the environment of every process for as long as they are there. Set them for the
first boot, then remove them.

Registration is invite-only, so these are the only way to create the first account.

---

## 8. Quick reference

| Variable | Default | Section |
|---|---|---|
| `SECRET_KEY` | random per process | [Core](#2-core--security) |
| `HTTPS` | `false` | [Core](#2-core--security) |
| `FLASK_DEBUG` | `false` | [Core](#2-core--security) |
| `PORT` | `5000` | [Core](#2-core--security) |
| `ENV_FILE` | — | [Core](#2-core--security) |
| `UPLOAD_FOLDER` | `./uploads` | [Storage](#3-storage--database) |
| `DATABASE_URL` | `sqlite:///pixelvault.db` | [Storage](#3-storage--database) |
| `DATA_DIRECTORY` | — | [Storage](#3-storage--database) |
| `FLASK_TEMPLATES_FOLDER` | `<repo>/templates` | [Storage](#3-storage--database) |
| `FLASK_STATIC_FOLDER` | `<repo>/static` | [Storage](#3-storage--database) |
| `MAX_UPLOAD_MB` | `500` | [Uploads](#4-uploads) |
| `UPLOAD_CHUNK_SIZE` | `8388608` | [Uploads](#4-uploads) |
| `UPLOAD_SESSION_TTL_HOURS` | `24` | [Uploads](#4-uploads) |
| `MAX_CONCURRENT_UPLOADS_PER_USER` | `10` | [Uploads](#4-uploads) |
| `MAX_INFLIGHT_UPLOAD_MB_PER_USER` | `2048` | [Uploads](#4-uploads) |
| `PUBLIC_BASE_URL` | — | [Mail](#5-mail--invites) |
| `MAIL_ENABLED` | `true` | [Mail](#5-mail--invites) |
| `SMTP_HOST` | — | [Mail](#5-mail--invites) |
| `SMTP_PORT` | `587` | [Mail](#5-mail--invites) |
| `SMTP_USERNAME` | — | [Mail](#5-mail--invites) |
| `SMTP_PASSWORD` | — | [Mail](#5-mail--invites) |
| `SMTP_SECURITY` | `starttls` | [Mail](#5-mail--invites) |
| `MAIL_FROM` | — | [Mail](#5-mail--invites) |
| `MAIL_FROM_NAME` | `PixelVault` | [Mail](#5-mail--invites) |
| `MAIL_TIMEOUT_SECONDS` | `10` | [Mail](#5-mail--invites) |
| `ADMIN_CONTACT` | `MAIL_FROM` | [Mail](#5-mail--invites) |
| `INVITE_TTL_HOURS` | `72` | [Mail](#5-mail--invites) |
| `INVITE_RESEND_COOLDOWN_SECONDS` | `60` | [Mail](#5-mail--invites) |
| `TRUSTED_PROXY_COUNT` | `1` | [Proxy](#6-reverse-proxy) |
| `ADMIN_USERNAME` | — | [Admin](#7-admin-bootstrap) |
| `ADMIN_EMAIL` | — | [Admin](#7-admin-bootstrap) |
| `ADMIN_PASSWORD` | — | [Admin](#7-admin-bootstrap) |
