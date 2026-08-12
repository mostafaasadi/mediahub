import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import flet as ft

from backend import (
    DB_NAME,
    DatabaseManager,
    PlayerManager,
    PosterManager,
    format_time,
    logger,
    fetch_episodes_from_folder,
    sort_episodes,
)

ICON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets",
    "icon.png",
)

SIZE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:[KMGT]i?B|[KMGT])\b"
)


class T:
    BG = "#050810"
    SURFACE = "#0B1220"
    SURFACE_2 = "#111A2E"
    SURFACE_3 = "#1F2B45"
    BORDER = "#22304D"
    BORDER_LIGHT = "#35476B"
    TEXT = "#EDF2FF"
    TEXT_SEC = "#B6C2D9"
    DIM = "#7C8AA5"
    ACCENT = "#6366F1"
    ACCENT_2 = "#38BDF8"
    ACCENT_GLOW = "#406366F1"
    ACCENT_SOFT = "#1F6366F1"
    ACCENT_HOVER = "#8B8DF6"
    WATCHED = "#34D399"
    WATCHED_SOFT = "#1F34D399"
    WATCHED_GLOW = "#3334D399"
    CARD_W = 200
    POSTER_H = 300
    FOOTER_H = 128
    CARD_H = POSTER_H + FOOTER_H
    TRACK_W = CARD_W - 28


def parse_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def parse_season_episode(filename):
    if not filename:
        return None
    match = re.search(
        r"[Ss](\d{1,2})\s*[Ee](\d{1,2})",
        filename,
    )
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        return f"S{season:02d} E{episode:02d}"
    match = re.search(r"(\d{1,2})x(\d{1,2})", filename)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        return f"S{season:02d} E{episode:02d}"
    return None


def parse_season_episode_tuple(filename):
    if not filename:
        return None
    match = re.search(
        r"[Ss](\d{1,2})\s*[Ee](\d{1,2})",
        filename,
    )
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d{1,2})x(\d{1,2})", filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def parse_option_label(label):
    match = re.search(
        r"Season\s+(\d+)\s*-\s*(.+)",
        label or "",
        re.IGNORECASE,
    )
    if match:
        return parse_int(match.group(1)), match.group(2).strip()
    return None, None


def normalize_links(media_type, links):
    options = []
    if not isinstance(links, dict):
        return options
    if media_type == "movie":
        for items in links.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not item.get("file"):
                    continue
                quality = str(item.get("quality") or "File")
                version = str(item.get("version") or "")
                if version:
                    label = f"{quality} - {version}"
                else:
                    label = quality
                options.append(
                    {
                        "kind": "file",
                        "label": label,
                        "url": item["file"],
                        "quality": quality,
                        "version": version,
                    }
                )
        return options
    for season_dict in links.values():
        if not isinstance(season_dict, dict):
            continue
        for season_key, items in season_dict.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not item.get("folder"):
                    continue
                season = parse_int(item.get("season", season_key))
                quality = str(item.get("quality") or "Quality")
                label = f"Season {season} - {quality}"
                options.append(
                    {
                        "kind": "folder",
                        "label": label,
                        "url": item["folder"],
                        "season": season,
                        "quality": quality,
                    }
                )
    options.sort(
        key=lambda opt: (
            opt.get("season", 0),
            opt.get("quality", ""),
        )
    )
    return options


def format_bytes(num):
    if num <= 0:
        return ""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}PB"


class ModernMediaCard(ft.Container):
    def __init__(
        self,
        data: dict,
        is_history: bool = False,
        on_click_callback=None,
        poster_url: str = "",
    ):
        super().__init__()
        self.data = data
        self.is_history = is_history
        self.on_click_callback = on_click_callback
        self.poster_url = poster_url

        title = data.get("title_en", "Unknown")
        year = data.get("year")
        media_type = data.get(
            "type",
            data.get("media_type", "movie"),
        )
        type_label = "Series" if media_type == "series" else "Movie"

        progress_seconds = float(data.get("progress_seconds") or 0.0)
        duration = float(data.get("progress_duration") or 0.0)
        progress_percent = 0.0
        if is_history and progress_seconds > 0:
            if duration > 0:
                progress_percent = min(progress_seconds / duration, 1.0)
            else:
                progress_percent = min(progress_seconds / 5400.0, 0.95)

        completed = is_history and progress_percent >= 0.95
        up_next = False
        if (
            completed
            and media_type == "series"
            and not data.get("_series_finished", True)
        ):
            completed = False
            up_next = True

        se_label = None
        if is_history and media_type == "series":
            se_label = parse_season_episode(
                data.get("last_episode_filename", "")
            )

        fill_w = 0
        if is_history and progress_seconds > 0 and progress_percent > 0:
            fill_w = max(6, int(T.TRACK_W * progress_percent))

        percent_int = int(progress_percent * 100)
        if progress_seconds > 0 and percent_int < 1:
            percent_label = "<1%"
        else:
            percent_label = f"{percent_int}%"

        poster_placeholder = ft.Container(
            width=T.CARD_W,
            height=T.POSTER_H,
            bgcolor=T.SURFACE_2,
            content=ft.Icon(
                ft.Icons.MOVIE_CREATION_OUTLINED,
                size=34,
                color=T.SURFACE_3,
            ),
            alignment=ft.Alignment(0, 0),
        )

        self.poster_img = ft.Image(
            src=poster_url,
            fit="cover",
            width=T.CARD_W,
            height=T.POSTER_H,
            opacity=1 if poster_url else 0,
            animate_opacity=ft.Animation(
                600,
                ft.AnimationCurve.EASE_OUT,
            ),
            animate_scale=ft.Animation(
                500,
                ft.AnimationCurve.EASE_OUT,
            ),
        )

        self.hover_scrim = ft.Container(
            width=T.CARD_W,
            height=T.POSTER_H,
            bgcolor="#B3050810",
            opacity=0,
            animate_opacity=ft.Animation(
                220,
                ft.AnimationCurve.EASE_OUT,
            ),
        )

        self.play_circle = ft.Container(
            width=56,
            height=56,
            border_radius=28,
            bgcolor=T.ACCENT_2,
            border=ft.Border.all(1, "#33FFFFFF"),
            alignment=ft.Alignment(0, 0),
            content=ft.Icon(
                ft.Icons.PLAY_ARROW_ROUNDED,
                color=T.BG,
                size=30,
            ),
            shadow=ft.BoxShadow(
                blur_radius=24,
                offset=ft.Offset(0, 8),
                color="#5538BDF8",
            ),
            opacity=0,
            scale=0.85,
            animate_opacity=ft.Animation(
                220,
                ft.AnimationCurve.EASE_OUT,
            ),
            animate_scale=ft.Animation(
                220,
                ft.AnimationCurve.EASE_OUT,
            ),
        )

        center_wrap = ft.Container(
            width=T.CARD_W,
            height=T.POSTER_H,
            alignment=ft.Alignment(0, 0),
            content=self.play_circle,
        )

        poster_stack = ft.Stack(
            [
                poster_placeholder,
                self.poster_img,
                self.hover_scrim,
                center_wrap,
            ],
            width=T.CARD_W,
            height=T.POSTER_H,
        )

        title_box = ft.Container(
            height=36,
            content=ft.Text(
                title,
                size=13,
                weight="700",
                color=T.TEXT,
                max_lines=2,
                overflow="ellipsis",
            ),
        )

        self.browse_btn = None
        self.next_btn = None

        if is_history:
            browse_icon = ft.Icons.VIDEO_LIBRARY_OUTLINED
            browse_tip = "Browse"
            if media_type == "movie":
                browse_icon = ft.Icons.TUNE_ROUNDED
                browse_tip = "Quality options"

            self.browse_btn = ft.Container(
                width=24,
                height=24,
                border_radius=12,
                bgcolor="#1AFFFFFF",
                alignment=ft.Alignment(0, 0),
                content=ft.Icon(
                    browse_icon,
                    size=14,
                    color=T.TEXT,
                ),
                tooltip=browse_tip,
                ink=True,
                on_click=self._on_browse_click,
                scale=1.0,
                animate_scale=ft.Animation(
                    180,
                    ft.AnimationCurve.EASE_OUT,
                ),
            )

            if media_type == "series":
                self.next_btn = ft.Container(
                    width=24,
                    height=24,
                    border_radius=12,
                    bgcolor="#1AFFFFFF",
                    alignment=ft.Alignment(0, 0),
                    content=ft.Icon(
                        ft.Icons.SKIP_NEXT_ROUNDED,
                        size=14,
                        color=T.TEXT,
                    ),
                    tooltip="Next Episode",
                    ink=True,
                    on_click=self._on_next_click,
                    scale=1.0,
                    animate_scale=ft.Animation(
                        180,
                        ft.AnimationCurve.EASE_OUT,
                    ),
                )

        meta_children = [
            ft.Text(
                str(year) if year else "—",
                size=10,
                color=T.DIM,
            ),
            ft.Container(
                width=3,
                height=3,
                border_radius=2,
                bgcolor="#33FFFFFF",
            ),
            ft.Text(type_label, size=10, color=T.DIM),
        ]

        if se_label:
            meta_children.append(
                ft.Text(
                    se_label,
                    size=10,
                    weight="700",
                    color=T.ACCENT_2,
                )
            )

        meta_children.append(ft.Container(expand=True))

        if self.browse_btn:
            meta_children.append(self.browse_btn)
        if self.next_btn:
            meta_children.append(self.next_btn)

        meta_row = ft.Row(
            meta_children,
            spacing=6,
            vertical_alignment="center",
        )

        if completed:
            pill_content = ft.Row(
                [
                    ft.Icon(
                        ft.Icons.DONE_ROUNDED,
                        size=10,
                        color=T.WATCHED,
                    ),
                    ft.Text(
                        "Watched",
                        size=9,
                        weight="700",
                        color=T.WATCHED,
                    ),
                ],
                spacing=3,
                tight=True,
            )
            pill_bg = T.WATCHED_SOFT
            pill_border = T.WATCHED_GLOW
            state_color = T.WATCHED
        elif up_next:
            pill_content = ft.Row(
                [
                    ft.Icon(
                        ft.Icons.SKIP_NEXT_ROUNDED,
                        size=10,
                        color=T.ACCENT_HOVER,
                    ),
                    ft.Text(
                        "Up Next",
                        size=9,
                        weight="700",
                        color=T.ACCENT_HOVER,
                    ),
                ],
                spacing=3,
                tight=True,
            )
            pill_bg = T.ACCENT_SOFT
            pill_border = T.ACCENT_GLOW
            state_color = T.ACCENT_2
        else:
            pill_content = ft.Text(
                percent_label,
                size=9,
                weight="700",
                color=T.ACCENT_HOVER,
            )
            pill_bg = T.ACCENT_SOFT
            pill_border = T.ACCENT_GLOW
            state_color = T.ACCENT_2

        percent_pill = ft.Container(
            bgcolor=pill_bg,
            border=ft.Border.all(1, pill_border),
            border_radius=10,
            padding=ft.Padding(
                left=7,
                top=2,
                right=7,
                bottom=2,
            ),
            content=pill_content,
        )

        info_row = ft.Row(
            [
                ft.Icon(
                    ft.Icons.PLAY_ARROW_ROUNDED,
                    size=11,
                    color=state_color,
                ),
                ft.Text(
                    format_time(progress_seconds),
                    size=10,
                    weight="600",
                    color=T.TEXT_SEC,
                ),
                ft.Container(expand=True),
                percent_pill,
            ],
            spacing=5,
            vertical_alignment="center",
        )

        dot = None
        if fill_w > 0:
            dot = ft.Container(
                left=min(max(0, fill_w - 4), T.TRACK_W - 7),
                top=0,
                width=7,
                height=7,
                border_radius=4,
                bgcolor=state_color,
                border=ft.Border.all(1.5, "#DDFFFFFF"),
            )

        bar_children = [
            ft.Container(
                left=0,
                top=2,
                width=T.TRACK_W,
                height=3,
                border_radius=2,
                bgcolor="#14FFFFFF",
            ),
        ]

        if completed:
            bar_children.append(
                ft.Container(
                    left=0,
                    top=2,
                    width=fill_w,
                    height=3,
                    border_radius=2,
                    bgcolor=T.WATCHED,
                )
            )
        else:
            bar_children.append(
                ft.Container(
                    left=0,
                    top=2,
                    width=fill_w,
                    height=3,
                    border_radius=2,
                    gradient=ft.LinearGradient(
                        begin=ft.Alignment(0, 0),
                        end=ft.Alignment(1, 0),
                        colors=[T.ACCENT, T.ACCENT_2],
                    ),
                )
            )

        if dot:
            bar_children.append(dot)

        bar = ft.Stack(bar_children, width=T.TRACK_W, height=7)

        footer_children = [
            title_box,
            ft.Container(height=6),
            meta_row,
        ]

        if is_history and progress_seconds > 0:
            footer_children.extend(
                [
                    ft.Container(height=10),
                    info_row,
                    ft.Container(height=8),
                    bar,
                ]
            )

        footer = ft.Container(
            width=T.CARD_W,
            height=T.FOOTER_H,
            bgcolor=T.SURFACE,
            padding=ft.Padding(
                left=14,
                top=10,
                right=14,
                bottom=10,
            ),
            content=ft.Column(
                footer_children,
                spacing=0,
                tight=True,
            ),
        )

        self.content = ft.Column(
            [poster_stack, footer],
            spacing=0,
            tight=True,
        )

        self.width = T.CARD_W
        self.height = T.CARD_H
        self.clip_behavior = "antiAlias"
        self.border_radius = 14
        self.bgcolor = T.SURFACE
        self.ink = True
        self.on_click = self._on_card_click
        self.on_hover = self._on_hover
        self.animate_scale = ft.Animation(
            220,
            ft.AnimationCurve.EASE_OUT,
        )
        self.animate_shadow = ft.Animation(
            220,
            ft.AnimationCurve.EASE_OUT,
        )

    def _on_hover(self, e: ft.HoverEvent):
        on = e.data == "true"
        self.scale = 1.03 if on else 1.0
        self.poster_img.scale = 1.06 if on else 1.0
        self.hover_scrim.opacity = 1 if on else 0
        self.play_circle.opacity = 1 if on else 0
        self.play_circle.scale = 1.0 if on else 0.85
        if self.browse_btn:
            self.browse_btn.scale = 1.12 if on else 1.0
            self.browse_btn.update()
        if self.next_btn:
            self.next_btn.scale = 1.12 if on else 1.0
            self.next_btn.update()
        self.hover_scrim.update()
        self.play_circle.update()
        if on:
            self.shadow = ft.BoxShadow(
                blur_radius=28,
                offset=ft.Offset(0, 12),
                color="#B3000000",
            )
        else:
            self.shadow = ft.BoxShadow(
                blur_radius=0,
                color="#00000000",
            )
        self.update()

    def _on_card_click(self, e):
        logger.info(
            "card clicked: imdb=%s is_history=%s title=%s",
            self.data.get("imdb_id"),
            self.is_history,
            self.data.get("title_en"),
        )
        if self.on_click_callback:
            self.on_click_callback(self.data, self.is_history)

    def _on_next_click(self, e):
        logger.info(
            "next episode button clicked: imdb=%s title=%s",
            self.data.get("imdb_id"),
            self.data.get("title_en"),
        )
        if self.on_click_callback:
            self.on_click_callback(
                self.data,
                True,
                show_next=True,
            )

    def _on_browse_click(self, e):
        logger.info(
            "browse button clicked: imdb=%s title=%s",
            self.data.get("imdb_id"),
            self.data.get("title_en"),
        )
        if self.on_click_callback:
            self.on_click_callback(
                self.data,
                True,
                browse=True,
            )


class MediaHubApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.db = DatabaseManager(DB_NAME)
        self.player = PlayerManager(self.db)
        self.player.on_error = lambda msg: self._show_snackbar(msg)
        self.poster_manager = PosterManager(self.db)
        self._size_cache = {}
        self._episodes_cache = {}

        self.page.title = "Media Hub"
        self.page.theme_mode = "dark"
        self.page.bgcolor = T.BG
        self.page.theme = ft.Theme(
            use_material3=True,
            color_scheme=ft.ColorScheme(
                primary=T.ACCENT,
                surface=T.SURFACE,
                on_surface=T.TEXT,
                outline=T.BORDER,
            ),
            scrollbar_theme=ft.ScrollbarTheme(
                thickness=5,
                radius=3,
                thumb_color=T.BORDER_LIGHT,
                track_color="transparent",
            ),
        )
        self.page.padding = 0
        self.page.scroll = "auto"
        self.page.window_width = 1200
        self.page.window_height = 800

        self.resize_timer = None
        self._page_width = None
        self._poster_cache = {}

        self._set_window_icon()

        self.search_timer = None
        self.last_search_query = ""

        self._build_ui()
        self._initial_load()

    def _set_window_icon(self):
        if not os.path.exists(ICON_PATH):
            return
        try:
            self.page.window.icon = ICON_PATH
        except Exception:
            try:
                self.page.window_icon = ICON_PATH
            except Exception:
                pass

    def _cached_poster(self, media):
        imdb_id = media.get("imdb_id", "")
        if not imdb_id:
            return ""
        return self._poster_cache.get(imdb_id, "")

    def _cards_per_row(self):
        width = self._page_width
        if not width:
            width = getattr(self.page, "window_width", 1200)
        if not width:
            width = 1200
        available = width - 80
        return max(1, int((available + 15) // (T.CARD_W + 15)))

    def _wrap_cards(self, cards):
        per_row = self._cards_per_row()
        rows = []
        for i in range(0, len(cards), per_row):
            rows.append(
                ft.Row(
                    cards[i:i + per_row],
                    spacing=15,
                )
            )
        return rows

    def _on_page_resize(self, e):
        self._page_width = getattr(e, "width", None)
        if self.resize_timer:
            self.resize_timer.cancel()
        self.resize_timer = threading.Timer(
            0.3,
            lambda: self.page.run_thread(
                self._refresh_ui,
                self.last_search_query,
            ),
        )
        self.resize_timer.start()

    def _get_all_history(self):
        try:
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM history ORDER BY timestamp DESC"
            )
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def _get_cached_episodes(self, folder_url):
        if not folder_url:
            return None
        now = time.time()
        cached = self._episodes_cache.get(folder_url)
        if cached and now - cached[0] < 1800:
            return cached[1]
        episodes = fetch_episodes_from_folder(folder_url)
        if episodes is not None:
            self._episodes_cache[folder_url] = (now, episodes)
        return episodes

    def _build_ui(self):
        self.continue_wrap = ft.Column(spacing=18)
        self.watched_series_wrap = ft.Column(spacing=18)
        self.watched_movies_wrap = ft.Column(spacing=18)
        self.library_wrap = ft.Column(spacing=18)

        self.search_clear_btn = ft.Container(
            content=ft.IconButton(
                icon=ft.Icons.CLOSE_ROUNDED,
                tooltip="Clear search",
                icon_color=T.DIM,
                icon_size=18,
                on_click=self._clear_search,
            ),
            padding=ft.Padding(
                left=0,
                top=0,
                right=6,
                bottom=0,
            ),
            visible=False,
        )

        self.search_field = ft.TextField(
            hint_text="Search movies & series...",
            prefix_icon=ft.Icons.SEARCH,
            border_radius=25,
            on_change=self._on_search_change,
            expand=True,
            height=46,
            bgcolor=T.SURFACE,
            border_color=T.BORDER,
            focused_border_color=T.ACCENT,
            color=T.TEXT,
            hint_style=ft.TextStyle(color=T.DIM, size=13),
        )
        self.search_field.suffix = self.search_clear_btn

        self.settings_btn = ft.IconButton(
            icon=ft.Icons.SETTINGS_OUTLINED,
            tooltip="Settings",
            on_click=self._open_settings,
            icon_color=T.DIM,
        )

        nav_bar = ft.Container(
            top=0,
            left=0,
            right=0,
            content=ft.Row(
                [
                    self.search_field,
                    ft.Container(width=8),
                    self.settings_btn,
                ],
                vertical_alignment="center",
            ),
            padding=ft.Padding(
                left=40,
                top=14,
                right=24,
                bottom=14,
            ),
            bgcolor="#D9050810",
            blur=ft.Blur(24, 24),
            border=ft.Border(
                bottom=ft.BorderSide(1, T.BORDER)
            ),
        )

        self.continue_section = ft.Container(
            content=ft.Column(
                [
                    self._section_header("Continue Watching"),
                    self.continue_wrap,
                ],
                spacing=14,
            ),
            padding=ft.Padding(
                left=40,
                top=28,
                right=40,
                bottom=10,
            ),
            visible=True,
        )

        self.watched_series_section = ft.Container(
            content=ft.Column(
                [
                    self._section_header("Watched Series"),
                    self.watched_series_wrap,
                ],
                spacing=14,
            ),
            padding=ft.Padding(
                left=40,
                top=28,
                right=40,
                bottom=10,
            ),
            visible=False,
        )

        self.watched_movies_section = ft.Container(
            content=ft.Column(
                [
                    self._section_header("Watched Movies"),
                    self.watched_movies_wrap,
                ],
                spacing=14,
            ),
            padding=ft.Padding(
                left=40,
                top=28,
                right=40,
                bottom=10,
            ),
            visible=False,
        )

        self.library_section = ft.Container(
            content=ft.Column(
                [
                    self._section_header("Search Results"),
                    self.library_wrap,
                ],
                spacing=14,
            ),
            padding=ft.Padding(
                left=40,
                top=28,
                right=40,
                bottom=40,
            ),
            visible=False,
        )

        main_content = ft.Container(
            top=0,
            left=0,
            right=0,
            bottom=0,
            content=ft.Column(
                [
                    self.continue_section,
                    self.watched_series_section,
                    self.watched_movies_section,
                    self.library_section,
                ],
                spacing=0,
                scroll="auto",
                expand=True,
            ),
            padding=ft.Padding(
                left=0,
                top=76,
                right=0,
                bottom=0,
            ),
        )

        self.page.add(
            ft.Stack([main_content, nav_bar], expand=True)
        )

        if hasattr(self.page, "on_resized"):
            self.page.on_resized = self._on_page_resize
        elif hasattr(self.page, "on_resize"):
            self.page.on_resize = self._on_page_resize

    def _section_header(self, label: str):
        return ft.Row(
            [
                ft.Text(
                    label,
                    size=16,
                    weight="800",
                    color=T.TEXT,
                    style=ft.TextStyle(letter_spacing=0.5),
                ),
                ft.Container(expand=True),
            ],
            vertical_alignment="center",
        )

    def _sheet_height(self, minimum=520):
        height = getattr(self.page, "window_height", 800)
        if not height:
            height = 800
        return max(minimum, int(height - 130))

    def _sheet_width(self, minimum=800):
        width = getattr(self.page, "window_width", 1200)
        if not width:
            width = 1200
        return max(minimum, min(1200, int(width * 0.8)))

    def _open_settings(self, e):
        logger.info("settings opened")
        current_url = self.db.get_js_url()
        current_api_key = self.db.get_omdb_api_key()

        self.url_input = ft.TextField(
            label="Base URL for Catalog Updates",
            value=current_url,
            border_radius=10,
            expand=True,
            text_size=13,
            hint_text="https://example.com/path/to/catalog.js",
            focused_border_color=T.ACCENT,
        )

        self.api_key_input = ft.TextField(
            label="OMDB API Key (for movie posters)",
            value=current_api_key,
            border_radius=10,
            expand=True,
            text_size=13,
            hint_text="Get free API key from omdbapi.com/apikey.aspx",
            password=True,
            can_reveal_password=True,
            focused_border_color=T.ACCENT,
        )

        settings_content = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        "Settings",
                        size=26,
                        color=T.TEXT,
                        style=ft.TextStyle(letter_spacing=2),
                    ),
                    ft.Container(height=1, bgcolor=T.BORDER),
                    ft.Text(
                        "Update Catalog Source URL",
                        size=13,
                        color=T.DIM,
                    ),
                    ft.Container(height=8),
                    self.url_input,
                    ft.Container(height=18),
                    ft.Text(
                        "OMDB API Key",
                        size=13,
                        color=T.DIM,
                    ),
                    ft.Container(height=8),
                    self.api_key_input,
                    ft.Text(
                        "Get your free API key from: "
                        "omdbapi.com/apikey.aspx",
                        size=11,
                        color=T.DIM,
                    ),
                    ft.Container(height=20),
                    ft.Row(
                        [
                            ft.Container(expand=True),
                            ft.OutlinedButton(
                                "Sync Now",
                                icon=ft.Icons.SYNC,
                                on_click=self._sync_and_close_settings,
                                style=ft.ButtonStyle(
                                    color=T.TEXT,
                                    side=ft.BorderSide(
                                        1,
                                        T.BORDER,
                                    ),
                                    shape=(
                                        ft.RoundedRectangleBorder(
                                            radius=8
                                        )
                                    ),
                                ),
                            ),
                            ft.FilledButton(
                                "Save & Sync",
                                icon=ft.Icons.SAVE,
                                on_click=self._save_settings,
                                style=ft.ButtonStyle(
                                    color=T.TEXT,
                                    bgcolor=T.ACCENT,
                                    shape=(
                                        ft.RoundedRectangleBorder(
                                            radius=8
                                        )
                                    ),
                                ),
                            ),
                            ft.TextButton(
                                "Cancel",
                                on_click=lambda e: (
                                    self._close_settings()
                                ),
                            ),
                        ],
                        alignment="end",
                        spacing=8,
                    ),
                ],
                tight=True,
                spacing=8,
            ),
            padding=24,
            bgcolor=T.SURFACE,
            border_radius=ft.BorderRadius(18, 18, 0, 0),
        )

        self.settings_sheet = ft.BottomSheet(
            content=settings_content
        )
        self.page.overlay.append(self.settings_sheet)
        self.settings_sheet.open = True
        self.page.update()

    def _clear_search(self, e=None):
        logger.info("search cleared")
        if self.search_timer:
            self.search_timer.cancel()
            self.search_timer = None
        self.search_field.value = ""
        self.last_search_query = ""
        self._set_clear_button_visible(False)
        try:
            self.search_field.update()
        except Exception:
            pass
        self._refresh_ui("")

    def _close_settings(self):
        if hasattr(self, "settings_sheet"):
            self.settings_sheet.open = False
        self.page.update()

    def _close_bottom_sheets(self):
        for overlay in list(self.page.overlay):
            if isinstance(overlay, ft.BottomSheet):
                overlay.open = False
        self.page.update()

    def _sync_and_close_settings(self, e):
        logger.info("sync now clicked")
        self._close_settings()
        self._sync_db(None)

    def _save_settings(self, e):
        new_url = self.url_input.value.strip()
        new_api_key = self.api_key_input.value.strip()
        logger.info("save settings clicked: url=%s", new_url)

        if not new_url:
            self._show_snackbar("URL cannot be empty!")
            return

        if not new_url.startswith(("http://", "https://")):
            self._show_snackbar(
                "URL must start with http:// or https://"
            )
            return

        url_saved = self.db.update_js_url(new_url)
        key_saved = self.db.update_omdb_api_key(new_api_key)

        if url_saved and key_saved:
            self.poster_manager.reload_api_key()
            self._close_settings()
            self._show_snackbar(
                "Settings saved! Syncing library..."
            )
            self._sync_db(None, force_url=new_url)
        else:
            self._show_snackbar("Failed to save settings.")

    def _initial_load(self):
        if not self.db.search(""):
            logger.info("initial load: library empty, starting sync")
            self._sync_db(None)
        else:
            logger.info("initial load: library found, refreshing UI")
            self._refresh_ui("")

    def _sync_db(self, _, force_url: str = None):
        logger.info("sync started")
        self.page.overlay.append(
            ft.ProgressBar(
                color=T.ACCENT,
                bgcolor=T.SURFACE_2,
            )
        )
        self.page.update()
        threading.Thread(
            target=self._sync_task,
            args=(force_url,),
            daemon=True,
        ).start()

    def _sync_task(self, force_url: str = None):
        try:
            success = self.db.sync_catalog(js_url=force_url)
        except Exception:
            logger.exception("sync task failed")
            success = False
        finally:
            self._post_sync(success)

    def _post_sync(self, success: bool):
        progress_bars = [
            item
            for item in self.page.overlay
            if isinstance(item, ft.ProgressBar)
        ]
        for bar in progress_bars:
            self.page.overlay.remove(bar)

        if success:
            self._refresh_ui("")
            self._show_snackbar("Library synced successfully!")
        else:
            self._show_snackbar(
                "Sync failed. Check connection and URL."
            )
        self.page.update()

    def _show_snackbar(self, message: str):
        logger.info("snackbar: %s", message)
        snack = ft.SnackBar(
            content=ft.Text(message, size=13, color=T.TEXT),
            bgcolor=T.SURFACE_2,
            behavior="floating",
            margin=18,
            shape=ft.RoundedRectangleBorder(radius=10),
        )
        self.page.overlay.append(snack)
        snack.open = True
        self.page.update()

    def _on_search_change(self, e):
        raw_value = e.control.value or ""
        query = raw_value.strip()
        self._set_clear_button_visible(bool(raw_value))
        if self.search_timer:
            self.search_timer.cancel()
        self.search_timer = threading.Timer(
            0.5,
            lambda: self._perform_search(query),
        )
        self.search_timer.start()

    def _set_clear_button_visible(self, visible: bool):
        if not hasattr(self, "search_clear_btn"):
            return
        if self.search_clear_btn.visible == visible:
            return
        self.search_clear_btn.visible = visible
        try:
            self.search_clear_btn.update()
        except Exception:
            try:
                self.page.update()
            except Exception:
                pass

    def _perform_search(self, query: str):
        if query == self.last_search_query:
            return
        self.last_search_query = query
        logger.info("search: %r", query)
        self.page.run_thread(self._refresh_ui, query)

    def _refresh_ui(self, query: str):
        if query:
            self.continue_section.visible = False
            self.watched_series_section.visible = False
            self.watched_movies_section.visible = False
            self.library_section.visible = True

            self.library_wrap.controls.clear()

            movies = self.db.search(query)
            logger.info(
                "search results: %s items for %r",
                len(movies),
                query,
            )

            cards = []
            for movie in movies:
                cards.append(
                    ModernMediaCard(
                        movie,
                        on_click_callback=self._handle_card_click,
                        poster_url=self._cached_poster(movie),
                    )
                )

            self.library_wrap.controls = self._wrap_cards(cards)
            self.page.update()
            self._load_posters_async(cards)
            return

        self.library_section.visible = False
        self.continue_wrap.controls.clear()
        self.watched_series_wrap.controls.clear()
        self.watched_movies_wrap.controls.clear()

        history = self._get_all_history()
        logger.info("all history: %s items", len(history))

        if not history:
            self.continue_section.visible = True
            self.watched_series_section.visible = False
            self.watched_movies_section.visible = False
            self.continue_wrap.controls = [self._empty_state()]
            self.page.update()
            return

        for item in history:
            ep_url = item.get("last_episode_url", "")
            if ep_url:
                item["progress_seconds"] = self.db.get_progress(
                    ep_url
                )
                item["progress_duration"] = self.db.get_duration(
                    ep_url
                )
            else:
                item["progress_seconds"] = 0.0
                item["progress_duration"] = 0.0

            progress_seconds = float(
                item["progress_seconds"] or 0.0
            )
            duration = float(item["progress_duration"] or 0.0)
            progress_percent = 0.0
            if progress_seconds > 0:
                if duration > 0:
                    progress_percent = min(
                        progress_seconds / duration, 1.0
                    )
                else:
                    progress_percent = min(
                        progress_seconds / 5400.0, 0.95
                    )
            item["_percent"] = progress_percent

        pending_series = []
        for item in history:
            if item["_percent"] >= 0.95:
                if item.get("media_type", "movie") == "series":
                    pending_series.append(item)
                else:
                    item["_section"] = "movies"
            else:
                item["_section"] = "continue"

        if pending_series:
            for item in pending_series:
                folder_url = item.get("last_folder_url", "")
                last_ep = item.get("last_episode_filename", "")
                if folder_url and last_ep:
                    cached_latest = self.db.get_cached_latest_episode(folder_url)
                    finished = (cached_latest == last_ep) if cached_latest is not None else False
                else:
                    finished = True
                item["_series_finished"] = finished
                item["_section"] = "series" if finished else "continue"

        continue_items = []
        series_items = []
        movie_items = []
        for item in history:
            section = item.get("_section", "continue")
            if section == "series":
                series_items.append(item)
            elif section == "movies":
                movie_items.append(item)
            else:
                continue_items.append(item)

        all_cards = []

        self.continue_section.visible = bool(continue_items)
        if continue_items:
            cards = []
            for item in continue_items:
                card = ModernMediaCard(
                    item,
                    is_history=True,
                    on_click_callback=self._handle_card_click,
                    poster_url=self._cached_poster(item),
                )
                cards.append(card)
                all_cards.append(card)
            self.continue_wrap.controls = self._wrap_cards(cards)

        self.watched_series_section.visible = bool(series_items)
        if series_items:
            cards = []
            for item in series_items:
                card = ModernMediaCard(
                    item,
                    is_history=True,
                    on_click_callback=self._handle_card_click,
                    poster_url=self._cached_poster(item),
                )
                cards.append(card)
                all_cards.append(card)
            self.watched_series_wrap.controls = self._wrap_cards(cards)

        self.watched_movies_section.visible = bool(movie_items)
        if movie_items:
            cards = []
            for item in movie_items:
                card = ModernMediaCard(
                    item,
                    is_history=True,
                    on_click_callback=self._handle_card_click,
                    poster_url=self._cached_poster(item),
                )
                cards.append(card)
                all_cards.append(card)
            self.watched_movies_wrap.controls = self._wrap_cards(cards)

        self.page.update()
        self._load_posters_async(all_cards)

    def _empty_state(self):
        return ft.Container(
            content=ft.Column(
                [
                    ft.Icon(
                        ft.Icons.THEATERS_OUTLINED,
                        size=40,
                        color=T.SURFACE_3,
                    ),
                    ft.Text(
                        "No recent activity. Start watching!",
                        size=13,
                        color=T.DIM,
                    ),
                ],
                horizontal_alignment="center",
                spacing=10,
                tight=True,
            ),
            padding=40,
            alignment=ft.Alignment(0, 0),
        )

    def _load_posters_async(self, cards: list):
        pending = [card for card in cards if not card.poster_url]
        if not pending:
            return
        logger.debug("loading posters for %s cards", len(pending))

        def fetch(card):
            imdb_id = card.data.get("imdb_id", "")
            return card, self.poster_manager.get_poster_url(imdb_id)

        def task():
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(fetch, pending))
            for card, poster_url in results:
                if not poster_url:
                    continue
                imdb_id = card.data.get("imdb_id", "")
                self._poster_cache[imdb_id] = poster_url
                try:
                    card.poster_url = poster_url
                    card.poster_img.src = poster_url
                    card.poster_img.opacity = 1
                    card.poster_img.update()
                except Exception:
                    pass

        threading.Thread(target=task, daemon=True).start()

    def _handle_card_click(
        self,
        data: dict,
        is_history: bool,
        show_next: bool = False,
        browse: bool = False,
    ):
        if is_history and show_next:
            self._handle_next(data)
        elif is_history and browse:
            self._open_browse(data)
        else:
            self._open_media_details(data, is_history)

    def _extract_size(self, a_tag):
        sibling = a_tag.next_sibling
        if sibling is not None and isinstance(sibling, str):
            match = SIZE_PATTERN.search(sibling)
            if match:
                return match.group(0).strip()
        row = a_tag.find_parent("tr")
        if row is not None:
            match = SIZE_PATTERN.search(row.get_text(" "))
            if match:
                return match.group(0).strip()
        return ""

    def _sort_episodes(self, episodes):
        def key(ep):
            filename = ep.get("filename", "")
            match = re.search(
                r"[Ss](\d{1,2})\s*[Ee](\d{1,2})",
                filename,
            )
            if match:
                return (
                    0,
                    int(match.group(1)),
                    int(match.group(2)),
                    filename,
                )
            match = re.search(r"(\d{1,2})x(\d{1,2})", filename)
            if match:
                return (
                    0,
                    int(match.group(1)),
                    int(match.group(2)),
                    filename,
                )
            return (1, 0, 0, filename)

        return sorted(episodes, key=key)

    def _find_episode_index(self, episodes, history_item):
        current_ep = (history_item.get("last_episode_filename") or "").strip()
        current_url = (history_item.get("last_episode_url") or "").strip()
        current_se = parse_season_episode_tuple(current_ep) if current_ep else None

        for idx, ep in enumerate(episodes):
            ep_filename = ep.get("filename", "").strip()
            ep_url = ep.get("url", "").strip()
            ep_se = parse_season_episode_tuple(ep_filename) if ep_filename else None

            if current_url and ep_url == current_url:
                return idx
            if current_ep and ep_filename == current_ep:
                return idx
            if current_se and ep_se and current_se == ep_se:
                return idx
        return -1

    def _get_movie_by_imdb(self, imdb_id):
        try:
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM movies WHERE imdb_id = ?",
                (imdb_id,),
            )
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def _get_catalog_options(self, imdb_id, media_type):
        if not imdb_id:
            return []
        movie = self._get_movie_by_imdb(imdb_id)
        if not movie:
            return []
        try:
            links = json.loads(movie.get("links", "{}"))
        except Exception:
            links = {}
        catalog_type = movie.get("type", media_type)
        return normalize_links(catalog_type, links)

    def _get_current_option(self, item, options):
        folder_url = item.get("last_folder_url", "")
        label = item.get("last_option_label", "")
        for opt in options:
            if opt.get("kind") != "folder":
                continue
            if folder_url and opt.get("url") == folder_url:
                return opt
            if label and opt.get("label") == label:
                return opt
        if folder_url:
            season, quality = parse_option_label(label)
            if season is not None:
                return {
                    "kind": "folder",
                    "label": label or f"Season {season}",
                    "url": folder_url,
                    "season": season,
                    "quality": quality or "",
                }
        return None

    def _get_option_context(self, option, item):
        if option:
            return option.get("season"), option.get("quality")
        return parse_option_label(item.get("last_option_label", ""))

    def _get_start_time(self, item, url, filename=""):
        direct = self.db.get_progress(url)
        if direct > 0:
            return direct

        last_url = item.get("last_episode_url", "")
        if not last_url or last_url == url:
            return 0.0

        media_type = item.get("media_type", item.get("type"))
        if media_type == "movie":
            return self.db.get_progress(last_url)

        old_code = parse_season_episode(
            item.get("last_episode_filename", "")
        )
        new_code = parse_season_episode(
            filename or os.path.basename(url)
        )
        if old_code and new_code and old_code == new_code:
            return self.db.get_progress(last_url)

        return 0.0

    def _get_file_size(self, url, session=None):
        if not url:
            return ""
        cached = self._size_cache.get(url)
        if cached:
            return cached
        size = self._fetch_file_size(url, session)
        if size:
            self._size_cache[url] = size
        return size

    def _fetch_file_size(self, url, session=None):
        referer = url.rsplit("/", 1)[0] + "/"
        if session is None:
            session = DatabaseManager._get_robust_session()
        try:
            res = session.head(
                url,
                timeout=6,
                allow_redirects=True,
                headers={"Referer": referer},
            )
            length = res.headers.get("Content-Length", "")
            if not length:
                res = session.get(
                    url,
                    stream=True,
                    timeout=6,
                    headers={"Referer": referer},
                )
                length = res.headers.get("Content-Length", "")
                res.close()
            if not length:
                return ""
            return format_bytes(int(length))
        except Exception:
            return ""

    def _enrich_sizes(self, pairs):
        session = DatabaseManager._get_robust_session()

        def worker(pair):
            option, subtitle, base = pair
            size = self._get_file_size(
                option.get("url", ""),
                session,
            )
            if not size:
                return
            parts = []
            if base:
                parts.append(base)
            parts.append(size)
            subtitle.value = " • ".join(parts)
            subtitle.visible = True
            self.page.update()

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(worker, pairs))

    def _handle_next(self, history_item):
        media_type = history_item.get("media_type")
        if media_type != "series":
            self._open_browse(history_item)
            return
        self._show_snackbar("Finding next episode...")
        threading.Thread(
            target=self._smart_next_task,
            args=(history_item,),
            daemon=True,
        ).start()

    def _smart_next_task(self, history_item):
        imdb_id = history_item.get("imdb_id", "")
        options = self._get_catalog_options(imdb_id, "series")
        current = self._get_current_option(history_item, options)

        folder_url = history_item.get("last_folder_url", "")
        if current:
            folder_url = current.get("url", folder_url)

        if not folder_url:
            if options:
                self._open_browse(history_item)
            else:
                self._show_snackbar("No folder URL found.")
            return

        episodes = fetch_episodes_from_folder(folder_url)
        if episodes is None:
            if options:
                self._open_browse(history_item)
            else:
                self._show_snackbar("Failed to load episodes.")
            return

        episodes = sort_episodes(episodes)
        if not episodes:
            self._show_snackbar("No episodes found.")
            return

        # First, try to find current episode by index
        current_idx = self._find_episode_index(episodes, history_item)
        if current_idx != -1 and current_idx + 1 < len(episodes):
            # Next episode exists in same folder
            option = current
            if not option:
                option = {
                    "kind": "folder",
                    "label": history_item.get("last_option_label", ""),
                    "url": folder_url,
                    "season": None,
                    "quality": "",
                }
            self._play_series_browse_episode(history_item, option, episodes[current_idx + 1])
            return

        # If current episode not found or it's the last one, try by season/episode numbers
        current_filename = (history_item.get("last_episode_filename") or "").strip()
        current_se = parse_season_episode_tuple(current_filename)
        next_idx = -1
        if current_se is not None:
            current_season, current_episode = current_se
            # Find the first episode with season > current_season or same season but episode > current_episode
            for idx, ep in enumerate(episodes):
                ep_filename = ep.get("filename", "").strip()
                ep_se = parse_season_episode_tuple(ep_filename)
                if ep_se is None:
                    continue
                ep_season, ep_episode = ep_se
                if ep_season > current_season or (ep_season == current_season and ep_episode > current_episode):
                    next_idx = idx
                    break

            if next_idx != -1:
                option = current
                if not option:
                    option = {
                        "kind": "folder",
                        "label": history_item.get("last_option_label", ""),
                        "url": folder_url,
                        "season": None,
                        "quality": "",
                    }
                self._play_series_browse_episode(history_item, option, episodes[next_idx])
                return

        # If we get here, either current episode is the last one in this folder or not found
        # Try next season
        season, quality = self._get_option_context(current, history_item)
        candidates = []
        if season is not None:
            for opt in options:
                if opt.get("kind") != "folder":
                    continue
                if parse_int(opt.get("season")) <= season:
                    continue
                candidates.append(opt)

        def candidate_key(opt):
            same_quality = opt.get("quality") == quality
            return (
                parse_int(opt.get("season")),
                0 if same_quality else 1,
                str(opt.get("quality", "")),
            )

        candidates.sort(key=candidate_key)

        for candidate in candidates:
            cand_eps = fetch_episodes_from_folder(candidate.get("url", ""))
            if not cand_eps:
                continue
            cand_eps = sort_episodes(cand_eps)
            if cand_eps:
                self._play_series_browse_episode(history_item, candidate, cand_eps[0])
                return

        # No more episodes anywhere
        if options:
            if season is None:
                self._open_browse(history_item)
            else:
                self._show_finished_sheet(history_item)
        else:
            self._show_snackbar("No more episodes found.")

    def _show_finished_sheet(self, history_item):
        self._close_bottom_sheets()

        def browse(e):
            self._open_browse(history_item)

        def replay(e):
            self._resume_history(history_item, True)

        def close(e):
            self._close_bottom_sheets()

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Series finished",
                            size=24,
                            color=T.TEXT,
                            style=ft.TextStyle(letter_spacing=2),
                        ),
                        ft.Text(
                            history_item.get("title_en", ""),
                            size=14,
                            weight="700",
                            color=T.TEXT,
                        ),
                        ft.Text(
                            "No next episode is available in this source.",
                            size=12,
                            color=T.DIM,
                        ),
                        ft.Row(
                            [
                                ft.FilledButton(
                                    "Browse episodes",
                                    icon=ft.Icons.VIDEO_LIBRARY_OUTLINED,
                                    on_click=browse,
                                    style=ft.ButtonStyle(
                                        color=T.BG,
                                        bgcolor=T.ACCENT,
                                    ),
                                ),
                                ft.OutlinedButton(
                                    "Replay last",
                                    icon=ft.Icons.REPLAY_ROUNDED,
                                    on_click=replay,
                                    style=ft.ButtonStyle(
                                        color=T.TEXT,
                                    ),
                                ),
                                ft.TextButton(
                                    "Close",
                                    on_click=close,
                                ),
                            ],
                            spacing=8,
                        ),
                    ],
                    tight=True,
                    spacing=10,
                ),
                padding=24,
                bgcolor=T.SURFACE,
                border_radius=ft.BorderRadius(18, 18, 0, 0),
            ),
        )
        self.page.overlay.append(sheet)
        sheet.open = True
        self.page.update()

    def _resume_history(self, history_item, start_zero=False):
        self._close_bottom_sheets()
        url = history_item.get("last_episode_url", "")
        if not url:
            self._show_snackbar("No playable item found.")
            return

        start_time = 0.0 if start_zero else self.db.get_progress(url)
        folder_url = history_item.get("last_folder_url", "")

        self.db.save_history(history_item)

        threading.Thread(
            target=self.player.play,
            args=(url, start_time, folder_url or None),
            daemon=True,
        ).start()

        title = history_item.get("title_en", "")
        self._show_snackbar(f"Playing: {title}")
        self._refresh_ui("")

    def _open_browse(self, history_item):
        media_type = history_item.get("media_type", "movie")
        options = self._get_catalog_options(
            history_item.get("imdb_id", ""),
            media_type,
        )

        if not options:
            if media_type == "series":
                self._show_current_folder_episodes(history_item)
            else:
                self._resume_history(history_item)
            return

        if media_type == "movie":
            self._open_movie_browse(history_item, options)
        else:
            self._open_series_browse(history_item, options)

    def _open_movie_browse(self, history_item, options):
        controls = []
        pairs = []
        current_url = history_item.get("last_episode_url", "")

        for opt in options:
            is_current = opt.get("url") == current_url
            title_weight = "700" if is_current else "400"
            title_color = T.ACCENT_2 if is_current else T.TEXT
            if is_current:
                leading_icon = ft.Icons.PLAY_ARROW_ROUNDED
            else:
                leading_icon = ft.Icons.MOVIE_OUTLINED

            base = ""
            subtitle = ft.Text(
                "",
                size=10,
                color=T.DIM,
                visible=False,
            )

            if is_current:
                base = "Current"
                subtitle.value = base
                subtitle.visible = True

            controls.append(
                ft.ListTile(
                    title=ft.Text(
                        opt.get("label", ""),
                        size=14,
                        weight=title_weight,
                        color=title_color,
                    ),
                    subtitle=subtitle,
                    leading=ft.Icon(
                        leading_icon,
                        color=T.ACCENT_2 if is_current else T.DIM,
                        size=20,
                    ),
                    on_click=lambda e, o=opt: (
                        self._play_movie_browse_option(
                            history_item,
                            o,
                        )
                    ),
                )
            )
            pairs.append((opt, subtitle, base))

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Quality options",
                            size=24,
                            color=T.TEXT,
                            style=ft.TextStyle(letter_spacing=2),
                        ),
                        ft.Text(
                            history_item.get("title_en", ""),
                            size=14,
                            weight="700",
                            color=T.TEXT,
                        ),
                        ft.ListView(
                            controls=controls,
                            expand=True,
                        ),
                    ],
                    spacing=10,
                    expand=True,
                ),
                padding=24,
                width=self._sheet_width(800),
                height=self._sheet_height(480),
                bgcolor=T.SURFACE,
                alignment=ft.Alignment(0, 0),
                border_radius=ft.BorderRadius(18, 18, 0, 0),
            ),
        )

        self.page.overlay.append(sheet)
        sheet.open = True
        self.page.update()

        threading.Thread(
            target=self._enrich_sizes,
            args=(pairs,),
            daemon=True,
        ).start()

    def _play_movie_browse_option(self, history_item, option):
        self._close_bottom_sheets()
        url = option.get("url", "")
        if not url:
            self._show_snackbar("No stream URL found.")
            return

        start_time = self._get_start_time(history_item, url)

        self.db.save_history(
            {
                "imdb_id": history_item.get("imdb_id"),
                "media_type": "movie",
                "title_en": history_item.get("title_en"),
                "year": history_item.get("year"),
                "last_option_label": option.get("label", ""),
                "last_folder_url": "",
                "last_episode_url": url,
                "last_episode_filename": "",
            }
        )

        threading.Thread(
            target=self.player.play,
            args=(url, start_time),
            daemon=True,
        ).start()

        title = history_item.get("title_en", "")
        self._show_snackbar(f"Playing: {title}")
        self._refresh_ui("")

    def _open_series_browse(self, history_item, options):
        label_to_option = {}
        option_order = []

        for opt in options:
            if opt.get("kind") != "folder":
                continue
            label = opt.get("label", "")
            if not label:
                continue
            if label not in label_to_option:
                label_to_option[label] = opt
                option_order.append(label)

        if not option_order:
            self._show_current_folder_episodes(history_item)
            return

        current_label = history_item.get("last_option_label", "")
        if current_label not in label_to_option:
            current_option = self._get_current_option(
                history_item,
                options,
            )
            if current_option:
                current_label = current_option.get("label", "")

        if current_label not in label_to_option:
            current_label = option_order[0]

        episode_list = ft.ListView(expand=True, spacing=0)

        progress = ft.ProgressBar(
            color=T.ACCENT,
            bgcolor=T.SURFACE_2,
            visible=False,
        )

        selector_label = ft.Text(
            current_label,
            size=14,
            weight="600",
            color=T.TEXT,
            expand=True,
            overflow="ellipsis",
        )

        menu_dialog = None
        menu_list = ft.ListView(spacing=0, expand=True)
        menu_box = ft.Container(
            content=menu_list,
            width=430,
        )

        def set_message(message):
            episode_list.controls = [
                ft.Container(
                    content=ft.Text(
                        message,
                        size=12,
                        color=T.DIM,
                    ),
                    padding=16,
                )
            ]
            progress.visible = False
            self.page.update()

        def load_option(option):
            progress.visible = True
            episode_list.controls = [
                ft.Container(
                    content=ft.Text(
                        "Loading episodes...",
                        size=12,
                        color=T.DIM,
                    ),
                    padding=16,
                )
            ]
            self.page.update()

            episodes = fetch_episodes_from_folder(option.get("url", ""))
            if episodes is None:
                set_message("Failed to load episodes.")
                return
            if not episodes:
                set_message("No episodes found.")
                return

            episodes = sort_episodes(episodes)
            current_url = history_item.get("last_episode_url", "")

            controls = []
            for ep in episodes:
                is_current = ep.get("url") == current_url
                controls.append(
                    self._episode_list_tile(
                        history_item,
                        option,
                        ep,
                        is_current,
                    )
                )

            episode_list.controls = controls
            progress.visible = False
            self.page.update()

        def close_option_menu():
            nonlocal menu_dialog
            if menu_dialog is None:
                return
            menu_dialog.open = False
            try:
                self.page.close(menu_dialog)
            except Exception:
                pass
            self.page.update()

        def choose_option(label):
            close_option_menu()
            selector_label.value = label
            self.page.update()
            option = label_to_option.get(label)
            if option:
                self.page.run_thread(load_option, option)

        def open_option_menu(e):
            nonlocal menu_dialog
            items = []
            for label in option_order:
                is_active = label == selector_label.value
                if is_active:
                    leading_icon = ft.Icons.CHECK_ROUNDED
                    title_color = T.ACCENT_2
                    title_weight = "700"
                else:
                    leading_icon = ft.Icons.MOVIE_OUTLINED
                    title_color = T.TEXT
                    title_weight = "400"
                items.append(
                    ft.ListTile(
                        title=ft.Text(
                            label,
                            size=13,
                            weight=title_weight,
                            color=title_color,
                        ),
                        leading=ft.Icon(
                            leading_icon,
                            color=(
                                T.ACCENT_2 if is_active else T.DIM
                            ),
                            size=18,
                        ),
                        on_click=lambda ev, lbl=label: (
                            choose_option(lbl)
                        ),
                    )
                )

            if menu_dialog is None:
                menu_dialog = ft.AlertDialog(
                    title=ft.Text(
                        "Season / Quality",
                        size=16,
                        weight="700",
                        color=T.TEXT,
                    ),
                    content=menu_box,
                    bgcolor=T.SURFACE,
                )
                self.page.overlay.append(menu_dialog)

            menu_list.controls = items
            menu_box.height = min(
                380,
                max(140, len(items) * 54),
            )
            menu_dialog.open = True
            self.page.update()

        selector = ft.Container(
            content=ft.Row(
                [
                    selector_label,
                    ft.Icon(
                        ft.Icons.ARROW_DROP_DOWN_ROUNDED,
                        color=T.DIM,
                        size=22,
                    ),
                ],
                spacing=8,
                vertical_alignment="center",
            ),
            padding=ft.Padding(
                left=16,
                top=13,
                right=12,
                bottom=13,
            ),
            border=ft.Border.all(1, T.BORDER_LIGHT),
            border_radius=10,
            bgcolor=T.SURFACE_2,
            ink=True,
            on_click=open_option_menu,
        )

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            history_item.get("title_en", ""),
                            size=24,
                            color=T.TEXT,
                            style=ft.TextStyle(letter_spacing=2),
                        ),
                        selector,
                        progress,
                        episode_list,
                    ],
                    spacing=12,
                    expand=True,
                ),
                padding=24,
                width=self._sheet_width(800),
                height=self._sheet_height(560),
                bgcolor=T.SURFACE,
                alignment=ft.Alignment(0, 0),
                border_radius=ft.BorderRadius(18, 18, 0, 0),
            ),
        )

        self.page.overlay.append(sheet)
        sheet.open = True
        self.page.update()

        initial_option = label_to_option.get(current_label)
        if initial_option:
            self.page.run_thread(load_option, initial_option)

    def _episode_list_tile(
        self,
        history_item,
        option,
        episode,
        is_current,
    ):
        url = episode.get("url", "")
        prog = self.db.get_progress(url)
        parts = []

        size = episode.get("size", "")
        if size:
            parts.append(size)

        if prog > 0:
            parts.append(format_time(prog))

        subtitle = None
        if parts:
            subtitle = ft.Text(
                " • ".join(parts),
                size=10,
                color=T.DIM,
            )

        title_weight = "700" if is_current else "400"
        title_color = T.ACCENT_2 if is_current else T.TEXT
        if is_current:
            leading_icon = ft.Icons.PLAY_ARROW_ROUNDED
        else:
            leading_icon = ft.Icons.MOVIE_OUTLINED

        return ft.ListTile(
            title=ft.Text(
                episode.get("filename", ""),
                size=13,
                weight=title_weight,
                color=title_color,
            ),
            subtitle=subtitle,
            leading=ft.Icon(
                leading_icon,
                color=T.ACCENT_2 if is_current else T.DIM,
                size=20,
            ),
            on_click=lambda e: self._play_series_browse_episode(
                history_item,
                option,
                episode,
            ),
        )

    def _play_series_browse_episode(
        self,
        history_item,
        option,
        episode,
    ):
        self._close_bottom_sheets()
        url = episode.get("url", "")
        filename = episode.get("filename", "")
        if not url:
            self._show_snackbar("No episode URL found.")
            return

        start_time = self._get_start_time(
            history_item,
            url,
            filename,
        )

        folder_url = option.get("url", "")
        if not folder_url:
            folder_url = history_item.get("last_folder_url", "")

        self.db.save_history(
            {
                "imdb_id": history_item.get("imdb_id"),
                "media_type": "series",
                "title_en": history_item.get("title_en"),
                "year": history_item.get("year"),
                "last_option_label": option.get("label", ""),
                "last_folder_url": folder_url,
                "last_episode_url": url,
                "last_episode_filename": filename,
            }
        )

        threading.Thread(
            target=self.player.play,
            args=(url, start_time, folder_url or None),
            daemon=True,
        ).start()

        self._show_snackbar(f"Playing: {filename}")
        self._refresh_ui("")

    def _show_current_folder_episodes(self, history_item):
        folder_url = history_item.get("last_folder_url", "")
        if not folder_url:
            self._show_snackbar("No folder URL found.")
            return

        self._show_snackbar("Loading episodes...")
        threading.Thread(
            target=self._load_current_folder_episodes,
            args=(history_item,),
            daemon=True,
        ).start()

    def _load_current_folder_episodes(self, history_item):
        folder_url = history_item.get("last_folder_url", "")
        episodes = fetch_episodes_from_folder(folder_url)

        if episodes is None:
            self._show_snackbar("Failed to load episodes.")
            return

        if not episodes:
            self._show_snackbar("No episodes found.")
            return

        episodes = sort_episodes(episodes)

        option = {
            "kind": "folder",
            "label": history_item.get("last_option_label", ""),
            "url": folder_url,
            "season": None,
            "quality": "",
        }

        current_url = history_item.get("last_episode_url", "")
        controls = []
        for ep in episodes:
            is_current = ep.get("url") == current_url
            controls.append(
                self._episode_list_tile(
                    history_item,
                    option,
                    ep,
                    is_current,
                )
            )

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Episodes",
                            size=24,
                            color=T.TEXT,
                            style=ft.TextStyle(letter_spacing=2),
                        ),
                        ft.Text(
                            history_item.get("title_en", ""),
                            size=14,
                            weight="700",
                            color=T.TEXT,
                        ),
                        ft.ListView(
                            controls=controls,
                            expand=True,
                        ),
                    ],
                    tight=True,
                    spacing=6,
                ),
                padding=24,
                width=self._sheet_width(800),
                height=self._sheet_height(560),
                bgcolor=T.SURFACE,
                alignment=ft.Alignment(0, 0),
                border_radius=ft.BorderRadius(18, 18, 0, 0),
            ),
        )

        self.page.overlay.append(sheet)
        sheet.open = True
        self.page.update()

    def _open_media_details(self, media, is_history=False):
        logger.info(
            "open media details: imdb=%s title=%s is_history=%s",
            media.get("imdb_id"),
            media.get("title_en"),
            is_history,
        )

        if is_history:
            if media.get("last_episode_url"):
                self._resume_history(media)
            else:
                self._open_browse(media)
            return

        try:
            links = json.loads(media.get("links", "{}"))
        except Exception:
            links = {}

        media_type = media.get("type", media.get("media_type"))
        options = normalize_links(media_type, links)

        logger.info(
            "media options: title=%s count=%s",
            media.get("title_en"),
            len(options),
        )

        if not options:
            self._show_snackbar("No streams available.")
            return

        list_controls = []
        pairs = []

        for opt in options:
            is_folder = opt.get("kind") == "folder"
            if is_folder:
                leading_icon = ft.Icons.FOLDER_OUTLINED
            else:
                leading_icon = ft.Icons.PLAY_ARROW

            subtitle = ft.Text(
                "",
                size=10,
                color=T.DIM,
                visible=False,
            )

            list_controls.append(
                ft.ListTile(
                    title=ft.Text(
                        opt.get("label", ""),
                        size=14,
                        color=T.TEXT,
                    ),
                    subtitle=subtitle,
                    leading=ft.Icon(
                        leading_icon,
                        color=T.ACCENT_2,
                        size=20,
                    ),
                    on_click=lambda e, o=opt: self._play_media(
                        media,
                        o,
                    ),
                )
            )

            if not is_folder:
                pairs.append((opt, subtitle, ""))

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            media.get("title_en"),
                            size=26,
                            color=T.TEXT,
                            style=ft.TextStyle(letter_spacing=2),
                        ),
                        ft.ListView(
                            controls=list_controls,
                            expand=True,
                        ),
                    ],
                    tight=True,
                ),
                padding=24,
                width=self._sheet_width(800),
                height=self._sheet_height(420),
                bgcolor=T.SURFACE,
                alignment=ft.Alignment(0, 0),
                border_radius=ft.BorderRadius(18, 18, 0, 0),
            ),
        )

        self.page.overlay.append(sheet)
        sheet.open = True
        self.page.update()

        if pairs:
            threading.Thread(
                target=self._enrich_sizes,
                args=(pairs,),
                daemon=True,
            ).start()

    def _play_media(self, media: dict, option: dict):
        self._close_bottom_sheets()
        target_url = option.get("url", "")

        logger.info(
            "play media: title=%s label=%s is_folder=%s",
            media.get("title_en"),
            option.get("label"),
            option.get("kind") == "folder",
        )

        if option.get("kind") == "folder":
            self._show_episode_picker(media, option)
            return

        start_time = self.db.get_progress(target_url)
        logger.info(
            "playing direct file: start=%s url=%s",
            start_time,
            target_url,
        )

        self.db.save_history(
            {
                "imdb_id": media.get("imdb_id"),
                "media_type": media.get(
                    "type",
                    media.get("media_type"),
                ),
                "title_en": media.get("title_en"),
                "year": media.get("year"),
                "last_option_label": option.get("label", ""),
                "last_folder_url": "",
                "last_episode_url": target_url,
                "last_episode_filename": "",
            }
        )

        threading.Thread(
            target=self.player.play,
            args=(target_url, start_time),
            daemon=True,
        ).start()

        title = media.get("title_en", "")
        self._show_snackbar(f"Playing: {title}")

    def _show_episode_picker(self, media: dict, option: dict):
        logger.info(
            "episode picker: title=%s label=%s",
            media.get("title_en"),
            option.get("label"),
        )
        self._show_snackbar("Loading episodes...")
        threading.Thread(
            target=self._load_episode_picker,
            args=(media, option),
            daemon=True,
        ).start()

    def _load_episode_picker(self, media: dict, option: dict):
        episodes = fetch_episodes_from_folder(option.get("url", ""))

        if episodes is None:
            self._show_snackbar("Failed to load episodes.")
            return

        if not episodes:
            self._show_snackbar("No episodes found.")
            return

        episodes = sort_episodes(episodes)
        logger.info(
            "episode picker loaded: %s episodes for %s",
            len(episodes),
            option.get("label"),
        )

        ep_controls = []
        for ep in episodes:
            size = ep.get("size", "")
            subtitle = None
            if size:
                subtitle = ft.Text(
                    size,
                    size=10,
                    color=T.DIM,
                )
            ep_controls.append(
                ft.ListTile(
                    title=ft.Text(
                        ep["filename"],
                        size=13,
                        color=T.TEXT,
                    ),
                    subtitle=subtitle,
                    leading=ft.Icon(
                        ft.Icons.MOVIE_OUTLINED,
                        color=T.DIM,
                        size=20,
                    ),
                    on_click=lambda e, ep=ep: self._play_episode(
                        media,
                        option,
                        ep,
                    ),
                )
            )

        sheet = ft.BottomSheet(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            option.get("label", ""),
                            size=24,
                            color=T.TEXT,
                            style=ft.TextStyle(letter_spacing=2),
                        ),
                        ft.ListView(
                            controls=ep_controls,
                            expand=True,
                        ),
                    ],
                    tight=True,
                    spacing=6,
                ),
                padding=24,
                width=self._sheet_width(800),
                height=self._sheet_height(560),
                bgcolor=T.SURFACE,
                alignment=ft.Alignment(0, 0),
                border_radius=ft.BorderRadius(18, 18, 0, 0),
            ),
        )

        self.page.overlay.append(sheet)
        sheet.open = True
        self.page.update()

    def _play_episode(
        self,
        media: dict,
        option: dict,
        episode: dict,
    ):
        self._close_bottom_sheets()

        start_time = self._get_start_time(
            media,
            episode["url"],
            episode["filename"],
        )

        folder_url = option.get("url", "")

        logger.info(
            "playing episode: title=%s file=%s start=%s referer=%s",
            media.get("title_en"),
            episode["filename"],
            start_time,
            folder_url,
        )

        self.db.save_history(
            {
                "imdb_id": media.get("imdb_id"),
                "media_type": "series",
                "title_en": media.get("title_en"),
                "year": media.get("year"),
                "last_option_label": option.get("label", ""),
                "last_folder_url": folder_url,
                "last_episode_url": episode["url"],
                "last_episode_filename": episode["filename"],
            }
        )

        threading.Thread(
            target=self.player.play,
            args=(episode["url"], start_time, folder_url or None),
            daemon=True,
        ).start()

        self._show_snackbar(f"Playing: {episode['filename']}")


def main(page: ft.Page):
    MediaHubApp(page)