# PixelVault Database Schema

```mermaid
erDiagram
    User {
        int id PK
        string username "unique, max 64"
        string email "unique, max 120"
        string password_hash "max 256"
        string session_token "UUID, rotated on password change"
        bool is_admin "default false"
        datetime created_at
    }

    AllowedEmail {
        int id PK
        string email "unique, max 120"
        string note "max 256"
        datetime added_at
        string token_hash "sha256 hex, max 64, nullable, indexed"
        datetime token_issued_at "nullable"
        datetime expires_at "nullable"
        string prefill_username "max 64, default empty"
        datetime last_sent_at "nullable"
        int send_count "default 0"
        string last_send_error "max 256, default empty"
        datetime accepted_at "nullable"
        int accepted_user_id FK "nullable"
        int invited_by_id FK "nullable"
    }

    Album {
        int id PK
        string name "max 128"
        int owner_id FK
        string token "UUID, unique"
        string view_token "UUID, unique, nullable"
        string description "max 512"
        bool allow_anonymous "default true"
        bool allow_upload "default true"
        datetime created_at
    }

    AlbumAccess {
        int id PK
        int user_id FK
        int album_id FK
        string access_type "upload or view"
        datetime accessed_at
    }

    Photo {
        int id PK
        int album_id FK
        int uploader_id FK "nullable"
        string stored_filename "UUID-based, max 256"
        string original_filename "max 256"
        string mime_type "max 64"
        string uploader_name "max 64, default Anonymous"
        int file_size "bytes"
        bool has_thumbnail "default false"
        datetime uploaded_at
    }

    User ||--o{ Album : "owns"
    User ||--o{ AllowedEmail : "invited"
    User ||--o| AllowedEmail : "accepted as"
    User ||--o{ AlbumAccess : "has access"
    User ||--o{ Photo : "uploads"
    Album ||--o{ Photo : "contains"
    Album ||--o{ AlbumAccess : "grants"
```

---

## User.session_token — what makes a session revocable

Not a secret about the password, and not a session store. It is one opaque value per
user that rides along in every cookie the app issues, because Flask-Login writes
`User.get_id()` into both the session cookie and the remember-me cookie, and
`User.get_id()` returns `"<id>:<session_token>"` rather than the bare primary key.

`extensions.load_user` compares the token half against the row on every request. So
rewriting the column — `User.rotate_session_token()`, one UPDATE — invalidates every
cookie already handed out for that user, on every device, with nothing to sweep and
no server-side session table.

Today one thing rotates it: changing a password on `/account`. That route re-issues
the calling browser's cookie immediately afterwards, because rotation is
indiscriminate and would otherwise sign the user out of the browser they changed it
in. See [account_page_design.md](account_page_design.md) §2.

Two consequences worth knowing before touching it:

- **A cookie carrying a bare id is refused**, not grandfathered. Sessions created
  before this column existed therefore ended on the deploy that added it — the
  alternative was letting a stolen pre-deploy cookie outlive the password change made
  to revoke it.
- **The migration's `DEFAULT ''` is a placeholder, not a value.** SQLite will not take
  a non-constant default on `ADD COLUMN`, so `_run_migrations()` backfills a distinct
  UUID per row straight afterwards. A shared empty token would make every user's
  cookie interchangeable.

---

## AllowedEmail — the invite lifecycle

`AllowedEmail` is not a passive whitelist. One row is both the assertion that an
address may register *and* the credential that carries that permission to a person,
so it has a lifecycle. See [invite_registration_design.md](invite_registration_design.md)
§4 for why this is one table and not two.

**The token is never stored.** `token_hash` holds the SHA-256 of a
`secrets.token_urlsafe(32)` value; the plaintext exists only in the sent email and,
for the copy-link fallback, in one flash message. A leaked backup or a screenshot of
the admin page therefore cannot be used to create an account — and, as the accepted
cost, a link can never be re-shown, so resending mints a new token and kills the old
one. Lookup is an indexed equality match on the hash.

`expires_at` is denormalised from `token_issued_at` + `INVITE_TTL_HOURS` so the admin
table can display and sort honest expiry without re-deriving it against a config value
that may have changed since the token was minted.

### States

`AllowedEmail.state` is a **derived property**, never a column: the expiry test reads
the wall clock, so a stored value would be wrong from the moment a TTL lapsed with
nobody looking. It is evaluated in this order, and the order is the specification —
several rows satisfy more than one test at once.

| # | State | Row looks like | What an admin does about it |
|---|---|---|---|
| 1 | `ACCEPTED` | `accepted_at` set (`token_hash` nulled by consumption) | Nothing. Terminal — it outranks a TTL that lapsed afterwards. |
| 2 | `LEGACY` | no `token_hash`, never accepted | **Send invite.** Pre-migration rows land here. |
| 3 | `EXPIRED` | `now >= expires_at` | **Resend** — which rotates the token. |
| 4 | `SEND_FAILED` | `last_send_error` non-empty, token still live | Resend, or hand over the copy-link. The credential is fine; only delivery failed, which is why it ranks below `EXPIRED`. |
| 5 | `ISSUED` | token live, `last_sent_at` is NULL | Nothing sent yet — the copy-link path, or mail disabled. |
| 6 | `SENT` | token live, delivered, unclicked | Wait, then resend past the cooldown. |

`is_pending` is true for exactly `ISSUED`, `SENT`, and `SEND_FAILED` — the states where
a working link is outstanding.

`LEGACY` is load-bearing, not cosmetic. Every `allowed_email` row created before this
feature has no token, and once registration is link-only those addresses can no longer
be used at all. The state is what lets the admin panel list them as *"No invite sent"*
with a **Send invite** button, rather than having a migration email a year-old address
on deploy day.

### Migration

All ten columns are added by `_run_migrations()` as individual
`ALTER TABLE allowed_email ADD COLUMN` statements, plus
`CREATE INDEX IF NOT EXISTS ix_allowed_email_token_hash`. The three non-nullable columns
carry a literal SQL `DEFAULT` (`''`, `0`, `''`) because SQLite rejects a non-constant
default on `ADD COLUMN` and existing rows must be writable; the nullable ones take no
default, which is exactly what makes an untouched row read as `LEGACY`.
