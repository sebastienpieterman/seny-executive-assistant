# Changelog

All notable changes to Seny are documented here.

## [1.1.0] - 2026-03-24

### Security & Auth Hardening

- **Token revocation on logout.** Previously, clicking "Log Out" only cleared your browser — the token was still technically valid until it expired. Now, logout invalidates the token on the server immediately. If someone had copied your token, it stops working the moment you log out.

- **Shorter token lifetimes.** Web login tokens now expire after 7 days (was 30 days). Desktop app tokens expire after 90 days (were permanent). This limits the damage window if a token is ever exposed.

- **Unique token IDs (JTI claims).** Every token now has a unique identifier, enabling individual token revocation rather than the nuclear option of rotating the secret key (which would log out everyone).

- **Rate limiting on login and registration.** Login is limited to 10 attempts per minute, registration to 5 per hour, to prevent brute-force attacks. The app now correctly reads the real client IP behind Railway's reverse proxy.

### Database

- **PostgreSQL only.** Removed the SQLite compatibility layer (-193 lines of proxy code). All SQL was already PostgreSQL-native — this just removes the translation layer that was no longer needed. `DATABASE_URL` is now required; the app will tell you how to set it up if it's missing.

- **Local development with Docker Compose.** Added `docker-compose.yml` so you can run PostgreSQL locally with one command (`docker-compose up -d`) instead of installing it manually.

### Cost Control

- **Daily classification limit.** The scanner that classifies your incoming emails, Slack messages, and Telegram messages now has a configurable daily cap (default: 200 items/day). This prevents runaway API costs if you have high message volume. Set to 0 for unlimited. Configurable in Settings > Scanner.

- **Usage indicator.** The Scanner settings tab now shows how many items have been classified today versus your daily limit, with a progress bar and a warning when the limit is reached.

### For Existing Users

All changes are automatic — just pull the latest code and restart. The new database columns are created on startup. Your existing login will continue to work (old tokens are accepted until they naturally expire).

If you're running the desktop app, regenerate your desktop token after updating (Settings > Desktop App) to get the new 90-day expiry instead of the old permanent token.

## [1.0.0] - 2026-03-19

Initial public release. Sanitized from private instance with setup wizard, dynamic system prompt, and full documentation.
