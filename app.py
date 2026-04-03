#!/usr/bin/env python3
"""
Podcast Feed Manager
Single-file Windows desktop app for managing multiple podcast RSS feeds.
"""

import os
import sys
import json
import re
import threading
import subprocess
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from io import BytesIO
from urllib.parse import urlparse, unquote

import tkinter as tk

try:
    import feedparser
    import requests
    import customtkinter as ctk
    from PIL import Image, ImageDraw, ImageTk
except ImportError as e:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Missing Dependencies",
        f"Missing required package: {e}\n\nPlease run:\n  pip install -r requirements.txt",
    )
    sys.exit(1)

# ─── Constants ────────────────────────────────────────────────────────────────

STATE_FILE = Path(__file__).parent / "state.json"
FEED_CACHE_FILE = Path(__file__).parent / "feed_cache.json"
THUMB_CACHE_DIR = Path(__file__).parent / "thumb_cache"
APP_TITLE = "Podcast Feed Manager"
DEFAULT_DOWNLOAD_FOLDER = str(Path.home() / "Downloads" / "Podcasts")

THUMBNAIL_SM  = (128, 72)   # 16:9 thumbnail in episode list
THUMBNAIL_LG  = (400, 225)  # 16:9 thumbnail in detail panel
AVATAR_SIZE   = (52, 52)    # host avatar in detail panel
PODCAST_NS    = "https://podcastindex.org/namespace/1.0"

VLC_PATHS = [
    # Windows
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    # Linux
    "/usr/bin/vlc",
    "/usr/local/bin/vlc",
    "/snap/bin/vlc",
]
MPC_PATHS = [
    # Windows only
    r"C:\Program Files\MPC-HC\mpc-hc64.exe",
    r"C:\Program Files\MPC-HC\mpc-hc.exe",
    r"C:\Program Files (x86)\MPC-HC\mpc-hc.exe",
]

# ─── State Manager ────────────────────────────────────────────────────────────

class StateManager:
    """Persists app config and download history to a local JSON file."""

    def __init__(self):
        self._data = {
            "download_folder": DEFAULT_DOWNLOAD_FOLDER,
            "player_path": "",
            "downloaded": {},  # guid → {"filename": str, "downloaded_at": str}
            "feeds": [],       # list of {"name": str, "url": str}
            "active_feed_idx": 0,
        }
        self._load()

    def _load(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    self._data.update(json.load(f))
            except Exception:
                pass

    def save(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            print(f"[state] save failed: {e}")

    @property
    def download_folder(self) -> str:
        return self._data["download_folder"]

    @download_folder.setter
    def download_folder(self, value: str):
        self._data["download_folder"] = value
        self.save()

    @property
    def player_path(self) -> str:
        return self._data["player_path"]

    @player_path.setter
    def player_path(self, value: str):
        self._data["player_path"] = value
        self.save()

    def mark_downloaded(self, guid: str, filename: str):
        self._data["downloaded"][guid] = {
            "filename": filename,
            "downloaded_at": datetime.now().isoformat(),
        }
        self.save()

    def is_downloaded(self, guid: str) -> bool:
        if guid not in self._data["downloaded"]:
            return False
        return self._get_path(guid).exists()

    def _get_path(self, guid: str) -> Path:
        filename = self._data["downloaded"][guid]["filename"]
        return Path(self.download_folder) / filename

    def get_filepath(self, guid: str):
        if guid not in self._data["downloaded"]:
            return None
        p = self._get_path(guid)
        return p if p.exists() else None

    # ── Favorites ─────────────────────────────────────────────────────────────

    def toggle_favorite(self, guid: str):
        favs = self._data.setdefault("favorites", [])
        if guid in favs:
            favs.remove(guid)
        else:
            favs.append(guid)
        self.save()

    def is_favorite(self, guid: str) -> bool:
        return guid in self._data.get("favorites", [])

    # ── Watched ───────────────────────────────────────────────────────────────

    def toggle_watched(self, guid: str):
        watched = self._data.setdefault("watched", [])
        if guid in watched:
            watched.remove(guid)
        else:
            watched.append(guid)
        self.save()

    def is_watched(self, guid: str) -> bool:
        return guid in self._data.get("watched", [])

    # ── Feeds ─────────────────────────────────────────────────────────────────

    @property
    def feeds(self) -> list:
        return self._data.setdefault("feeds", [])

    @property
    def active_feed_idx(self) -> int:
        return self._data.get("active_feed_idx", 0)

    @property
    def active_feed(self):
        feeds = self.feeds
        if not feeds:
            return None
        idx = max(0, min(self.active_feed_idx, len(feeds) - 1))
        return feeds[idx]

    @property
    def active_feed_url(self) -> str:
        f = self.active_feed
        return f["url"] if f else ""

    @property
    def active_feed_name(self) -> str:
        f = self.active_feed
        return f["name"] if f else ""

    def add_feed(self, name: str, url: str):
        self.feeds.append({"name": name, "url": url})
        self._data["active_feed_idx"] = len(self.feeds) - 1
        self.save()

    def remove_feed(self, idx: int):
        feeds = self.feeds
        if 0 <= idx < len(feeds):
            feeds.pop(idx)
            new_idx = max(0, min(self.active_feed_idx, len(feeds) - 1))
            self._data["active_feed_idx"] = new_idx
            self.save()

    def set_active_feed(self, idx: int):
        self._data["active_feed_idx"] = idx
        self.save()


# ─── Utilities ────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180]


def format_duration(raw) -> str:
    """Accept 'HH:MM:SS', 'MM:SS', or integer seconds; return human string."""
    if not raw:
        return ""
    try:
        parts = str(raw).split(":")
        if len(parts) == 3:
            secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            secs = int(parts[0]) * 60 + int(parts[1])
        else:
            secs = int(raw)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except (TypeError, ValueError):
        return str(raw)


def format_date(raw: str) -> str:
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(raw)
        return dt.strftime("%B %d, %Y")
    except Exception:
        return raw or "Unknown date"


def _load_pil_image(url: str, size: tuple):
    """Core loader — returns a PIL Image (already resized) using a two-level
    disk cache, or None on failure.

    Level 1 — sized JPEG: <hash>_WxH.jpg  (pre-resized; just Image.open on hit)
    Level 2 — raw bytes:  <hash>           (original download, written once)
    """
    try:
        THUMB_CACHE_DIR.mkdir(exist_ok=True)
        base = hashlib.md5(url.encode()).hexdigest()
        sized_file = THUMB_CACHE_DIR / f"{base}_{size[0]}x{size[1]}.jpg"

        if sized_file.exists():
            return Image.open(sized_file).convert("RGB")

        raw_file = THUMB_CACHE_DIR / base
        if raw_file.exists():
            raw = raw_file.read_bytes()
        else:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            raw = r.content
            raw_file.write_bytes(raw)

        img = Image.open(BytesIO(raw)).convert("RGB").resize(size, Image.BILINEAR)
        img.save(sized_file, "JPEG", quality=82)
        return img
    except Exception:
        return None


def fetch_image(url: str, size: tuple):
    """Returns a CTkImage (for CTkLabel widgets) or None."""
    img = _load_pil_image(url, size)
    if img is None:
        return None
    return ctk.CTkImage(light_image=img, dark_image=img, size=size)


# ─── Thumbnail loader pool ────────────────────────────────────────────────────
# A fixed pool of worker threads drains a single queue so we never spawn
# hundreds of threads at once and the UI stays responsive while loading.

import queue as _queue

_thumb_queue: _queue.Queue = _queue.Queue()


def _thumb_worker():
    while True:
        url, size, cb = _thumb_queue.get()
        try:
            cb(_load_pil_image(url, size))   # delivers PIL Image, not CTkImage
        except Exception:
            pass
        finally:
            _thumb_queue.task_done()


def _start_thumb_workers(n: int = 4):
    for _ in range(n):
        threading.Thread(target=_thumb_worker, daemon=True).start()


def enqueue_thumbnail(url: str, size: tuple, callback):
    """Schedule a thumbnail fetch; callback(CTkImage|None) runs in a worker thread."""
    _thumb_queue.put((url, size, callback))


def _feed_cache_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def save_feed_cache(url: str, episodes: list):
    try:
        data = {}
        if FEED_CACHE_FILE.exists():
            with open(FEED_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    data = raw
    except Exception:
        data = {}
    data[_feed_cache_key(url)] = episodes
    try:
        with open(FEED_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[cache] feed save failed: {e}")


def load_feed_cache(url: str) -> list:
    if not FEED_CACHE_FILE.exists():
        return []
    try:
        with open(FEED_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get(_feed_cache_key(url), [])
        return []  # old single-feed format — discard
    except Exception:
        return []


def _open_with_system_default(path: str):
    """Open a file or URL with the system default handler, cross-platform."""
    if sys.platform == "win32":
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(["cmd", "/c", "start", "", path], shell=False)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def open_video(filepath: str, player_path: str = ""):
    """Open a file/URL in the best available player."""
    filepath = str(filepath)
    # Custom player first
    if player_path and os.path.exists(player_path):
        subprocess.Popen([player_path, filepath])
        return
    for p in VLC_PATHS + MPC_PATHS:
        if os.path.exists(p):
            subprocess.Popen([p, filepath])
            return
    # System default
    _open_with_system_default(filepath)


# ─── Feed Parser ──────────────────────────────────────────────────────────────

def _thumb_from_entry(entry, feed) -> str:
    """Try several locations feedparser puts thumbnails."""
    # media:thumbnail
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url", "")
    # media:content with type image
    if hasattr(entry, "media_content"):
        for m in entry.media_content:
            if "image" in m.get("type", ""):
                return m.get("url", "")
    # itunes image
    if hasattr(entry, "image"):
        href = getattr(entry.image, "href", None)
        if href:
            return href
    # feed-level image
    feed_img = getattr(getattr(feed, "feed", None), "image", None)
    if feed_img:
        return getattr(feed_img, "href", "") or feed_img.get("href", "")
    return ""


def _extract_hosts_by_guid(raw_xml: str) -> dict:
    """Parse <podcast:person role="host"> elements from raw RSS XML.
    Returns {guid_text: [{"name", "img", "href"}]}.
    """
    try:
        root = ET.fromstring(raw_xml.encode("utf-8"))
    except ET.ParseError:
        try:
            cleaned = re.sub(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]", "", raw_xml)
            root = ET.fromstring(cleaned)
        except Exception:
            return {}
    channel = root.find("channel")
    if channel is None:
        return {}
    tag = f"{{{PODCAST_NS}}}person"
    result = {}
    for item in channel.findall("item"):
        guid_el = item.find("guid")
        guid = (guid_el.text or "").strip() if guid_el is not None else ""
        if not guid:
            link_el = item.find("link")
            guid = (link_el.text or "").strip() if link_el is not None else ""
        if not guid:
            continue
        hosts = [
            {"name": (p.text or "").strip(),
             "img":  p.get("img", ""),
             "href": p.get("href", "")}
            for p in item.findall(tag)
            if p.get("role", "").lower() == "host" and (p.text or "").strip()
        ]
        if hosts:
            result[guid] = hosts
    return result


def parse_feed(url: str) -> list:
    # Fetch raw XML once so both feedparser and the ET host-extractor can use it
    try:
        resp = requests.get(url, timeout=30,
                            headers={"User-Agent": "TWiT-FeedManager/1.0"})
        raw_xml = resp.text
    except Exception:
        raw_xml = ""
    feed = feedparser.parse(raw_xml or url)
    hosts_by_guid = _extract_hosts_by_guid(raw_xml) if raw_xml else {}

    episodes = []
    for entry in feed.entries:
        # Enclosure
        enc_url = enc_type = ""
        for enc in getattr(entry, "enclosures", []):
            enc_url = enc.get("href", enc.get("url", ""))
            enc_type = enc.get("type", "")
            if enc_url:
                break

        # Description (prefer rich content over summary)
        description = ""
        if hasattr(entry, "content") and entry.content:
            description = entry.content[0].get("value", "")
        if not description:
            description = getattr(entry, "summary", "") or ""
        description_clean = re.sub(r"<[^>]+>", "", description).strip()
        description_clean = re.sub(r"\n{3,}", "\n\n", description_clean)

        duration_raw = getattr(entry, "itunes_duration", "") or ""
        published = getattr(entry, "published", "")
        guid = getattr(entry, "id", None) or getattr(entry, "link", None) or str(len(episodes))

        episodes.append({
            "guid": guid,
            "title": getattr(entry, "title", "Untitled"),
            "date_raw": published,
            "date": format_date(published),
            "duration": format_duration(duration_raw),
            "description": description_clean,
            "thumbnail_url": _thumb_from_entry(entry, feed),
            "enclosure_url": enc_url,
            "enclosure_type": enc_type,
            "hosts": hosts_by_guid.get(guid, []),
        })
    return episodes


# ─── Episode List (canvas-based) ──────────────────────────────────────────────

class EpisodeListCanvas(ctk.CTkFrame):
    ITEM_H   = 94
    THUMB_W, THUMB_H = THUMBNAIL_SM
    TEXT_X   = 6 + THUMBNAIL_SM[0] + 8

    BG        = "#1c1c1c"
    ROW_NORM  = "#2a2a2a"
    ROW_HOVER = "#333344"
    ROW_SEL   = "#1a3a5c"
    C_TITLE   = "#e8e8e8"
    C_TITLE_W = "#666677"   # watched (dimmed)
    C_META    = "#777788"
    C_DL      = "#4ecca3"   # downloaded ✓
    C_FAV     = "#f5c218"   # favourite ★

    def __init__(self, parent, on_select, state: StateManager, **kw):
        super().__init__(parent, fg_color=self.BG, corner_radius=0, **kw)
        self._on_select  = on_select
        self._state      = state
        self._all_eps    = []   # full unfiltered list
        self._episodes   = []   # currently displayed (filtered) list
        self._selected   = -1
        self._hover      = -1
        self._scope_full = False   # False = title only, True = title + description
        self._photos     = {}   # guid → ImageTk.PhotoImage
        self._img_items  = {}   # guid → canvas item id

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # ── Row 0: header + view selector ────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 2))
        hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr, text="EPISODES",
            font=ctk.CTkFont(size=10, weight="bold"), text_color="gray45",
        ).grid(row=0, column=0, sticky="w")

        self._view_btn = ctk.CTkSegmentedButton(
            hdr, values=["All", "★ Favs", "Watched"],
            command=self._on_view_change, width=210,
            font=ctk.CTkFont(size=11),
        )
        self._view_btn.set("All")
        self._view_btn.grid(row=0, column=1, sticky="e")

        # ── Row 1: search bar + scope toggle ─────────────────────────────────
        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.grid(row=1, column=0, columnspan=2, sticky="ew",
                        padx=8, pady=(2, 4))
        search_row.grid_columnconfigure(0, weight=1)

        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        ctk.CTkEntry(
            search_row, textvariable=self._search_var,
            placeholder_text="Search episodes…",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self._scope_btn = ctk.CTkButton(
            search_row, text="Title", width=52,
            fg_color="gray25", hover_color="gray35",
            command=self._toggle_scope,
            font=ctk.CTkFont(size=11),
        )
        self._scope_btn.grid(row=0, column=1)

        # ── Row 2: canvas + scrollbar ─────────────────────────────────────────
        self._cv = tk.Canvas(self, bg=self.BG, highlightthickness=0, bd=0,
                             yscrollincrement=self.ITEM_H // 3)
        self._cv.grid(row=2, column=0, sticky="nsew")

        self._sb = ctk.CTkScrollbar(self, command=self._cv.yview)
        self._sb.grid(row=2, column=1, sticky="ns")
        self._cv.configure(yscrollcommand=self._sb.set)

        self._cv.bind("<Configure>",  lambda e: self._full_redraw())
        self._cv.bind("<MouseWheel>", self._on_wheel)
        self._cv.bind("<Button-1>",   self._on_click)
        self._cv.bind("<Motion>",     self._on_motion)
        self._cv.bind("<Leave>",      self._on_leave)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_episodes(self, episodes: list):
        """Load a new episode list (clears photos and re-queues thumbnails)."""
        self._all_eps  = episodes
        self._selected = -1
        self._hover    = -1
        self._photos.clear()
        self._img_items.clear()
        self._apply_filter()
        for ep in episodes:
            url = ep.get("thumbnail_url", "")
            if url:
                enqueue_thumbnail(
                    url, THUMBNAIL_SM,
                    lambda pil, g=ep["guid"]: self._cv.after(
                        0, lambda _p=pil, _g=g: self._set_thumb(_g, _p)
                    ),
                )

    def refresh(self):
        """Recompute filter + redraw — call when state (fav/watched) changes."""
        self._apply_filter()

    def refresh_badges(self):
        """Redraw after a download completes."""
        self._apply_filter()

    # ── Filter ────────────────────────────────────────────────────────────────

    def _get_filtered(self) -> list:
        view  = self._view_btn.get()
        query = self._search_var.get().strip().lower()
        result = []
        for ep in self._all_eps:
            guid = ep["guid"]
            if view == "★ Favs"  and not self._state.is_favorite(guid):
                continue
            if view == "Watched" and not self._state.is_watched(guid):
                continue
            if query:
                in_title = query in ep["title"].lower()
                in_desc  = query in ep.get("description", "").lower()
                if self._scope_full:
                    if not (in_title or in_desc):
                        continue
                else:
                    if not in_title:
                        continue
            result.append(ep)
        return result

    def _apply_filter(self):
        """Recompute the visible list, preserve selection guid, redraw."""
        old_guid = (self._episodes[self._selected]["guid"]
                    if 0 <= self._selected < len(self._episodes) else None)
        self._episodes = self._get_filtered()
        self._selected = -1
        if old_guid:
            for i, ep in enumerate(self._episodes):
                if ep["guid"] == old_guid:
                    self._selected = i
                    break
        self._full_redraw()

    def _on_view_change(self, _):
        self._apply_filter()

    def _toggle_scope(self):
        self._scope_full = not self._scope_full
        if self._scope_full:
            self._scope_btn.configure(text="Title+Desc",
                                      fg_color="#1f6aa5", hover_color="#155280")
        else:
            self._scope_btn.configure(text="Title",
                                      fg_color="gray25", hover_color="gray35")
        self._apply_filter()

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _full_redraw(self):
        cv = self._cv
        cv.delete("all")
        self._img_items.clear()
        w = cv.winfo_width() or 350
        cv.configure(scrollregion=(0, 0, w, len(self._episodes) * self.ITEM_H))
        for i, ep in enumerate(self._episodes):
            self._draw_row(i, w, ep)

    def _draw_row(self, i: int, w: int, ep: dict):
        cv   = self._cv
        y    = i * self.ITEM_H
        guid = ep["guid"]
        watched  = self._state.is_watched(guid)
        fav      = self._state.is_favorite(guid)
        dl       = self._state.is_downloaded(guid)

        cv.create_rectangle(
            4, y + 2, w - 4, y + self.ITEM_H - 2,
            fill=self._row_color(i), outline="", tags=f"row{i}",
        )

        # Thumbnail or placeholder
        tx = 6
        ty = y + (self.ITEM_H - self.THUMB_H) // 2
        if guid in self._photos:
            item = cv.create_image(tx, ty, image=self._photos[guid], anchor="nw")
            self._img_items[guid] = item
        else:
            cv.create_rectangle(tx, ty, tx + self.THUMB_W, ty + self.THUMB_H,
                                 fill="#111111", outline="")

        # Title — dimmed when watched
        cv.create_text(
            self.TEXT_X, y + 10,
            text=ep["title"], anchor="nw",
            fill=self.C_TITLE_W if watched else self.C_TITLE,
            font=("Segoe UI", 10, "bold"),
            width=w - self.TEXT_X - 26,
        )

        # Date · duration
        meta = ep["date"]
        if ep.get("duration"):
            meta += f"  ·  {ep['duration']}"
        cv.create_text(
            self.TEXT_X, y + self.ITEM_H - 20,
            text=meta, anchor="nw",
            fill=self.C_META, font=("Segoe UI", 9),
        )

        # Badges (right edge, stacked top-to-bottom)
        bx = w - 8
        by = y + 8
        if fav:
            cv.create_text(bx, by, text="★", anchor="ne",
                           fill=self.C_FAV, font=("Segoe UI", 11, "bold"))
            by += 16
        if dl:
            cv.create_text(bx, by, text="✓", anchor="ne",
                           fill=self.C_DL, font=("Segoe UI", 11, "bold"))

    def _row_color(self, i: int) -> str:
        if i == self._selected: return self.ROW_SEL
        if i == self._hover:    return self.ROW_HOVER
        return self.ROW_NORM

    def _recolor_row(self, i: int):
        if 0 <= i < len(self._episodes):
            self._cv.itemconfigure(f"row{i}", fill=self._row_color(i))

    # ── Thumbnail callback ────────────────────────────────────────────────────

    def _set_thumb(self, guid: str, pil_img):
        if pil_img is None:
            return
        photo = ImageTk.PhotoImage(pil_img)
        self._photos[guid] = photo
        if guid in self._img_items:
            self._cv.itemconfigure(self._img_items[guid], image=photo)
        else:
            for i, ep in enumerate(self._episodes):
                if ep["guid"] == guid:
                    tx = 6
                    ty = i * self.ITEM_H + (self.ITEM_H - self.THUMB_H) // 2
                    item = self._cv.create_image(tx, ty, image=photo, anchor="nw")
                    self._img_items[guid] = item
                    break

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_wheel(self, e):
        self._cv.yview_scroll(-1 * (e.delta // 120) * 3, "units")

    def _row_at(self, canvas_y: float) -> int:
        i = int(canvas_y // self.ITEM_H)
        return i if 0 <= i < len(self._episodes) else -1

    def _on_click(self, e):
        i = self._row_at(self._cv.canvasy(e.y))
        if i < 0:
            return
        prev, self._selected = self._selected, i
        self._recolor_row(prev)
        self._recolor_row(i)
        self._on_select(self._episodes[i])

    def _on_motion(self, e):
        i = self._row_at(self._cv.canvasy(e.y))
        if i == self._hover:
            return
        prev, self._hover = self._hover, i
        self._recolor_row(prev)
        self._recolor_row(i)

    def _on_leave(self, _=None):
        prev, self._hover = self._hover, -1
        self._recolor_row(prev)


# ─── Settings Dialog ──────────────────────────────────────────────────────────

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, state: StateManager):
        super().__init__(parent)
        self._state = state
        self.title("Settings")
        self.geometry("520x240")
        self.resizable(False, False)
        self.grab_set()
        self.grid_columnconfigure(1, weight=1)

        # Download folder row
        ctk.CTkLabel(self, text="Download Folder:", anchor="w").grid(
            row=0, column=0, padx=16, pady=(20, 2), sticky="w")

        self._folder_var = ctk.StringVar(value=state.download_folder)
        ctk.CTkEntry(self, textvariable=self._folder_var).grid(
            row=1, column=0, columnspan=2, padx=16, pady=(0, 6), sticky="ew")
        ctk.CTkButton(self, text="Browse…", width=90,
                      command=self._browse_folder).grid(
            row=1, column=2, padx=(4, 16), pady=(0, 6))

        # Player path row
        ctk.CTkLabel(self, text="Video Player Path (leave blank to auto-detect):",
                     anchor="w").grid(
            row=2, column=0, columnspan=3, padx=16, pady=(8, 2), sticky="w")

        self._player_var = ctk.StringVar(value=state.player_path)
        ctk.CTkEntry(self, textvariable=self._player_var,
                     placeholder_text="Auto-detect VLC → MPC-HC → Windows default").grid(
            row=3, column=0, columnspan=2, padx=16, pady=(0, 6), sticky="ew")
        ctk.CTkButton(self, text="Browse…", width=90,
                      command=self._browse_player).grid(
            row=3, column=2, padx=(4, 16), pady=(0, 6))

        # Buttons
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=4, column=0, columnspan=3, padx=16, pady=12, sticky="e")
        ctk.CTkButton(btn_row, text="Cancel", width=80,
                      fg_color="gray30", hover_color="gray40",
                      command=self.destroy).pack(side="right", padx=(6, 0))
        ctk.CTkButton(btn_row, text="Save", width=80,
                      command=self._save).pack(side="right")

    def _browse_folder(self):
        from tkinter import filedialog
        path = filedialog.askdirectory(initialdir=self._folder_var.get())
        if path:
            self._folder_var.set(path)

    def _browse_player(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select video player",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
            initialdir=r"C:\Program Files",
        )
        if path:
            self._player_var.set(path)

    def _save(self):
        self._state.download_folder = self._folder_var.get()
        self._state.player_path = self._player_var.get()
        self.destroy()


# ─── Detail Panel ─────────────────────────────────────────────────────────────

class DetailPanel(ctk.CTkFrame):
    def __init__(self, parent, state: StateManager,
                 on_downloaded=None, on_state_changed=None, **kw):
        super().__init__(parent, **kw)
        self._state = state
        self._on_downloaded    = on_downloaded     # callback() when a download finishes
        self._on_state_changed = on_state_changed  # callback() when fav/watched toggles
        self._episode = None
        self._thumb_img = None

        # Persistent blank placeholder — setting image=None on a CTkLabel that
        # previously held an image destroys the underlying PhotoImage while
        # tkinter still references it by name, causing TclError on the next
        # redraw. Keeping a stable CTkImage on the label at all times avoids
        # the issue; we overlay status text with compound="center".
        _ph = Image.new("RGB", THUMBNAIL_LG, (20, 20, 20))
        self._blank_thumb = ctk.CTkImage(
            light_image=_ph, dark_image=_ph, size=THUMBNAIL_LG
        )

        # Download tracking (written by bg thread, read by after() poll)
        self._dl_progress: float = 0.0   # 0.0–1.0 or -1 for unknown size
        self._dl_bytes: int = 0           # raw bytes when size unknown
        self._dl_status: str = ""         # "downloading" | "done" | "error:<msg>"
        self._poll_id = None

        self._build()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self):
        self.grid_rowconfigure(4, weight=1)   # description row
        self.grid_columnconfigure(0, weight=1)

        # Large thumbnail — always holds a CTkImage (blank or real) so the
        # underlying PhotoImage name is never stale when tkinter redraws.
        self._thumb = ctk.CTkLabel(
            self, image=self._blank_thumb,
            text="Select an episode to view details",
            compound="center",
            height=THUMBNAIL_LG[1], fg_color="#141414", corner_radius=8,
            font=ctk.CTkFont(size=14), text_color="gray40",
        )
        self._thumb.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")

        # Title
        self._title = ctk.CTkLabel(
            self, text="", anchor="w", justify="left",
            font=ctk.CTkFont(size=17, weight="bold"),
            wraplength=460,
        )
        self._title.grid(row=1, column=0, padx=16, pady=(4, 0), sticky="w")

        # Metadata (date · duration)
        self._meta = ctk.CTkLabel(
            self, text="", anchor="w",
            font=ctk.CTkFont(size=12), text_color="gray55",
        )
        self._meta.grid(row=2, column=0, padx=16, pady=(2, 8), sticky="w")

        # Hosts bar — hidden until an episode with hosts is loaded
        self._hosts_frame = ctk.CTkFrame(self, fg_color="#1a1a1a", corner_radius=6)
        self._hosts_frame.grid(row=3, column=0, padx=16, pady=(0, 6), sticky="ew")
        self._hosts_inner = ctk.CTkFrame(self._hosts_frame, fg_color="transparent")
        self._hosts_inner.pack(fill="x", padx=10, pady=6)
        self._hosts_frame.grid_remove()
        self._host_avatars = []   # keep CTkImage refs alive

        # Description textbox
        self._desc = ctk.CTkTextbox(
            self, wrap="word", state="disabled",
            font=ctk.CTkFont(size=12), fg_color="#1c1c1c",
        )
        self._desc.grid(row=4, column=0, padx=16, pady=(0, 6), sticky="nsew")

        # Progress row (hidden until a download starts)
        self._prog_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._prog_frame.grid(row=5, column=0, padx=16, pady=(0, 2), sticky="ew")
        self._prog_frame.grid_columnconfigure(0, weight=1)
        self._prog_bar = ctk.CTkProgressBar(self._prog_frame, height=12)
        self._prog_bar.set(0)
        self._prog_bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._prog_label = ctk.CTkLabel(
            self._prog_frame, text="0%", width=52,
            font=ctk.CTkFont(size=11),
        )
        self._prog_label.grid(row=0, column=1)
        self._prog_frame.grid_remove()

        # Action buttons
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=6, column=0, padx=16, pady=(4, 4), sticky="w")

        self._dl_btn = ctk.CTkButton(
            btn_row, text="Download", width=120,
            command=self._download,
            fg_color="#1f6aa5", hover_color="#155280",
        )
        self._dl_btn.pack(side="left", padx=(0, 8))

        self._play_btn = ctk.CTkButton(
            btn_row, text="▶  Play", width=90,
            command=self._play,
            fg_color="#2a6e2a", hover_color="#1d4e1d",
        )
        self._play_btn.pack(side="left", padx=(0, 8))

        self._folder_btn = ctk.CTkButton(
            btn_row, text="Open Folder", width=110,
            command=self._open_folder,
            fg_color="#474747", hover_color="#363636",
        )
        self._folder_btn.pack(side="left")

        # Second button row: favourite + watched
        btn_row2 = ctk.CTkFrame(self, fg_color="transparent")
        btn_row2.grid(row=7, column=0, padx=16, pady=(0, 14), sticky="w")

        self._fav_btn = ctk.CTkButton(
            btn_row2, text="☆  Favourite", width=130,
            command=self._toggle_favourite,
            fg_color="gray25", hover_color="gray35",
        )
        self._fav_btn.pack(side="left", padx=(0, 8))

        self._watched_btn = ctk.CTkButton(
            btn_row2, text="○  Mark Watched", width=140,
            command=self._toggle_watched,
            fg_color="gray25", hover_color="gray35",
        )
        self._watched_btn.pack(side="left")

        self._set_btns(False)

    def _set_btns(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for b in (self._dl_btn, self._play_btn, self._folder_btn,
                  self._fav_btn, self._watched_btn):
            b.configure(state=state)

    # ── Public API ────────────────────────────────────────────────────────────

    def load_episode(self, episode: dict):
        self._cancel_poll()
        self._episode = episode
        self._thumb_img = None

        # Reset progress UI
        self._prog_frame.grid_remove()
        self._prog_bar.set(0)
        self._prog_label.configure(text="0%")

        self._title.configure(text=episode["title"])

        meta = episode["date"]
        if episode["duration"]:
            meta += f"  ·  {episode['duration']}"
        self._meta.configure(text=meta)

        self._desc.configure(state="normal")
        self._desc.delete("1.0", "end")
        self._desc.insert("1.0", episode["description"] or "No description available.")
        self._desc.configure(state="disabled")

        # Thumbnail — switch to blank+text first, then load the real image async.
        # Never pass image=None: see comment on self._blank_thumb above.
        self._thumb.configure(
            image=self._blank_thumb, text="Loading preview…",
            compound="center", text_color="gray50",
        )
        url = episode.get("thumbnail_url", "")
        if url:
            guid = episode["guid"]
            def _fetch():
                img = fetch_image(url, THUMBNAIL_LG)
                if img and self._episode and self._episode["guid"] == guid:
                    self._thumb_img = img
                    try:
                        self._thumb.configure(image=img, text="", compound="center")
                    except Exception:
                        pass
            threading.Thread(target=_fetch, daemon=True).start()
        else:
            self._thumb.configure(
                image=self._blank_thumb, text="No Preview Available",
                compound="center", text_color="gray50",
            )

        self._load_hosts(episode.get("hosts", []))
        self._set_btns(True)
        self._refresh_dl_btn()
        self._refresh_fav_btn()
        self._refresh_watched_btn()

    # ── Download button state ─────────────────────────────────────────────────

    def _refresh_dl_btn(self):
        if not self._episode:
            return
        if self._state.is_downloaded(self._episode["guid"]):
            self._dl_btn.configure(
                text="Downloaded ✓", state="disabled",
                fg_color="#1e4a1e",
            )
            self._play_btn.configure(state="normal")
        else:
            self._dl_btn.configure(
                text="Download", state="normal",
                fg_color="#1f6aa5",
            )

    # ── Download logic ────────────────────────────────────────────────────────

    def _download(self):
        if not self._episode or not self._episode.get("enclosure_url"):
            return

        self._dl_btn.configure(state="disabled", text="Downloading…")
        self._prog_frame.grid()
        self._prog_bar.set(0)
        self._prog_label.configure(text="0%")
        self._dl_progress = 0.0
        self._dl_bytes = 0
        self._dl_status = "downloading"

        folder = Path(self._state.download_folder)
        folder.mkdir(parents=True, exist_ok=True)
        episode = self._episode

        def _worker():
            url = episode["enclosure_url"]
            parsed = urlparse(url)
            raw_name = unquote(parsed.path.split("/")[-1])
            ext = Path(raw_name).suffix or ".mp4"
            filename = sanitize_filename(episode["title"]) + ext
            filepath = folder / filename

            try:
                resp = requests.get(url, stream=True, timeout=30,
                                    headers={"User-Agent": "TWiT-FeedManager/1.0"})
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                received = 0

                with open(filepath, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=131072):
                        if chunk:
                            fh.write(chunk)
                            received += len(chunk)
                            self._dl_bytes = received
                            if total:
                                self._dl_progress = received / total

                self._state.mark_downloaded(episode["guid"], filename)
                self._dl_progress = 1.0
                self._dl_status = "done"

            except Exception as exc:
                self._dl_status = f"error:{exc}"
                # Remove partial file
                try:
                    if filepath.exists():
                        filepath.unlink()
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()
        self._poll_progress()

    def _poll_progress(self):
        status = self._dl_status

        if status == "done":
            self._prog_bar.set(1.0)
            self._prog_label.configure(text="100%")
            self._prog_frame.grid_remove()
            self._refresh_dl_btn()
            if self._on_downloaded and self._episode:
                self._on_downloaded(self._episode["guid"])
            return

        if status.startswith("error:"):
            self._prog_frame.grid_remove()
            msg = status[6:60]
            self._dl_btn.configure(
                state="normal", text="Retry Download",
                fg_color="#7a1a1a",
            )
            return

        # Still downloading
        prog = self._dl_progress
        if prog > 0:
            self._prog_bar.set(prog)
            self._prog_label.configure(text=f"{int(prog * 100)}%")
        else:
            # Unknown total size — show MB received
            mb = self._dl_bytes / 1_048_576
            self._prog_label.configure(text=f"{mb:.1f} MB")

        self._poll_id = self.after(250, self._poll_progress)

    def _cancel_poll(self):
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

    # ── Hosts ─────────────────────────────────────────────────────────────────

    def _load_hosts(self, hosts: list):
        for w in self._hosts_inner.winfo_children():
            w.destroy()
        self._host_avatars = []

        if not hosts:
            self._hosts_frame.grid_remove()
            return

        self._hosts_frame.grid()

        ctk.CTkLabel(
            self._hosts_inner, text="Hosts",
            font=ctk.CTkFont(size=10, weight="bold"), text_color="gray50",
        ).pack(side="left", padx=(0, 12), anchor="n", pady=(10, 0))

        sz = AVATAR_SIZE[0]
        blank_pil = Image.new("RGB", AVATAR_SIZE, (30, 30, 30))

        for idx, host in enumerate(hosts):
            cell = ctk.CTkFrame(self._hosts_inner, fg_color="transparent")
            cell.pack(side="left", padx=8)

            blank_img = ctk.CTkImage(light_image=blank_pil, dark_image=blank_pil,
                                     size=AVATAR_SIZE)
            self._host_avatars.append(blank_img)

            av = ctk.CTkLabel(cell, image=blank_img, text="",
                              width=sz, height=sz, corner_radius=sz // 2)
            av.pack()

            ctk.CTkLabel(
                cell, text=host["name"],
                font=ctk.CTkFont(size=10), text_color="gray65",
                wraplength=80, justify="center",
            ).pack(pady=(3, 0))

            img_url = host.get("img", "")
            if img_url:
                def _cb(pil_img, _av=av, _idx=idx):
                    if pil_img is None:
                        return
                    # Circular crop
                    circ = pil_img.convert("RGBA")
                    mask = Image.new("L", circ.size, 0)
                    ImageDraw.Draw(mask).ellipse((0, 0, circ.width - 1, circ.height - 1),
                                                fill=255)
                    bg = Image.new("RGB", circ.size, (30, 30, 30))
                    bg.paste(circ.convert("RGB"), mask=mask)
                    ctk_img = ctk.CTkImage(light_image=bg, dark_image=bg, size=AVATAR_SIZE)
                    if _idx < len(self._host_avatars):
                        self._host_avatars[_idx] = ctk_img
                    try:
                        _av.after(0, lambda _i=ctk_img: _av.configure(image=_i))
                    except Exception:
                        pass
                enqueue_thumbnail(img_url, AVATAR_SIZE, _cb)

    # ── Favourite / Watched button state ─────────────────────────────────────

    def _refresh_fav_btn(self):
        if not self._episode:
            return
        if self._state.is_favorite(self._episode["guid"]):
            self._fav_btn.configure(text="★  Favourited",
                                    fg_color="#7a5c00", hover_color="#5a4400")
        else:
            self._fav_btn.configure(text="☆  Favourite",
                                    fg_color="gray25", hover_color="gray35")

    def _refresh_watched_btn(self):
        if not self._episode:
            return
        if self._state.is_watched(self._episode["guid"]):
            self._watched_btn.configure(text="●  Watched",
                                        fg_color="#2a4a6a", hover_color="#1e3a52")
        else:
            self._watched_btn.configure(text="○  Mark Watched",
                                        fg_color="gray25", hover_color="gray35")

    def _toggle_favourite(self):
        if not self._episode:
            return
        self._state.toggle_favorite(self._episode["guid"])
        self._refresh_fav_btn()
        if self._on_state_changed:
            self._on_state_changed()

    def _toggle_watched(self):
        if not self._episode:
            return
        self._state.toggle_watched(self._episode["guid"])
        self._refresh_watched_btn()
        if self._on_state_changed:
            self._on_state_changed()

    # ── Button actions ────────────────────────────────────────────────────────

    def _play(self):
        if not self._episode:
            return
        fp = self._state.get_filepath(self._episode["guid"])
        if fp:
            open_video(str(fp), self._state.player_path)
        else:
            url = self._episode.get("enclosure_url", "")
            if url:
                open_video(url, self._state.player_path)

    def _open_folder(self):
        if not self._episode:
            return
        fp = self._state.get_filepath(self._episode["guid"])
        if fp:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(fp)])
            else:
                # Open the containing folder; file selection not supported cross-platform
                _open_with_system_default(str(fp.parent))
        else:
            folder = self._state.download_folder
            os.makedirs(folder, exist_ok=True)
            _open_with_system_default(folder)


# ─── Add Feed Dialog ──────────────────────────────────────────────────────────

class AddFeedDialog(ctk.CTkToplevel):
    def __init__(self, parent, state: StateManager, on_added):
        super().__init__(parent)
        self.title("Add Feed")
        self.geometry("480x150")
        self.resizable(False, False)
        self.grab_set()
        self._state = state
        self._on_added = on_added
        self._build()
        self.after(100, self.lift)

    def _build(self):
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Feed URL:").grid(
            row=0, column=0, padx=16, pady=(20, 4), sticky="e"
        )
        self._url_var = ctk.StringVar()
        ctk.CTkEntry(self, textvariable=self._url_var, width=320).grid(
            row=0, column=1, padx=(4, 16), pady=(20, 4), sticky="ew"
        )

        self._err = ctk.CTkLabel(
            self, text="", text_color="#c0392b", font=ctk.CTkFont(size=12)
        )
        self._err.grid(row=1, column=0, columnspan=2, padx=16, pady=2)

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(4, 16))

        self._add_btn = ctk.CTkButton(
            btn_frame, text="Add", width=110, command=self._fetch_and_save
        )
        self._add_btn.pack(side="left", padx=8)
        ctk.CTkButton(
            btn_frame, text="Cancel", width=110,
            fg_color="#383838", hover_color="#484848",
            command=self.destroy,
        ).pack(side="left", padx=8)

    def _fetch_and_save(self):
        url = self._url_var.get().strip()
        if not url.startswith("http"):
            self._err.configure(text="Feed URL must start with http.")
            return
        self._add_btn.configure(state="disabled", text="Fetching…")
        self._err.configure(text="")
        threading.Thread(target=self._worker, args=(url,), daemon=True).start()

    def _worker(self, url: str):
        try:
            feed = feedparser.parse(url)
            title = (getattr(feed.feed, "title", "") or "").strip()
            if not title:
                self.after(0, lambda: self._on_error("Could not read feed title. Check the URL."))
                return
            self.after(0, lambda t=title, u=url: self._on_success(t, u))
        except Exception as exc:
            self.after(0, lambda: self._on_error(f"Error fetching feed: {exc}"))

    def _on_error(self, msg: str):
        self._err.configure(text=msg)
        self._add_btn.configure(state="normal", text="Add")

    def _on_success(self, name: str, url: str):
        # Deduplicate name if another feed already has the same title
        existing_names = {f["name"] for f in self._state.feeds}
        final_name = name
        n = 2
        while final_name in existing_names:
            final_name = f"{name} ({n})"
            n += 1
        self._state.add_feed(final_name, url)
        self.destroy()
        self._on_added()


# ─── Main Application ─────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self._state = StateManager()
        self._episodes: list = []
        self._batch_dl_active = False

        self.title(APP_TITLE)
        self.geometry("1140x720")
        self.minsize(820, 520)

        _start_thumb_workers(4)
        self._build()
        self.after(200, self._startup_load)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # ── Top bar ──────────────────────────────────────────────────────────
        bar = ctk.CTkFrame(self, height=52, fg_color="#141428", corner_radius=0)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(2, weight=1)

        # ── Feed selector ─────────────────────────────────────────────────────
        feed_frame = ctk.CTkFrame(bar, fg_color="transparent")
        feed_frame.grid(row=0, column=0, padx=(12, 4), pady=8, sticky="w")

        ctk.CTkLabel(
            feed_frame, text="Feed:",
            font=ctk.CTkFont(size=13),
            text_color="#a0a0c0",
        ).pack(side="left", padx=(0, 6))

        feed_names = [f["name"] for f in self._state.feeds]
        initial_name = self._state.active_feed_name or "(no feeds)"
        self._feed_var = ctk.StringVar(value=initial_name)
        self._feed_menu = ctk.CTkOptionMenu(
            feed_frame,
            variable=self._feed_var,
            values=feed_names if feed_names else ["(no feeds)"],
            width=220,
            fg_color="#1e1e38",
            button_color="#2a2a48",
            button_hover_color="#3a3a5a",
            command=self._on_feed_changed,
        )
        self._feed_menu.pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            feed_frame, text="+", width=30,
            fg_color="#2a4a2a", hover_color="#3a6a3a",
            command=self._add_feed,
        ).pack(side="left", padx=2)

        self._remove_feed_btn = ctk.CTkButton(
            feed_frame, text="−", width=30,
            fg_color="#4a2a2a", hover_color="#6a3a3a",
            command=self._remove_feed,
        )
        self._remove_feed_btn.pack(side="left", padx=2)

        # ── App title (small, centred) ─────────────────────────────────────────
        ctk.CTkLabel(
            bar, text=APP_TITLE,
            font=ctk.CTkFont(size=13),
            text_color="#505068",
        ).grid(row=0, column=1, padx=8)

        self._status = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=12), text_color="gray55",
        )
        self._status.grid(row=0, column=2, padx=8)

        btn_bar = ctk.CTkFrame(bar, fg_color="transparent")
        btn_bar.grid(row=0, column=3, padx=10, pady=8)

        self._refresh_btn = ctk.CTkButton(
            btn_bar, text="⟳  Refresh", width=100,
            fg_color="#2a2a44", hover_color="#3a3a58",
            command=self._refresh_feed,
        )
        self._refresh_btn.pack(side="left", padx=4)

        self._dl_all_btn = ctk.CTkButton(
            btn_bar, text="↓  Download All", width=130,
            fg_color="#1a4a7a", hover_color="#123558",
            command=self._download_all,
        )
        self._dl_all_btn.pack(side="left", padx=4)

        ctk.CTkButton(
            btn_bar, text="⚙  Settings", width=100,
            fg_color="#383838", hover_color="#484848",
            command=self._open_settings,
        ).pack(side="left", padx=4)

        # ── Left: episode list ────────────────────────────────────────────────
        self._ep_list = EpisodeListCanvas(
            self, on_select=self._select_episode, state=self._state,
            width=350,
        )
        self._ep_list.grid(row=1, column=0, sticky="nsew")
        self._ep_list.grid_propagate(False)

        # ── Right: detail panel ───────────────────────────────────────────────
        self._detail = DetailPanel(
            self, self._state,
            on_downloaded=self._on_episode_downloaded,
            on_state_changed=self._on_state_changed,
            fg_color="#222222", corner_radius=0,
        )
        self._detail.grid(row=1, column=1, sticky="nsew")

    # ── Feed loading ──────────────────────────────────────────────────────────

    def _startup_load(self):
        """Show cached feed instantly, then silently refresh from network."""
        url = self._state.active_feed_url
        if not url:
            self._episodes = []
            self._ep_list.set_episodes([])
            self._status.configure(
                text="No feeds configured. Click + to add a feed.",
                text_color="gray55",
            )
            return
        cached = load_feed_cache(url)
        if cached:
            self._episodes = cached
            self._status.configure(
                text=f"{len(cached)} episodes  ·  Checking for updates…",
                text_color="gray55",
            )
            self._populate_list()
            # Background refresh — only re-populates if content actually changed
            threading.Thread(target=self._silent_refresh_worker, daemon=True).start()
        else:
            # First run for this feed — must load from network
            self._refresh_feed()

    def _silent_refresh_worker(self):
        url = self._state.active_feed_url
        try:
            eps = parse_feed(url)
            self.after(0, lambda e=eps, u=url: self._on_silent_refresh_done(e, u))
        except Exception:
            # Network unavailable — cached data is fine, just update status
            self.after(0, lambda: self._status.configure(
                text=f"{len(self._episodes)} episodes  ·  (offline — showing cached)",
                text_color="gray55",
            ))

    def _on_silent_refresh_done(self, new_eps: list, url: str):
        save_feed_cache(url, new_eps)
        ts = datetime.now().strftime("%H:%M")
        old_guids = [e["guid"] for e in self._episodes]
        new_guids = [e["guid"] for e in new_eps]
        if new_guids != old_guids:
            # New or reordered episodes — rebuild the list
            self._episodes = new_eps
            self._populate_list()
        self._status.configure(
            text=f"{len(new_eps)} episodes  ·  Updated {ts}",
            text_color="gray55",
        )

    def _refresh_feed(self):
        """Explicit refresh triggered by the Refresh button."""
        url = self._state.active_feed_url
        if not url:
            return
        self._status.configure(text="Refreshing feed…", text_color="gray55")
        self._refresh_btn.configure(state="disabled")

        def _fetch():
            try:
                eps = parse_feed(url)
                self.after(0, lambda e=eps, u=url: self._on_feed_ready(e, u))
            except Exception as exc:
                self.after(0, lambda: self._on_feed_error(str(exc)))

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_feed_ready(self, episodes: list, url: str):
        save_feed_cache(url, episodes)
        self._episodes = episodes
        self._refresh_btn.configure(state="normal")
        ts = datetime.now().strftime("%H:%M")
        self._status.configure(
            text=f"{len(episodes)} episodes  ·  Updated {ts}",
            text_color="gray55",
        )
        self._populate_list()

    def _on_feed_error(self, msg: str):
        self._refresh_btn.configure(state="normal")
        self._status.configure(
            text=f"Feed error: {msg[:80]}",
            text_color="#c0392b",
        )

    def _populate_list(self):
        self._ep_list.set_episodes(self._episodes)

    # ── Selection ─────────────────────────────────────────────────────────────

    def _select_episode(self, episode: dict):
        self._detail.load_episode(episode)

    # ── Download callbacks ────────────────────────────────────────────────────

    def _on_episode_downloaded(self, _):
        self._ep_list.refresh_badges()

    def _on_state_changed(self):
        self._ep_list.refresh()

    # ── Batch download ────────────────────────────────────────────────────────

    def _download_all(self):
        if self._batch_dl_active:
            return

        pending = [
            ep for ep in self._episodes
            if not self._state.is_downloaded(ep["guid"])
            and ep.get("enclosure_url")
        ]

        if not pending:
            self._status.configure(
                text="All episodes already downloaded.", text_color="#27ae60",
            )
            return

        self._batch_dl_active = True
        self._dl_all_btn.configure(state="disabled")

        folder = Path(self._state.download_folder)
        folder.mkdir(parents=True, exist_ok=True)
        total_count = len(pending)

        def _worker():
            for idx, ep in enumerate(pending, 1):
                self.after(0, lambda i=idx, t=ep["title"]: self._status.configure(
                    text=f"Batch download {i}/{total_count}: {t[:50]}…",
                    text_color="gray55",
                ))

                url = ep["enclosure_url"]
                parsed = urlparse(url)
                raw_name = unquote(parsed.path.split("/")[-1])
                ext = Path(raw_name).suffix or ".mp4"
                filename = sanitize_filename(ep["title"]) + ext
                filepath = folder / filename

                try:
                    resp = requests.get(
                        url, stream=True, timeout=30,
                        headers={"User-Agent": "TWiT-FeedManager/1.0"},
                    )
                    resp.raise_for_status()
                    with open(filepath, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=131072):
                            if chunk:
                                fh.write(chunk)
                    self._state.mark_downloaded(ep["guid"], filename)
                    self.after(0, lambda g=ep["guid"]: self._on_episode_downloaded(g))

                except Exception as exc:
                    print(f"[batch] failed '{ep['title']}': {exc}")
                    try:
                        if filepath.exists():
                            filepath.unlink()
                    except Exception:
                        pass

            self.after(0, self._on_batch_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_batch_done(self):
        self._batch_dl_active = False
        self._dl_all_btn.configure(state="normal")
        self._status.configure(
            text="Batch download complete.", text_color="#27ae60",
        )

    # ── Feed management ───────────────────────────────────────────────────────

    def _on_feed_changed(self, name: str):
        feeds = self._state.feeds
        for i, f in enumerate(feeds):
            if f["name"] == name:
                if i == self._state.active_feed_idx:
                    return  # already active
                self._state.set_active_feed(i)
                break
        self._episodes = []
        self._ep_list.set_episodes([])
        self._startup_load()

    def _add_feed(self):
        AddFeedDialog(self, self._state, on_added=self._on_feed_added)

    def _on_feed_added(self):
        self._refresh_feed_menu()
        self._startup_load()

    def _remove_feed(self):
        if not self._state.feeds:
            return
        name = self._state.active_feed_name
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Remove Feed", f"Remove '{name}'?", parent=self
        ):
            return
        self._state.remove_feed(self._state.active_feed_idx)
        self._refresh_feed_menu()
        self._episodes = []
        self._ep_list.set_episodes([])
        if self._state.feeds:
            self._startup_load()
        else:
            self._status.configure(
                text="No feeds configured. Click + to add a feed.",
                text_color="gray55",
            )

    def _refresh_feed_menu(self):
        feeds = self._state.feeds
        names = [f["name"] for f in feeds]
        if not names:
            self._feed_menu.configure(values=["(no feeds)"])
            self._feed_var.set("(no feeds)")
        else:
            self._feed_menu.configure(values=names)
            self._feed_var.set(self._state.active_feed_name)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _open_settings(self):
        SettingsDialog(self, self._state)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
