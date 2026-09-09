# Integration Health Centre v1.0.0

## Accepted local behaviour

The accepted runtime patch added a read-only settings dashboard at
`/settings/integration-health` for operational diagnostics. It covered Floppy's
core runtime dependencies and the integrations most useful to diagnose from the
UI: database, Redis/cache, Celery/background tasks, TMDb, Kodi JSON-RPC, Kodi
HTTP Scrobbler activity and MDBList rating activity.

The Kodi/MDBList telemetry contract retained the most recent event snapshots in
cache for 30 days:

- `integration-health:kodi:last-received:{user_id}`
- `integration-health:kodi:last-success:{user_id}`
- `integration-health:mdblist:last-rating:{user_id}`

Telemetry was explicitly best-effort. A cache failure was never allowed to
change webhook processing.

## Source-native implementation

The source port keeps the dashboard observational and additive:

- Django database connectivity is tested with a read-only `SELECT 1`.
- Redis/cache is tested through the configured Django cache backend. Optional
  Redis memory and eviction-policy metadata is reported when the backend makes
  it available, but the dashboard never changes Redis configuration.
- Celery uses the current configured Celery app and a short worker ping.
- TMDb resolves the active credential through Floppy's current provider
  credential precedence chain and performs a lightweight configuration probe.
  Credential values and raw provider exceptions are never rendered.
- Kodi uses the source-native `KodiClient` and `JSONRPC.Ping`, preserving its
  current configuration, authentication and protocol boundaries.
- Kodi/MDBList event telemetry is recorded at the current
  `KodiWebhookProcessor` boundary, leaving Kodi Control + Sync v2.7 runtime
  behaviour unchanged.
- The dashboard is available from Settings > Integration Health and refreshes
  with a read-only GET request.

## Deliberate changes from the runtime patch

The old runtime patch-harness health endpoint is not recreated. Runtime file
mounts are no longer the integration source of truth; source history and CI on
`chris/integration` replace that role.

Floppy already ships machine-oriented `django-health-check` endpoints including
cache, migrations, Celery ping, Redis and database heartbeat. The Integration
Health Centre does not replace `/health/full/`. It is the human/operator-facing
layer that explains configuration state, degraded integrations and recent
user-scoped scrobbler activity.

## Safety boundaries

- No health check mutates media, ratings, watched state or integration config.
- No secret/token/password is included in rendered details.
- External probes use short timeouts and convert failures into status rows.
- Telemetry cache failures are swallowed and logged at debug level.
- Kodi Control + Sync, MDBList rating handling and other integrations do not
  depend on the Health Centre being available.
