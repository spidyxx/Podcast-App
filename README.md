# Podcast Feed Manager

A Windows desktop app for managing and downloading podcast/video RSS feeds. Built with Python and customtkinter.

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Multiple feeds** — add and switch between any number of RSS feeds from a dropdown in the top bar
- **Episode browser** — scrollable list with thumbnails, titles, dates, and durations
- **Detail panel** — large thumbnail, full description, and host avatars (from `<podcast:person>` tags)
- **Search and filter** — live search by title or title+description; filter by All / Favourites / Watched
- **Download** — download individual episodes or all undownloaded episodes at once, with per-episode progress
- **Playback** — opens downloaded files or stream URLs in VLC, MPC-HC, or your system default player
- **Favourites and watched** — mark episodes with star/tick badges visible in the list
- **Fast startup** — feed and thumbnails are cached locally; the app loads instantly and refreshes in the background
- **Podcast namespace support** — displays host names and avatars from feeds that use the [Podcast Index namespace](https://podcastindex.org/namespace/1.0)

## Requirements

- Python 3.9 or later
- Windows 10 or later

## Installation

```bash
git clone https://github.com/spidyxx/Podcast-App.git
cd Podcast-App
pip install -r requirements.txt
python app.py
```

## Usage

### Adding a feed

Click the **+** button next to the feed dropdown. Paste a RSS feed URL — the app will fetch the feed title automatically and add it to the dropdown.

> Feeds that require authentication (e.g. Memberful, Patreon) can be added by including the auth token in the URL, for example:
> `https://example.com/rss/feed?auth=your_token_here`
>
> Keep your auth tokens private — do not share your feed URLs publicly.

### Switching feeds

Select a feed from the dropdown at the top of the window. The episode list switches immediately, loading from cache if available or fetching from the network.

### Removing a feed

Select the feed you want to remove, then click the **−** button. You will be asked to confirm. This removes the feed from the list but does not delete any downloaded files.

### Browsing episodes

Click any episode in the list to see its details on the right: thumbnail, date, duration, description, and hosts. Episodes that have been downloaded show a **✓** badge; favourites show a **★** badge.

### Searching

Type in the search box below the filter bar. The list filters live as you type.

- **Title** mode (default) — searches episode titles only
- **Title + Desc** mode — also searches episode descriptions; toggle with the scope button next to the search box

### Filtering

Use the **All / ★ Favs / Watched** buttons above the search box to filter the episode list.

### Downloading

- **Download** button in the detail panel — downloads the selected episode
- **↓ Download All** button in the top bar — downloads all episodes that have not been downloaded yet

Downloaded files are saved to `~/Downloads/Podcasts/` by default. You can change this in Settings.

### Playing an episode

Click **Play** in the detail panel. If the episode has been downloaded the local file is opened; otherwise the stream URL is opened directly.

Player priority: VLC → MPC-HC → Windows default.

### Settings

Click **⚙ Settings** to configure:

- **Download folder** — where episode files are saved
- **Custom player** — path to an executable to use instead of the auto-detected player

## Local files created at runtime

| File / Folder | Purpose |
|---|---|
| `state.json` | Feed list, download history, favourites, watched, settings |
| `feed_cache.json` | Cached episode data for each feed (keyed by feed URL) |
| `thumb_cache/` | Downloaded and resized thumbnail images |

None of these are committed to git (covered by `.gitignore`).

## Privacy

Feed URLs (including any embedded auth tokens) are stored only in `state.json` on your local machine and are never sent anywhere other than the feed server itself.

## Dependencies

| Package | Purpose |
|---|---|
| `customtkinter` | Dark-themed modern UI widgets |
| `feedparser` | RSS/Atom feed parsing |
| `requests` | HTTP downloads and feed fetching |
| `Pillow` | Image processing and thumbnail caching |
