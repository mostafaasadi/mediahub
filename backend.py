import json
import logging
import sqlite3
import subprocess
import sys
import re

import chompjs
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

try:
    import mpv
    HAS_PYTHON_MPV = True
except ImportError:
    mpv = None
    HAS_PYTHON_MPV = False

logger = logging.getLogger("media_hub")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if not logger.handlers:
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setLevel(logging.INFO)
    _sh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(_sh)

DEFAULT_JS_URL = (
    "https://dls6.aparatchi-dlcenter.top/DonyayeSerial/10_thous.js"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DB_NAME = "media_hub.db"

SIZE_PATTERN = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:[KMGT]i?B|[KMGT])\b"
)


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


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS movies (
                imdb_id TEXT PRIMARY KEY,
                type TEXT,
                title_en TEXT,
                title_fa TEXT,
                year INTEGER,
                rating REAL,
                links TEXT
            );

            CREATE TABLE IF NOT EXISTS history (
                imdb_id TEXT PRIMARY KEY,
                media_type TEXT,
                title_en TEXT,
                year INTEGER,
                last_option_label TEXT,
                last_folder_url TEXT,
                last_episode_url TEXT,
                last_episode_filename TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS watch_progress (
                media_url TEXT PRIMARY KEY,
                time_pos REAL,
                duration REAL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS poster_cache (
                imdb_id TEXT PRIMARY KEY,
                poster_url TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS series_latest_episode (
                folder_url TEXT PRIMARY KEY,
                latest_episode_filename TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS omdb_season_cache (
                imdb_id TEXT PRIMARY KEY,
                total_seasons INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()

        try:
            cursor.execute(
                "ALTER TABLE watch_progress ADD COLUMN duration REAL"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass

        self._ensure_indexes(conn)

        cursor.execute(
            "SELECT value FROM settings WHERE key = 'js_url'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO settings (key, value) VALUES ('js_url', ?)",
                (DEFAULT_JS_URL,),
            )

        cursor.execute(
            "SELECT value FROM settings WHERE key = 'omdb_api_key'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO settings (key, value) "
                "VALUES ('omdb_api_key', '')"
            )

        conn.commit()
        conn.close()

    def _ensure_indexes(self, conn: sqlite3.Connection):
        cursor = conn.cursor()
        indexes = (
            """
            CREATE INDEX IF NOT EXISTS idx_movies_title_en
            ON movies(title_en)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_movies_title_fa
            ON movies(title_fa)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_movies_type
            ON movies(type)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_history_timestamp
            ON history(timestamp DESC)
            """,
        )
        for sql in indexes:
            cursor.execute(sql)
        conn.commit()

    def get_js_url(self) -> str:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM settings WHERE key = 'js_url'"
        )
        row = cursor.fetchone()
        conn.close()
        return row["value"] if row else DEFAULT_JS_URL

    def update_js_url(self, new_url: str) -> bool:
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) "
                "VALUES ('js_url', ?)",
                (new_url.strip(),),
            )
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    def get_omdb_api_key(self) -> str:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT value FROM settings WHERE key = 'omdb_api_key'"
        )
        row = cursor.fetchone()
        conn.close()
        return row["value"] if row else ""

    def update_omdb_api_key(self, new_key: str) -> bool:
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) "
                "VALUES ('omdb_api_key', ?)",
                (new_key.strip(),),
            )
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    def get_cached_poster(self, imdb_id: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT poster_url FROM poster_cache WHERE imdb_id = ?",
            (imdb_id,),
        )
        row = cursor.fetchone()
        conn.close()
        return row["poster_url"] if row else None

    def save_poster_cache(self, imdb_id: str, poster_url: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO poster_cache "
            "(imdb_id, poster_url) VALUES (?, ?)",
            (imdb_id, poster_url),
        )
        conn.commit()
        conn.close()

    def get_cached_latest_episode(self, folder_url: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT latest_episode_filename FROM series_latest_episode "
            "WHERE folder_url = ?",
            (folder_url,),
        )
        row = cursor.fetchone()
        conn.close()
        return row["latest_episode_filename"] if row else None

    def save_latest_episode(self, folder_url: str, filename: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO series_latest_episode "
            "(folder_url, latest_episode_filename) VALUES (?, ?)",
            (folder_url, filename),
        )
        conn.commit()
        conn.close()

    def get_cached_total_seasons(self, imdb_id: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT total_seasons FROM omdb_season_cache WHERE imdb_id = ?",
            (imdb_id,),
        )
        row = cursor.fetchone()
        conn.close()
        return row["total_seasons"] if row else None

    def save_total_seasons(self, imdb_id: str, total_seasons: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO omdb_season_cache "
            "(imdb_id, total_seasons) VALUES (?, ?)",
            (imdb_id, total_seasons),
        )
        conn.commit()
        conn.close()

    def sync_catalog(self, js_url: str = None) -> bool:
        if js_url is None:
            js_url = self.get_js_url()

        session = self._get_robust_session()

        try:
            response = session.get(js_url, timeout=45)
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("sync_catalog: request failed")
            return False

        content = response.text

        if "let data = " in content:
            raw_js_data = content.split("let data = ", 1)[1].strip()
        else:
            raw_js_data = content.strip()

        if raw_js_data.endswith(";"):
            raw_js_data = raw_js_data[:-1]

        try:
            data = chompjs.parse_js_object(raw_js_data)
        except Exception:
            logger.exception("sync_catalog: parse_js_object failed")
            return False

        rows = []
        for item in data:
            if not isinstance(item, list) or len(item) < 8:
                continue
            if item[1] not in ("movie", "series"):
                continue

            rows.append(
                (
                    item[0],
                    item[1],
                    item[2],
                    item[3],
                    item[4],
                    item[5],
                    json.dumps(item[7], ensure_ascii=False),
                )
            )

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM movies")
        cursor.executemany(
            "INSERT OR REPLACE INTO movies VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        logger.info(
            "sync_catalog: %s items imported",
            len(rows),
        )

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT imdb_id, last_folder_url FROM history "
            "WHERE media_type = 'series' AND last_folder_url IS NOT NULL AND last_folder_url != ''"
        )
        history_series = cursor.fetchall()
        conn.close()

        for row in history_series:
            folder_url = row["last_folder_url"]
            episodes = fetch_episodes_from_folder(folder_url)
            if episodes is None:
                continue

            sorted_eps = sort_episodes(episodes)
            if sorted_eps:
                latest = ""

                for ep in reversed(sorted_eps):
                    filename = ep.get("filename", "")
                    if parse_season_episode_tuple(filename):
                        latest = filename
                        break

                if not latest:
                    latest = sorted_eps[-1].get("filename", "")

                if latest:
                    self.save_latest_episode(folder_url, latest)

        return True

    def search(self, query: str) -> list:
        conn = self._get_conn()
        cursor = conn.cursor()
        search_query = f"%{query.lower()}%"
        cursor.execute(
            "SELECT * FROM movies "
            "WHERE LOWER(title_en) LIKE ? OR LOWER(title_fa) LIKE ? "
            "LIMIT 50",
            (search_query, search_query),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_movie(self, imdb_id: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM movies WHERE imdb_id = ?",
            (imdb_id,),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_continue_watching(self) -> list:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM history "
            "ORDER BY timestamp DESC LIMIT 10"
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def save_history(self, history_data: dict):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO history "
            "(imdb_id, media_type, title_en, year, "
            "last_option_label, last_folder_url, last_episode_url, "
            "last_episode_filename, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                history_data.get("imdb_id"),
                history_data.get("media_type"),
                history_data.get("title_en"),
                history_data.get("year"),
                history_data.get("last_option_label"),
                history_data.get("last_folder_url"),
                history_data.get("last_episode_url"),
                history_data.get("last_episode_filename"),
            ),
        )
        conn.commit()
        conn.close()

    def save_progress(
        self,
        url: str,
        time_pos: float,
        duration: float = None,
    ):
        conn = self._get_conn()
        cursor = conn.cursor()

        if duration is not None and duration > 0:
            cursor.execute(
                "INSERT OR REPLACE INTO watch_progress "
                "(media_url, time_pos, duration) VALUES (?, ?, ?)",
                (url, time_pos, duration),
            )
        else:
            cursor.execute(
                "SELECT duration FROM watch_progress WHERE media_url = ?",
                (url,),
            )
            row = cursor.fetchone()
            existing_dur = row["duration"] if row else None

            cursor.execute(
                "INSERT OR REPLACE INTO watch_progress "
                "(media_url, time_pos, duration) VALUES (?, ?, ?)",
                (url, time_pos, existing_dur),
            )

        conn.commit()
        conn.close()

    def get_progress(self, url: str) -> float:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT time_pos FROM watch_progress WHERE media_url = ?",
            (url,),
        )
        row = cursor.fetchone()
        conn.close()
        return row["time_pos"] if row else 0.0

    def get_duration(self, url: str) -> float:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT duration FROM watch_progress WHERE media_url = ?",
            (url,),
        )
        row = cursor.fetchone()
        conn.close()
        return row["duration"] if row and row["duration"] else 0.0

    @staticmethod
    def _get_robust_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
            }
        )

        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session


class PosterManager:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.session = self._get_fast_session()
        self.api_key = self.db.get_omdb_api_key()
        self.failed = set()
        self.season_failed = set()

    def reload_api_key(self):
        self.api_key = self.db.get_omdb_api_key()
        self.failed.clear()
        self.season_failed.clear()

    def get_poster_url(self, imdb_id: str) -> str:
        if not imdb_id or imdb_id in self.failed:
            return ""

        cached = self.db.get_cached_poster(imdb_id)
        if cached is not None:
            return "" if cached == "N/A" else cached

        if not self.api_key:
            return ""

        try:
            response = self.session.get(
                "https://www.omdbapi.com/",
                params={"i": imdb_id, "apikey": self.api_key},
                timeout=3,
            )
            response.raise_for_status()
            data = response.json()

            poster_url = data.get("Poster", "")

            if data.get("Response") == "True" and poster_url:
                if poster_url != "N/A":
                    self.db.save_poster_cache(imdb_id, poster_url)
                    return poster_url

            self.failed.add(imdb_id)
        except Exception:
            self.failed.add(imdb_id)

        return ""

    def get_total_seasons(self, imdb_id: str):
        if not imdb_id or imdb_id in self.season_failed:
            return None

        cached = self.db.get_cached_total_seasons(imdb_id)
        if cached is not None:
            if cached > 0:
                return cached
            return None

        if not self.api_key:
            return None

        try:
            response = self.session.get(
                "https://www.omdbapi.com/",
                params={"i": imdb_id, "apikey": self.api_key},
                timeout=3,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("Response") == "True":
                raw = data.get("totalSeasons", "")

                if raw and raw != "N/A":
                    try:
                        total = int(str(raw).strip())
                    except Exception:
                        total = 0

                    if total > 0:
                        self.db.save_total_seasons(imdb_id, total)
                        return total

                self.db.save_total_seasons(imdb_id, 0)
                return None

            self.season_failed.add(imdb_id)
        except Exception:
            self.season_failed.add(imdb_id)

        return None

    @staticmethod
    def _get_fast_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
            }
        )

        retries = Retry(
            total=1,
            backoff_factor=0.2,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session


class PlayerManager:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.current_time_pos = 0.0
        self.current_duration = 0.0
        self.on_error = None

    def play(
        self,
        url: str,
        start_time: float = 0.0,
        referer: str = None,
    ):
        try:
            if HAS_PYTHON_MPV:
                self._play_with_python_mpv(url, start_time, referer)
            else:
                self._play_with_subprocess(url, start_time, referer)
        except Exception as exc:
            logger.exception("PlayerManager.play failed")
            message = f"Playback failed: {exc}"
            if self.on_error:
                self.on_error(message)

    def _play_with_python_mpv(
        self,
        url: str,
        start_time: float,
        referer: str = None,
    ):
        self.current_time_pos = 0.0
        self.current_duration = 0.0

        if referer is None:
            referer = url.rsplit("/", 1)[0] + "/"

        kwargs = {
            "input_default_bindings": True,
            "osc": True,
            "user_agent": USER_AGENT,
            "http_header_fields": [f"Referer: {referer}"],
        }

        if start_time > 0:
            kwargs["start"] = str(start_time)

        player = mpv.MPV(**kwargs)

        @player.property_observer("time-pos")
        def _time_observer(_name, value):
            if value is not None:
                self.current_time_pos = value

        @player.property_observer("duration")
        def _dur_observer(_name, value):
            if value is not None:
                self.current_duration = value

        player.play(url)
        player.wait_for_playback()

        if self.current_duration <= 0:
            logger.warning(
                "python-mpv duration is 0; stream may have failed to open"
            )

        if self.current_time_pos > 5:
            self.db.save_progress(
                url,
                self.current_time_pos,
                self.current_duration,
            )

        player.terminate()

    def _play_with_subprocess(
        self,
        url: str,
        start_time: float,
        referer: str = None,
    ):
        if referer is None:
            referer = url.rsplit("/", 1)[0] + "/"

        cmd = [
            "mpv",
            "--save-position-on-quit",
            "--osc=yes",
            f"--user-agent={USER_AGENT}",
            f"--referer={referer}",
        ]

        if start_time > 0:
            cmd.append(f"--start={start_time}")

        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()
            detail = tail[-1] if tail else "unknown mpv error"
            raise RuntimeError(detail)


def format_time(seconds: float) -> str:
    if seconds <= 0:
        return "0:00"

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


def fetch_episodes_from_folder(folder_url: str):
    try:
        base = folder_url.rstrip("/") + "/"
        logger.info("fetching episodes from: %s", base)

        session = DatabaseManager._get_robust_session()
        res = session.get(base, timeout=15)

        logger.debug(
            "fetch episodes: HTTP %s content_len=%s",
            res.status_code,
            len(res.text),
        )

        res.raise_for_status()

        soup = BeautifulSoup(res.text, "html.parser")
        episodes = []

        for a in soup.find_all("a"):
            href = a.get("href", "")

            if not href.lower().endswith((".mkv", ".mp4", ".avi")):
                continue

            if href.startswith("http"):
                url = href
            else:
                url = base + href.lstrip("/")

            filename = a.text.strip()

            episodes.append(
                {
                    "filename": filename,
                    "url": url,
                    "size": _extract_size(a),
                }
            )

        logger.info("found %s episode links", len(episodes))
        return episodes
    except Exception:
        logger.exception("fetch episodes failed: %s", folder_url)
        return None


def _extract_size(a_tag):
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


def sort_episodes(episodes):
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
