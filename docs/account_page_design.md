# Account Page — Design and Implementation Plan

Design plan for [#31](https://github.com/gfvandehei/PixelVault/issues/31).

**Status:** accepted. Scope is fixed by §1; §2 is the part worth reading before touching any
of it, because everything difficult in this feature follows from one decision about cookies.

---

## 1. Scope

A signed-in user gets one page showing their username and email, and a form to change their
password. The issue is three lines long, so four questions were settled before planning:

| Question | Decision | Consequence |
|---|---|---|
| What is editable? | **Password only** | Username and email render read-only. No uniqueness checks, no email re-verification, no ripple through `uploader_name` or share links. |
| Other sessions after a change? | **Keep current, evict the rest** | Needs a per-user session token, a `get_id()` override, and a stricter `user_loader`. Everything difficult in this plan comes from this row. |
| Notify by email? | **Yes, non-fatally** | A second message type in `emails.py`. A dead relay must never undo a password that has already been committed. |
| Admin / forgot-password reset? | **Out of scope** | Filed as [#33](https://github.com/gfvandehei/PixelVault/issues/33) — emailed self-service reset, built on the mechanism this issue adds. |

No new environment variables. One migration, one backfill.

---

## 2. The session model, and why it has to change

Flask-Login stores whatever `User.get_id()` returns in the session cookie and, for a remembered
login, in `remember_token`. PixelVault inherits the default, which returns the primary key. A
cookie saying `3` stays valid forever: it survives a password change, because nothing in it
depends on the password.

So "changing my password logs out my other devices" needs the cookie to carry something that can
be rotated. One opaque per-user value does it:

```python
# src/pixelvault/models.py — on User
session_token: Mapped[str] = mapped_column(
    String(36), nullable=False, default=lambda: str(uuid.uuid4())
)

def get_id(self):
    # What Flask-Login writes into the session cookie and the remember cookie.
    return f"{self.id}:{self.session_token}"

def rotate_session_token(self):
    self.session_token = str(uuid.uuid4())
```

The loader then has to actually check it — an override of `get_id` with a loader that ignores the
second half buys nothing:

```python
# src/pixelvault/extensions.py
@login_manager.user_loader
def load_user(user_id):
    ident, sep, token = str(user_id).partition(':')
    if not sep:
        return None   # a cookie minted before this feature; see below
    try:
        user = db.session.get(User, int(ident))
    except ValueError:
        return None
    if user is None or not secrets.compare_digest(user.session_token, token):
        return None
    return user
```

Rotation now invalidates the session cookie *and* the remember cookie on every other device in
one write, with no server-side session store to add.

### Everyone is logged out once, on deploy

Existing cookies hold a bare `3`, which the loader above rejects. The alternative —
grandfathering bare ids — would mean a stolen pre-deploy cookie survives the very password change
made to revoke it, so the feature would ship already lying. One forced re-login is the honest
price. It belongs in the changelog and in any deploy note: an operator who hears about it from a
user is an operator who thinks they have been breached.

### `tests/conftest.py` logs in by hand

The `login()` helper writes `sess["_user_id"] = str(user_ref.id)` to skip 600k PBKDF2 rounds per
test. That is an invalid identity under the new loader, so it must write
`f"{id}:{session_token}"`, and the `Ref` built by `_make_user` must carry the token. A one-line
change that otherwise breaks every authenticated test in the suite at once.

---

## 3. Implementation order

Ordered by dependency: each step compiles and its tests pass before the next begins. Steps 1–2
are the only ones that touch existing behaviour.

### 1. Give the user a rotatable session token — `models.py`, `extensions.py`

Add the column, `get_id()`, and `rotate_session_token()` to `User`; tighten `load_user` as above.
Document on the column *why* it exists — it looks like dead weight to anyone who has not read
this file, and a well-meaning cleanup that drops the `get_id` override silently restores immortal
cookies with no test failing.

### 2. Migrate, then backfill — `__init__.py` / `_run_migrations()`

SQLite refuses a non-constant `DEFAULT` on `ADD COLUMN`, so the column arrives with a literal
default and every existing row is then given its own value — the same two-move pattern
`album.view_token` already uses at the bottom of that function.

```python
"ALTER TABLE user ADD COLUMN session_token VARCHAR(36) NOT NULL DEFAULT ''"

# …then, beside the existing view_token backfill:
users = db.session.query(User).filter(User.session_token == '').all()
for u in users:
    u.session_token = str(uuid.uuid4())
if users:
    db.session.commit()
```

A shared `''` across all rows would make every user's cookie interchangeable, so the backfill is
load-bearing, not tidying.

### 3. The routes — `routes/account.py` (new)

A new route module, registered in `routes/__init__.py`, per the house rule that a feature gets
its own file. Two endpoints: `GET /account` renders, `POST /account/password` acts.

```python
@app.route('/account/password', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def account_change_password():
    current = request.form.get('current_password', '')
    new     = request.form.get('new_password', '')
    confirm = request.form.get('confirm_password', '')

    # Length first, always: check_password() spends 600k PBKDF2 rounds on
    # whatever it is handed, so an unbounded field is a CPU sink.
    if len(current) > MAX_PASSWORD_LEN or not current_user.check_password(current):
        # … flash 'Your current password is incorrect.' and re-render

    # … the four new-password rules from §4 …

    user = db.session.get(User, current_user.id)
    user.set_password(new)
    user.rotate_session_token()          # every other cookie dies here
    db.session.commit()                  # commit BEFORE sending, as invites do

    # The current browser's cookie holds the old token now. Re-issue it, or the
    # user is logged out by their own password change on the next request.
    login_user(user, remember=bool(request.cookies.get('remember_token')))

    try:
        emails.send_password_changed(mailer, user)
    except MailError as exc:
        logger.warning("Password-changed notice failed for user %s: %s", user.id, exc)
        flash('Your password was changed, but the confirmation email could not be sent.', 'info')
```

Three ordering rules, each with a failure mode behind it: length-check before hash (CPU), commit
before send (a relay outage must not lose a committed password), and `login_user` after rotation
(otherwise the user's own browser is evicted along with the attacker's).

### 4. The page — `templates/account.html` (new), `templates/base.html`

Two stacked cards in the existing `.auth-card` idiom: *Your account* (username, email, member
since, and an admin badge when applicable — all read-only, rendered from `current_user`) and
*Change password* (current, new, confirm, with `autocomplete="current-password"` /
`"new-password"` so password managers behave).

The email field carries the same settled-not-broken styling the invite form uses, plus one line
of hint text saying an address change means asking an admin — otherwise the read-only field
generates a support question. Add **Account** to the nav dropdown in `base.html`, above *Log
out*; that dropdown is hidden under 700px, so the page must also be reachable from the dashboard.

### 5. The notification email — `emails.py`, `templates/email/password_changed.{txt,html}`

`build_password_changed_email(user, *, changed_at, …)` and `send_password_changed(mailer, user)`,
mirroring the invite pair: text part authored first via `set_content`, HTML added as the
alternative, `Date` and `Message-ID` set explicitly.

- **No link, no token.** There is no reset flow yet, so the message says what changed, when
  (UTC, labelled), and to contact `ADMIN_CONTACT` if it was not them.
- **No `mark_sent` equivalent.** That bookkeeping belongs to invites; this message has no row and
  no resend.
- **Unlike the invite, it must not raise on missing config.** `PUBLIC_BASE_URL` is decorative
  here, and with `MAIL_FROM` empty the sender cannot be built — so `send_password_changed` logs
  and returns rather than raising, and the password change stands regardless.

### 6. Tests — `tests/test_account.py` (new), `tests/conftest.py`

Fix `conftest.login()` first (§2) — the whole suite is red until it is. Then the new module (§5).

### 7. Documentation — `CLAUDE.md`, `docs/database_schema.md`, `CHANGELOG.md`

See §6. No new environment variables, so `docs/configuration.md` is untouched.

---

## 4. Rules and wording

The password rules are exactly the ones `invite_submit` enforces, down to the sentences — a rule
that quietly relaxes during a copy is how an 8-character minimum disappears without anyone
deciding to remove it.

| Check | Message | Why |
|---|---|---|
| Current password wrong | Your current password is incorrect. | The one control that makes the form safe to POST. Also mitigates the app's absent CSRF tokens — see §7. |
| New < 8 chars | Password must be at least 8 characters. | Identical to registration. |
| New > `MAX_PASSWORD_LEN` | Password is too long. | 600k rounds per hash; unbounded input is a worker thread on demand. |
| Confirmation mismatch | Passwords do not match. | Identical to registration. |
| New equals current | Choose a password different from your current one. | A no-op change that reports success while evicting the user's other devices is worse than a refusal. |

On success: `flash('Your password has been changed. Any other devices you were signed in on have
been signed out.', 'success')` then redirect to `/account` — the eviction is stated because a
user who does not expect it reads their phone logging out as a breach.

### Rate limits (new rows for the CLAUDE.md table)

| Endpoint | Limit | Keyed on |
|---|---|---|
| `GET /account` | default (200/hour) | user |
| `POST /account/password` | 10/hour | user |

Ten is generous for a real person and tight for a session-hijacker guessing the current password
through this form: `rate_limit_key()` keys on the user id, which is unspoofable here because the
route is `@login_required`. It also caps the CPU a single session can spend on PBKDF2.

---

## 5. Tests

One new module. The `mailer` fixture already swaps a `MemoryMailer` into
`app.extensions['mailer']`, so the email assertions need no network and no monkeypatching.

| Test | Asserts |
|---|---|
| Page renders identity | `GET /account` is 200 and contains the username and email; anonymous gets redirected to `/login`. |
| Happy path | Hash changes, `check_password(new)` is true, response redirects, success flash present. |
| **Other sessions die** | A second client logged in as the same user gets 200 before the change and is bounced to `/login` after it. The load-bearing test of this issue. |
| Current session survives | The changing client can still `GET /dashboard` afterwards — proves the `login_user` re-issue. |
| Wrong current password | Refused, hash unchanged, *and* `session_token` unchanged — a failed attempt must not evict anyone. |
| Each validation rule | Short, over-long, mismatched, and unchanged passwords are all refused with the stated wording. |
| Notification sent | `MemoryMailer` holds one message to the user's address, with no token-shaped string in either part. |
| Relay failure is non-fatal | A mailer raising `MailError` still leaves the new password committed — the invariant that makes the send safe to do inline. |
| Other users untouched | A second user's `session_token` and hash are unchanged. |
| Rate limit | The 11th POST in an hour is 429. |

---

## 6. Documentation to update

- **`CLAUDE.md`** — project structure (`routes/account.py`, the two new email templates), the
  `User` row in Data Models, the rate-limit table, the tests table, and a Security Notes line
  stating that a password change rotates the session token and therefore ends every other session.
- **`docs/database_schema.md`** — `user.session_token`: what it is, that it is not a secret about
  the password, and that rotating it is what logs other devices out.
- **`CHANGELOG.md`** — under Improvements, with issue link, mentioning the one-time forced
  re-login on deploy.
- **`docs/registration_invites.md`** — one cross-reference: the operator answer to "I forgot my
  password" is still manual until [#33](https://github.com/gfvandehei/PixelVault/issues/33).

---

## 7. Flags

Three things found while reading the code that this plan does not fix, in descending order of how
much they matter.

- **No CSRF tokens anywhere in the app.** Login, invite acceptance, album settings and now this
  form all POST without one. The session cookie is `SameSite=Lax`, which blocks the cross-site
  POST case in current browsers, and this form additionally demands the current password — so the
  account page does not make things worse. But `REMEMBER_COOKIE_SAMESITE` is unset (Flask-Login's
  default), so the remember cookie carries no `SameSite` attribute of its own. Setting it to
  `'Lax'` beside the existing session-cookie config is a one-line hardening worth doing here;
  app-wide CSRF deserves its own issue.
- **Rate-limit state is per-worker and in-memory.** `storage_uri="memory://"` with two Gunicorn
  workers means the 10/hour is really up to 20/hour, and resets on deploy. Consistent with every
  other limit in the app, and the current-password requirement is the actual control — noted so
  nobody reads the number as a guarantee.
- **The forced re-login lands on real users.** Not a bug, but the only user-visible regression in
  the release.
