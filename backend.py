import json
import logging
import sqlite3
import subprocess
import sys

import chompjs
import requests
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
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        )
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

    def reload_api_key(self):
        self.api_key = self.db.get_omdb_api_key()
        self.failed.clear()

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
