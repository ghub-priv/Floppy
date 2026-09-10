# Scoped Integration Tokens v1.0.1

This source port reconciles the accepted r13 Scoped Integration Tokens patch with current Floppy.

Current source already provided the `IntegrationToken` model, SHA-256 digest-only storage, expiry/revocation state, token generation, authentication lookup and `HasScope`. Those pieces are reused rather than duplicated.

The port adds the behaviour that remained runtime-only:

- a central URL-name and HTTP-method scope policy for `IntegrationToken` credentials;
- deny-by-default handling for scoped credentials on routes absent from that policy;
- five-minute throttling of `last_used_at` telemetry after authorised requests;
- v1.0.1 ListenBrainz `submit-listens` and `validate-token` access for `scrobble:write`;
- a Settings > Integrations > Integration Tokens management subpage;
- per-client name, optional client identifier, selectable scopes and optional expiry;
- one-time plaintext display after creation;
- individual per-user revocation;
- legacy account-wide `User.token` compatibility.

The historical `watchlist:read` and `watchlist:write` names are intentionally retained for compatibility. In this contract they represent mutable user-library state, including tracked status, Collection and custom lists.
