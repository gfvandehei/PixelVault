# [0.2.0]
## BugFixes

## Improvements

- **Client-side photo caching**: Images viewed in the lightbox are now cached in memory for the duration of the session, eliminating re-fetches when navigating back to previously viewed photos.
- **HTTP cache headers**: Media endpoints now respond with `Cache-Control: private, max-age=31536000, immutable`, allowing the browser to cache photos and thumbnails across page loads without re-validating.
- **Expanded preload window**: The lightbox preloads 2 images in both directions (previously only forward), so backward navigation is as fast as forward.