# [0.2.0]
## BugFixes

## Improvements

- **Invite-based registration** ([#7](https://github.com/gfvandehei/PixelVault/issues/7)): registration is now link-only. Adding an address in the admin panel mints a single-use, expiring invitation and emails it automatically; the recipient clicks through to a form with their address filled in and locked, picks a username and password, and is signed in. The public sign-up page is gone — `/register` no longer exists.
- **Invite management in the admin panel**: every authorized address now shows its state (sent, not emailed, expired, send failed, accepted, or a pre-invite legacy row), when it was last sent and when it expires, plus **Resend** and **Copy link** actions. Copy link works with no mail relay at all, so SMTP is optional rather than a hard dependency of registration.
- **Outbound email**: the app can now send mail over SMTP, configured entirely through environment variables (`SMTP_HOST`, `SMTP_SECURITY`, `MAIL_FROM`, and friends — see `docs/configuration.md` §5). With no relay configured, invitations are printed to the application log instead, so a fresh checkout can complete the flow without one.
- **Guests without accounts get an answer**: the Access Required page a share-link visitor lands on now names who to ask for an invitation (`ADMIN_CONTACT`), instead of pointing at a sign-up page that no longer exists.

- **Client-side photo caching**: Images viewed in the lightbox are now cached in memory for the duration of the session, eliminating re-fetches when navigating back to previously viewed photos.
- **HTTP cache headers**: Media endpoints now respond with `Cache-Control: private, max-age=31536000, immutable`, allowing the browser to cache photos and thumbnails across page loads without re-validating.
- **Expanded preload window**: The lightbox preloads 2 images in both directions (previously only forward), so backward navigation is as fast as forward.