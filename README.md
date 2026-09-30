# Spotify DNA

Create a unique playlist with an artist-by-artist shuffle of your Spotify followed artists.

---

## Description

Spotify DNA offers a fresh way to rediscover your music library. Instead of a completely random shuffle, this script creates a playlist by taking a single, random track from a random album or single by each artist you follow on Spotify. The result is a unique "artist-by-artist" shuffle that serves as a DNA sample of your musical taste.

It's fast, efficient, and a great way to listen to a broad cross-section of the artists you love without getting stuck on one album or style.

## Features

-   **Artist-by-Artist Shuffle**: Creates a playlist with one random album or single track per followed artist.
-   **Concurrent Fetching**: Uses `asyncio` to fetch track information concurrently.
-   **Spotipy Retry Configuration**: Exposes Spotipy's own retry, timeout, status retry, and backoff options instead of custom HTTP handling.
-   **Customizable Playlists**:
    -   Set a maximum number of tracks for the playlist.
    -   Specify the number of tracks to choose per artist.
    -   Name your playlist whatever you like.
    -   Choose the market used for Spotify album and track availability.
    -   Tune concurrency, album retry attempts, and Spotipy retry behavior.
    -   Create the playlist as public when it does not already exist.
-   **Verbose Mode**: Optional detailed output for debugging or curiosity.
-   **Safe Playlist Handling**: Replaces an existing playlist only after all new tracks have been successfully fetched.

## Requirements

-   [uv](https://github.com/astral-sh/uv)
-   A Spotify app from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)

The script is self-contained and uses a shebang to declare its own Python dependencies. When you run it, `uv` will automatically create a virtual environment and install the correct version of the `spotipy` library (`2.26.0`).

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/bodiug/spotify-dna.git
    cd spotify-dna
    ```

2.  **Make the script executable:**
    ```bash
    chmod +x spotify_dna.py
    ```

## Authentication

Create a Spotify app in the Spotify Developer Dashboard and add a redirect URI, for example:

```text
http://127.0.0.1:8888/callback
```

Then export your Spotify app credentials:

```bash
export SPOTIPY_CLIENT_ID="your-client-id"
export SPOTIPY_CLIENT_SECRET="your-client-secret"
export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8888/callback"
```

The first time you run the script, Spotipy will open a browser where you can authenticate with your Spotify account.

After a successful login, the library will save your session credentials to `~/.spotify-dna-cache`. On subsequent runs, the script will use this file to log in automatically.

The script requests these Spotify scopes:

```text
user-follow-read playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public
```

If the requested scopes change after you have already logged in once, remove the cached token and run the script again:

```bash
rm ~/.spotify-dna-cache
```

Spotify removed the `GET /artists/{id}/top-tracks` endpoint for Development Mode apps in 2026. By default, this script now uses Spotify search to find tracks with one request per selected artist, then filters the results by the exact Spotify artist ID. Among the matching search results, tracks with lower `popularity` are weighted higher, so picks skew toward deeper cuts instead of an artist's biggest hits, while staying random. The older albums-based mode is still available with `--track-source albums`; it fetches one page of up to 10 albums or singles per artist and then album tracks, sampling across an artist's whole discography rather than just their most relevant search hits, but at roughly double the API requests per artist. To keep repeated `albums` runs cheaper, the artist's album list is cached locally (see `--album-cache-file` and `--album-cache-ttl-days`); only the album list is cached, not which track gets picked, so track selection stays random on every run.

## Usage

The script is run directly from the command line. `uv` handles the execution and dependency management automatically.

```bash
./spotify_dna.py [OPTIONS]
```

### Arguments

| Argument              | Default                 | Description                                                               |
| --------------------- | ----------------------- | ------------------------------------------------------------------------- |
| `--cache-file`        | `~/.spotify-dna-cache`  | Path to your Spotipy OAuth token cache.                                   |
| `--playlist-name`     | `Spotify DNA`           | The name of the playlist to create or update.                             |
| `--min-playlist-age-hours` | (none)             | Skip regenerating an existing playlist if it was last updated more recently than this. |
| `--tracks-per-artist` | `1`                     | The number of random album or single tracks to select from each artist.   |
| `--max-tracks`        | (none)                  | The maximum total number of tracks to include in the playlist.            |
| `--market`            | `US`                    | Spotify market for album and track availability, such as `US`, `NL`, or `ES`. |
| `--album-attempts`    | `1`                     | Maximum random albums or singles to try per artist.                       |
| `--track-source`      | `search`                | How to find artist tracks: `search` uses fewer requests, `albums` samples releases. |
| `--search-results`    | `10`                    | Search results to request per artist when `--track-source search` is used. |
| `--album-cache-file`  | `~/.spotify-dna-album-cache.json` | Path to the per-artist album list cache used by `--track-source albums`. |
| `--album-cache-ttl-days` | `30`                 | How long a cached artist album list stays fresh; `0` disables the cache.  |
| `--concurrency`       | `1`                     | Maximum number of artists to process at the same time.                    |
| `--requests-timeout`  | `5`                     | Spotipy requests timeout in seconds.                                      |
| `--retries`           | `0`                     | Spotipy total retry count.                                                |
| `--status-retries`    | `0`                     | Spotipy retry count for HTTP status codes in `--status-forcelist`.        |
| `--backoff-factor`    | `0.3`                   | Spotipy/urllib3 retry backoff factor.                                     |
| `--status-forcelist`  | `500,502,503,504`       | Comma-separated HTTP status codes Spotipy should retry.                   |
| `--public`            | (disabled)              | Create the playlist as public if it does not already exist.               |
| `-v`, `--verbose`     | (disabled)              | Enable verbose output, including track-finding steps and API request counts.|
| `-q`, `--quiet`       | (disabled)              | Suppress normal progress output; only warnings and errors are printed. Cannot be combined with `--verbose`. |

---

## Examples

### Basic Usage

Create a playlist named "Spotify DNA" with one random album or single track from each artist you follow.

```bash
./spotify_dna.py
```

### Use the Netherlands Market

Create a playlist using Spotify's Netherlands market for album and track availability.

```bash
./spotify_dna.py --market NL
```

### Create a Smaller Playlist

Create a playlist named "My Quick Mix" with a maximum of 50 tracks.

```bash
./spotify_dna.py --playlist-name "My Quick Mix" --max-tracks 50
```

### Conservative Run

Create a smaller run that tries one album or single per artist and processes one artist at a time.

```bash
./spotify_dna.py --market NL --max-tracks 25
```

### Skip Runs That Are Too Soon

Useful when the script runs on a schedule (e.g. a cron job) and you only want it to actually regenerate the playlist once a day. If the playlist already exists and its tracks were added less than 24 hours ago, the script logs a message and exits (status 0) without touching followed artists, tracks, or the playlist:

```bash
./spotify_dna.py --min-playlist-age-hours 24
```

This checks the `added_at` timestamp of the playlist's first track (one extra Spotify request), so it reflects when the script itself last replaced the playlist contents — not when the playlist was created. A newly-created, still-empty playlist is always regenerated regardless of this setting.

### If Spotify Rate Limits You

Fetching followed artists usually needs only a few Spotify requests. The default `search` source normally uses one `GET /search` request per selected artist. The optional `albums` source uses more requests because it checks one albums/singles page and then album tracks for each selected artist.

By default, the script stops on Spotify rate limits instead of waiting for a long `Retry-After` value. Spotipy retries are also disabled by default so the script prints one clear rate-limit message. If you want Spotipy to retry rate limits and wait according to Spotify's headers, include `429` in the status forcelist and increase retries/status retries:

```bash
./spotify_dna.py --market NL --retries 3 --status-forcelist 429,500,502,503,504 --status-retries 3
```

When the script stops on a Spotify 429, it always prints the Spotify API request count by endpoint and a total, even without `--verbose`.

Spotify may also return a 429 response with `reason: QUOTA_EXCEEDED` for Development Mode quota exhaustion. The script reports this separately because quota is not the same as a short rolling rate limit.

For Development Mode apps:

-   The app owner's Spotify account must have Premium.
-   Only allowlisted Spotify users can authenticate, up to 5 users.
-   Quota is counted per Spotify developer account, not per Client ID.
-   Multiple Development Mode Client IDs share the same quota pool.
-   Endpoints are grouped into quota buckets, so requests to related endpoints may count toward the same shared limit.
-   Creating another Client ID will not increase the shared quota.
-   Check **App Status** in the Spotify Developer Dashboard to see whether the app is in Development Mode or Extended Quota Mode.
-   If the script reports `QUOTA_EXCEEDED`, use the printed `Retry-After` value if Spotify provides one, try again after the quota resets, or request Extended Quota Mode.

### Get More Variety

Create a playlist with up to 5 tracks per artist, instead of just one track.

```bash
./spotify_dna.py --tracks-per-artist 5 --max-tracks 100
```

### Use Albums Mode

Use the older albums/singles sampling approach instead of search. This samples across an artist's whole discography rather than just their top search hits, at roughly double the API requests per artist; the artist's album list is cached locally afterward so repeated runs need fewer requests.

**Always keep the album cache enabled when using `--track-source albums`.** The default cache lasts 30 days. Reuse the same cache file between runs and keep `--album-cache-ttl-days` greater than zero; deleting or disabling the cache forces the script to fetch those album lists again.

With many followed artists, it can take several days of spaced-out runs before the cache fills and runs stop hitting rate limits. The script saves the album lists fetched so far even when it stops on a Spotify rate limit or quota error, so later runs can reuse that progress. Wait for the reported `Retry-After` period before trying again, and use a smaller `--max-tracks` if needed. The cache only stores album lists: album tracks still require API requests on every run, so caching does not guarantee that rate limits disappear.

```bash
./spotify_dna.py --track-source albums --album-attempts 1
```

The script always prints how many artists were loaded from the album cache and, at the end of the run, how many were served from cache (`hit`) versus freshly fetched from Spotify (`miss`), for example:

```text
Album cache: 200 artist(s) loaded from /Users/you/.spotify-dna-album-cache.json
...
Album cache: 180 hit(s), 20 miss(es)
```

Adjust the album list cache lifetime while keeping caching enabled:

```bash
./spotify_dna.py --track-source albums --album-cache-ttl-days 7
```

### Create a Public Playlist

Create the playlist as public if it does not already exist.

```bash
./spotify_dna.py --public
```

### Verbose Output

Run the script with detailed logging to see every step of the process.

```bash
./spotify_dna.py --verbose
```

Verbose output also prints a Spotify API request summary by endpoint, plus a total, after successful runs. Rate-limit and Development Mode quota exits always print this summary.

### Run from Cron

Use `--quiet` to suppress normal progress output, so cron only sends you mail when something actually goes wrong (a rate limit, an API error, or a missing environment variable still print, since those are warnings/errors):

```bash
./spotify_dna.py --quiet --min-playlist-age-hours 24
```

`--quiet` and `--verbose` are mutually exclusive.

## License

This project is licensed under the [MIT License](LICENSE).
