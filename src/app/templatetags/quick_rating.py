# Quick Rating Overlay capability and route resolver.
#
# Normal media types use Floppy's standard update_media_score contract.
# Music uses entity-aware artist/album/track endpoints. Podcast remains
# excluded because its scoring contract is separate and not yet mapped.

from django import template
from django.urls import NoReverseMatch, reverse

from app.models import MediaTypes


register = template.Library()

QUICK_RATING_OVERLAY_VERSION = "4.1.3"

NON_STANDARD_SCORE_TYPES = frozenset(
    {
        MediaTypes.MUSIC.value,
        MediaTypes.PODCAST.value,
    }
)

MUSIC_SCORE_ROUTES = {
    "artist": "update_artist_score",
    "album": "update_album_score",
    "track": "update_track_score",
}


def _authenticated(user):
    return bool(user and getattr(user, "is_authenticated", False))


def _has_template_value(value):
    """Treat Django's missing-variable empty string like an unset value."""
    return value is not None and value != ""


def _attr_or_key(value, name):
    """Read one field from either a model/object or a serialized mapping."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


@register.simple_tag
def quick_rating_values(
    media=None,
    instance_id=None,
    quick_rating_value=None,
    rating_value=None,
):
    """Backward-compatible value resolver retained for mounted templates."""

    resolved_instance_id = (
        instance_id
        if _has_template_value(instance_id)
        else getattr(media, "id", None)
    )

    if _has_template_value(quick_rating_value):
        resolved_rating = quick_rating_value
    elif _has_template_value(rating_value):
        resolved_rating = rating_value
    else:
        resolved_rating = getattr(media, "score", None)

    return {
        "instance_id": resolved_instance_id,
        "rating_value": resolved_rating,
    }


@register.simple_tag
def quick_rating_capability(
    user,
    media_type,
    instance_id,
    public_view=False,
    recommend_mode=False,
    history_mode="",
):
    """Return capability for media using the generic score endpoint."""

    result = {
        "enabled": False,
        "url": "",
        "reason": "",
        "target_kind": "standard",
    }

    if not _authenticated(user):
        result["reason"] = "anonymous"
        return result

    if public_view:
        result["reason"] = "public_view"
        return result

    if recommend_mode:
        result["reason"] = "recommend_mode"
        return result

    if str(history_mode or "") == "release":
        result["reason"] = "release_history"
        return result

    media_type = str(media_type or "").strip()
    if not media_type or not instance_id:
        result["reason"] = "missing_instance"
        return result

    if media_type in NON_STANDARD_SCORE_TYPES:
        result["reason"] = "specialised_score_path"
        return result

    try:
        result["url"] = reverse(
            "update_media_score",
            args=[media_type, instance_id],
        )
    except NoReverseMatch:
        result["reason"] = "no_standard_score_route"
        return result

    result["enabled"] = True
    return result


def _resolve_music_target(context, media):
    """Resolve what a music card represents: artist, album, or track."""

    # direct_music_entity: explicit model identity takes priority over related
    # fields. An Album has an .artist relation and must not be mistaken for an
    # artist card.
    class_name = media.__class__.__name__ if media is not None else ""
    media_id = _attr_or_key(media, "id")

    if class_name == "Artist" and _has_template_value(media_id):
        return "artist", media_id

    if class_name == "Album" and _has_template_value(media_id):
        return "album", media_id

    if class_name == "ArtistTracker":
        artist_id = _attr_or_key(media, "artist_id")
        if _has_template_value(artist_id):
            return "artist", artist_id

    if class_name == "AlbumTracker":
        album_id = _attr_or_key(media, "album_id")
        if _has_template_value(album_id):
            return "album", album_id

    explicit_kind = context.get("quick_rating_music_kind")
    explicit_id = context.get("quick_rating_music_id")
    if explicit_kind in MUSIC_SCORE_ROUTES and _has_template_value(explicit_id):
        return explicit_kind, explicit_id

    # History aggregates music by album. Its instance_id is a representative
    # Music row, not the Album ID, so prefer the serialized album.
    if context.get("quick_rating_media_type") == MediaTypes.MUSIC.value:
        entry = context.get("entry")
        album = _attr_or_key(entry, "album")
        album_id = _attr_or_key(album, "id")
        if _has_template_value(album_id):
            return "album", album_id

    # A direct Music row is the Tracks-subview entity itself. Resolve it to
    # update_track_score before following its album/artist relations.
    if class_name == "Music" and _has_template_value(media_id):
        return "track", media_id

    # Normal music wrappers still use album-first, then artist, matching
    # the navigation semantics of those wrapper cards.
    album = _attr_or_key(media, "album")
    album_id = _attr_or_key(album, "id")
    if _has_template_value(album_id):
        return "album", album_id

    artist = _attr_or_key(media, "artist")
    artist_id = _attr_or_key(artist, "id")
    if _has_template_value(artist_id):
        return "artist", artist_id

    # A genuine Music instance with no higher-level relation can use the track
    # score endpoint.
    track = _attr_or_key(media, "track")
    if track is not None and _has_template_value(media_id):
        return "track", media_id

    return None, None


def _music_quick_rating_capability(
    user,
    target_kind,
    target_id,
    public_view=False,
    recommend_mode=False,
    history_mode="",
):
    """Return capability for specialised music score endpoints."""

    result = {
        "enabled": False,
        "url": "",
        "reason": "",
        "target_kind": target_kind or "",
    }

    if not _authenticated(user):
        result["reason"] = "anonymous"
        return result

    if public_view:
        result["reason"] = "public_view"
        return result

    if recommend_mode:
        result["reason"] = "recommend_mode"
        return result

    if str(history_mode or "") == "release":
        result["reason"] = "release_history"
        return result

    route_name = MUSIC_SCORE_ROUTES.get(target_kind)
    if not route_name or not _has_template_value(target_id):
        result["reason"] = "music_target_missing"
        return result

    try:
        result["url"] = reverse(route_name, args=[target_id])
    except NoReverseMatch:
        result["reason"] = "no_music_score_route"
        return result

    result["enabled"] = True
    return result


@register.simple_tag(takes_context=True)
def quick_rating_context(context):
    """Resolve the complete shared-component context safely."""

    media = context.get("media")

    def first_value(*values):
        for value in values:
            if _has_template_value(value):
                return value
        return None

    media_type = first_value(
        context.get("quick_rating_media_type"),
        context.get("resolved_media_type"),
        _attr_or_key(media, "media_type"),
    )

    instance_id = first_value(
        context.get("quick_rating_instance_id"),
        _attr_or_key(media, "id"),
    )

    rating_value = first_value(
        context.get("quick_rating_value"),
        context.get("rating_value"),
        _attr_or_key(media, "score"),
    )

    # home_music_card_wrapper: the normal Music grid marks its wrapper objects
    # with this capability. The wrapper itself is authoritative, so the overlay
    # must not depend on resolved_media_type being supplied as "music".
    home_music_card = bool(_attr_or_key(media, "home_music_card"))
    direct_music_entity = (
        media.__class__.__name__
        if media is not None
        else ""
    ) in {"Music", "Artist", "Album", "ArtistTracker", "AlbumTracker"}

    if home_music_card or direct_music_entity:
        media_type = MediaTypes.MUSIC.value

    if media_type == MediaTypes.MUSIC.value:
        target_kind, target_id = _resolve_music_target(context, media)
        result = _music_quick_rating_capability(
            context.get("user"),
            target_kind,
            target_id,
            context.get("public_view", False),
            context.get("recommend_mode", False),
            context.get("history_mode", ""),
        )
        if _has_template_value(target_id):
            instance_id = target_id
    else:
        result = quick_rating_capability(
            context.get("user"),
            media_type,
            instance_id,
            context.get("public_view", False),
            context.get("recommend_mode", False),
            context.get("history_mode", ""),
        )

    result.update(
        {
            "media_type": media_type or "",
            "instance_id": instance_id,
            "rating_value": rating_value,
        }
    )
    return result
