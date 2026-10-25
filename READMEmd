# SYSTEM PROMPT: AI CONTEXT FILE
> **TARGET AUDIENCE:** Large Language Models (LLMs) / AI Assistants.
> **PURPOSE:** This document provides full context of the project architecture, goals, and core code. Read this file to instantly understand the current state of development and how to assist the human user in integrating or expanding this module.

---

## 1. PROJECT OVERVIEW & CURRENT STATE
**Goal:** Integrate a seamless, zero-lag YouTube Music streaming experience directly into `Lecthon.py`, an existing Tkinter/`pygame.mixer`-based local music player ("Lecteur de Musique Pro").
**The Problem:** Directly passing web streams to `pygame.mixer` is highly unstable, leading to lag, freezing, and crashes.
**The Solution:** A background caching system (`youtube_manager.py`) that preemptively downloads the next recommendations as MP3 files to a temporary directory, maintaining a strict buffer (default: 3 tracks). The player's existing sidebar mode-switcher (previously "Lecteur" / "Editeur") now has a third mode, **"YouTube"**, that surfaces the user's personalized "Quick Picks" ("Sélection rapide") and lets playback seamlessly hand off between the local library and the YouTube buffer.

**Status: integrated and working end-to-end**, including two authentication paths (OAuth and browser-header), ffmpeg-dependency handling, and a round of robustness/code-quality hardening. Remaining open item: periodic verification that the browser-header auth method still parses correctly if ytmusicapi's expected input format changes in a future release (see §5).

## 2. FILES & ARCHITECTURE
Two files, kept deliberately separate (by user's explicit choice — do not merge them):

- **`Lecthon.py`** — the pre-existing music player (~3700 lines, Tkinter + `pygame.mixer`). Owns the GUI, playback engine, library/playlists/queue logic, and now also the `YoutubeTab` UI class plus the YouTube-related methods added to `MusicPlayer` (see §3).
- **`youtube_manager.py`** — self-contained `YouTubeManager` class: all `ytmusicapi`/`yt-dlp` interaction, buffering, caching, retries, and disk cleanup. Imported by `Lecthon.py` via a **protected import**:
  ```python
  try:
      from youtube_manager import YouTubeManager
      YOUTUBE_TAB_AVAILABLE = True
  except ImportError:
      YOUTUBE_TAB_AVAILABLE = False
  ```
  If the import fails (missing dependency or missing file), the app still launches normally, with the YouTube tab replaced by an explanatory placeholder label. **Both files must live in the same directory.**

### Dependencies added
`ytmusicapi`, `yt-dlp`, and a working **ffmpeg** binary on the `PATH` (used by `yt-dlp`'s `FFmpegExtractAudio` postprocessor to produce 192kbps MP3s; without it, downloads fail at the postprocessing step).

## 3. GUI INTEGRATION (`Lecthon.py`)

### Mode switcher (sidebar)
`setup_sidebar()` now builds **three** mode buttons instead of two: `btn_app_lecteur`, `btn_app_editeur`, `btn_app_youtube`. `update_app_mode_highlight(mode)` and the three `show_app_*()` methods handle showing/hiding `lecteur_app_frame` / `editor_app_frame` / `youtube_app_frame` and the sidebar's library navigation (hidden for both Editeur and YouTube modes, since it's not relevant there). Switching modes never touches `pygame.mixer` or any active timers — playback continues uninterrupted in the background regardless of which tab is visible.

### `YoutubeTab` class (new, defined near the end of `Lecthon.py`, before `if __name__ == "__main__":`)
Builds the tab's UI inside the frame `MusicPlayer` provides it. Responsibilities:
- **Connection UI**: a "🔌 Se connecter" button opens a small chooser (`connect()`) between the two auth methods — see §5 — then runs the (network-bound) `YouTubeManager(...)` construction in a background thread, updating status labels via `root.after(0, ...)` once done.
- **Sélection rapide list**: scrollable canvas of track rows (thumbnail, title, artist, "▶ Écouter" button), populated via `get_selection_rapide()`, also called in a background thread. Thumbnails are fetched and cached (`self.thumbnail_cache`, keyed by URL) to avoid re-downloading images already shown.
- **Buffer status bar**: polls `manager.ready_queue.qsize()` every 2s (`_status_loop`, self-rescheduling via `root.after`) and shows "Tampon : n/max prêt(s)", plus a "📻 Lancer la radio" button.
- Clicking "▶ Écouter" on a track: disables/relabels that specific button to "⏳ Téléchargement..." immediately (visual feedback — a bare click with no feedback was a real bug found and fixed during development, see §6), downloads the track ad hoc in a thread, then plays it and resets the button regardless of success/failure.

### `MusicPlayer` methods added/modified for YouTube playback
- **`play_track(track_path, add_to_history=True, override_info=None, is_youtube=False)`** — extended (previously had no `override_info`/`is_youtube` params). `override_info` supplies `{"titre", "album", "annee", "duree"}` for tracks absent from the local library (i.e. any YouTube-sourced file), so the "now playing" panel displays correctly. `is_youtube` sets `self.youtube_source_active`, which controls auto-chaining (below).
- **`play_next()`** — extended with a new fallback branch: if the manual/auto queues are empty and shuffle is off, and `self.youtube_source_active` is `True`, it calls `play_next_from_youtube_buffer()` instead of stopping. This makes YouTube playback behave like a continuous radio once started.
- **`play_next_from_youtube_buffer(manual=False, retries_left=5)`** — pulls from `manager.ready_queue`; on natural end-of-track auto-chaining it retries a few times (1.5s apart) if the buffer is momentarily empty (still downloading) before giving up, rather than stopping playback for a transient stall.
- **`play_youtube_item_now(item)`** — downloads (if not cached) and plays a specific track chosen from the Sélection rapide list. Returns an error string (or `None`) rather than popping a `messagebox` itself, so the calling `YoutubeTab` button can always reset its own state regardless of outcome.
- **`_update_youtube_cover(url)`** — fetches and displays the YouTube thumbnail as the "now playing" cover art (falls back to the existing placeholder cover if no URL/failure).
- **`stop()`** — now also resets `self.youtube_source_active = False`, so an explicit stop cleanly exits radio mode.
- **`on_closing()`** — now also calls `self.youtube_manager.stop()` if a manager exists, ensuring the background thread halts and the temp cache directory is deleted on app exit.

### Logging (added during the code-quality pass)
`Lecthon.py` previously had no logging at all. A module-level `logger = logging.getLogger("lecteur_pro")` was added; the three previously-bare `except: pass` blocks (in `load_playlists`, `update_cover_art`, `on_slide_release`) were narrowed to specific exceptions and now log at an appropriate level instead of failing silently with zero trace. A stray `print()` in `play_track`'s exception handler was replaced with `logger.error`.

## 4. `YouTubeManager` (`youtube_manager.py`) — CURRENT DESIGN

```python
class YouTubeManager:
    def __init__(self, oauth_path="oauth.json", client_id=None, client_secret=None,
                 buffer_size=3, max_cache_mb=200, history_path="download_history.json",
                 max_retries=3, history_save_every=5):
        ...
    def get_selection_rapide(self) -> list: ...          # Quick Picks for the GUI
    def get_next_online_track(self) -> Optional[dict]:   # non-blocking pop from the buffer
    def mark_track_played(self, filepath: str) -> None:  # optional immediate cache cleanup
    def stop(self) -> None:                               # clean shutdown + temp dir removal
```

Key internal mechanisms (full implementation lives in the file — this is a map, not a copy, to avoid duplicating ~390 lines here):

- **Anti-duplicate**: `self.seen_ids` (session memory) + `self.history_ids` (persisted to `history_path` as JSON, loaded on startup, saved periodically — every `history_save_every` downloads, not only at clean shutdown — via `_mark_history()`).
- **Retries with backoff**: `_call_with_retry()` for API calls, a dedicated loop in `_download_track()` for downloads (each has its own backoff cap: 30s vs 15s, since a stalled API call and a stalled download warrant different patience). Both use `_interruptible_sleep()` (wraps `threading.Event.wait()`) rather than plain `time.sleep()`, so `stop()` — which calls `self._stop_event.set()` — wakes any in-progress backoff immediately instead of leaving the thread blocked for up to 30s.
- **ffmpeg guard**: `_download_track()` checks `shutil.which("ffmpeg")` up front and raises a clear `RuntimeError` with install instructions rather than letting `yt-dlp` fail cryptically mid-postprocessing after wasting a full download.
- **Cache cleanup**: `_enforce_cache_limit()` deletes the oldest cached MP3s once `max_cache_mb` is exceeded; `mark_track_played()` is available for the player to call for immediate cleanup right after a track finishes, instead of waiting for the size limit.
- **Clean shutdown**: `stop()` sets `is_running=False` + `_stop_event.set()`, drains the queue (in case the loader thread is blocked on `put()`), joins the thread (5s timeout, it's a daemon thread so this is a courtesy, not a hard requirement), then `shutil.rmtree()`s the temp cache directory.
- **Logging**: module-level `logger = logging.getLogger("youtube_manager")`, used throughout instead of silent `except: pass`.

## 5. AUTHENTICATION — TWO SUPPORTED METHODS
Google requires either an OAuth flow or a browser-session snapshot; both are supported and selectable from `YoutubeTab.connect()`'s chooser dialog:

### A. OAuth (more robust, longer setup)
Requires a **Client ID + Client Secret** from Google Cloud Console (project with **YouTube Data API v3** enabled, OAuth client of type **"TVs and Limited Input devices"** — this requirement was added by Google in **November 2024** and is easy to miss if following older tutorials). Generate the credentials file with:
```
python -m ytmusicapi oauth
```
(note: `ytmusicapi` ships as a console-script entry point, **not** a `__main__.py` — so `python -m ytmusicapi ...` fails with "cannot be directly executed" on some setups; the plain `ytmusicapi oauth` command, or the full path to `Scripts/ytmusicapi.exe`, is the reliable invocation). `YouTubeManager` then builds an `OAuthCredentials(client_id=..., client_secret=...)` and passes it to `YTMusic(oauth_path, oauth_credentials=...)`. `YoutubeTab._get_client_credentials()` asks for these once and persists them locally to `youtube_client_credentials.json` (⚠️ contains the Client Secret in plaintext — not safe to commit/share).

### B. Browser headers (faster setup, can expire)
No Google Cloud project needed. User copies request headers from a `music.youtube.com` `/browse` request in DevTools using **"Copy as fetch (Node.js)"** specifically (the plain "Copy as fetch" browser variant deliberately omits the `cookie` header for security and will fail with `YTMusicUserError: missing x-goog-authuser, cookie`). The pasted payload must be trimmed to a 6-key JSON object (`Accept`, `Authorization`, `Content-Type`, `X-Goog-AuthUser`, `x-origin`, `Cookie`) before `ytmusicapi browser` will accept it. Result: `browser.json`, passed to `YouTubeManager(oauth_path=browser_path)` **without** `client_id`/`client_secret` (the fallback branch in `__init__` calls plain `YTMusic(oauth_path)`, which auto-detects the browser-auth format). Trade-off: expires if the browser session/cookies are invalidated, with no advance warning.

**Security note for any AI assistant reading this file**: both the OAuth token and especially the browser cookie are equivalent to live, authenticated Google session credentials. Never suggest posting them anywhere public, and flag this explicitly if a user shares them in a debugging context.

## 6. KNOWN GOTCHAS ENCOUNTERED DURING DEVELOPMENT (worth knowing before debugging further)
- **Spyder's standalone installer ships Python without `pip`** (intentional, to protect Spyder's own environment). Fix: install a separate full Python (python.org or Anaconda) and run/install dependencies with *that* interpreter — don't fight the bundled one.
- **`ytmusicapi` has no `__main__.py`** → `python -m ytmusicapi ...` fails; use the bare `ytmusicapi` command (or its full path in `Scripts/`) instead.
- **Chrome's "Copy as fetch" (non-Node.js) strips the `cookie` header** by design — always use **"Copy as fetch (Node.js)"** for `ytmusicapi browser` setup.
- **A button with no loading state looks broken even when it's working** — the first `play_now()` implementation gave zero visual feedback during a 10–20s download+convert, which read as "nothing happens" from the user's side. Any future action that does blocking I/O in a background thread should disable/relabel the triggering widget immediately and always reset it (success or failure) via a `root.after(0, ...)` callback.

## 7. SCOPE / LEGAL NOTE
This project downloads copyrighted audio from YouTube Music outside the official app, for personal offline caching — this sits in tension with YouTube's Terms of Service. It's scoped and intended for strictly personal use, not redistribution. Any AI assistant extending this project should keep that framing in mind and avoid adding features oriented toward distribution/sharing of downloaded content.

## 8. FUTURE IMPROVEMENT IDEAS (not yet implemented)
Ideas discussed but not yet built, grouped by theme. Listed here so a future session (human or AI) doesn't have to re-derive them from scratch.

### Usability
- **Real download progress bar**: `yt-dlp` supports progress hooks (%, speed) — more precise than the current static "⏳ Téléchargement..." label.
- **Buffer content preview**: show the titles of tracks currently being cached, not just a "2/3 ready" count.
- **YouTube Music search**: `ytmusicapi` exposes a `search()` method — a search bar would go beyond the Sélection rapide only.
- **"Keep this track" button**: cached YouTube files are always eventually deleted (cache limit or post-playback cleanup). A button to move the MP3 into the permanent local library would be a clear UX win.

### Robustness
- **Session-expiry detection**: if browser-auth expires, the app currently just accumulates silent failures in the logs. Detecting a 401/403 and prompting the user to reconnect would surface the problem clearly instead of silently degrading.
- **Auto-reconnect on startup**: remember the oauth/browser.json path used last time, to avoid re-clicking "Se connecter" every launch.
- **Config file** (`config.json`) for `buffer_size`, `max_cache_mb`, `max_retries` instead of hardcoded defaults — could then be exposed as settings in the YouTube tab UI.

### Performance
- **Parallel downloads**: currently one track at a time to fill the 3-track buffer — parallelizing would reduce initial wait time.
- **Consider `python-vlc` instead of `pygame.mixer`** for playback: VLC natively handles more codecs, which could potentially skip the ffmpeg conversion step entirely (saves time + one fewer dependency). More invasive change — needs weighing against the rewrite cost.

### Features
- Support for YouTube playlists by URL (not just the personalized Sélection rapide).
- Sync "likes" to YouTube Music (`ytmusicapi` supports this).
- Synced lyrics, if exposed by the API.

### Security / project hygiene
- A `.gitignore` covering `oauth.json`, `browser.json`, `youtube_client_credentials.json`, `download_history.json` — to avoid ever committing them by accident.
- `requirements.txt` pinning versions (`ytmusicapi`, `yt-dlp`, `pygame`, etc.) for reproducibility.

### Code quality / testing
- Unit tests on `youtube_manager.py`'s pure logic (filename sanitization, cache eviction, deduplication) — need neither network nor GUI, easy to isolate.
- Extend logging to the rest of `Lecthon.py` in small targeted passes rather than all at once (as discussed previously — the file is large enough that a full pass risks untested regressions).

