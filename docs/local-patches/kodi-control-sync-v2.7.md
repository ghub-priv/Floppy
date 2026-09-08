# Kodi Control + Sync v2.7

This port preserves the accepted local Kodi Control + Sync v2.7 behaviour while adapting it to current Floppy architecture.

## Behavioural contract

- Launch TMDb movies and episodes through TMDb Helper with `Player.Open`.
- Browse TV shows and seasons through TMDb Helper with `GUI.ActivateWindow`.
- Queue Floppy resume progress before `Player.Open`; apply it only after HTTP Scrobbler identifies the exact Kodi session.
- Pause, resume, stop and seek ±30 seconds through authenticated Kodi JSON-RPC.
- Treat Kodi start, pause, resume, seek, interval, stop and end events as live playback events.
- Keep interval events cache-only. They must not generate periodic durable progress writes.
- Persist progress checkpoints on pause, seek, stop and end only.
- Rating-only webhook payloads must never enter playback/watch-history processing.
- Episode rating updates must bypass `Episode.save()` side effects and must never manufacture a watch record.
- An ordinary partial replay must not reopen a completed season unless a rewatch is active.
- Reconcile stale or missing Kodi Now Playing state conservatively.

## Current-source adaptation

Floppy's live playback cache is shared by multiple integrations. Kodi-originated state is therefore marked with `control_backend="kodi"`. Kodi controls, deferred resume and reconciliation act only on Kodi-owned state and must never operate on Plex/Jellyfin sessions.

The port uses Floppy's current `PlaybackProgress` store and current provider/anime resolution in `BaseWebhookProcessor`; it does not restore obsolete r13 copies of those subsystems.

## Deployment configuration

Kodi Control + Sync is optional. A blank `KODI_HOST` leaves it unconfigured. The Compose examples pass these host/Compose environment variables into the Floppy container without storing credentials in the repository:

- `KODI_HOST` - Kodi host or IP address.
- `KODI_PORT` - Kodi web-server port, default `8080`.
- `KODI_USERNAME` and `KODI_PASSWORD` - Kodi HTTP Basic Auth credentials when enabled.
- `KODI_SCHEME` - `http` by default, or `https` when Kodi is exposed that way.
- `KODI_TIMEOUT` - JSON-RPC HTTP timeout in seconds, default `6`.
- `KODI_FLOPPY_USER_ID` - the Floppy user mapped to this Kodi instance for cold Now Playing recovery.

The Kodi web server and JSON-RPC access must be enabled in Kodi, and TMDb Helper must be installed for the Play in Kodi detail actions.

## Deliberate separation

Integration Health telemetry is not part of this port. It is a later dependent patch and should instrument these Kodi services additively rather than making Kodi Control + Sync depend on Integration Health.
