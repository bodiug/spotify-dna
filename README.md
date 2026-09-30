# Spotify DNA

Create a Spotify playlist with random tracks from the artists you follow. By default, the script picks one track per artist using Spotify search, with less popular tracks weighted higher.

It creates a playlist named **Spotify DNA**, or replaces its contents if you already own a playlist with that name. New playlists are private unless you use `--public`.

## Setup

You need [uv](https://github.com/astral-sh/uv) and an app from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard). Each user should use their own app credentials.

```bash
git clone https://github.com/bodiug/spotify-dna.git
cd spotify-dna
chmod +x spotify_dna.py
```

Add `http://127.0.0.1:8888/callback` as a redirect URI in your Spotify app, then set these environment variables:

```bash
export SPOTIPY_CLIENT_ID="your-client-id"
export SPOTIPY_CLIENT_SECRET="your-client-secret"
export SPOTIPY_REDIRECT_URI="http://127.0.0.1:8888/callback"
```

Run the script:

```bash
./spotify_dna.py
```

`uv` installs the dependencies automatically. The first run opens a browser for Spotify login and permission to read your followed artists and playlists, and modify playlists. Login tokens are stored in `~/.spotify-dna-cache`; keep this file and your client secret private. If OAuth scopes change, delete this token cache and log in again.

## Usage

Create a smaller playlist with up to 50 tracks available in the Netherlands:

```bash
./spotify_dna.py --playlist-name "My Quick Mix" --max-tracks 50 --market NL
```

Pick up to five tracks per artist:

```bash
./spotify_dna.py --tracks-per-artist 5 --max-tracks 100
```

For a daily cron job, suppress normal output and skip playlists updated less than 24 hours ago:

```bash
./spotify_dna.py --quiet --min-playlist-age-hours 24
```

The age check uses the first track's `added_at` timestamp. Empty playlists are always regenerated.

| Option | Default | Purpose |
| --- | --- | --- |
| `--playlist-name` | `Spotify DNA` | Playlist to create or replace |
| `--max-tracks` | No limit | Maximum playlist size |
| `--tracks-per-artist` | `1` | Tracks to pick per artist |
| `--market` | `US` | Country for track availability |
| `--track-source` | `search` | Track selection using `search` or `albums` |
| `--min-playlist-age-hours` | No minimum | Skip recently updated playlists |
| `--public` | Off | Make newly created playlists public |
| `--verbose` | Off | Show track details and API request counts |
| `--quiet` | Off | Show only warnings and errors |

`--quiet` and `--verbose` cannot be combined. For all options, including cache paths, concurrency, timeouts and retries, run `./spotify_dna.py --help`.

## Albums mode and caching

**Albums mode is the recommended choice for a richer, more varied playlist and discovering lesser-known tracks from the artists you follow.** It picks tracks from a random album or single, giving album tracks and less prominent releases more opportunity to appear than in search mode. Search mode is faster and uses fewer API requests, but its selection is limited to the returned search results.

```bash
./spotify_dna.py --track-source albums --max-tracks 25
```

This mode fetches up to 10 releases per artist and tries one by default. Increase `--album-attempts` to try more releases when needed; this can use more API requests.

**Always keep the album cache enabled in albums mode.** It stores album lists in `~/.spotify-dna-album-cache.json` for 30 days by default. Reuse the same file across runs and keep `--album-cache-ttl-days` greater than zero. Deleting or disabling it forces the script to fetch those lists again. Track choices stay random because only album lists are cached.

With many followed artists, it can take several days of spaced-out runs to build the cache before runs stop hitting rate limits. Progress is saved even when a Spotify rate limit or quota error stops a run. Album tracks still need API requests on every run, so caching does not guarantee that rate limits disappear. Cache hit/miss counts are printed during runs.

## Rate limits

The script stops on Spotify rate limits by default and prints API request counts. Wait for the reported `Retry-After` period before trying again. Reduce `--max-tracks`, space out scheduled runs, or use the default search mode to reduce requests.

`QUOTA_EXCEEDED` indicates Development Mode quota exhaustion. Check your app's status in the Spotify Developer Dashboard and wait for the quota to reset; repeatedly restarting the script will not resolve it.

To let Spotipy wait and retry rate limits automatically:

```bash
./spotify_dna.py --retries 3 --status-retries 3 --status-forcelist 429,500,502,503,504
```

Tracks are collected before existing playlist contents are replaced. Updates use batches of 100 tracks, so an API failure during the update can leave a partially filled playlist.

## License

[MIT](LICENSE).
