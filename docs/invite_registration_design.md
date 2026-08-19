# Invite-Based Registration — Architectural Design

Design plan for [#7](https://github.com/gfvandehei/PixelVault/issues/7).

**Status:** accepted. Every open question in §11 is now decided. §13 is the **API contract**:
modules are built in sequence by separate contributors, so no one may change a signature there
without editing this file first — the same rule `docs/upload_protocol.md` runs under.

---

## 1. What changes, and why

Registration today is a two-step ritual split across two people who are not in the same room:
an admin types an email into `/admin`, then tells the invitee out-of-band to go to `/register`
and type that *same* email, a username, and a password. Nothing connects the two halves — the
whitelist row is a passive assertion checked at the end of the form, and every mismatch
(typo, alias, `+tag`, different capitalisation) surfaces as the same dead-end message,
*"This email address has not been authorized to register."*

The target flow, from the issue's comments:

1. Admin adds an email. **An invite email sends automatically**, carrying a link.
2. Invitee clicks the link. It opens the registration form with the **email already filled in
   and not editable**; they choose a username and password and submit.
3. The link is **consumed** — single-use, and expiring on its own if never clicked.
4. **Registration is reachable only through such a link.** The "Sign up" link on the login
   page goes away.
5. The admin panel shows **per-invite state** and offers **resend**.

The architectural consequence is larger than the feature: `AllowedEmail` stops being a passive
whitelist and becomes a **credential with a lifecycle** (issued → sent → accepted / expired /
revoked), and the app acquires its **first outbound side effect** — SMTP. Both deserve their
own modules rather than more code inside route handlers.

---

## 2. Design principles applied

The repo already demonstrates the pattern this should follow: `uploads.py` holds the upload
session lifecycle as plain functions over the ORM, and `routes/share.py` is a thin HTTP layer
over it. Invites get the same treatment.

- **Domain logic out of routes.** `invites.py` never imports `flask.request` and never
  renders. It can be tested without an HTTP client, the way `uploads.py` can.
- **Transport does not know about invites.** `mailer.py` sends a message. It has no idea what
  an invite is, which makes it reusable for the next email the app needs (password reset,
  upload notifications, issue #11 comment notifications) without a rewrite.
- **Content is not transport.** Rendering "here is your invite" into a subject + text + HTML
  body is a third concern, in `emails.py`. Swapping SMTP for a provider API touches one file;
  rewording the invite touches a different one.
- **Config is env-driven and validated at boot**, consistent with `config.py`.
- **Migrations are additive `ALTER TABLE`**, consistent with `_run_migrations()`.

---

## 3. Module layout

```
src/pixelvault/
├── config.py            # (+) SMTP settings, PUBLIC_BASE_URL, invite TTL & cooldown
├── extensions.py        # (+) `mailer` singleton with init_app(app), beside db/login_manager/limiter
├── models.py            # (+) invite columns on AllowedEmail (see §4)
├── mailer.py            # NEW — transport only: build a Mailer from config, send a message
├── emails.py            # NEW — renders app messages (invite) from Jinja templates
├── invites.py           # NEW — invite lifecycle: issue, rotate, validate, consume, sweep
└── routes/
    ├── admin.py         # (~) add now issues+sends; new resend / revoke / copy-link actions
    └── auth.py          # (~) /register removed; /invite/<token> GET+POST added
templates/
├── email/
│   ├── invite.txt       # NEW — plain-text part (the part that always renders)
│   └── invite.html      # NEW — HTML part
├── register.html        # (~) becomes the invite acceptance form: email locked, token carried
├── login.html           # (~) "Sign up" link removed
└── admin.html           # (~) invite state column + resend / copy-link buttons
docs/
└── registration_invites.md   # NEW — operator doc (SMTP setup, troubleshooting, states)
tests/
├── test_invite_lifecycle.py  # NEW
├── test_invite_access.py     # NEW
└── test_mailer.py            # NEW
```

### Dependency direction

```
routes/admin.py ─┐
routes/auth.py ──┼──▶ invites.py ──▶ models.py
                 └──▶ emails.py ──▶ mailer.py ──▶ smtplib
                                └──▶ Jinja (templates/email/)
```

Nothing points back up. `invites.py` does not send email; the route asks `invites` for a token
and then asks `emails` to deliver it, so an invite can be issued without SMTP configured at all
(the copy-link fallback, §7.3).

---

## 4. Data model

**Recommendation: extend `AllowedEmail` rather than add an `Invitation` table.** An authorized
email and an outstanding invite are the same fact in this app — there is no case where one
exists without the other, so two tables would mean two rows to keep in sync and a join on every
admin page render. Keeping the table name `allowed_email` also means the migration is pure
`ADD COLUMN`, which is what `_run_migrations()` does well. (See §11 Q1 — a separate table wins
if you ever want invite *history* per address.)

New columns:

| Column | Type | Purpose |
|---|---|---|
| `token_hash` | `String(64)`, nullable, indexed | SHA-256 of the invite token. Null once accepted or for legacy rows. |
| `token_issued_at` | `DateTime`, nullable | When the current token was minted; expiry measured from here. |
| `expires_at` | `DateTime`, nullable | Denormalised for cheap querying and honest display in the admin table. |
| `prefill_username` | `String(64)`, default `''` | Optional admin-suggested username (issue comment 1). |
| `last_sent_at` | `DateTime`, nullable | Drives the resend cooldown and the "Sent 3 days ago" label. |
| `send_count` | `Integer`, default `0` | How many times an email went out; surfaces "resent 4×, still not accepted". |
| `last_send_error` | `String(256)`, default `''` | Truncated SMTP failure, shown to the admin. Empty on success. |
| `accepted_at` | `DateTime`, nullable | Set when the account is created. Non-null = terminal state. |
| `accepted_user_id` | `Integer`, FK `user.id`, nullable | Links the invite to the account it produced. |
| `invited_by_id` | `Integer`, FK `user.id`, nullable | Audit: which admin issued it. |

Derived state — a property on the model, not a stored column, so it cannot go stale:

```
accepted_at is not None            → ACCEPTED
token_hash is None                 → LEGACY      (pre-migration row; needs an invite issued)
now >= expires_at                  → EXPIRED
last_send_error                    → SEND_FAILED (token is valid; delivery is what failed)
last_sent_at is None               → ISSUED      (token exists, never emailed — copy-link path)
otherwise                          → SENT
```

`LEGACY` is not a cosmetic state. Existing production rows have no token, and once `/register`
is link-only they would be silently unusable. The admin panel must show them as
*"No invite sent"* with a **Send invite** button, and the release notes must say so.

### Token handling

- Generated with `secrets.token_urlsafe(32)` (256 bits) — brute force is not a threat model.
- **Only the SHA-256 hash is stored.** The plaintext exists in the email and, for the copy-link
  fallback, in one flash message. This matters because the token is a bearer credential that
  creates an account bound to a real person's email; a leaked DB backup or an admin-page
  screenshot should not be enough to use it. Plain SHA-256 (not bcrypt) is correct here: the
  secret is 256 random bits, so there is no dictionary to slow down.
- Lookup is `WHERE token_hash = sha256(presented)`, an indexed equality match — no scan, and no
  timing signal worth defending against beyond `hmac.compare_digest` on the final compare.
- **Resend rotates the token.** The consequence of hashing is that a link cannot be re-shown, so
  "resend" mints a new one and the old link stops working. That is the safer default anyway: a
  resend usually means the first link went somewhere it shouldn't have or is presumed lost.
  (§11 Q2 if you would rather the link stay stable.)
- Consumption is a single transaction: create `User`, set `accepted_at` / `accepted_user_id`,
  null out `token_hash`, commit. A double-submit or a shared link therefore loses the race
  cleanly on the unique constraint rather than creating two accounts.

---

## 5. `mailer.py` — transport

One small surface, several backends:

```python
class Mailer:
    def send(self, message: EmailMessage) -> None: ...   # raises MailError

class SMTPMailer(Mailer):  ...   # STARTTLS or implicit TLS, timeout-bounded
class ConsoleMailer(Mailer): ... # prints to the log — dev default, no config needed
class NullMailer(Mailer):  ...   # discards; MAIL_ENABLED=false
class MemoryMailer(Mailer): ...  # keeps a list; the test fixture
```

- `build_mailer(config)` picks one from env: SMTP host set → `SMTPMailer`; `MAIL_ENABLED=false`
  → `NullMailer`; otherwise `ConsoleMailer`. A dev checkout with no SMTP config keeps working,
  and the invite link is in the console where a developer can click it.
- **Gmail is the first relay, and nothing above `build_mailer` may learn that.** `SMTPMailer`
  stays generic — host, port, security mode, credentials, all from config — and Gmail's
  specifics (app password, `MAIL_FROM` forced to the account address because Google rewrites
  anything else, the 500/day cap) live in `docs/registration_invites.md` as one profile among
  several. Switching to Postmark or SES later is then an `.env` edit, and switching to a
  provider's **HTTP API** is a new `Mailer` subclass plus one line in `build_mailer` — no caller
  changes, because `emails.py` only ever sees `Mailer.send`. That seam is the entire reason
  transport is its own module.
- Registered as `mailer` in `extensions.py` with `init_app(app)`, matching `db` / `limiter`, so
  routes get to it the same way they get to everything else, and tests swap it in one place.
- Every send is bounded by `MAIL_TIMEOUT_SECONDS` (default 10) on the socket. Without it a
  hung SMTP server pins a Gunicorn thread — there are only 8.
- Failures raise `MailError`; the caller decides. `mailer.py` never flashes, never aborts, and
  **never logs the message body** (the invite token is in it).

### When the send happens

**Recommendation: synchronously, inside the admin request.** Invites are rare, admin-triggered,
and one-at-a-time; a 10-second worst case on a button an admin presses a handful of times a
month is a fair trade for the admin learning *immediately* whether delivery worked. The
alternative — a thread or an outbox table — buys throughput this app has no use for and costs a
polling UI, and 2×4 threads means a background sender competes with uploads for the same pool.
The recorded `last_send_error` plus a **Resend** button covers the failure case without a queue.
(§11 Q3.)

---

## 6. `emails.py` — content

```python
def build_invite_email(invite, token, base_url) -> EmailMessage
def send_invite(mailer, invite, token, base_url) -> None    # build + send + stamp the row
```

- Bodies render from `templates/email/invite.{txt,html}` via the app's Jinja env, so the copy
  lives with the other templates and is reviewable as text, not as a Python string.
- **`multipart/alternative`, and the text part is authored first.** The HTML part is the
  garnish; if it is ever malformed, the invite link still arrives intact.
- The link is built from **`PUBLIC_BASE_URL`**, not `url_for(_external=True)`. Behind
  Cloudflare → nginx → Gunicorn, the URL the app reconstructs is only as trustworthy as the
  forwarded headers, and an attacker-controlled `Host` header on the *add-email* request would
  otherwise steer an invite link at a domain they own. An explicit configured origin removes
  the question. Boot fails loudly if SMTP is enabled and `PUBLIC_BASE_URL` is unset.
- Nothing token-shaped is ever passed to a logger.

---

## 7. Request flows

### 7.1 Issuing (admin adds an email)

```
POST /admin/email/add   (admin, 60/hour)
  ├─ normalise + validate email (reuse RE_EMAIL — currently this route only checks for '@')
  ├─ already a User with that email?      → flash "already registered", stop
  ├─ already an AllowedEmail?             → flash "already invited", offer resend, stop
  ├─ invites.issue(email, note, prefill_username, invited_by=current_user)
  │     → row committed, plaintext token returned once
  ├─ emails.send_invite(...)              → on MailError: record last_send_error, flash the
  │                                          failure with a "copy link" fallback (§7.3)
  └─ redirect to /admin
```

The row is committed **before** the send is attempted. If SMTP is down the invite still exists
and is resendable; the alternative (rollback on send failure) throws away a valid invite because
of an unrelated outage.

### 7.2 Accepting

```
GET  /invite/<token>   (anonymous, 60/hour per IP)
  ├─ invites.validate(token) → invite | InvalidInvite | ExpiredInvite
  ├─ already logged in?  → log out or refuse; an invite must not attach to a live session
  ├─ stash the token in the session, redirect to GET /invite  (see §11 Q5)
  └─ render register.html: email shown read-only, username prefilled, token in a hidden field

POST /invite           (anonymous, 20/hour per IP)
  ├─ re-validate the token from the session — the GET's verdict is not carried over
  ├─ validate username / password exactly as today
  ├─ **email comes from the invite row, never from the form**
  ├─ single transaction: create User, mark invite accepted, null token_hash
  └─ login_user(), redirect to dashboard
```

The email is taken from the server-side row, not the posted field. Anything else lets a holder
of an invite for `alice@` register as `bob@` and defeats the whole point of the whitelist —
this is the single most important line in the feature.

### 7.3 Copy-link fallback

When SMTP fails, or is not configured at all (a self-hoster who does not want a mail relay),
the admin needs a way to hand over the link. `POST /admin/invite/<id>/link` **rotates** the
token and flashes the fresh URL once, with a "copy" button. This makes SMTP genuinely optional
rather than a hard dependency of the whole registration system, and it is the thing to reach
for when an invite lands in someone's spam folder.

### 7.4 Resend

`POST /admin/invite/<id>/resend` — rotates the token, re-sends, bumps `send_count`, stamps
`last_sent_at`. Rejected if `last_sent_at` is within `INVITE_RESEND_COOLDOWN_SECONDS`
(default 60). The cooldown is not primarily about the admin: an invite email is mail *this
server sends to a third party on request*, and an unthrottled resend button is a mail-bomb
primitive pointed at whatever address is typed in. It also protects the sending domain's
reputation with the relay.

### 7.5 Revoking

The existing `POST /admin/email/<id>/remove` keeps working and now also kills the token, since
the token lives on the row being deleted. For accepted invites, deletion should **not** cascade
to the user account — worth a confirm-dialog wording change ("this removes the invite record;
the account stays").

---

## 8. Removing public registration

- `/register` is deleted as a route. Requests to it → 404 (not a redirect to `/login`, which
  would imply the page moved). `templates/register.html` is repurposed as the invite acceptance
  form rather than deleted, since 90% of it is unchanged.
- `login.html` loses its "Don't have an account? Sign up" footer. Replacing it with a plain
  *"PixelVault is invite-only. Ask an admin for an invitation."* is honest and stops the
  support question before it is asked.
- **Known collateral — decided (§11 Q6):** every album route is `@login_required`, so a guest
  who receives a share link and has no account currently self-registers to view it. After this
  change that path closes, so `request_permission.html` must stop being a dead end: it gains
  copy naming the admin to contact for an invitation, sourced from a config value
  (`ADMIN_CONTACT`, defaulting to `MAIL_FROM`) rather than hard-coded. No public
  "request an invite" endpoint — that would be a new unauthenticated write surface to throttle
  and moderate, for a flow that happens a handful of times a year.
- The `AllowedEmail` check stays in the acceptance path as a belt-and-braces assertion even
  though a valid token already implies it — the row *is* the whitelist entry.

---

## 9. Configuration

| Variable | Default | Description |
|---|---|---|
| `PUBLIC_BASE_URL` | — | Canonical external origin, e.g. `https://photos.example.com`. **Required when mail is enabled.** |
| `MAIL_ENABLED` | `true` | `false` → `NullMailer`; invites are issued but nothing is sent. |
| `SMTP_HOST` | — | Unset → `ConsoleMailer` (link printed to the log). |
| `SMTP_PORT` | `587` | 465 implies implicit TLS, 587 STARTTLS. |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | — | Blank = unauthenticated relay. |
| `SMTP_SECURITY` | `starttls` | `starttls` \| `ssl` \| `none`. |
| `MAIL_FROM` | — | Envelope + header From. |
| `MAIL_FROM_NAME` | `PixelVault` | Display name. |
| `MAIL_TIMEOUT_SECONDS` | `10` | Socket timeout; bounds how long one Gunicorn thread is held. |
| `ADMIN_CONTACT` | falls back to `MAIL_FROM` | Address shown to accountless guests on `request_permission.html`. |
| `INVITE_TTL_HOURS` | `72` | Link lifetime from issue/rotate. |
| `INVITE_RESEND_COOLDOWN_SECONDS` | `60` | Minimum gap between sends for one invite. |

Validated at import in `config.py`; `create_app()` fails fast on the incoherent combinations
(mail enabled with no `MAIL_FROM`, SMTP configured with no `PUBLIC_BASE_URL`). `.env.example`,
`.env.prod`, both compose files and `docker/Dockerfile.prod`'s env passthrough all need the new
keys — the compose files enumerate variables explicitly, so anything not added there is silently
absent in production.

**No new Python dependency.** `smtplib` and `email.message` are stdlib; Flask-Mail would add a
dependency to wrap them and would still not give us the state machine, which is the actual work.
A future HTTP-API backend would add `requests`/`httpx` — one more reason to keep that behind the
`Mailer` interface rather than in the routes.

**Gmail profile** (the initial deployment): `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
`SMTP_SECURITY=starttls`, `SMTP_USERNAME` = the account, `SMTP_PASSWORD` = a 16-character
**app password** (not the account password — it requires 2FA to be enabled on the account), and
`MAIL_FROM` = that same account, since Google rewrites a `From` it does not own. Worth stating
in the operator doc: invites from a `@gmail.com` sender land in spam more often than from a
domain with SPF/DKIM, which is the argument for moving to a transactional provider later.

---

## 10. Rate limits, security, and testing

### Rate limits (new rows for the CLAUDE.md table)

| Endpoint | Limit | Key |
|---|---|---|
| `GET /invite/<token>` | 60/hour | IP (pre-auth) |
| `POST /invite` | 20/hour | IP |
| `POST /admin/invite/<id>/resend` | 30/hour | user |
| `POST /admin/invite/<id>/link` | 30/hour | user |

Per `extensions.rate_limit_key`, admin actions key on user id and the anonymous invite routes
fall back to IP — the same split `/login` and `/register` already use.

### Security checklist

- Email is server-side, never from the form (§7.2). The highest-value line in the change.
- Token hashed at rest; plaintext only in the email and one flash.
- Single-use, enforced in the same transaction as user creation.
- TTL-bounded; expiry measured from issue/rotate.
- Invite link never logged; `last_send_error` is truncated and carries no token.
- `PUBLIC_BASE_URL` removes `Host`-header influence over where the link points.
- Resend cooldown so the app is not a mail-bomb relay.
- Accepting while already logged in is refused, not silently merged.
- `Referrer-Policy: strict-origin-when-cross-origin` is already set, so a token in the URL does
  not leak cross-origin — but it *does* land in nginx access logs and browser history, which is
  what §11 Q5's session-stash redirect is about.

### Tests

Following `conftest.py`'s existing discipline (env stamped before `pixelvault` is imported, so
invite TTL and cooldown must be chosen there, not in a test):

| Module | Covers |
|---|---|
| `test_mailer.py` | Backend selection from config; timeout applied; `MailError` on refusal; `MemoryMailer` captures subject/recipients/both parts. |
| `test_invite_lifecycle.py` | Issue → send → accept; token single-use; rotation on resend invalidates the old link; TTL expiry; cooldown; state property across all six states incl. `LEGACY`. |
| `test_invite_access.py` | **Email cannot be overridden via the form**; invalid/garbage token; accepting twice; accepting while logged in; `/register` is gone; admin routes reject non-admins; rate limits. |

A `mailer` fixture swapping `MemoryMailer` into `extensions` gives every test a real assertion
on what would have been sent, with no network.

---

## 11. Decisions and open questions

### Settled

**Q2 — Token at rest: hashed.** Only the SHA-256 lands in the DB, so a leaked backup or an admin
screenshot cannot be used to register. The accepted cost: a link is unrecoverable, so **resend
and copy-link both rotate the token** and the previous link dies immediately.

**Q4 — Relay: Gmail now, swappable later.** `SMTPMailer` stays relay-agnostic; Gmail's app
password, forced `MAIL_FROM`, and daily cap are documented as one profile in the operator doc
(§9). Moving to Postmark/SES is an `.env` edit; moving to a provider's HTTP API is one new
`Mailer` subclass. Nothing above `build_mailer` may reference Gmail.

**Q6 — Accountless share-link guests: name the admin.** `request_permission.html` tells them who
to ask, using `ADMIN_CONTACT`. No public invite-request endpoint.

**Q9 — Legacy `allowed_email` rows: manual.** They render as `LEGACY` / "No invite sent" with a
**Send invite** button. Nothing is emailed without an admin click, so a forgotten address from a
year ago does not get a surprise invite on deploy day.

### Also settled (recommended defaults, accepted)

**Q1 — One table.** `AllowedEmail` is extended; no separate `Invitation` table. One row per
address, migration is pure `ADD COLUMN`, no join on the admin page. The cost accepted: an
address keeps only its *current* invite state, not a history of every rotation.

**Q3 — Synchronous send.** Inside the admin request, bounded by `MAIL_TIMEOUT_SECONDS`. The
admin learns immediately whether delivery worked. No thread, no outbox table, no polling UI.

**Q5 — Stash the token in the session, redirect to a clean URL.** `GET /invite/<token>`
validates, puts the token in `session['invite_token']`, and redirects to `GET /invite`, which
carries no secret. Keeps the token out of nginx access logs and browser history — the other half
of the protection hashing at rest gives us.

**Q7 — 72-hour TTL, expiring to a dead row.** An expired invite stays visible in the admin panel
as `EXPIRED` and needs an explicit **Resend**. No auto-reissue: that would silently re-email
someone, which is exactly what the Q9 decision rules out.

**Q8 — Prefill is a suggested username plus the admin-only note.** The invitee may change the
username. **No `is_admin` flag on invites** — admin accounts stay a deliberate, separate act, so
a mistyped checkbox on an invite form can never mint an administrator.

---

## 13. API contract

Signatures are fixed here so modules built in sequence fit together without rework.

### `mailer.py` — transport

```python
class MailError(RuntimeError): ...

class Mailer:                                     # ABC
    def send(self, message: EmailMessage) -> None

class SMTPMailer(Mailer):    # (host, port, username, password, security, timeout)
class ConsoleMailer(Mailer): # logs the rendered message; dev default
class NullMailer(Mailer):    # discards; MAIL_ENABLED=false
class MemoryMailer(Mailer):  # .outbox: list[EmailMessage]; the test fixture

def build_mailer() -> Mailer                      # chooses a backend from config
```

### `extensions.py` — how routes reach it

```python
class MailerProxy:
    def init_app(self, app) -> None               # build_mailer(), store at app.extensions['mailer']
    def send(self, message: EmailMessage) -> None # delegate to the current app's backend
    @property
    def backend(self) -> Mailer

mailer = MailerProxy()                            # beside db / login_manager / limiter
```

Tests swap a backend with `app.extensions['mailer'] = MemoryMailer()` — no monkeypatching of
module globals, so it survives the session-scoped `app` fixture.

### `models.py` — invite state

```python
class InviteState(str, Enum):
    ACCEPTED, LEGACY, EXPIRED, SEND_FAILED, ISSUED, SENT

class AllowedEmail(Base):
    @property
    def state(self) -> InviteState                # evaluated in the §4 order, never stored
    @property
    def is_pending(self) -> bool                  # ISSUED | SENT | SEND_FAILED
```

### `invites.py` — lifecycle (no Flask imports)

```python
class InviteError(Exception): ...
class InvalidInvite(InviteError): ...             # no such token
class ExpiredInvite(InviteError): ...             # past expires_at
class AlreadyAccepted(InviteError): ...           # consumed
class ResendTooSoon(InviteError): ...             # inside the cooldown

def hash_token(token: str) -> str                 # sha256 hexdigest, the stored form

def issue(session, email, *, note='', prefill_username='',
          invited_by_id=None, ttl_hours=INVITE_TTL_HOURS) -> tuple[AllowedEmail, str]
def rotate(session, invite, *, ttl_hours=INVITE_TTL_HOURS) -> str
def validate(session, token, *, now=None) -> AllowedEmail
def consume(session, invite, *, username, password) -> User
def mark_sent(session, invite, error='') -> None
def check_resend_allowed(invite, *, cooldown_seconds=INVITE_RESEND_COOLDOWN_SECONDS,
                         now=None) -> None
```

`issue` and `rotate` return the **plaintext token exactly once**; it is never readable again.
`consume` creates the user, stamps `accepted_at` / `accepted_user_id` and nulls `token_hash` in
one transaction.

Three behaviours are fixed here because they are seams where independently built modules drift:

* **`rotate` is the only renewal path.** Resend, copy-link, and the *Send invite* button on a
  `LEGACY` row all call it. `issue` is strictly for an address never seen before.
* **`rotate` does not clear `last_send_error`.** `mark_sent` does. The correct resend sequence is
  `check_resend_allowed` → `rotate` → send → `mark_sent(error=...)` **on both outcomes**; skipping
  it on the failure path leaves `send_count` undercounting and the panel showing no error.
* **A consumed link is indistinguishable from a typo.** `consume` nulls `token_hash`, so replaying
  a used link raises `InvalidInvite`, never `AlreadyAccepted`. The acceptance page must word that
  message for both audiences at once.

**Accepted limitation:** a duplicate address given to `issue`, and a username collision inside
`consume`, both raise the base `InviteError` rather than a dedicated subclass. Both are backstops
for a race — §7.1 has the admin route check for an existing `AllowedEmail` first, and the
acceptance route checks username availability first, exactly as `/register` does today. Two admins
adding one address in the same second is rare enough that a generic message is the right cost.

### `emails.py` — content

```python
def build_invite_email(invite, token, *, base_url, from_addr, from_name) -> EmailMessage
def send_invite(mailer, session, invite, token) -> None
```

`send_invite` builds, sends, and calls `mark_sent`. On `MailError` it records the error on the
row **and re-raises**, so the route can flash the truth and offer the copy-link fallback.

### Routes and endpoint names

| Method | Path | Endpoint |
|---|---|---|
| `GET` | `/invite/<token>` | `invite_link` — validate, stash in session, redirect |
| `GET` | `/invite` | `invite_form` |
| `POST` | `/invite` | `invite_submit` |
| `POST` | `/admin/invite/<int:entry_id>/resend` | `admin_resend_invite` |
| `POST` | `/admin/invite/<int:entry_id>/link` | `admin_invite_link` |

Session key: `session['invite_token']`.

---

## 12. Implementation order

Each step leaves the app working and testable.

1. **`mailer.py` + config + `test_mailer.py`.** No app behaviour changes; `ConsoleMailer` is the
   default so nothing needs SMTP yet.
2. **Model columns + `_run_migrations()` entries + `AllowedEmail.state` + schema doc.** Additive;
   every existing row reads as `LEGACY`.
3. **`invites.py` + lifecycle tests.** Pure domain logic, no routes touched.
4. **`emails.py` + the two email templates.** Renderable and assertable via `MemoryMailer`.
5. **Admin routes and panel UI** — issue-on-add, resend, copy-link, state column. At this point
   invites work end to end while `/register` still exists, so nobody is locked out mid-deploy.
6. **`/invite` acceptance routes + `register.html` rework + access tests.**
7. **Remove `/register` and the login-page link** — the one irreversible-feeling step, taken last
   and only once step 6 is verified in the Docker test container.
8. **Docs:** `docs/registration_invites.md`, plus CLAUDE.md (env table, rate limits, models,
   security notes, tests table), `docs/database_schema.md`, `.env.example`, `.env.prod`, compose
   files, README.
