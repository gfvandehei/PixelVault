# Registration & Invites — Operator Guide

How accounts get created on a PixelVault instance, what the admin panel's buttons do, and
what to do when an invitation does not arrive.

This is the operations side. The variables themselves are documented in
[configuration.md §5](configuration.md#5-mail--invites); the reasoning behind the design is in
[invite_registration_design.md](invite_registration_design.md); the columns are in
[database_schema.md](database_schema.md#allowedemail--the-invite-lifecycle).

---

## 1. The short version

**There is no sign-up page.** The only way an account comes into existence is:

1. An admin adds an address in **Admin → Allowed Emails**.
2. The app mints a single-use link and emails it to that address.
3. The recipient clicks it, picks a username and password, and is signed in.

The link expires on its own after `INVITE_TTL_HOURS` (default 72), stops working the moment it
is used, and is bound to the address it was issued for — the acceptance form shows that address
read-only and the server never reads an address from the form. An invitee cannot register under
a different email than the one you invited, even by editing the page.

If mail is not working, **Copy link** on the invite's row gives you the URL to hand over
yourself. SMTP is a convenience here, not a dependency.

---

## 2. First-time setup

Registration works with no mail configuration at all — you just have to hand links over
manually. To make it automatic you need two things set:

```bash
PUBLIC_BASE_URL=https://photos.example.com   # the origin your users actually type
SMTP_HOST=smtp.gmail.com                     # plus port/security/credentials
MAIL_FROM=photos@example.com
```

`PUBLIC_BASE_URL` is not optional once you send mail, and the app refuses to boot without it
when `SMTP_HOST` is set. It is where every invite link points. It is configured rather than
derived from the incoming request on purpose: behind Cloudflare → nginx → Gunicorn, the origin
Flask would reconstruct comes from forwarded headers, so a spoofed `Host` on the add-email
request could otherwise put an attacker's domain in front of a real token.

See [configuration.md §5.4](configuration.md#54-gmail-profile) for the Gmail app-password
profile, which is the usual starting point.

**Verify it before you need it.** Invite yourself at an address you can read, on the real
deployment, before you invite anybody else. A relay that authenticates but silently drops mail
looks exactly like a relay that works, right up until the moment someone is waiting for a link.

### Without a relay

| You want | Set | Result |
|---|---|---|
| Links printed to the app log | leave `SMTP_HOST` empty | `ConsoleMailer` — read the link out of `docker compose logs` |
| No mail at all, ever | `MAIL_ENABLED=false` | `NullMailer` — invites are issued, **Copy link** is the only way to deliver them |

Both still need `PUBLIC_BASE_URL` for a link to be composable. Neither is checked at boot, so
if you run this way and see *"no email could be composed"* on the flash, that is the missing
variable.

---

## 3. Inviting someone

**Admin → Allowed Emails → add an address.** Two optional fields go with it:

- **Note** — free text for you, shown only in the panel. Who this is, why they were invited.
- **Suggested username** — pre-fills the acceptance form. It is a suggestion; the invitee can
  change it. There is deliberately no "make this person an admin" checkbox: a mistyped
  checkbox on this form must never be able to mint an administrator. Promote a real account
  by hand afterwards instead.

Adding the address does three things, in this order: it commits the invite row, mints the
token, and *then* tries to send. The order matters when things go wrong — a relay outage
leaves a perfectly good invite behind for you to resend or hand over, rather than losing it.

You will get one of these back:

| Flash | Meaning |
|---|---|
| *"has been invited — an invitation is on its way"* | Sent. |
| *"was invited, but the email could not be sent: …"* | The invite exists and its link is live. Delivery failed. Use **Copy link**. |
| *"already has an account, so there is nothing to invite"* | They can just sign in. |
| *"has already been invited. Use Resend…"* | There is a row for this address already. |

---

## 4. Reading the panel

Every row carries a badge. The state behind it is computed when the page renders — never
stored — so an expiry that lapsed overnight shows up without anything having run.

| Badge | What it means | What to do |
|---|---|---|
| **accepted** | The account exists. | Nothing. Terminal. |
| **no invite** | A whitelist entry from before invites existed — no token was ever minted. | **Send invite**, if the address is still someone you want. |
| **expired** | The link lapsed unused. | **Resend**. |
| **send failed** | The token is fine; delivery failed. The relay's error is shown on the row. | **Resend**, or **Copy link**. |
| **not emailed** | A live link nobody has emailed — the result of **Copy link**, or of mail being off. | Hand the link over. |
| **sent** | Delivered, not yet clicked. | Wait. Then resend past the cooldown. |

The row also shows when it was sent (and how many attempts, past the first), when it expires,
your note, and the suggested username.

### Deploy day

Every address you had whitelisted before this feature reads as **no invite**. Those
people can no longer register — the whitelist alone is not a way in any more. Nothing was
emailed to them on upgrade, deliberately: mailing a year-old list on deploy day is a good way
to get a sending domain blocked. Go through the rows and press **Send invite** on the ones
still worth inviting, then delete the rest.

---

## 5. The three buttons

### Resend / Send invite

One button under two labels — it reads **Send invite** on a row that has never been emailed
(a `no invite` row, or one created by **Copy link**) and **Resend** otherwise. Same route,
same behaviour.

Mints a **new** token, sends it, and **kills the previous link immediately**. This is not a
retry — it is a replacement. Anyone still holding the old link gets "this invitation link is
not valid".

That is a consequence of storing only the hash of a token: the plaintext is unrecoverable, so
renewal must mint. It is also the safer default, since a resend usually means the first link
was lost or went somewhere it should not have.

Refused inside `INVITE_RESEND_COOLDOWN_SECONDS` (default 60) of the last send, with the
remaining wait named in the message. The cooldown is not about your patience: this is mail the
server sends to a third party on request, and an unthrottled resend button is a mail-bomb
primitive aimed at whatever address is typed in. The check runs *before* the rotation, so a
refusal costs the invitee nothing — the link they already hold keeps working.

### Copy link

Rotates the token and shows the URL **once**, in a flash message. Sends no mail, and takes no
cooldown — gating it would disable the fallback in exactly the minute after a failed send,
which is the minute it is most needed.

**Copy it before you navigate away.** Only the SHA-256 is stored; closing the page loses the
link and the only fix is another rotation, which invalidates this one too.

Reach for this when the relay is down, when there is no relay, or when an invite has landed in
someone's spam folder and a second email will land there too.

### Remove

Deletes the invite row, which is also how you **revoke** an outstanding link — the token hash
lives on that row, so deleting it makes any link unmatchable on the next lookup. There is no
separate revoke button because there does not need to be one.

Removing an **accepted** row does *not* delete the account it produced. The row is bookkeeping
about a past event; `accepted_user_id` has no cascade behind it. Delete the user from the Users
table if that is what you meant.

---

## 6. What the invitee sees

Clicking the link lands them on the acceptance form with their address filled in and locked,
and the suggested username (if you set one) filled in and editable. They choose a username and
a password — same rules as the old sign-up form: 3–64 characters, letters/numbers/hyphens/
underscores, and a password of at least 8 characters. On submit the account is created and
they are signed in.

The token does not stay in the address bar. The link validates, moves the token into the signed
session, and redirects to `/invite` — so the credential does not end up in nginx access logs,
browser history, or a "here's the page I'm on" message.

If the link does not work they see one of four messages, each naming a different fix:

| They see | Because | Your move |
|---|---|---|
| "…has expired. Ask an admin for a new one" | past `INVITE_TTL_HOURS` | Resend. |
| "…is not valid. Check that you copied the whole link… if you have already used it, sign in" | no such token | Read below. |
| "…already been accepted and the account exists" | replayed on a row still findable | Tell them to sign in. |
| "You are already signed in…" | they clicked while logged in as someone else | Log out first. |

**"Not valid" is deliberately ambiguous** and cannot be made specific. Accepting an invite
nulls the token hash, so a used link and a mistyped link are the same thing to the server —
nothing matches either way. The message is worded for both readers at once because guessing
between them would strand whichever reader we guessed against.

Accepting while already signed in is **refused, not merged**. The app will not log someone out
on the strength of a URL they opened, and it cannot tell whether the signed-in person is the
invitee.

---

## 7. Troubleshooting

**"was invited, but the email could not be sent"**
Delivery, not the invite. The row reads **Send failed** with the relay's error on it. Check
`SMTP_HOST`/`SMTP_PORT`/`SMTP_SECURITY` agree (587 wants `starttls`, 465 wants `ssl`), and that
`SMTP_PASSWORD` is a Gmail *app password* rather than the account password. Meanwhile, **Copy
link** gets the person in.

**"no email could be composed"**
`PUBLIC_BASE_URL` is unset. Nothing was sent; the invite is live and stays that way.

**Sends succeed but nothing arrives**
Check spam. Mail from a `@gmail.com` sender to strangers lands there routinely — this is the
argument for a domain with SPF/DKIM, or a transactional provider. Hand this one over with
**Copy link** and fix the sending domain before the next batch.

**The invite email's link 404s or points at the wrong host**
`PUBLIC_BASE_URL` does not match the origin users reach. It must be the external origin
including scheme, no trailing slash — `https://photos.example.com`, not the container's
internal address and not `http://` on an HTTPS deployment.

**Requests hang when adding an address**
Sends run synchronously inside the admin request, bounded by `MAIL_TIMEOUT_SECONDS` (default
10). A wrong port typically shows up as a full-timeout stall rather than an error. Production
runs 2 workers × 4 threads, so a long timeout on a dead relay ties up an eighth of the server
per attempt — do not raise it much.

**Someone insists they never got a link and Resend refuses**
You are inside the cooldown; the message names the seconds remaining. Use **Copy link**, which
has none.

**A guest with a share link cannot see the album**
Album routes require an account. They land on the Access Required page, which now names
`ADMIN_CONTACT` (falling back to `MAIL_FROM`) as who to ask. If both are unset the page tells
them to ask whoever shared the link — still actionable, but set `ADMIN_CONTACT` if guests are
common.

---

## 8. Housekeeping

Expired invites are not swept. Nothing breaks — an expired row simply stops working, and the
state is computed at render time — but the table grows, so delete rows you are done with.
Removing an accepted row does not touch the account, so an accepted row can be cleared once you
no longer care to know who invited whom.

The invite endpoints are rate-limited per IP: 60/hour for clicking a link and 20/hour for
submitting the form. Admin actions are limited per user: 60/hour for adding an address, 30/hour
each for resend and copy-link. Limits live in per-worker memory and reset on deploy, so they
damp abuse rather than prevent it — the cooldown is the durable protection against the
mail-bomb case.
