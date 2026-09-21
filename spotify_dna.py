#!/usr/bin/env -S uv run --quiet --with spotipy==2.26.0
# /// script
# dependencies = ["spotipy==2.26.0"]
# ///
"""
Spotify DNA: Create a playlist with an artist-by-artist shuffle.

This script generates a playlist by taking random tracks from random albums or
singles by each of your followed Spotify artists, creating a unique
artist-by-artist shuffle. It uses asyncio for concurrent API calls around
Spotipy's synchronous client.
"""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import os
import random
from pathlib import Path
import sys
import threading
import time

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth


logger = logging.getLogger(__name__)

SCOPES = (
    "user-follow-read "
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-private "
    "playlist-modify-public"
)
REQUIRED_ENV_VARS = (
    "SPOTIPY_CLIENT_ID",
    "SPOTIPY_CLIENT_SECRET",
    "SPOTIPY_REDIRECT_URI",
)
DEFAULT_STATUS_FORCELIST = "500,502,503,504"
api_request_counts = Counter()
api_request_counts_lock = threading.Lock()


def record_api_request(endpoint):
    """
    Records one high-level Spotipy API request.
    """
    with api_request_counts_lock:
        api_request_counts[endpoint] += 1


def log_api_request_summary():
    """
    Logs counted Spotify API requests by endpoint.

    Logged at warning level so it still appears in --quiet mode when it is
    printed alongside an error.
    """
    with api_request_counts_lock:
        counts = api_request_counts.copy()

    if not counts:
        logger.warning("Spotify API requests: 0")
        return

    total = sum(counts.values())
    logger.warning("Spotify API requests:")
    for endpoint, count in sorted(counts.items()):
        logger.warning(f"  {count:>3}  {endpoint}")
    logger.warning(f"  {total:>3}  total")


def parse_status_forcelist(value):
    """
    Parses a comma-separated list of HTTP status codes for Spotipy retries.
    """
    try:
        return tuple(int(code.strip()) for code in value.split(",") if code.strip())
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            "--status-forcelist must be a comma-separated list of HTTP status codes"
        ) from e


def is_spotify_rate_limit_error(error):
    """
    Returns True when Spotipy raised a Spotify 429 rate-limit error.
    """
    return isinstance(error, SpotifyException) and error.http_status == 429


def is_spotify_quota_error(error):
    """
    Returns True when Spotify reports Development Mode quota exhaustion.
    """
    return is_spotify_rate_limit_error(error) and error.reason == "QUOTA_EXCEEDED"


def format_retry_after(headers):
    """
    Formats Spotify's Retry-After header, if present.
    """
    if not headers:
        return None

    retry_after = headers.get("Retry-After")
    if not retry_after:
        return None

    try:
        seconds = int(retry_after)
    except ValueError:
        return f"{retry_after} seconds"

    if seconds < 60:
        return f"{seconds} seconds"

    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{seconds} seconds (~{hours}h {remaining_minutes}m)"
    return f"{seconds} seconds (~{minutes}m {remaining_seconds}s)"


def format_rate_limit_error(error):
    """
    Formats a concise Spotify rate-limit message.
    """
    endpoint = getattr(error, "spotify_dna_endpoint", None)
    endpoint_context = f" on {endpoint}" if endpoint else ""
    retry_after = format_retry_after(error.headers)
    if is_spotify_quota_error(error):
        reset_context = f"\nRetry after: {retry_after}." if retry_after else ""
        return (
            f"\nSpotify Development Mode quota exceeded{endpoint_context}."
            f"{reset_context} See README for quota details."
        )

    if retry_after:
        return (
            f"Spotify rate limit reached{endpoint_context}. Stopping instead of waiting "
            f"{retry_after}. Try again later or lower --max-tracks."
        )
    return f"Spotify rate limit reached{endpoint_context}. Stopping. Try again later or lower --max-tracks."


def exit_after_rate_limit(error):
    """
    Logs a Spotify 429 with request counts and exits.
    """
    logger.error(format_rate_limit_error(error))
    logger.warning("")
    log_api_request_summary()
    sys.exit(1)


def format_spotify_api_error(error):
    """
    Formats a concise non-rate-limit Spotify API error.
    """
    endpoint = getattr(error, "spotify_dna_endpoint", None)
    endpoint_context = f" on {endpoint}" if endpoint else ""
    reason = f" Reason: {error.reason}." if error.reason else ""
    return (
        f"Spotify API error{endpoint_context}: HTTP {error.http_status}. "
        f"{error.msg}{reason}"
    )


def exit_after_spotify_api_error(error):
    """
    Logs a Spotify API error with request counts and exits.
    """
    logger.error(format_spotify_api_error(error))
    logger.warning("")
    log_api_request_summary()
    sys.exit(1)


def spotify_call(endpoint, func, *args, context=None, **kwargs):
    """
    Calls Spotipy and preserves endpoint context on Spotify exceptions.
    """
    record_api_request(endpoint)
    try:
        return func(*args, **kwargs)
    except SpotifyException as e:
        e.spotify_dna_endpoint = f"{endpoint} ({context})" if context else endpoint
        raise


def fetch_artist_album_page(spotify, artist_id, market="US", offset=0, limit=10, artist_name=None):
    """
    Fetches one album or single page for an artist.
    """
    return spotify_call(
        "GET /v1/artists/{artist_id}/albums",
        spotify.artist_albums,
        artist_id,
        context=artist_name,
        include_groups="album,single",
        country=market,
        limit=limit,
        offset=offset,
    )


def load_album_cache(path):
    """
    Loads the cached per-artist album list, if present and readable.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_album_cache(path, cache):
    """
    Writes the per-artist album list cache to disk.
    """
    try:
        with open(path, "w") as f:
            json.dump(cache, f)
    except OSError as e:
        logger.debug(f"Couldn't write album cache: {e}")


def get_cached_artist_albums(spotify, artist, market, cache, cache_lock, ttl_days, cache_stats=None):
    """
    Returns an artist's albums/singles, from cache when fresh enough.

    Caching only the album list (not which track gets picked) keeps track
    selection random on every run while skipping the artist_albums request
    for artists already seen within `ttl_days`.
    """
    key = f"{artist['id']}:{market}"
    caching_enabled = cache is not None and ttl_days > 0

    if caching_enabled:
        with cache_lock:
            entry = cache.get(key)
            fresh = entry and (time.time() - entry.get("fetched_at", 0)) < ttl_days * 86400
            if fresh and cache_stats is not None:
                cache_stats["hits"] += 1
        if fresh:
            return entry["albums"]

    album_page = fetch_artist_album_page(
        spotify,
        artist["id"],
        market=market,
        artist_name=artist.get("name"),
    )
    albums = [
        {"id": album["id"], "name": album.get("name")}
        for album in album_page.get("items", [])
        if album.get("id")
    ]

    if caching_enabled:
        with cache_lock:
            if cache_stats is not None:
                cache_stats["misses"] += 1
            cache[key] = {"fetched_at": time.time(), "albums": albums}

    return albums


def pick_random_artist_album(albums, tried_album_ids=None):
    """
    Picks one random album or single from an already-fetched album page.

    Args:
        albums (list): Album or single items.
        tried_album_ids (set): Album IDs already attempted for this artist.

    Returns:
        A Spotify album dict, or None if none could be found.
    """
    tried_album_ids = tried_album_ids if tried_album_ids is not None else set()
    available_albums = [
        album
        for album in albums
        if album.get("id") and album.get("id") not in tried_album_ids
    ]
    if not available_albums:
        return None

    album = random.choice(available_albums)
    tried_album_ids.add(album["id"])
    return album


def fetch_album_tracks(spotify, album_id, artist_id, market="NL", limit=50, album_name=None):
    """
    Fetches tracks from an album that include the requested artist.

    Args:
        spotify: The authenticated Spotipy client.
        album_id (str): Spotify album ID.
        artist_id (str): Spotify artist ID.
        market (str): ISO 3166-1 alpha-2 country code for track availability.
        limit (int): Page size for each request.

    Returns:
        A list of Spotify track dicts.
    """
    tracks = []
    offset = 0

    while True:
        result = spotify_call(
            "GET /v1/albums/{album_id}/tracks",
            spotify.album_tracks,
            album_id,
            context=album_name,
            limit=limit,
            offset=offset,
            market=market,
        )
        items = result.get("items", [])
        tracks.extend(
            track
            for track in items
            if any(track_artist.get("id") == artist_id for track_artist in track.get("artists", []))
        )

        if not result.get("next"):
            break
        offset += limit

    return tracks


def weighted_sample_without_replacement(items, weights, count):
    """
    Picks `count` items at random without replacement, weighted by `weights`.
    """
    items = list(items)
    weights = [max(weight, 1e-9) for weight in weights]
    chosen = []

    for _ in range(min(count, len(items))):
        pick = random.uniform(0, sum(weights))
        cumulative = 0.0
        for index, weight in enumerate(weights):
            cumulative += weight
            if cumulative >= pick:
                chosen.append(items.pop(index))
                weights.pop(index)
                break

    return chosen


def search_artist_tracks(spotify, artist, count=1, market="US", limit=10):
    """
    Searches tracks by artist name and keeps only exact artist ID matches.

    Less popular tracks are weighted higher, so results skew toward deep cuts
    over the artist's most popular hits.
    """
    artist_name = artist.get("name", "")
    result = spotify_call(
        "GET /v1/search",
        spotify.search,
        q=f'artist:"{artist_name}"',
        type="track",
        market=market,
        limit=limit,
        context=artist_name,
    )
    tracks = [
        track
        for track in result.get("tracks", {}).get("items", [])
        if any(track_artist.get("id") == artist["id"] for track_artist in track.get("artists", []))
    ]
    weights = [max(1, 101 - track.get("popularity", 50)) for track in tracks]
    return weighted_sample_without_replacement(tracks, weights, count)


def choose_random_artist_tracks(
    spotify,
    artist,
    count=1,
    market="US",
    album_attempts=3,
    track_source="search",
    search_results=10,
    album_cache=None,
    album_cache_lock=None,
    album_cache_ttl_days=30,
    album_cache_stats=None,
):
    """
    Chooses random tracks from random albums or singles by an artist.

    Args:
        spotify: The authenticated Spotipy client.
        artist: A Spotify artist dict.
        count (int): The number of random tracks to get.
        market (str): ISO 3166-1 alpha-2 country code for track availability.
        album_attempts (int): Maximum random albums or singles to try.
        track_source (str): Whether to find tracks using search or albums.
        search_results (int): Search results to request per artist.
        album_cache (dict): Shared cache of per-artist album lists.
        album_cache_lock (threading.Lock): Lock guarding `album_cache`.
        album_cache_ttl_days (int): How long a cached album list stays fresh.
        album_cache_stats (dict): Shared hit/miss counters for `album_cache`.

    Returns:
        A list of random Spotify track dicts.
    """
    if track_source == "search":
        return search_artist_tracks(
            spotify,
            artist,
            count=count,
            market=market,
            limit=search_results,
        )

    selected_tracks = []
    seen_track_ids = set()
    tried_album_ids = set()
    albums = get_cached_artist_albums(
        spotify,
        artist,
        market,
        album_cache,
        album_cache_lock,
        album_cache_ttl_days,
        album_cache_stats,
    )

    for _ in range(album_attempts):
        album = pick_random_artist_album(albums, tried_album_ids=tried_album_ids)
        if not album:
            break

        tracks = fetch_album_tracks(
            spotify,
            album["id"],
            artist["id"],
            market=market,
            album_name=album.get("name"),
        )
        random.shuffle(tracks)

        for track in tracks:
            track_id = track.get("id")
            if not track_id or track_id in seen_track_ids:
                continue

            seen_track_ids.add(track_id)
            selected_tracks.append(track)
            if len(selected_tracks) >= count:
                return selected_tracks

    return selected_tracks


async def get_random_artist_tracks(
    spotify,
    artist,
    count=1,
    market="US",
    album_attempts=3,
    track_source="search",
    search_results=10,
    album_cache=None,
    album_cache_lock=None,
    album_cache_ttl_days=30,
    album_cache_stats=None,
):
    """
    Fetches a number of random tracks for a given artist asynchronously.
    """
    try:
        return await asyncio.to_thread(
            choose_random_artist_tracks,
            spotify,
            artist,
            count,
            market,
            album_attempts,
            track_source,
            search_results,
            album_cache,
            album_cache_lock,
            album_cache_ttl_days,
            album_cache_stats,
        )
    except Exception as e:
        if isinstance(e, SpotifyException):
            raise
        logger.error(f"Couldn't get tracks for {artist.get('name', 'unknown')}: {e}")
        return []


def fetch_all_followed_artists(spotify, limit=50):
    """
    Fetches all artists followed by the current Spotify user.

    Args:
        spotify: The authenticated Spotipy client.
        limit (int): Page size for each request.

    Returns:
        A list of Spotify artist dicts.
    """
    artists = []
    after = None

    while True:
        result = spotify_call(
            "GET /v1/me/following",
            spotify.current_user_followed_artists,
            limit=limit,
            after=after,
        )
        page = result.get("artists", {})
        items = page.get("items", [])
        artists.extend(items)

        after = page.get("cursors", {}).get("after")
        if not after or not items:
            break

    return artists


def get_or_create_playlist(spotify, user_id, name, description, public=False):
    """
    Gets an existing playlist by name or creates a new one.

    Args:
        spotify: The authenticated Spotipy client.
        user_id (str): Spotify user ID.
        name (str): The name of the playlist.
        description (str): The description for a new playlist.
        public (bool): Whether a newly-created playlist should be public.

    Returns:
        A tuple containing the playlist dict and a boolean indicating if it existed.
    """
    offset = 0
    limit = 50

    while True:
        result = spotify_call(
            "GET /v1/me/playlists",
            spotify.current_user_playlists,
            limit=limit,
            offset=offset,
        )
        playlists = result.get("items", [])
        for playlist in playlists:
            if playlist.get("name") == name and playlist.get("owner", {}).get("id") == user_id:
                logger.debug(f"Found existing playlist: {name}")
                return playlist, True

        if not result.get("next"):
            break
        offset += limit

    logger.debug(f"Creating new playlist: {name}")
    playlist = spotify_call(
        "POST /v1/users/{user_id}/playlists",
        spotify.current_user_playlist_create,
        context=name,
        name=name,
        public=public,
        description=description,
    )
    return playlist, False


def get_playlist_age_hours(spotify, playlist_id):
    """
    Returns hours since the playlist's tracks were last added, or None if
    the playlist has no tracks yet.
    """
    result = spotify_call(
        "GET /v1/playlists/{playlist_id}/tracks",
        spotify.playlist_items,
        playlist_id,
        fields="items(added_at)",
        limit=1,
    )
    items = result.get("items", [])
    added_at = items[0].get("added_at") if items else None
    if not added_at:
        return None

    added_at_dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - added_at_dt).total_seconds() / 3600


def replace_playlist_tracks(spotify, playlist_id, track_uris):
    """
    Replaces playlist contents, adding tracks in Spotify's 100-item batches.
    """
    first_batch = track_uris[:100]
    spotify_call(
        "PUT /v1/playlists/{playlist_id}/tracks",
        spotify.playlist_replace_items,
        playlist_id,
        first_batch,
    )

    for index in range(100, len(track_uris), 100):
        spotify_call(
            "POST /v1/playlists/{playlist_id}/tracks",
            spotify.playlist_add_items,
            playlist_id,
            track_uris[index:index + 100],
        )


def parse_arguments():
    """
    Parses command-line arguments.

    Returns:
        An argparse.Namespace object containing the arguments.
    """
    parser = argparse.ArgumentParser(
        description="Spotify DNA: Create a playlist with an artist-by-artist shuffle."
    )
    parser.add_argument(
        "--cache-file",
        default=str(Path.home() / ".spotify-dna-cache"),
        help="Path to Spotipy OAuth token cache",
    )
    parser.add_argument(
        "--playlist-name",
        default="Spotify DNA",
        help="Name of the playlist",
    )
    parser.add_argument(
        "--min-playlist-age-hours",
        type=float,
        default=None,
        help="Skip regenerating an existing playlist if it was last updated more recently than this",
    )
    parser.add_argument(
        "--tracks-per-artist",
        type=int,
        default=1,
        help="Number of tracks per artist",
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        help="Maximum total tracks in playlist",
    )
    parser.add_argument(
        "--market",
        default="US",
        help="ISO 3166-1 alpha-2 market for album and track availability, e.g. US, NL, ES",
    )
    parser.add_argument(
        "--album-attempts",
        type=int,
        default=1,
        help="Maximum random albums or singles to try per artist",
    )
    parser.add_argument(
        "--track-source",
        choices=("search", "albums"),
        default="search",
        help="How to find artist tracks; search uses fewer requests, albums samples releases",
    )
    parser.add_argument(
        "--search-results",
        type=int,
        default=10,
        help="Search results to request per artist when --track-source search is used; max 10",
    )
    parser.add_argument(
        "--album-cache-file",
        default=str(Path.home() / ".spotify-dna-album-cache.json"),
        help="Path to the per-artist album list cache used by --track-source albums",
    )
    parser.add_argument(
        "--album-cache-ttl-days",
        type=int,
        default=30,
        help="How long a cached artist album list stays fresh; 0 disables the cache",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Maximum number of artists to process at the same time",
    )
    parser.add_argument(
        "--requests-timeout",
        type=float,
        default=5,
        help="Spotipy requests timeout in seconds",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        help="Spotipy total retry count; default 0 avoids duplicate retry messages",
    )
    parser.add_argument(
        "--status-retries",
        type=int,
        default=0,
        help="Spotipy retry count for HTTP status codes in --status-forcelist; default 0 stops on rate limits",
    )
    parser.add_argument(
        "--backoff-factor",
        type=float,
        default=0.3,
        help="Spotipy/urllib3 retry backoff factor",
    )
    parser.add_argument(
        "--status-forcelist",
        type=parse_status_forcelist,
        default=parse_status_forcelist(DEFAULT_STATUS_FORCELIST),
        help=f"Comma-separated HTTP status codes Spotipy should retry; default: {DEFAULT_STATUS_FORCELIST}",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Create the playlist as public if it does not exist",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output, including API request counts",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress normal progress output; only warnings and errors are printed. Useful for cron",
    )
    return parser.parse_args()


def validate_environment():
    """
    Verifies that the required Spotipy OAuth environment variables are set.
    """
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        logger.error("Missing required environment variable(s): " + ", ".join(missing))
        logger.error(
            "Set them in the same terminal before running the script, for example:\n"
            'export SPOTIPY_CLIENT_ID="your-client-id"\n'
            'export SPOTIPY_CLIENT_SECRET="your-client-secret"\n'
            'export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8888/callback"'
        )
        sys.exit(1)


async def main():
    """
    The main asynchronous function to run the script logic.
    """
    args = parse_arguments()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("spotipy").setLevel(logging.CRITICAL)
    if args.quiet and args.verbose:
        logger.error("--quiet and --verbose cannot be used together")
        sys.exit(1)
    if args.quiet:
        logger.setLevel(logging.WARNING)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.tracks_per_artist < 1:
        logger.error("--tracks-per-artist must be at least 1")
        sys.exit(1)

    if args.max_tracks is not None and args.max_tracks < 1:
        logger.error("--max-tracks must be at least 1")
        sys.exit(1)

    if args.min_playlist_age_hours is not None and args.min_playlist_age_hours < 0:
        logger.error("--min-playlist-age-hours must be at least 0")
        sys.exit(1)

    if args.album_attempts < 1:
        logger.error("--album-attempts must be at least 1")
        sys.exit(1)

    if args.search_results < 1 or args.search_results > 10:
        logger.error("--search-results must be between 1 and 10")
        sys.exit(1)

    if args.album_cache_ttl_days < 0:
        logger.error("--album-cache-ttl-days must be at least 0")
        sys.exit(1)

    if args.concurrency < 1:
        logger.error("--concurrency must be at least 1")
        sys.exit(1)

    if args.requests_timeout <= 0:
        logger.error("--requests-timeout must be greater than 0")
        sys.exit(1)

    if args.retries < 0:
        logger.error("--retries must be at least 0")
        sys.exit(1)

    if args.status_retries < 0:
        logger.error("--status-retries must be at least 0")
        sys.exit(1)

    if args.backoff_factor < 0:
        logger.error("--backoff-factor must be at least 0")
        sys.exit(1)

    validate_environment()

    logger.info("Authenticating with Spotify...")
    auth_manager = SpotifyOAuth(
        scope=SCOPES,
        cache_path=args.cache_file,
        open_browser=True,
    )
    spotify = spotipy.Spotify(
        auth_manager=auth_manager,
        requests_timeout=args.requests_timeout,
        retries=args.retries,
        status_retries=args.status_retries,
        backoff_factor=args.backoff_factor,
        status_forcelist=args.status_forcelist,
    )

    try:
        user = await asyncio.to_thread(
            spotify_call,
            "GET /v1/me",
            spotify.current_user,
        )
    except Exception as e:
        if is_spotify_rate_limit_error(e):
            exit_after_rate_limit(e)
        logger.error(
            "Spotify authentication failed. Set SPOTIPY_CLIENT_ID, "
            "SPOTIPY_CLIENT_SECRET, and SPOTIPY_REDIRECT_URI, then try again."
        )
        logger.debug(f"Authentication error: {e}")
        sys.exit(1)

    user_id = user["id"]
    logger.info(f"Authenticated as: {user.get('display_name') or user_id}")
    description = (
        "A playlist generated by Spotify DNA: an artist-by-artist shuffle of my "
        "followed artists."
    )
    logger.info(f"Looking for playlist: {args.playlist_name}")
    try:
        playlist, exists = await asyncio.to_thread(
            get_or_create_playlist,
            spotify,
            user_id,
            args.playlist_name,
            description,
            args.public,
        )
    except SpotifyException as e:
        if is_spotify_rate_limit_error(e):
            exit_after_rate_limit(e)
        exit_after_spotify_api_error(e)
    action = "Using existing playlist" if exists else "Created playlist"
    logger.info(f"{action}: {playlist['name']}")

    if exists and args.min_playlist_age_hours is not None:
        try:
            age_hours = await asyncio.to_thread(get_playlist_age_hours, spotify, playlist["id"])
        except SpotifyException as e:
            if is_spotify_rate_limit_error(e):
                exit_after_rate_limit(e)
            exit_after_spotify_api_error(e)

        if age_hours is not None and age_hours < args.min_playlist_age_hours:
            logger.info(
                f"Playlist '{playlist['name']}' was last updated {age_hours:.1f}h ago "
                f"(< --min-playlist-age-hours {args.min_playlist_age_hours}h). Skipping."
            )
            if args.verbose:
                log_api_request_summary()
            sys.exit(0)

    logger.info("Fetching followed artists...")
    try:
        followed_artists = await asyncio.to_thread(fetch_all_followed_artists, spotify)
    except SpotifyException as e:
        if is_spotify_rate_limit_error(e):
            exit_after_rate_limit(e)
        exit_after_spotify_api_error(e)
    if not followed_artists:
        logger.error("No followed artists found")
        sys.exit(1)

    total_followed_artists = len(followed_artists)
    logger.info(f"Found {total_followed_artists} followed artists")
    random.shuffle(followed_artists)

    if args.max_tracks:
        num_artists = (args.max_tracks + args.tracks_per_artist - 1) // args.tracks_per_artist
        followed_artists = followed_artists[:num_artists]

    target_tracks = len(followed_artists) * args.tracks_per_artist
    if args.track_source == "search":
        logger.info(
            "Searching for up to %s track(s) across %s artist(s) "
            "(%s track(s) per artist, source search, %s search result(s), concurrency %s)",
            target_tracks,
            len(followed_artists),
            args.tracks_per_artist,
            args.search_results,
            args.concurrency,
        )
    else:
        logger.info(
            "Searching for up to %s track(s) across %s artist(s) "
            "(%s track(s) per artist, source albums, %s album attempt(s), concurrency %s)",
            target_tracks,
            len(followed_artists),
            args.tracks_per_artist,
            args.album_attempts,
            args.concurrency,
        )

    album_cache = {}
    album_cache_lock = threading.Lock()
    album_cache_stats = {"hits": 0, "misses": 0}
    if args.track_source == "albums":
        album_cache = await asyncio.to_thread(load_album_cache, args.album_cache_file)
        logger.info(f"Album cache: {len(album_cache)} artist(s) loaded from {args.album_cache_file}")

    pending_tasks = set()
    artist_iterator = iter(followed_artists)

    def schedule_next_artist():
        try:
            artist = next(artist_iterator)
        except StopIteration:
            return

        pending_tasks.add(asyncio.create_task(get_random_artist_tracks(
            spotify,
            artist,
            args.tracks_per_artist,
            args.market,
            args.album_attempts,
            args.track_source,
            args.search_results,
            album_cache,
            album_cache_lock,
            args.album_cache_ttl_days,
            album_cache_stats,
        )))

    for _ in range(min(args.concurrency, len(followed_artists))):
        schedule_next_artist()

    all_tracks = []
    logger.info("Finding tracks...")

    try:
        while pending_tasks:
            done_tasks, pending_tasks = await asyncio.wait(
                pending_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for future in done_tasks:
                tracks = await future
                for track in tracks:
                    all_tracks.append(track)
                    artists = ", ".join(artist["name"] for artist in track.get("artists", []))
                    logger.debug(f"Found '{track['name']}' by {artists}")

                schedule_next_artist()
    except SpotifyException as e:
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        if args.track_source == "albums":
            logger.info(
                f"Album cache: {album_cache_stats['hits']} hit(s), "
                f"{album_cache_stats['misses']} miss(es)"
            )
            await asyncio.to_thread(save_album_cache, args.album_cache_file, album_cache)
        if is_spotify_rate_limit_error(e):
            exit_after_rate_limit(e)
        exit_after_spotify_api_error(e)

    if args.track_source == "albums":
        logger.info(
            f"Album cache: {album_cache_stats['hits']} hit(s), "
            f"{album_cache_stats['misses']} miss(es)"
        )
        await asyncio.to_thread(save_album_cache, args.album_cache_file, album_cache)

    random.shuffle(all_tracks)

    if args.max_tracks:
        all_tracks = all_tracks[:args.max_tracks]

    if all_tracks:
        track_uris = [track["uri"] for track in all_tracks]
        logger.info(f"Selected {len(track_uris)} track(s)")

        if exists:
            logger.info("Replacing existing playlist contents...")

        logger.info(f"Adding {len(track_uris)} tracks to playlist '{playlist['name']}'...")
        try:
            await asyncio.to_thread(replace_playlist_tracks, spotify, playlist["id"], track_uris)
        except SpotifyException as e:
            if is_spotify_rate_limit_error(e):
                exit_after_rate_limit(e)
            exit_after_spotify_api_error(e)
        logger.info(f"\nDone! Added {len(track_uris)} tracks to playlist '{playlist['name']}'")
    else:
        logger.info("No tracks found to add to the playlist.")

    if args.verbose:
        log_api_request_summary()


if __name__ == "__main__":
    asyncio.run(main())
