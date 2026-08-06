"""
Doraemon Search Bot — Improved Edition v4.1
A Discord bot for searching Doraemon manga stories, episodes, and characters.

Architecture: Class-based DatabaseManager with abstract data sources, locking,
caching, rate limiting, structured logging, search indexing, fuzzy search,
word-boundary character matching, smart dedup, backwards-compatible CSV parsing,
interactive pagination with Discord UI buttons, select menus, autocomplete,
cross-source search, SQLite cache persistence, and much more.

Improvements in v4.1 (from review):
- Signal handler rewritten to use flag + loop-scheduled cleanup (no async-in-signal)
- 
SQLite connections now thread-locked with check_same_thread=False
- Pagination custom_ids made unique per-channel-per-message
- Embed total length calculation includes title + description + fields
- Background task cancelled on graceful shutdown
- MetricsCollector now thread-safe with threading.Lock
- All datetime.now() replaced with datetime.now(timezone.utc)
- File hash failures now logged as warnings
- CSV injection prevention expanded (pipe character)
- Cross-source search deduplicates results
- View timeouts pulled from config
- SQLite transactions wrapped with try/except/rollback
- Null safety improvements on .get() chains
- Levenshtein identical-string shortcut for performance
"""

import json
import os
import re
import csv
import asyncio
import logging
import logging.handlers
import time
import hashlib
import sys
import signal
import sqlite3
import random
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Callable, Set, Iterable
from enum import Enum

import discord
from discord import app_commands, Embed, Intents, ui, Interaction, ButtonStyle
from discord.ext import commands, tasks
from dotenv import load_dotenv

# External dependencies (optional)
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    print('WARNING: aiohttp not installed. Remote CSV fetching will be disabled.')
    print('Install with: pip install aiohttp')
    AIOHTTP_AVAILABLE = False


# ============================================
# CONSTANTS & MAGIC NUMBERS
# ============================================

ZERO_WIDTH_SPACE = '\u200b'
MAX_EMBED_FIELDS_DISPLAY = 22
SAFE_ADD_FIELD_BUFFER = 50
DEFAULT_PAGE_SIZE = 10
BOT_VERSION = '4.1'
CACHE_DB_FILENAME = 'search_cache.sqlite'
SHUTDOWN_TIMEOUT_SECONDS = 10
AUTO_REFRESH_INTERVAL_HOURS = 6
HEALTH_CHECK_CACHE_KEY = '__health__'

# Known magazine codes for validation
KNOWN_MAGAZINES = frozenset({'YK', 'KG', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'BK', 'SD', 'TK', 'CC', 'CD'})

# Column aliases for backwards compatibility
CSV_COLUMN_ALIASES: Dict[str, List[str]] = {
    'Magazine code':           ['Magazine code', 'Magazine Code', 'magazine_code', 'Magazine'],
    'Publication Date':         ['Publication Date', 'publication_date', 'Pub Date', 'Date'],
    'English title':           ['English title', 'English Title', 'english_title', 'Title'],
    'Japanese title':          ['Japanese title', 'Japanese Title', 'japanese_title', 'JP Title'],
    'English Kindle':          ['English Kindle', 'english_kindle', 'Kindle'],
    'Bilingual Print':         ['Bilingual Print', 'bilingual_print', 'Bilingual'],
    'Tankōbon':                ['Tankōbon', 'Tankobon', 'tankobon', 'tankōbon'],
    'Complete Collection':     ['Complete Collection', 'complete_collection', 'Complete'],
    '1979 anime':              ['1979 anime', '1979_anime', 'Anime 1979'],
    '1979 anime extra':        ['1979 anime extra', '1979_anime_extra', 'Anime 1979 Extra'],
    '2005 anime':              ['2005 anime', '2005_anime', 'Anime 2005'],
    '2005 anime extra 1':      ['2005 anime extra 1', '2005_anime_extra_1', 'Anime 2005 Extra 1'],
    '2005 anime extra 2':      ['2005 anime extra 2', '2005_anime_extra_2', 'Anime 2005 Extra 2'],
    'Movie':                   ['Movie', 'movie'],
    'Movie extra':             ['Movie extra', 'movie_extra'],
    'Short film':              ['Short film', 'short_film', 'Short Film'],
    'Notes':                   ['Notes', 'notes'],
}

CHAR_COLUMN_ALIASES: Dict[str, List[str]] = {
    'Character Name':      ['Character Name', 'character_name', 'Name', 'Char Name'],
    'Alternative Name':    ['Alternative Name', 'alternative_name', 'Alt Name', 'Alt'],
    'Category':            ['Category', 'category', 'Type', 'Cat'],
    'Description':         ['Description', 'description', 'Desc', 'Details'],
    'Source References':   ['Source References', 'source_references', 'Sources', 'Refs'],
}

CSV_FIELD_NAMES = [
    'Magazine code', 'Publication Date', 'English title', 'Japanese title',
    'English Kindle', 'Bilingual Print', 'Tankōbon', 'Complete Collection',
    '1979 anime', '1979 anime extra', '2005 anime', '2005 anime extra 1',
    '2005 anime extra 2', 'Movie', 'Movie extra', 'Short film', 'Notes'
]

CHAR_CSV_FIELD_NAMES = [
    'Character Name',
    'Alternative Name',
    'Category',
    'Description',
    'Source References',
]

VOLUME_COLUMNS = {
    'tankobon':  'Tankōbon',
    'complete':  'Complete Collection',
    'kindle':    'English Kindle',
    'bilingual': 'Bilingual Print',
}

# Character CSV constants
CHAR_CSV_REPO_OWNER = 'star884'
CHAR_CSV_REPO_NAME = 'Database-dorav2'
CHAR_CSV_BRANCH = 'main'
CHAR_CSV_FILENAME = 'All_Characters_database_fixed-1.csv'

CHAR_CSV_RAW_URL = (
    f'https://raw.githubusercontent.com/'
    f'{CHAR_CSV_REPO_OWNER}/{CHAR_CSV_REPO_NAME}/'
    f'{CHAR_CSV_BRANCH}/{CHAR_CSV_FILENAME}'
)


# ============================================
# ENUMS
# ============================================

class SourceType(Enum):
    JSON = 'json'
    CSV = 'csv'
    CSV_CHAR = 'csv_char'


# ============================================
# HELPER FUNCTIONS
# ============================================

def hex_to_int(hex_str: str) -> int:
    """Convert a hex color string (with or without 0x prefix) to integer."""
    if hex_str.startswith('0x') or hex_str.startswith('0X'):
        return int(hex_str, 16)
    try:
        return int(hex_str, 16)
    except ValueError:
        return int(hex_str)


def generate_correlation_id() -> str:
    """Generate a short unique correlation ID for request tracing."""
    return f"{int(time.time() * 1000)}-{random.randint(1000, 9999)}"


def make_progress_bar(current: int, total: int, width: int = 10) -> str:
    """Create a Unicode progress bar string."""
    if total <= 0:
        return '[' + '░' * width + ']'
    filled = int((current / total) * width)
    filled = min(filled, width)
    return '[' + '█' * filled + '░' * (width - filled) + ']'


def sanitize_csv_cell(value: str) -> str:
    """Prevent CSV injection by prefixing dangerous cell-starting characters.

    v4.1: Added pipe (|) to dangerous characters per CSV injection best practices.
    """
    DANGEROUS_PREFIXES = ('=', '+', '-', '@', '\t', '\r', '|')
    if value and value[0] in DANGEROUS_PREFIXES:
        return "'" + value
    return value


def is_admin(interaction: discord.Interaction) -> bool:
    """Check if the user has administrator permissions or is the bot owner."""
    # Bot owner whitelist
    if interaction.user.id == 1236713171142840403:
        return True
    # Fallback to Discord permissions
    if not interaction.guild:
        return False
    perms = interaction.app_permissions
    return perms.administrator if perms else False


def utc_now() -> datetime:
    """Return current UTC datetime. Centralized for consistency."""
    return datetime.now(timezone.utc)


# Scoring constants for character search tiers
SCORE_EXACT_NAME_BONUS = 50.0
SCORE_WORD_BOUNDARY_NAME = 100.0
SCORE_WORD_BOUNDARY_ALT = 80.0
SCORE_SUBSTRING_NAME = 60.0
SCORE_CATEGORY_MATCH = 40.0
SCORE_DESC_BASE = 20.0
SCORE_DESC_OVERLAY_WEIGHT = 10.0
SCORE_FUZZY_NAME_BASE = 15.0
SCORE_FUZZY_ALT_BASE = 12.0
SCORE_FUZZY_WEIGHT = 5.0


# ============================================
# CONFIGURATION MANAGEMENT
# ============================================

@dataclass
class BotConfig:
    """Central configuration loaded from environment or defaults."""

    page_size: int = int(os.getenv('PAGE_SIZE', str(DEFAULT_PAGE_SIZE)))
    max_pages: int = int(os.getenv('MAX_PAGES', '999'))
    search_cache_ttl: int = int(os.getenv('CACHE_TTL', '300'))
    cache_max_size: int = int(os.getenv('CACHE_MAX_SIZE', '1000'))

    rate_per_user: int = int(os.getenv('RATE_LIMIT_USER', '10'))
    rate_per_guild: int = int(os.getenv('RATE_LIMIT_GUILD', '100'))
    rate_window_seconds: int = int(os.getenv('RATE_WINDOW', '60'))

    http_timeout_seconds: int = int(os.getenv('HTTP_TIMEOUT', '30'))
    max_retries: int = int(os.getenv('MAX_RETRIES', '3'))
    retry_backoff_base: int = int(os.getenv('RETRY_BACKOFF', '2'))

    color_primary: int = hex_to_int(os.getenv('COLOR_PRIMARY', '0x6d4aff'))
    color_error: int = hex_to_int(os.getenv('COLOR_ERROR', '0xcc0000'))
    color_warning: int = hex_to_int(os.getenv('COLOR_WARNING', '0xffaa00'))
    color_success: int = hex_to_int(os.getenv('COLOR_SUCCESS', '0x00cc00'))

    embed_field_name_max: int = int(os.getenv('EMBED_FIELD_NAME_MAX', '256'))
    embed_field_value_max: int = int(os.getenv('EMBED_FIELD_VALUE_MAX', '1024'))
    embed_total_max: int = int(os.getenv('EMBED_TOTAL_MAX', '6000'))
    embed_field_max_count: int = int(os.getenv('EMBED_FIELD_MAX_COUNT', '25'))

    max_search_results: int = int(os.getenv('MAX_SEARCH_RESULTS', '10'))
    max_query_length: int = int(os.getenv('MAX_QUERY_LENGTH', '200'))
    fuzzy_threshold: float = float(os.getenv('FUZZY_THRESHOLD', '0.6'))

    log_level: str = os.getenv('LOG_LEVEL', 'INFO')
    log_file: str = os.getenv('LOG_FILE', 'bot.log')
    log_rotation_mb: int = int(os.getenv('LOG_ROTATION_MB', '100'))
    log_rotation_days: int = int(os.getenv('LOG_ROTATION_DAYS', '7'))
    log_json: bool = os.getenv('LOG_JSON', 'false').lower() == 'true'

    home_dir: str = os.getenv('HOME_DIR', os.path.expanduser("~"))
    json_db_path: str = os.getenv('JSON_DB_PATH', '')  # ← NEW

    # v4.1: Configurable view/UI timeouts (previously hardcoded 180)
    view_timeout_seconds: int = int(os.getenv('VIEW_TIMEOUT', '180'))

    @property
    def csv_local_path(self) -> str:
        return os.path.join(self.home_dir, "Doraemon-CSV-DB", "database") + "/"

    @property
    def char_csv_local_path(self) -> str:
        return os.path.join(self.home_dir, 'Database-dorav2') + "/"

    @property
    def csv_raw_base_url(self) -> str:
        return 'https://raw.githubusercontent.com/star884/Doraemon-CSV-DB/main/database/'

    def validate(self) -> List[str]:
        """Validate config values at startup. Returns list of warnings."""
        warnings = []
        if self.page_size > 25:
            warnings.append(f"PAGE_SIZE={self.page_size} exceeds Discord's 25-field limit; results may be truncated.")
        if self.fuzzy_threshold < 0.0 or self.fuzzy_threshold > 1.0:
            warnings.append(f"FUZZY_THRESHOLD={self.fuzzy_threshold} should be between 0.0 and 1.0.")
        if self.max_retries < 1:
            warnings.append(f"MAX_RETRIES={self.max_retries} should be at least 1.")
        if self.rate_per_user < 1:
            warnings.append(f"RATE_LIMIT_USER={self.rate_per_user} should be at least 1.")
        if self.cache_max_size < 10:
            warnings.append(f"CACHE_MAX_SIZE={self.cache_max_size} is very low; may cause frequent evictions.")
        if self.view_timeout_seconds < 10 or self.view_timeout_seconds > 3600:
            warnings.append(f"VIEW_TIMEOUT={self.view_timeout_seconds} should be between 10 and 3600 seconds.")
        return warnings


# ============================================
# LOGGING SETUP WITH ROTATION
# ============================================

class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            'timestamp': datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if hasattr(record, 'correlation_id'):
            log_entry['correlation_id'] = record.correlation_id
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)


def setup_logging(config: BotConfig) -> logging.Logger:
    """Configure logging with file rotation and console output."""
    logger = logging.getLogger('doraemon-bot')
    logger.setLevel(getattr(logging, config.log_level.upper()))

    if config.log_json:
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    log_path = Path(config.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=config.log_rotation_mb * 1024 * 1024,
        backupCount=config.log_rotation_days,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    console_formatter = JsonFormatter() if config.log_json else logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# ============================================
# METRICS / TELEMETRY (Thread-Safe)
# ============================================

class MetricsCollector:
    """Collect and report bot usage metrics. Thread-safe via threading.Lock.

    v4.1: Added threading.Lock to prevent race conditions on concurrent
    Discord interactions accessing shared counters.
    """

    __slots__ = ('search_queries', 'cache_hits', 'cache_misses', 'errors',
                 'commands_used', 'start_time', 'last_db_load_time', '_lock')

    def __init__(self):
        self.search_queries: Dict[str, int] = defaultdict(int)
        self.cache_hits: Dict[str, int] = defaultdict(int)
        self.cache_misses: Dict[str, int] = defaultdict(int)
        self.errors: Dict[str, int] = defaultdict(int)
        self.commands_used: Dict[str, int] = defaultdict(int)
        self.start_time: float = time.time()
        self.last_db_load_time: Optional[str] = None
        self._lock = threading.Lock()

    def record_search(self, source: str, cache_hit: bool) -> None:
        with self._lock:
            self.search_queries[source] += 1
            if cache_hit:
                self.cache_hits[source] += 1
            else:
                self.cache_misses[source] += 1

    def record_command(self, command: str) -> None:
        with self._lock:
            self.commands_used[command] += 1

    def record_error(self, error_type: str) -> None:
        with self._lock:
            self.errors[error_type] += 1

    def record_db_load(self) -> None:
        with self._lock:
            self.last_db_load_time = utc_now().strftime('%Y-%m-%d %H:%M:%S')

    def get_uptime(self) -> str:
        uptime = time.time() - self.start_time
        hours, remainder = divmod(int(uptime), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"

    def get_cache_stats(self) -> Dict[str, float]:
        with self._lock:
            stats = {}
            for source in set(self.cache_hits.keys()) | set(self.cache_misses.keys()):
                hits = self.cache_hits.get(source, 0)
                misses = self.cache_misses.get(source, 0)
                total = hits + misses
                stats[source] = (hits / total * 100) if total > 0 else 0.0
            return stats

    def get_error_summary(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.errors)

    def get_command_summary(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.commands_used)


# ============================================
# INPUT SANITIZATION
# ============================================

def sanitize_query(query: str, max_length: int = 200) -> str:
    """Sanitize user input for safe processing."""
    if not query:
        return ""
    query = query.strip()
    if len(query) > max_length:
        query = query[:max_length]
    return query


def validate_discord_token(token: str) -> bool:
    """Validate Discord token format with stronger checks."""
    if not token:
        return False
    if len(token) < 50:
        return False
    parts = token.split('.')
    if len(parts) != 3:
        return False
    # Try base64-decoding the first segment to extract bot ID
    import base64
    try:
        padded = parts[0] + '=' * (-len(parts[0]) % 4)
        decoded = base64.b64decode(padded, validate=True)
        decoded.decode('ascii')  # Should be a numeric string (bot user ID)
    except Exception:
        return False
    return True


# ============================================
# BACKWARDS COMPATIBILITY — COLUMN MAPPING
# ============================================

def normalize_csv_header(header: List[str], alias_map: Dict[str, List[str]]) -> List[str]:
    """Map arbitrary CSV headers to canonical names using an alias table."""
    header_clean = [h.strip() for h in header]
    result: List[str] = []
    for col in header_clean:
        mapped = None
        col_lower = col.lower().strip()
        for canonical, aliases in alias_map.items():
            if col_lower in [a.lower().strip() for a in aliases]:
                mapped = canonical
                break
        result.append(mapped if mapped else col)
    return result


def get_field_compat(row: dict, canonical: str, alias_map: Dict[str, List[str]]) -> str:
    """Retrieve a field value trying the canonical name first, then aliases.

    v4.1: Ensures return is always str (never None) for null safety.
    """
    val = row.get(canonical, '')
    if val:
        return str(val)
    for alias in alias_map.get(canonical, []):
        val = row.get(alias, '')
        if val:
            return str(val)
    return ''


def validate_csv_structure(header: List[str], expected_fields: List[str], alias_map: Dict[str, List[str]]) -> List[str]:
    """Validate that required columns exist in the CSV header. Returns missing field names."""
    normalized = normalize_csv_header(header, alias_map)
    normalized_set = set(normalized)
    missing = []
    for field in expected_fields:
        aliases = alias_map.get(field, [field])
        if not any(a in normalized_set for a in aliases + [field]):
            missing.append(field)
    return missing


# ============================================
# FUZZY STRING MATCHING
# ============================================

def levenshtein_ratio(s1: str, s2: str) -> float:
    """Compute Levenshtein-based similarity ratio (0.0–1.0). Optimized DP.

    v4.1: Added identical-string shortcut for performance.
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    s1, s2 = s1.lower(), s2.lower()

    # v4.1: Identical-string shortcut
    if s1 == s2:
        return 1.0

    len1, len2 = len(s1), len(s2)

    if abs(len1 - len2) > max(len1, len2) * 0.7:
        return 0.0

    prev_row = list(range(len2 + 1))

    for i in range(1, len1 + 1):
        curr_row = [i] + [0] * len2
        for j in range(1, len2 + 1):
            if s1[i - 1] == s2[j - 1]:
                curr_row[j] = prev_row[j - 1]
            else:
                curr_row[j] = 1 + min(prev_row[j], curr_row[j - 1], prev_row[j - 1])
        prev_row = curr_row

    dist = prev_row[len2]
    return 1.0 - dist / max(len1, len2)


def fuzzy_contains(haystack: str, needle: str, threshold: float = 0.6) -> bool:
    """Check if any token in haystack fuzzy-matches needle."""
    if not haystack or not needle:
        return False

    haystack_lower = haystack.lower()
    needle_lower = needle.lower()

    if needle_lower in haystack_lower:
        return True

    tokens = re.findall(r'\w+', haystack_lower)
    for token in tokens:
        if levenshtein_ratio(token, needle_lower) >= threshold:
            return True
    return False


def token_overlap_ratio(query_tokens: List[str], text: str) -> float:
    """Fraction of query tokens present in text (0.0–1.0)."""
    if not query_tokens:
        return 0.0
    text_lower = text.lower()
    text_tokens = set(re.findall(r'\w+', text_lower))
    matched = sum(1 for t in query_tokens if t in text_tokens)
    return matched / len(query_tokens)


# ============================================
# RATE LIMITER WITH DEQUE-BASED CLEANUP
# ============================================

class RateLimiter:
    """Per-user and per-guild sliding-window rate limiter with periodic cleanup.

    v4.1: cleanup() now safely handles empty deques before accessing [-1].
    """

    __slots__ = ('_user_usage', '_guild_usage', 'default_limit', 'guild_limit',
                 'window_seconds', 'last_cleanup', 'cleanup_interval')

    def __init__(self, default_limit: int = 10, guild_limit: int = 100, window_seconds: int = 60):
        self._user_usage: Dict[Tuple[str, str], deque] = defaultdict(lambda: deque())
        self._guild_usage: Dict[str, deque] = defaultdict(lambda: deque())
        self.default_limit = default_limit
        self.guild_limit = guild_limit
        self.window_seconds = window_seconds
        self.last_cleanup = time.time()
        self.cleanup_interval = 3600

    def check(self, user_id: str, command: str, guild_id: Optional[str] = None, limit: Optional[int] = None) -> bool:
        now = time.time()

        if now - self.last_cleanup > self.cleanup_interval:
            self.cleanup()
            self.last_cleanup = now

        max_allowed = limit or self.default_limit
        cutoff = now - self.window_seconds
        key = (user_id, command)

        # Per-user check using deque
        dq = self._user_usage[key]
        while dq and dq[0] <= cutoff:
            dq.popleft()

        if len(dq) >= max_allowed:
            return False
        dq.append(now)

        # Per-guild check
        if guild_id:
            gdq = self._guild_usage[guild_id]
            while gdq and gdq[0] <= cutoff:
                gdq.popleft()
            if len(gdq) >= self.guild_limit:
                return False
            gdq.append(now)

        return True

    def cleanup(self) -> None:
        """Remove expired entries to prevent unbounded growth.

        v4.1: Fixed potential IndexError on empty deques. The check `not v`
        short-circuits before accessing v[-1], but we make this explicit
        for readability and robustness.
        """
        now = time.time()
        cutoff = now - self.window_seconds

        # Collect keys to delete (avoid modifying dict during iteration)
        empty_user_keys = []
        for k, v in self._user_usage.items():
            if not v:
                empty_user_keys.append(k)
            elif v[-1] <= cutoff:
                empty_user_keys.append(k)
        for k in empty_user_keys:
            del self._user_usage[k]

        empty_guild_keys = []
        for k, v in self._guild_usage.items():
            if not v:
                empty_guild_keys.append(k)
            elif v[-1] <= cutoff:
                empty_guild_keys.append(k)
        for k in empty_guild_keys:
            del self._guild_usage[k]

    def get_remaining(self, user_id: str, command: str) -> int:
        """Get remaining requests for user/command."""
        now = time.time()
        cutoff = now - self.window_seconds
        key = (user_id, command)
        dq = self._user_usage.get(key, deque())
        valid = sum(1 for t in dq if t > cutoff)
        return max(0, self.default_limit - valid)


# ============================================
# RATE LIMITED DECORATOR
# ============================================

# Forward declarations — populated in BOT SETUP section
rate_limiter: Optional[RateLimiter] = None
CFG: Optional[BotConfig] = None

def rate_limited(command_name: str, limit: Optional[int] = None):
    """Decorator for slash commands that enforces rate limiting."""
    def decorator(func):
        @wraps(func)
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild.id) if interaction.guild else None
            if not rate_limiter.check(user_id, command_name, guild_id, limit):
                remaining = rate_limiter.get_remaining(user_id, command_name)
                embed = Embed(
                    title='Rate Limited',
                    description=(
                        f'You\'re using `/{command_name}` too quickly. '
                        f'Please wait {CFG.rate_window_seconds}s and try again.\n'
                        f'Requests remaining: {remaining}'
                    ),
                    color=CFG.color_warning
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            return await func(interaction, *args, **kwargs)
        return wrapper
    return decorator


def admin_only():
    """Decorator that restricts a command to administrators."""
    def decorator(func):
        @wraps(func)
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            if not is_admin(interaction):
                embed = Embed(
                    title='Permission Denied',
                    description='This command requires administrator permissions.',
                    color=CFG.color_error
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            return await func(interaction, *args, **kwargs)
        return wrapper
    return decorator


# ============================================
# SEARCH CACHE WITH SQLITE PERSISTENCE (Thread-Safe)
# ============================================

class SearchCache:
    """TTL-based cache with bounded size, LRU eviction, and SQLite persistence.

    v4.1: SQLite operations now protected by threading.Lock with
    check_same_thread=False. Transactions wrapped with try/except/rollback.
    Persistent connection instead of creating/destroying connections per operation.
    """

    __slots__ = ('_cache', '_meta', 'ttl', 'max_size', '_db_path', '_source_map', '_sqlite_lock', '_conn')

    def __init__(self, ttl: int = 300, max_size: int = 1000, db_path: str = CACHE_DB_FILENAME):
        self._cache: Dict[str, Tuple[List[dict], float]] = {}
        self._source_map: Dict[str, str] = {}  # key -> source_type for pattern invalidation
        self.ttl = ttl
        self.max_size = max_size
        self._db_path = db_path
        self._sqlite_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_persistent_connection()
        self._load_from_db()

    def _init_persistent_connection(self) -> None:
        """Initialize a persistent SQLite connection for all cache operations."""
        try:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute('''
                CREATE TABLE IF NOT EXISTS search_cache (
                    key TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    results TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            ''')
            self._conn.commit()
        except Exception as e:
            logging.getLogger('doraemon-bot').warning(f'Could not init SQLite cache connection: {e}')
            self._conn = None

    def _key(self, source_type: str, query: str) -> str:
        """Plain-string key (no MD5 hashing needed)."""
        return f"{source_type}:{query.lower().strip()}"

    def get(self, source_type: str, query: str) -> Optional[List[dict]]:
        key = self._key(source_type, query)
        if key in self._cache:
            results, ts = self._cache[key]
            if time.time() - ts < self.ttl:
                return results
            del self._cache[key]
        return None

    def set(self, source_type: str, query: str, results: List[dict]) -> None:
        key = self._key(source_type, query)

        if len(self._cache) >= self.max_size:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
            self._source_map.pop(oldest_key, None)

        self._cache[key] = (results, time.time())
        self._source_map[key] = source_type
        self._persist_entry(key, source_type, results)

    def invalidate(self) -> None:
        self._cache.clear()
        self._source_map.clear()
        self._clear_db()

    def invalidate_pattern(self, source_type: str) -> None:
        """Invalidate all entries for a specific source type.

        v4.1: Confirmed safe — keys collected first, then deleted separately.
        Additional guard: only delete keys that still exist (in case of
        concurrent modification).
        """
        keys_to_delete = [k for k, st in self._source_map.items() if st == source_type]
        for k in keys_to_delete:
            self._cache.pop(k, None)
            self._source_map.pop(k, None)

    def get_size(self) -> int:
        return len(self._cache)

    def _load_from_db(self) -> None:
        """Load cached results from SQLite on startup using persistent connection."""
        if not self._conn:
            return
        try:
            with self._sqlite_lock:
                cursor = self._conn.cursor()
                now = time.time()
                cursor.execute('SELECT key, source_type, results, timestamp FROM search_cache')
                for key, source_type, results_json, ts in cursor.fetchall():
                    if now - ts < self.ttl and len(self._cache) < self.max_size:
                        results = json.loads(results_json)
                        self._cache[key] = (results, ts)
                        self._source_map[key] = source_type
                    else:
                        cursor.execute('DELETE FROM search_cache WHERE key = ?', (key,))
                self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            logging.getLogger('doraemon-bot').warning(f'SQLite cache load failed, rolling back: {e}')

    def _persist_entry(self, key: str, source_type: str, results: List[dict]) -> None:
        """Persist a single cache entry to SQLite using persistent connection."""
        if not self._conn:
            return
        try:
            with self._sqlite_lock:
                self._conn.execute(
                    'INSERT OR REPLACE INTO search_cache (key, source_type, results, timestamp) VALUES (?, ?, ?, ?)',
                    (key, source_type, json.dumps(results, ensure_ascii=False), time.time())
                )
                self._conn.commit()
        except Exception as e:
            try:
                self._conn.rollback()
            except Exception:
                pass
            logging.getLogger('doraemon-bot').debug(f'Could not persist cache entry (rolled back): {e}')

    def _clear_db(self) -> None:
        """Clear the SQLite cache table using persistent connection."""
        if not self._conn:
            return
        try:
            with self._sqlite_lock:
                self._conn.execute('DELETE FROM search_cache')
                self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass

    def close(self) -> None:
        """Close the persistent SQLite connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ============================================
# SEARCH INDEX
# ============================================

class SearchIndex:
    """Inverted-index for fast title and field searching with fuzzy fallback."""

    __slots__ = ('by_title_token', 'by_magazine', 'by_category',
                 'by_char_name_token', 'by_char_category', 'by_char_desc_token',
                 'by_anime_1979', 'by_anime_2005',
                 '_data', '_char_data', '_char_index_built',
                 '_magazine_buckets', '_date_buckets',
                 '_anime_1979_buckets', '_anime_2005_buckets')

    def __init__(self):
        self.by_title_token: Dict[str, List[int]] = {}
        self.by_magazine: Dict[str, List[int]] = {}
        self.by_category: Dict[str, List[int]] = {}
        self.by_char_name_token: Dict[str, List[int]] = {}
        self.by_char_category: Dict[str, List[int]] = {}
        self.by_char_desc_token: Dict[str, List[int]] = {}
        self.by_anime_1979: Dict[str, List[int]] = {}
        self.by_anime_2005: Dict[str, List[int]] = {}
        self._data: List[dict] = []
        self._char_data: List[dict] = []
        self._char_index_built: bool = False
        self._magazine_buckets: Dict[str, List[dict]] = {}
        self._date_buckets: Dict[str, List[dict]] = {}
        self._anime_1979_buckets: Dict[str, List[dict]] = {}
        self._anime_2005_buckets: Dict[str, List[dict]] = {}

    def build_story_index(self, data: List[dict]) -> None:
        """Build inverted index for story data and precompute filter buckets.

        Builds into temporary dicts then atomically swaps to avoid
        race conditions with concurrent search reads.
        """
        new_title_token: Dict[str, List[int]] = {}
        new_magazine: Dict[str, List[int]] = {}
        new_mag_buckets: Dict[str, List[dict]] = {}
        new_date_buckets: Dict[str, List[dict]] = {}
        new_anime_1979: Dict[str, List[int]] = {}
        new_anime_2005: Dict[str, List[int]] = {}
        new_anime_1979_buckets: Dict[str, List[dict]] = {}
        new_anime_2005_buckets: Dict[str, List[dict]] = {}

        for idx, item in enumerate(data):
            title = (item.get('English title', '') or item.get('story_a', '')).lower()
            for token in re.findall(r'\w+', title):
                new_title_token.setdefault(token, []).append(idx)
            mag = (item.get('Magazine code', '') or '').upper().strip()
            if mag:
                new_magazine.setdefault(mag, []).append(idx)
                new_mag_buckets.setdefault(mag, []).append(item)
            date = (item.get('Publication Date', '') or '').strip()
            if date:
                new_date_buckets.setdefault(date, []).append(item)

            # Build anime episode indices
            anime_1979 = (item.get('1979 anime', '') or '').lower()
            if anime_1979:
                for ep_ref in re.findall(r'[a-z]*\d+[a-z]*', anime_1979):
                    new_anime_1979.setdefault(ep_ref, []).append(idx)
                new_anime_1979_buckets.setdefault(anime_1979, []).append(item)

            anime_2005 = (item.get('2005 anime', '') or '').lower()
            if anime_2005:
                for ep_ref in re.findall(r'[a-z]*\d+[a-z]*', anime_2005):
                    new_anime_2005.setdefault(ep_ref, []).append(idx)
                new_anime_2005_buckets.setdefault(anime_2005, []).append(item)

        # Atomic swap
        self._data = data
        self.by_title_token = new_title_token
        self.by_magazine = new_magazine
        self._magazine_buckets = new_mag_buckets
        self._date_buckets = new_date_buckets
        self.by_anime_1979 = new_anime_1979
        self.by_anime_2005 = new_anime_2005
        self._anime_1979_buckets = new_anime_1979_buckets
        self._anime_2005_buckets = new_anime_2005_buckets

        logger.info(f'Built story index: {len(self.by_title_token)} title tokens, '
                     f'{len(self.by_magazine)} magazine codes, '
                     f'{len(self._magazine_buckets)} magazine buckets, '
                     f'{len(self._date_buckets)} date buckets, '
                     f'{len(self.by_anime_1979)} 1979-anime refs, '
                     f'{len(self.by_anime_2005)} 2005-anime refs')

    def build_char_index(self, data: List[dict]) -> None:
        """Build inverted index for character data using atomic swap."""
        new_name_token: Dict[str, List[int]] = {}
        new_category: Dict[str, List[int]] = {}
        new_desc_token: Dict[str, List[int]] = {}

        for idx, item in enumerate(data):
            for field_name in ('Character Name', 'Alternative Name'):
                name = (item.get(field_name, '') or '').lower()
                for token in re.findall(r'\w+', name):
                    new_name_token.setdefault(token, []).append(idx)
            cat = (item.get('Category', '') or '').lower().strip()
            if cat:
                new_category.setdefault(cat, []).append(idx)
            desc = (item.get('Description', '') or '').lower()
            for token in re.findall(r'\w+', desc):
                new_desc_token.setdefault(token, []).append(idx)

        # Atomic swap
        self._char_data = data
        self.by_char_name_token = new_name_token
        self.by_char_category = new_category
        self.by_char_desc_token = new_desc_token
        self._char_index_built = True

        logger.info(f'Built character index: {len(self.by_char_name_token)} name tokens, '
                     f'{len(self.by_char_category)} categories, '
                     f'{len(self.by_char_desc_token)} desc tokens')

    def ensure_char_index(self) -> None:
        """Lazily build character index if not yet built."""
        if not self._char_index_built and self._char_data:
            self.build_char_index(self._char_data)

    def search_title(self, query: str) -> List[dict]:
        """Search stories by title using inverted index with fuzzy fallback."""
        tokens = re.findall(r'\w+', query.lower())
        if not tokens:
            return []

        candidate_sets: List[Set[int]] = []
        fuzzy_needed = False

        for token in tokens:
            if token in self.by_title_token:
                candidate_sets.append(set(self.by_title_token[token]))
            else:
                # Fuzzy fallback: find closest index tokens
                # Restrict to tokens within a plausible length range for performance
                token_len = len(token)
                min_len = max(1, int(token_len * 0.5))
                max_len = int(token_len * 1.5) + 1
                fuzzy_hits: Set[int] = set()
                for idx_token, indices in self.by_title_token.items():
                    if len(idx_token) < min_len or len(idx_token) > max_len:
                        continue
                    if levenshtein_ratio(idx_token, token) >= CFG.fuzzy_threshold:
                        fuzzy_hits.update(indices)
                if fuzzy_hits:
                    candidate_sets.append(fuzzy_hits)
                else:
                    return []

        candidates = candidate_sets[0]
        for s in candidate_sets[1:]:
            candidates &= s

        return [self._data[i] for i in sorted(candidates)][:CFG.max_search_results]

    def search_char_index(self, query: str) -> List[int]:
        """Return character indices whose name tokens match all query tokens."""
        self.ensure_char_index()
        tokens = re.findall(r'\w+', query.lower())
        if not tokens:
            return []

        candidate_sets: List[Set[int]] = []
        for token in tokens:
            if token in self.by_char_name_token:
                candidate_sets.append(set(self.by_char_name_token[token]))
            else:
                return []

        candidates = candidate_sets[0]
        for s in candidate_sets[1:]:
            candidates &= s

        return sorted(candidates)

    def get_magazine_bucket(self, mag_code: str) -> List[dict]:
        """Get pre-filtered stories for a magazine code."""
        return self._magazine_buckets.get(mag_code.upper().strip(), [])

    def get_date_bucket(self, date: str) -> List[dict]:
        """Get pre-filtered stories for a publication date."""
        return self._date_buckets.get(date.strip(), [])

    def get_anime_1979_bucket(self, ep_ref: str) -> List[dict]:
        """Get pre-filtered stories for a 1979 anime episode reference."""
        return self._anime_1979_buckets.get(ep_ref.lower().strip(), [])

    def get_anime_2005_bucket(self, ep_ref: str) -> List[dict]:
        """Get pre-filtered stories for a 2005 anime episode reference."""
        return self._anime_2005_buckets.get(ep_ref.lower().strip(), [])


# ============================================
# WORD BOUNDARY + RANKED CHARACTER SEARCH
# ============================================

def word_boundary_match(query: str, text: str) -> bool:
    """Match query as a whole word within text using \\b boundaries."""
    if not query or not text:
        return False
    pattern = r'\b' + re.escape(query.lower()) + r'\b'
    return re.search(pattern, text.lower()) is not None


def _score_characters(
    query: str,
    characters: List[dict],
    use_word_boundary: bool = True,
    use_fuzzy: bool = True
) -> List[Tuple[float, dict]]:
    """Shared scoring core for character search. Returns sorted (score, char) list."""
    q = query.lower().strip()
    if not q:
        return []

    query_tokens = re.findall(r'\w+', q)
    scored: List[Tuple[float, dict]] = []
    seen_ids: Set[int] = set()

    def add(score: float, char: dict) -> None:
        oid = id(char)
        if oid not in seen_ids:
            scored.append((score, char))
            seen_ids.add(oid)

    for char in characters:
        name = char.get('Character Name', '') or ''
        alt = char.get('Alternative Name', '') or ''
        cat = char.get('Category', '') or ''
        desc = char.get('Description', '') or ''

        name_lower = name.lower()
        alt_lower = alt.lower()
        cat_lower = cat.lower()
        desc_lower = desc.lower()

        if use_word_boundary and word_boundary_match(q, name_lower):
            score = SCORE_WORD_BOUNDARY_NAME
            if name_lower == q:
                score += SCORE_EXACT_NAME_BONUS
            add(score, char)
            continue

        if use_word_boundary and word_boundary_match(q, alt_lower):
            add(SCORE_WORD_BOUNDARY_ALT, char)
            continue

        if q in name_lower:
            add(SCORE_SUBSTRING_NAME, char)
            continue

        if q == cat_lower:
            add(SCORE_CATEGORY_MATCH, char)
            continue

        if q in desc_lower:
            overlap = token_overlap_ratio(query_tokens, desc)
            add(SCORE_DESC_BASE + overlap * SCORE_DESC_OVERLAY_WEIGHT, char)
            continue

        if use_fuzzy:
            name_tokens = re.findall(r'\w+', name_lower)
            for nt in name_tokens:
                ratio = levenshtein_ratio(nt, q)
                if ratio >= CFG.fuzzy_threshold:
                    add(SCORE_FUZZY_NAME_BASE + ratio * SCORE_FUZZY_WEIGHT, char)
                    break
            else:
                alt_tokens = re.findall(r'\w+', alt_lower)
                for at in alt_tokens:
                    ratio = levenshtein_ratio(at, q)
                    if ratio >= CFG.fuzzy_threshold:
                        add(SCORE_FUZZY_ALT_BASE + ratio * SCORE_FUZZY_WEIGHT, char)
                        break

    scored.sort(key=lambda x: -x[0])
    return scored


def search_characters(
    query: str,
    characters: List[dict],
    use_word_boundary: bool = True,
    use_fuzzy: bool = True
) -> List[dict]:
    """Search characters with tiered matching and ranking. Returns dicts only."""
    scored = _score_characters(query, characters, use_word_boundary, use_fuzzy)
    return [c for _, c in scored[:CFG.max_search_results]]


def search_characters_with_scores(
    query: str,
    characters: List[dict],
    use_word_boundary: bool = True,
    use_fuzzy: bool = True
) -> List[Tuple[float, dict]]:
    """Like search_characters but returns (score, char) tuples for relevance display."""
    scored = _score_characters(query, characters, use_word_boundary, use_fuzzy)
    return scored[:CFG.max_search_results]


def _search_stories_linear(
    query: str,
    episodes: List[dict],
    max_results: int
) -> List[dict]:
    """Shared linear-scan fallback for story search across episodes list."""
    q = query.lower()
    query_tokens = re.findall(r'\w+', q)
    results: List[dict] = []
    for ep in episodes:
        story_a = (ep.get('story_a', '') or '').lower()
        story_b = (ep.get('story_b', '') or '').lower()
        if q in story_a or q in story_b:
            results.append(ep)
        elif query_tokens and (
            token_overlap_ratio(query_tokens, story_a) >= 0.5
            or token_overlap_ratio(query_tokens, story_b) >= 0.5
        ):
            results.append(ep)
    return results[:max_results]


def search_stories(query: str, episodes: List[dict], csv_mode: bool) -> List[dict]:
    """Unified story search: index lookup → fuzzy fallback → linear scan.
    Used by search_cmd, cross_source_search, and other search paths.
    """
    q = query.lower().strip()
    if not q:
        return []

    # CSV mode: try specialized lookups first
    if csv_mode:
        if len(q) <= 4 and q.upper() in KNOWN_MAGAZINES:
            return search_index.get_magazine_bucket(q.upper())
        if re.match(r'^\d{4}-\d{2}$', q):
            return search_index.get_date_bucket(q)

    # Index lookup
    index_hits = search_index.search_title(q)
    if index_hits:
        return index_hits

    # Linear scan fallback
    return _search_stories_linear(q, episodes, CFG.max_search_results)


# ============================================
# ABSTRACT DATA SOURCE
# ============================================

class DataSource:
    """Abstract base class for data sources."""

    def __init__(self, source_id: int, name: str, url: str, source_type: SourceType,
                 files: List[str], fetch_method: str = 'default'):
        self.id = source_id
        self.name = name
        self.url = url
        self.source_type = source_type
        self.files = files
        self.fetch_method = fetch_method

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id, 'name': self.name, 'url': self.url,
            'type': self.source_type.value, 'files': self.files,
            'fetch_method': self.fetch_method
        }

    async def load(self, dm: 'DatabaseManager') -> bool:
        raise NotImplementedError

    def get_record_count(self, dm: 'DatabaseManager') -> int:
        raise NotImplementedError


# ============================================
# DATABASE MANAGER
# ============================================

class DatabaseManager:
    """Thread-safe manager for all data sources with retry logic, incremental loading, and fallback chains."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self.episodes: List[dict] = []
        self.db: Dict[str, Any] = {}
        self.csv_stories: List[dict] = []
        self.csv_data_loaded: bool = False
        self.char_data: List[dict] = []
        self.char_data_loaded: bool = False
        self._file_hashes: Dict[str, str] = {}
        self._session = session

        self.available_sources: List[DataSource] = [
            DataSource(1, 'JSON Database (Primary)',
                       'https://github.com/star884/Build-Doraemon-Databasev3',
                       SourceType.JSON, ['database/search_index.json']),
            DataSource(2, 'CSV Database (Local Clone - Termux)',
                       CFG.csv_local_path, SourceType.CSV,
                       ['story_index.csv', 'manga_details.csv'], 'local_file'),
            DataSource(3, 'Characters CSV (Remote - GitHub)',
                       CHAR_CSV_RAW_URL, SourceType.CSV_CHAR,
                       [CHAR_CSV_FILENAME], 'github_raw'),
            DataSource(4, 'Characters CSV (Local Clone)',
                       CFG.char_csv_local_path, SourceType.CSV_CHAR,
                       [CHAR_CSV_FILENAME], 'local_file'),
        ]

        self.current_source: Dict[str, Any] = self.available_sources[0].to_dict()
        self._lock = asyncio.Lock()
        self.episodes_source: Optional[SourceType] = None

    @property
    def current_source_obj(self) -> Optional[DataSource]:
        sid = self.current_source.get('id', 1)
        return next((s for s in self.available_sources if s.id == sid), None)

    async def safe_update_episodes(self, episodes: List[dict]) -> None:
        async with self._lock:
            self.episodes = episodes

    async def safe_update_csv(self, stories: List[dict]) -> None:
        async with self._lock:
            self.csv_stories = stories
            self.csv_data_loaded = True

    async def safe_update_chars(self, chars: List[dict]) -> None:
        async with self._lock:
            self.char_data = chars
            self.char_data_loaded = True

    async def safe_update_source(self, source: Dict[str, Any]) -> None:
        async with self._lock:
            self.current_source = source.copy()

    def is_csv_source(self) -> bool:
        return self.current_source['type'] == SourceType.CSV.value

    def is_char_source(self) -> bool:
        return self.current_source['type'] == SourceType.CSV_CHAR.value

    async def get_session(self) -> Optional[aiohttp.ClientSession]:
        """Get or create a shared aiohttp ClientSession (connection pooling)."""
        if not AIOHTTP_AVAILABLE:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=CFG.http_timeout_seconds),
                connector=aiohttp.TCPConnector(limit=10, limit_per_host=5)
            )
        return self._session

    async def close_session(self) -> None:
        """Close the shared session on shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _compute_file_hash(self, filepath: str) -> Optional[str]:
        """Compute SHA256 of a file for incremental loading.

        v4.1: Failures are now logged as warnings instead of silently
        returning None. Caller still treats None as 'no change' but
        the warning helps diagnose permission/path issues.
        """
        try:
            h = hashlib.sha256()
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
            return h.hexdigest()
        except PermissionError:
            logger.warning(f'Permission denied computing hash for: {filepath}')
            return None
        except FileNotFoundError:
            logger.warning(f'File not found computing hash for: {filepath}')
            return None
        except Exception as e:
            logger.warning(f'Could not compute file hash for {filepath}: {type(e).__name__}: {e}')
            return None

    def load_json_database(self) -> bool:
        """Load data from local JSON database."""
        try:
            if CFG.json_db_path:
                db_path = CFG.json_db_path
            else:
                base_dir = Path(__file__).parent.resolve()
                db_path = str(base_dir / 'database' / 'search_index.json')
            path = Path(db_path)

            if not path.exists():
                logger.warning(f'JSON database file not found at: {path.absolute()}')
                logger.warning(f'Current working directory: {Path.cwd()}')
                self.episodes = []
                return False

            with open(path, encoding='utf-8') as f:
                self.db = json.load(f)

            self.episodes = self.db.get('items', [])
            logger.info(f'Loaded {len(self.episodes)} episodes from JSON database')
            if len(self.episodes) == 0:
                logger.warning('JSON loaded but contains NO records!')
                logger.warning(f'JSON keys found: {list(self.db.keys())[:10]}')
            self.episodes_source = SourceType.JSON
        except json.JSONDecodeError as e:
            self.episodes = []
            logger.error(f'Invalid JSON in database file: {e}')
            metrics.record_error('JSONDecodeError')
            return False
        except Exception as e:
            self.episodes = []
            logger.error(f'Error loading JSON database: {e}')
            metrics.record_error(type(e).__name__)
            return False
    def _parse_csv_rows(self, reader) -> List[dict]:
        """Parse CSV rows with backwards-compatible column mapping and validation."""
        stories: List[dict] = []
        header = next(reader, None)

        if not header:
            header = CSV_FIELD_NAMES
        else:
            header = normalize_csv_header(header, CSV_COLUMN_ALIASES)

        # Validate CSV structure
        missing = validate_csv_structure(header, CSV_FIELD_NAMES, CSV_COLUMN_ALIASES)
        if missing:
            logger.warning(f'CSV missing expected columns: {missing}')

        num_cols = len(header)
        logger.info(f'CSV has {num_cols} columns (normalized)')

        for row in reader:
            if not row or all(c.strip() == '' for c in row):
                continue
            while len(row) < num_cols:
                row.append('')
            story: dict = {}
            for i, col_name in enumerate(header):
                val = row[i].strip() if i < len(row) else ''
                story[col_name] = val  # No sanitization here - done at display time
            story['_raw'] = row
            stories.append(story)

        logger.info(f'Parsed {len(stories)} story rows from CSV')
        return stories

    def _parse_char_csv_rows(self, reader) -> List[dict]:
        """Parse character CSV rows with smart deduplication (name+description) — O(n) using name sets."""
        characters: List[dict] = []
        header = next(reader, None)

        if header:
            header = normalize_csv_header(header, CHAR_COLUMN_ALIASES)
            logger.info(f'Character CSV header (normalized): {header}')
        else:
            header = CHAR_CSV_FIELD_NAMES

        # Validate
        missing = validate_csv_structure(header, CHAR_CSV_FIELD_NAMES, CHAR_COLUMN_ALIASES)
        if missing:
            logger.warning(f'Character CSV missing expected columns: {missing}')

        raw_entries: List[dict] = []
        for row in reader:
            if not row or all(c.strip() == '' for c in row):
                continue
            while len(row) < len(header):
                row.append('')
            entry: dict = {}
            for i, col_name in enumerate(header):
                val = row[i].strip() if i < len(row) else ''
                entry[col_name] = val  # No sanitization here - done at display time
            entry['_raw'] = row
            raw_entries.append(entry)

        # Strict dedup: name + description must BOTH match — O(n) using sets
        seen_keys: Dict[Tuple[str, str], int] = {}
        # Track names for detecting same-name-diff-desc entries
        name_set: Set[str] = set()
        merged_count = 0
        skipped_no_name = 0
        separate_same_name_diff_desc = 0

        for entry in raw_entries:
            name = (entry.get('Character Name', '') or '').strip()
            desc = (entry.get('Description', '') or '').strip()
            alt = (entry.get('Alternative Name', '') or '').strip()
            refs = (entry.get('Source References', '') or '').strip()

            if not name:
                skipped_no_name += 1
                continue

            name_lower = name.lower().strip()
            key = (name_lower, desc.lower().strip())

            if key in seen_keys:
                # Same name AND same description → merge
                existing_idx = seen_keys[key]
                existing = characters[existing_idx]

                # Union Source References
                existing_refs_str = existing.get('Source References', '').strip()
                existing_refs_set: Set[str] = {
                    r.strip().lower() for r in re.split(r'[;,\n]', existing_refs_str) if r.strip()
                }
                new_refs_list = [r.strip() for r in re.split(r'[;,\n]', refs) if r.strip()]
                refs_to_add = [r for r in new_refs_list if r.lower() not in existing_refs_set]
                if refs_to_add:
                    if existing_refs_str:
                        existing['Source References'] = f"{existing_refs_str}; {'; '.join(refs_to_add)}"
                    else:
                        existing['Source References'] = '; '.join(refs_to_add)
                    for r in refs_to_add:
                        existing_refs_set.add(r.lower())

                # Union Alternative Names
                existing_alt_str = existing.get('Alternative Name', '').strip()
                if alt:
                    existing_alts: Set[str] = {
                        a.strip().lower() for a in re.split(r'[;,\n]', existing_alt_str) if a.strip()
                    }
                    if alt.lower() not in existing_alts:
                        if existing_alt_str:
                            existing['Alternative Name'] = f"{existing_alt_str}; {alt}"
                        else:
                            existing['Alternative Name'] = alt
                        existing_alts.add(alt.lower())

                merged_count += 1
            else:
                # Check if name matches but desc differs → separate entry
                if name_lower in name_set:
                    separate_same_name_diff_desc += 1

                seen_keys[key] = len(characters)
                name_set.add(name_lower)
                characters.append(dict(entry))

        logger.info(
            f'Strict Dedup (name+description) complete. '
            f'Kept {len(characters)} entries. Merged {merged_count} duplicates. '
            f'Separate (same name, diff desc): {separate_same_name_diff_desc}. '
            f'Skipped {skipped_no_name} empty names.'
        )

        return characters

    def load_csv_database_local(self, base_path: str) -> bool:
        """Load CSV data from local files with path validation and incremental loading."""
        try:
            base = Path(base_path)

            if not base.is_dir():
                logger.warning(f'Directory not found: {base_path}')
                logger.info('Run: git clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB')
                self.csv_stories = []
                self.csv_data_loaded = False
                return False

            story_file = base / 'story_index.csv'

            if not story_file.is_file():
                logger.warning(f'Story index not found at {story_file}')
                self.csv_stories = []
                self.csv_data_loaded = False
                return False

            # Incremental loading: skip if file hasn't changed
            file_hash = self._compute_file_hash(str(story_file))
            if file_hash and self._file_hashes.get(str(story_file)) == file_hash:
                logger.info(f'Story CSV unchanged (hash match), skipping re-parse')
                return True
            if file_hash:
                self._file_hashes[str(story_file)] = file_hash

            with open(story_file, 'r', encoding='utf-8') as f:
                stories = self._parse_csv_rows(csv.reader(f))

            self.csv_stories = stories
            self.csv_data_loaded = True
            self.episodes_source = SourceType.CSV
            logger.info(f'Loaded {len(self.csv_stories)} stories from local CSV (offline mode)')
            metrics.record_db_load()
            return True

        except PermissionError:
            logger.error(f'Permission denied accessing: {base_path}')
            self.csv_stories = []
            self.csv_data_loaded = False
            return False
        except Exception as e:
            logger.error(f'Error loading local CSV: {type(e).__name__}: {e}')
            metrics.record_error(type(e).__name__)
            self.csv_stories = []
            self.csv_data_loaded = False
            return False

    async def load_csv_database_live(self, source_url: str) -> bool:
        """Fetch CSV data from GitHub with retry logic using shared session."""
        if not AIOHTTP_AVAILABLE:
            logger.error('aiohttp not installed - remote fetching disabled')
            self.csv_stories = []
            self.csv_data_loaded = False
            return False

        async with self._lock:
            try:
                story_url = CFG.csv_raw_base_url + 'story_index.csv'
                success = await self._fetch_with_retry(story_url, self._parse_csv_rows)

                if success:
                    self.csv_data_loaded = True
                    logger.info(f'Loaded {len(self.csv_stories)} stories from CSV (online mode)')
                    metrics.record_db_load()
                    return True
                else:
                    self.csv_data_loaded = False
                    return False

            except Exception as e:
                logger.error(f'Error fetching CSV: {type(e).__name__}: {e}')
                metrics.record_error(type(e).__name__)
                self.csv_stories = []
                self.csv_data_loaded = False
                return False

    async def _fetch_with_retry(self, url: str, parser: Callable, retries: int = 3) -> bool:
        """Fetch URL with exponential backoff retry logic using shared session."""
        session = await self.get_session()
        if not session:
            return False

        for attempt in range(retries):
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f'Attempt {attempt + 1}: HTTP {resp.status}')
                        if attempt == retries - 1:
                            return False
                        await asyncio.sleep(CFG.retry_backoff_base ** attempt)
                        continue

                    content = await resp.text(encoding='utf-8')
                    lines = content.splitlines()

                    if not lines:
                        return False

                    self.csv_stories = parser(csv.reader(lines))
                    return True

            except Exception as e:
                logger.warning(f'Attempt {attempt + 1} failed: {e}')
                if attempt == retries - 1:
                    return False
                await asyncio.sleep(CFG.retry_backoff_base ** attempt)

        return False

    def load_char_csv_local(self, filepath: str) -> bool:
        """Load characters CSV from a local file with incremental loading."""
        try:
            path = Path(filepath)
            if not path.is_file():
                logger.warning(f'Character CSV not found at {filepath}')
                self.char_data = []
                self.char_data_loaded = False
                return False

            # Incremental loading
            file_hash = self._compute_file_hash(filepath)
            if file_hash and self._file_hashes.get(filepath) == file_hash:
                logger.info(f'Character CSV unchanged (hash match), skipping re-parse')
                return True
            if file_hash:
                self._file_hashes[filepath] = file_hash

            with open(filepath, 'r', encoding='utf-8') as f:
                self.char_data = self._parse_char_csv_rows(csv.reader(f))

            self.char_data_loaded = True
            logger.info(f'Loaded {len(self.char_data)} characters from local CSV')
            metrics.record_db_load()
            return True

        except Exception as e:
            logger.error(f'Error loading local character CSV: {type(e).__name__}: {e}')
            metrics.record_error(type(e).__name__)
            self.char_data = []
            self.char_data_loaded = False
            return False

    async def load_char_csv_remote(self, url: str) -> bool:
        """Fetch and parse characters CSV from GitHub raw URL using shared session."""
        if not AIOHTTP_AVAILABLE:
            logger.error('aiohttp not installed - remote fetching disabled')
            self.char_data = []
            self.char_data_loaded = False
            return False

        async with self._lock:
            try:
                session = await self.get_session()
                if not session:
                    return False

                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.error(f'Failed to fetch character CSV: HTTP {resp.status}')
                        return False

                    content = await resp.text(encoding='utf-8')
                    lines = content.splitlines()

                    if not lines:
                        return False

                    self.char_data = self._parse_char_csv_rows(csv.reader(lines))
                    self.char_data_loaded = True
                    logger.info(f'Loaded {len(self.char_data)} characters from remote CSV')
                    metrics.record_db_load()
                    return True

            except Exception as e:
                logger.error(f'Error fetching character CSV: {type(e).__name__}: {e}')
                metrics.record_error(type(e).__name__)
                self.char_data = []
                self.char_data_loaded = False
                return False

    def convert_csv_to_episode_format(self, stories: List[dict]) -> List[dict]:
        """Map CSV data to standard EPISODES format (backwards-compatible)."""
        episodes: List[dict] = []
        for story in stories:
            anime_1979 = get_field_compat(story, '1979 anime', CSV_COLUMN_ALIASES)
            anime_2005 = get_field_compat(story, '2005 anime', CSV_COLUMN_ALIASES)
            anime_1979_extra = get_field_compat(story, '1979 anime extra', CSV_COLUMN_ALIASES)
            anime_2005_extra = get_field_compat(story, '2005 anime extra 1', CSV_COLUMN_ALIASES)

            anime_1979_full = f"{anime_1979}; {anime_1979_extra}" if anime_1979 and anime_1979_extra else anime_1979 or ''
            anime_2005_full = f"{anime_2005}; {anime_2005_extra}" if anime_2005 and anime_2005_extra else anime_2005 or ''

            in_ep = anime_1979 or anime_2005 or 'N/A'

            ep = {
                'in_season_episode': in_ep,
                'alt_in_episode': '',
                'jp_story_numbers': '',
                'jp_story_all': '',
                'story_a': get_field_compat(story, 'English title', CSV_COLUMN_ALIASES) or get_field_compat(story, 'Japanese title', CSV_COLUMN_ALIASES),
                'story_b': get_field_compat(story, 'Japanese title', CSV_COLUMN_ALIASES),
                'category': 'manga_stories',
                'title': get_field_compat(story, 'English title', CSV_COLUMN_ALIASES) or get_field_compat(story, 'Japanese title', CSV_COLUMN_ALIASES),
                'Magazine code': get_field_compat(story, 'Magazine code', CSV_COLUMN_ALIASES),
                'Publication Date': get_field_compat(story, 'Publication Date', CSV_COLUMN_ALIASES),
                'English title': get_field_compat(story, 'English title', CSV_COLUMN_ALIASES),
                'Japanese title': get_field_compat(story, 'Japanese title', CSV_COLUMN_ALIASES),
                'English Kindle': get_field_compat(story, 'English Kindle', CSV_COLUMN_ALIASES),
                'Bilingual Print': get_field_compat(story, 'Bilingual Print', CSV_COLUMN_ALIASES),
                'Tankōbon': get_field_compat(story, 'Tankōbon', CSV_COLUMN_ALIASES),
                'Complete Collection': get_field_compat(story, 'Complete Collection', CSV_COLUMN_ALIASES),
                '1979 anime': anime_1979_full,
                '2005 anime': anime_2005_full,
                'Movie': get_field_compat(story, 'Movie', CSV_COLUMN_ALIASES),
                'Short film': get_field_compat(story, 'Short film', CSV_COLUMN_ALIASES),
                'Notes': get_field_compat(story, 'Notes', CSV_COLUMN_ALIASES),
                'jp_parsed': [],
                'jp_has_specials': False,
                'jp_has_standalone_specials': False,
            }
            episodes.append(ep)
        return episodes

    async def load_csv_database(self, source_config: Dict[str, Any]) -> bool:
        """Unified loader supporting both local and remote CSV."""
        fetch_method = source_config.get('fetch_method', 'github_api')
        source_url = source_config['url']

        if fetch_method == 'local_file':
            success = self.load_csv_database_local(source_url)
            if not success:
                logger.info('Local load failed, attempting remote fallback...')
                success = await self.load_csv_database_live(CFG.csv_raw_base_url)
        else:
            success = await self.load_csv_database_live(source_url)

        if success:
            async with self._lock:
                self.episodes = self.convert_csv_to_episode_format(self.csv_stories)
            logger.info(f'CSV database ready with {len(self.episodes)} stories')
            search_index.build_story_index(self.episodes)
            search_cache.invalidate()
            return True

        return False

    async def load_char_database(self, source_config: Dict[str, Any]) -> bool:
        """Load character database (local or remote) with proper assignment."""
        fetch_method = source_config.get('fetch_method', 'github_raw')
        source_url = source_config['url']

        temp_char_data: List[dict] = []

        if fetch_method == 'local_file':
            filename = source_config.get('files', [CHAR_CSV_FILENAME])[0]
            filepath = os.path.join(source_url.rstrip('/'), filename)

            if os.path.isfile(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        temp_char_data = self._parse_char_csv_rows(csv.reader(f))
                    logger.info(f'Local character CSV found at {filepath}')
                except Exception as e:
                    logger.error(f'Error reading local file: {e}')
                    temp_char_data = []

        if not temp_char_data and AIOHTTP_AVAILABLE:
            async with self._lock:
                try:
                    session = await self.get_session()
                    if session:
                        async with session.get(CHAR_CSV_RAW_URL) as resp:
                            if resp.status == 200:
                                content = await resp.text(encoding='utf-8')
                                lines = content.splitlines()
                                if lines:
                                    temp_char_data = self._parse_char_csv_rows(csv.reader(lines))
                                    logger.info(f'Fetched {len(temp_char_data)} characters remotely')
                            else:
                                logger.error(f'Failed to fetch character CSV: HTTP {resp.status}')
                except Exception as e:
                    logger.error(f'Error fetching character CSV: {type(e).__name__}: {e}')

        if temp_char_data:
            self.char_data = temp_char_data
            self.char_data_loaded = True
            logger.info(f'Character database ready with {len(self.char_data)} characters')
            search_cache.invalidate()
            return True

        self.char_data = []
        self.char_data_loaded = False
        return False

    def check_csv_source(self) -> Tuple[bool, Optional[Embed]]:
        """Check if CSV source is available."""
        if not self.csv_data_loaded or len(self.csv_stories) == 0:
            embed = Embed(
                title='CSV Not Available',
                description=(
                    'CSV database is not loaded.\n\n'
                    '**Possible causes:**\n'
                    '- Directory not found: `~/Doraemon-CSV-DB/database/`\n'
                    '- No internet connection (for online mode)\n\n'
                    f'Current source: **{self.current_source["name"]}**\n\n'
                    '**Fix:** Clone the repo:\n'
                    '```\ngit clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB\n```'
                ),
                color=CFG.color_error
            )
            return False, embed
        return True, None

    def check_char_source(self) -> Tuple[bool, Optional[Embed]]:
        """Check if character CSV source is available."""
        if not self.char_data_loaded or len(self.char_data) == 0:
            embed = Embed(
                title='Character Database Not Available',
                description=(
                    'Character database is not loaded.\n\n'
                    '**Possible causes:**\n'
                    '- No internet connection\n'
                    '- Repository unavailable\n\n'
                    f'Current source: **{self.current_source["name"]}**\n\n'
                    '**Fix:** Try `/source_change 1` (JSON), `/source_change 2` (Manga CSV), '
                    '`/source_change 3` (Remote Chars), or `/source_change 4` (Local Chars)'
                ),
                color=CFG.color_error
            )
            return False, embed
        return True, None

    async def load_all_with_fallback(self) -> None:
        """Fallback chain: JSON → local CSV → remote CSV → cached data → warning."""
        # Step 1: Try JSON
        success = self.load_json_database()
        if success:
            search_index.build_story_index(self.episodes)
            return

        logger.warning('JSON failed, trying local CSV fallback...')

        # Step 2: Try local CSV
        csv_success = self.load_csv_database_local(CFG.csv_local_path)
        if csv_success:
            async with self._lock:
                self.episodes = self.convert_csv_to_episode_format(self.csv_stories)
                self.current_source = self.available_sources[1].to_dict()
                self.episodes_source = SourceType.CSV
            search_index.build_story_index(self.episodes)
            return

        logger.warning('Local CSV failed, trying remote CSV fallback...')

        # Step 3: Try remote CSV
        if AIOHTTP_AVAILABLE:
            remote_success = await self.load_csv_database_live(CFG.csv_raw_base_url)
            if remote_success:
                async with self._lock:
                    self.episodes = self.convert_csv_to_episode_format(self.csv_stories)
                    self.current_source = self.available_sources[1].to_dict()
                search_index.build_story_index(self.episodes)
                return

        logger.warning('All CSV sources failed. Database will be empty.')

    async def cross_source_search(self, query: str) -> Dict[str, List[dict]]:
        """Search across all loaded sources simultaneously.

        v4.1: Added deduplication by title+date to prevent the same story
        appearing from both JSON and CSV sources.
        """
        results: Dict[str, List[dict]] = {}
        q = query.lower().strip()
        seen_keys: Set[str] = set()

        def _dedup_key(item: dict) -> str:
            """Generate a dedup key from title + publication date."""
            title = (item.get('English title', '') or item.get('story_a', '') or '').lower().strip()
            date = (item.get('Publication Date', '') or '').lower().strip()
            return f"{title}|{date}"

        if self.char_data_loaded and self.char_data:
            char_results = search_characters(q, self.char_data, use_word_boundary=True, use_fuzzy=True)
            if char_results:
                results['characters'] = char_results

        if self.csv_data_loaded and self.csv_stories:
            story_hits = search_stories(q, self.episodes, csv_mode=True)
            if story_hits:
                deduped = []
                for item in story_hits:
                    k = _dedup_key(item)
                    if k not in seen_keys:
                        seen_keys.add(k)
                        deduped.append(item)
                results['manga_stories'] = deduped

        if self.episodes and not self.is_csv_source() and not self.is_char_source():
            json_hits = search_stories(q, self.episodes, csv_mode=False)
            if json_hits:
                deduped = []
                for item in json_hits:
                    k = _dedup_key(item)
                    if k not in seen_keys:
                        seen_keys.add(k)
                        deduped.append(item)
                results['episodes'] = deduped

        return results

# ============================================
# EMBED HELPERS WITH SAFETY CHECKS (Shared)
# ============================================

def truncate(text: str, max_len: int) -> str:
    """Safely truncate text to fit Discord limits."""
    if text and len(text) > max_len:
        return text[:max_len - 1] + '…'
    return text or ZERO_WIDTH_SPACE

def calculate_embed_space(embed: Embed) -> int:
    """Calculate remaining space in embed.

    v4.1: Now includes title and description length in the calculation
    (Discord's 6000-char limit covers the entire embed, not just fields).
    """
    used = len(embed.title or '') + len(embed.description or '')
    for f in embed.fields:
        used += len(f.name) + len(f.value)
    return CFG.embed_total_max - used

def safe_add_field(embed: Embed, *, name: str, value: str = '', inline: bool = False) -> bool:
    """Add a field to an embed with space checking. Returns False if field was skipped.

    v4.1: Embed total length now accounts for title + description + all fields.
    """
    if len(embed.fields) >= CFG.embed_field_max_count:
        return False

    remaining_space = calculate_embed_space(embed)
    field_size = len(name) + len(value)

    if remaining_space < field_size + SAFE_ADD_FIELD_BUFFER:
        logger.warning(f'Embed space insufficient: {remaining_space} < {field_size + SAFE_ADD_FIELD_BUFFER}')
        return False

    name = truncate(name or ZERO_WIDTH_SPACE, CFG.embed_field_name_max)
    value = value if value else ZERO_WIDTH_SPACE
    value = truncate(value, CFG.embed_field_value_max)

    embed.add_field(name=name, value=value, inline=inline)
    return True

def safe_display(value: str) -> str:
    """Sanitize a value for safe display in Discord embeds (prevents CSV injection if exported)."""
    return sanitize_csv_cell(value) if value else ''

def build_csv_result_fields(embed: Embed, story: dict) -> None:
    """Populate an embed with standard CSV story fields."""
    mag = story.get('Magazine code', '') or 'N/A'
    pub_date = story.get('Publication Date', '') or 'N/A'
    jp_title = story.get('Japanese title', '') or 'N/A'
    anime_1979 = story.get('1979 anime', '') or 'None'
    anime_2005 = story.get('2005 anime', '') or 'None'
    tankobon = story.get('Tankōbon', '') or 'None'
    complete = story.get('Complete Collection', '') or 'None'
    kindle = story.get('English Kindle', '') or 'None'
    movie = story.get('Movie', '') or ''
    short_film = story.get('Short film', '') or ''
    notes = story.get('Notes', '') or ''

    safe_add_field(embed, name='Magazine Code', value=mag, inline=True)
    safe_add_field(embed, name='Publication Date', value=pub_date, inline=True)
    safe_add_field(embed, name='Japanese Title', value=truncate(jp_title, CFG.embed_field_value_max), inline=False)
    safe_add_field(embed, name='1979 Anime', value=truncate(anime_1979, CFG.embed_field_value_max), inline=True)
    safe_add_field(embed, name='2005 Anime', value=truncate(anime_2005, CFG.embed_field_value_max), inline=True)
    safe_add_field(embed, name='Tankōbon', value=tankobon, inline=True)
    safe_add_field(embed, name='Complete Collection', value=complete, inline=True)
    safe_add_field(embed, name='English Kindle', value=kindle, inline=True)

    extras: List[str] = []
    if movie:
        extras.append(f'**Movie:** {movie}')
    if short_film:
        extras.append(f'**Short Film:** {short_film}')
    if notes:
        extras.append(f'**Notes:** {truncate(notes, 200)}')
    if extras:
        safe_add_field(embed, name='Additional Info', value='\n'.join(extras), inline=False)

def build_more_matches_field(embed: Embed, results: List[dict], is_csv: bool, max_shown: int = 5) -> None:
    """Add a 'More Matches' field listing additional results."""
    count = min(len(results) - 1, max_shown)
    if count <= 0:
        return

    lines: List[str] = []
    for r in results[1:max_shown + 1]:
        if is_csv:
            t = r.get('English title', r.get('story_a', '?'))[:40]
            m = r.get('Magazine code', '?')
            d = r.get('Publication Date', '?')
            lines.append(f'- [{m} {d}] {t}')
        elif r.get('Category') and r.get('Character Name'):
            lines.append(f'- {r.get("Character Name", "?")} [{r.get("Category", "")}]')
        else:
            ep = r.get('in_season_episode', '?')
            title = r.get('story_a', '?')[:40]
            lines.append(f'- {ep}: {title}')

    safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value='\n'.join(lines), inline=False)

def build_char_more_matches_field(embed: Embed, results: List[dict], max_shown: int = 5) -> None:
    """Add a 'More Matches' field specifically for character results."""
    count = min(len(results) - 1, max_shown)
    if count <= 0:
        return

    lines: List[str] = []
    for r in results[1:max_shown + 1]:
        name = r.get('Character Name', '?')
        cat = r.get('Category', '')
        lines.append(f'- {name} [{cat}]')

    safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value='\n'.join(lines), inline=False)

# ============================================
# SHARED EMBED BUILDER (DRY)
# ============================================

def build_list_item_field(embed: Embed, record: dict, source_type: str) -> None:
    """Add a single list item as an embed field based on source type (shared logic)."""
    if source_type == SourceType.CSV_CHAR.value:
        name = record.get('Character Name', '?') or '?'
        alt = record.get('Alternative Name', '') or ''
        cat = record.get('Category', '?') or '?'
        field_name = f'{name} [{cat}]'
        field_value = truncate(record.get('Description', '') or '', 50)
        if alt:
            field_value += f'\n*Alt Name:* {alt}'
        safe_add_field(embed, name=field_name, value=field_value, inline=False)

    elif source_type == SourceType.CSV.value:
        mag = record.get('Magazine code', '?') or '?'
        pub_date = record.get('Publication Date', '?') or '?'
        title_entry = record.get('English title', '') or record.get('Japanese title', 'Unknown') or 'Unknown'
        jp_title = record.get('Japanese title', '') or ''
        anime_1979 = record.get('1979 anime', '') or ''
        anime_2005 = record.get('2005 anime', '') or ''
        field_name = f'[{mag}] {pub_date} | {truncate(title_entry, 40)}'
        field_value = jp_title[:50]
        if anime_1979:
            field_value += f' | 1979: {anime_1979}'
        if anime_2005:
            field_value += f' | 2005: {anime_2005}'
        safe_add_field(embed, name=field_name, value=field_value, inline=False)

    else:
        in_ep = record.get('in_season_episode', 'N/A') or 'N/A'
        jp_st = record.get('jp_story_all', '-') or '-'
        story_a = record.get('story_a', 'Unknown') or 'Unknown'
        story_b = record.get('story_b', '') or ''
        title_display = story_a[:40]
        if story_b:
            title_display += f' / {truncate(story_b, 40)}'
        safe_add_field(embed, name=f'{in_ep} | JP: {jp_st}', value=title_display, inline=False)

def build_list_embed(data: List[dict], page: int, page_size: int, source_type: str,
                     filter_val: Optional[str], source_name: str) -> Embed:
    """Build a paginated list embed (shared by list_cmd and ListNavigationView)."""
    total_pages = max(1, (len(data) + page_size - 1) // page_size if data else 1)
    start = (page - 1) * page_size
    end = min(start + page_size, len(data))
    chunk = data[start:end]
    filter_display = filter_val if filter_val else 'All'

    if source_type == SourceType.CSV_CHAR.value:
        title = f'Characters - Page {page} of {total_pages} ({filter_display})'
    elif source_type == SourceType.CSV.value:
        title = f'Manga Stories - Page {page} of {total_pages} ({filter_display})'
    else:
        title = f'Doraemon Episodes - Page {page} of {total_pages} ({filter_display})'

    embed = Embed(title=title, color=CFG.color_primary)
    embed.description = f'Total: **{len(data)}** | Showing {start + 1}-{end}'
    embed.timestamp = utc_now()

    for r in chunk:
        build_list_item_field(embed, r, source_type)

    progress = make_progress_bar(page, total_pages)
    footer = f'{progress} Page {page} of {total_pages} | Source: {source_name}'
    embed.set_footer(text=footer)

    return embed

# ============================================
# JUMP-TO-PAGE MODAL
# ============================================

class JumpToPageModal(ui.Modal, title='Jump to Page'):
    """Modal for entering a page number to jump to.

    v4.1: Page validation improved — negative values and out-of-range
    values are clamped with user feedback.
    """

    page_input = ui.TextInput(
        label='Enter page number',
        placeholder='e.g. 5',
        required=True,
        min_length=1,
        max_length=4
    )

    def __init__(self, view: 'ListNavigationView'):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: Interaction) -> None:
        try:
            page = int(self.page_input.value)
        except ValueError:
            await interaction.response.send_message(
                'Please enter a valid number.', ephemeral=True
            )
            return

        total = self.view.total_pages
        if page < 1:
            page = 1
        elif page > total:
            page = total

        self.view.page = page
        await self.view.update_message(interaction)

# ============================================
# PAGINATION VIEW WITH BUTTONS
# ============================================

class ListNavigationView(ui.View):
    """Interactive pagination view with Prev/Next/First/Last/Jump buttons.

    v4.1: Custom IDs now include channel_id and message_id to prevent
    collisions when multiple pagination views exist simultaneously.
    Timeout pulled from config (CFG.view_timeout_seconds).
    Ownership checks prevent other users from using the paginator.
    """

    def __init__(self, data: List[dict], page: int, page_size: int,
                 source_type: str, filter_val: Optional[str], dm_ref,
                 channel_id: int = 0, message_id: Optional[int] = None):
        super().__init__(timeout=CFG.view_timeout_seconds)
        self.data = data
        self.page = page
        self.page_size = page_size
        self.source_type = source_type
        self.filter_val = filter_val
        self.dm_ref = dm_ref
        self.channel_id = channel_id
        self.message_id = message_id
        self.original_user_id = None
        self.message = None

        self.total_pages = max(1, (len(data) + page_size - 1) // page_size if data else 1)

        self._update_button_states()

    def _update_button_states(self) -> None:
        self.prev_button.disabled = self.page <= 1
        self.first_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.total_pages
        self.last_button.disabled = self.page >= self.total_pages

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, 'message') and self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

    def _build_embed(self) -> Embed:
        return build_list_embed(
            self.data, self.page, self.page_size,
            self.source_type, self.filter_val,
            self.dm_ref.current_source['name']
        )

    async def update_message(self, interaction: Interaction) -> None:
        if hasattr(self, 'original_user_id') and self.original_user_id is not None:
            if interaction.user.id != self.original_user_id:
                await interaction.response.send_message(
                    'This paginator belongs to another user.', ephemeral=True
                )
                return
        self._update_button_states()
        embed = self._build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        metrics.record_command('list_page_navigation')

    @ui.button(label='⏮ First', style=ButtonStyle.primary)
    async def first_button(self, interaction: Interaction, button: ui.Button):
        if self.page > 1:
            self.page = 1
            await self.update_message(interaction)

    @ui.button(label='◀ Prev', style=ButtonStyle.secondary)
    async def prev_button(self, interaction: Interaction, button: ui.Button):
        if self.page > 1:
            self.page -= 1
            await self.update_message(interaction)

    @ui.button(label='Next ▶', style=ButtonStyle.secondary)
    async def next_button(self, interaction: Interaction, button: ui.Button):
        if self.page < self.total_pages:
            self.page += 1
            await self.update_message(interaction)

    @ui.button(label='Last ⏭', style=ButtonStyle.primary)
    async def last_button(self, interaction: Interaction, button: ui.Button):
        if self.page < self.total_pages:
            self.page = self.total_pages
            await self.update_message(interaction)

    @ui.button(label='🔢 Jump', style=ButtonStyle.success)
    async def jump_button(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_modal(JumpToPageModal(self))

# ============================================
# MULTI-RESULT SELECT MENU
# ============================================

class ResultSelectMenu(ui.Select):
    """Dropdown select menu for expanding multiple search results."""

    def __init__(self, results: List[dict], source_type: str):
        self.results = results
        self.source_type = source_type

        options = []
        for i, r in enumerate(results[:25]):
            if source_type == SourceType.CSV_CHAR.value:
                label = r.get('Character Name', f'Result {i+1}') or f'Result {i+1}'
                desc_text = r.get('Category', '') or ''
            elif source_type == SourceType.CSV.value:
                label = (r.get('English title', '') or r.get('story_a', f'Result {i+1}'))[:100]
                desc_text = f"[{r.get('Magazine code', '?')}] {r.get('Publication Date', '?')}"
            else:
                label = (r.get('story_a', f'Result {i+1}'))[:100]
                desc_text = r.get('in_season_episode', '?') or '?'

            options.append(discord.SelectOption(
                label=truncate(label, 100),
                description=truncate(desc_text, 100),
                value=str(i)
            ))

        super().__init__(
            placeholder='Select a result to view details...',
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: Interaction) -> None:
        view = self.view
        if hasattr(view, 'original_user_id') and view.original_user_id is not None:
            if interaction.user.id != view.original_user_id:
                await interaction.response.send_message(
                    'This menu belongs to another user.', ephemeral=True
                )
                return
        idx = int(self.values[0])
        result = self.results[idx]

        if self.source_type == SourceType.CSV_CHAR.value:
            embed = Embed(
                title=f'Character: {truncate(result.get("Character Name", "Unknown") or "Unknown", 70)}',
                color=CFG.color_primary
            )
            embed.add_field(name='Alternative Name', value=result.get('Alternative Name', '') or 'N/A', inline=True)
            embed.add_field(name='Category', value=result.get('Category', 'Unknown') or 'Unknown', inline=True)
            safe_add_field(embed, name='Description', value=truncate(result.get('Description', '') or '', CFG.embed_field_value_max), inline=False)
            refs = result.get('Source References', '') or ''
            if refs:
                safe_add_field(embed, name='Source References', value=truncate(refs, CFG.embed_field_value_max), inline=False)
            embed.timestamp = utc_now()

        elif self.source_type == SourceType.CSV.value:
            title = result.get('English title', '') or result.get('Japanese title', 'Unknown') or 'Unknown'
            embed = Embed(title=f'Found: {truncate(title, 70)}', color=CFG.color_primary)
            embed.description = f'**Category:** Manga Stories'
            build_csv_result_fields(embed, result)
            embed.timestamp = utc_now()

        else:
            in_ep = result.get('in_season_episode', 'N/A') or 'N/A'
            jp_display = result.get('jp_story_all', 'Not indexed') or 'Not indexed'
            story_a = result.get('story_a', 'Unknown') or 'Unknown'
            story_b = result.get('story_b', '') or ''
            category = (result.get('category', 'unknown') or 'unknown').replace('_', ' ').title()
            title = (result.get('title', 'Unknown') or 'Unknown')[:70]

            embed = Embed(title=f'Episode Found: {title}', color=CFG.color_primary)
            embed.description = f'**Category:** {category}'
            embed.add_field(name='Indian Episode', value=in_ep, inline=True)
            embed.add_field(name='Japanese Story #', value=jp_display, inline=True)
            safe_add_field(embed, name='Story A', value=truncate(story_a, CFG.embed_field_value_max), inline=False)
            if story_b:
                safe_add_field(embed, name='Story B', value=truncate(story_b, CFG.embed_field_value_max), inline=False)
            embed.timestamp = utc_now()

        embed.set_footer(text=f'Source: {dm.current_source["name"]}')
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ResultSelectView(ui.View):
    """View containing the result select menu.

    v4.1: Timeout pulled from config. Ownership tracking added.
    """

    def __init__(self, results: List[dict], source_type: str, original_user_id: Optional[int] = None):
        super().__init__(timeout=CFG.view_timeout_seconds)
        self.original_user_id = original_user_id
        self.message = None
        self.add_item(ResultSelectMenu(results, source_type))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, 'message') and self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

# ============================================
# BOT SETUP
# ============================================

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print('ERROR: DISCORD_TOKEN environment variable not set')
    print('Create a .env file with: DISCORD_TOKEN=your_token_here')
    exit(1)

if not validate_discord_token(TOKEN):
    print('ERROR: DISCORD_TOKEN appears invalid (too short or malformed)')
    exit(1)

if os.getenv('GITHUB_ACTIONS') == 'true':
    print('Running in GitHub Actions - Bot startup skipped')
    exit(0)

# ============================================
# GLOBAL INITIALIZATION (Top-level, before any usage)
# ============================================

CFG = BotConfig()
logger = setup_logging(CFG)

# Validate config
config_warnings = CFG.validate()
for warning in config_warnings:
    logger.warning(f'Config validation: {warning}')

metrics = MetricsCollector()
rate_limiter = RateLimiter(
    default_limit=CFG.rate_per_user,
    guild_limit=CFG.rate_per_guild,
    window_seconds=CFG.rate_window_seconds
)
search_cache = SearchCache(ttl=CFG.search_cache_ttl, max_size=CFG.cache_max_size)
search_index = SearchIndex()
dm = DatabaseManager()

bot = commands.Bot(command_prefix='!', intents=Intents.default())

# on_ready guard
_ready_flag = False

# v4.1: Shutdown flag for async-safe cleanup (set in signal handler, consumed by event loop)
_shutdown_event = asyncio.Event()

# ============================================
# GRACEFUL SHUTDOWN (v4.1: Async-safe signal handling)
# ============================================

def graceful_shutdown(signum, frame):
    """Handle SIGTERM/SIGINT for clean exits.

    v4.1: No longer calls asyncio.run_until_complete() inside the signal
    handler (which is unsafe and deprecated). Instead sets a flag and
    schedules cleanup on the event loop. Uses os._exit as last resort.
    """
    logger.info(f'Received signal {signum}, initiating graceful shutdown...')

    # Try to schedule cleanup on the running event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Schedule the async cleanup task
            loop.call_soon_threadsafe(_schedule_async_cleanup, signum)
        else:
            # Loop not running, do synchronous cleanup
            try:
                if auto_refresh_task.is_running():
                    auto_refresh_task.cancel()
                    logger.info('Auto-refresh task cancelled.')
            except Exception:
                pass
            loop.run_until_complete(dm.close_session())
            logger.info('Graceful shutdown complete. Goodbye!')
            sys.exit(0)
    except Exception as e:
        logger.warning(f'Async cleanup scheduling failed ({e}), forcing exit.')
        sys.exit(0)

def _schedule_async_cleanup(signum: int) -> None:
    """Schedule the async cleanup coroutine on the running event loop."""
    asyncio.ensure_future(_async_shutdown())

async def _async_shutdown() -> None:
    """Perform async cleanup: cancel background task, close session, close bot, then exit."""
    logger.info('Starting async shutdown sequence...')
    try:
        if auto_refresh_task.is_running():
            auto_refresh_task.cancel()
            logger.info('Auto-refresh task cancelled.')
            try:
                await asyncio.wait_for(auto_refresh_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
    except Exception:
        pass

    try:
        await dm.close_session()
        logger.info('aiohttp session closed.')
    except Exception:
        pass

    try:
        search_cache.close()
        logger.info('SQLite cache connection closed.')
    except Exception:
        pass

    try:
        await bot.close()
        logger.info('Bot gateway closed.')
    except Exception:
        pass

    logger.info('Graceful shutdown complete. Goodbye!')
    os._exit(0)

signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)

# ============================================
# AUTOCOMPLETE FUNCTIONS
# ============================================

async def autocomplete_character_names(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """Autocomplete character names as the user types."""
    if not dm.char_data_loaded:
        return []
    q = current.lower().strip()
    if not q:
        # Return first 25 alphabetically
        names = sorted([c.get('Character Name', '') or '' for c in dm.char_data])[:25]
    else:
        names = [
            c.get('Character Name', '') or ''
            for c in dm.char_data
            if q in (c.get('Character Name', '') or '').lower()
        ][:25]
    return [app_commands.Choice(name=n, value=n) for n in names if n]

async def autocomplete_magazine_codes(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """Autocomplete magazine codes."""
    q = current.upper().strip()
    codes = sorted(KNOWN_MAGAZINES)
    if q:
        codes = [c for c in codes if q in c]
    return [app_commands.Choice(name=c, value=c) for c in codes[:25]]

async def autocomplete_categories(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """Autocomplete character categories."""
    if not dm.char_data_loaded:
        return []
    q = current.lower().strip()
    cats = set((c.get('Category', '') or '') for c in dm.char_data if c.get('Category'))
    cats = sorted(cats)
    if q:
        cats = [c for c in cats if q in c.lower()]
    return [app_commands.Choice(name=c, value=c) for c in cats[:25]]

# ============================================
# MODE-AWARE SEARCH AUTOCOMPLETE
# ============================================

async def autocomplete_search(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """Mode-aware autocomplete for /search."""
    q = current.lower().strip()

    if dm.is_char_source() and dm.char_data_loaded:
        names = [
            c.get('Character Name', '') or ''
            for c in dm.char_data
            if q in (c.get('Character Name', '') or '').lower()
        ][:25]
        return [app_commands.Choice(name=n, value=n) for n in names if n]

    elif dm.is_csv_source() and dm.csv_data_loaded:
        titles = [
            s.get('English title', '') or s.get('Japanese title', '') or ''
            for s in dm.csv_stories
            if q in (s.get('English title', '') or s.get('Japanese title', '') or '').lower()
        ][:25]
        return [app_commands.Choice(name=t, value=t) for t in titles if t]

    elif dm.episodes:
        titles = [
            ep.get('story_a', '') or ep.get('title', '') or ''
            for ep in dm.episodes
            if q in (ep.get('story_a', '') or ep.get('title', '') or '').lower()
        ][:25]
        return [app_commands.Choice(name=t, value=t) for t in titles if t]

    return []

# ============================================
# SOURCE MANAGEMENT COMMANDS
# ============================================

@bot.tree.command(name='source', description='Shows the current database source')
@rate_limited('source')
async def source_cmd(interaction: discord.Interaction):
    embed = Embed(title='Current Database Source', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.add_field(name='Source Name', value=dm.current_source['name'], inline=True)
    embed.add_field(name='Type', value=dm.current_source['type'].title(), inline=True)

    url_display = dm.current_source['url'][:60] + '...' if len(dm.current_source['url']) > 60 else dm.current_source['url']
    if interaction.user.id != 1236713171142840403:
        url_display = '`Hidden — Admin only`'
    embed.add_field(name='URL/Path', value=url_display, inline=False)

    if dm.current_source['type'] == SourceType.CSV.value:
        status = 'Loaded' if dm.csv_data_loaded else 'Not loaded'
        embed.add_field(name='Status', value=status, inline=True)
        embed.add_field(name='Records', value=str(len(dm.csv_stories)), inline=True)
    elif dm.current_source['type'] == SourceType.CSV_CHAR.value:
        status = 'Loaded' if dm.char_data_loaded else 'Not loaded'
        embed.add_field(name='Status', value=status, inline=True)
        embed.add_field(name='Records', value=str(len(dm.char_data)), inline=True)
    elif dm.current_source['type'] == SourceType.JSON.value:
        embed.add_field(name='Total Episodes', value=str(len(dm.episodes)), inline=True)

    embed.set_footer(text=f'Source ID: {dm.current_source["id"]} | Switch with /source_change <id>')
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_all', description='Lists all available database sources')
@rate_limited('source_all')
async def source_all_cmd(interaction: discord.Interaction):
    embed = Embed(title='Available Database Sources', color=CFG.color_primary)
    embed.timestamp = utc_now()

    description = ''
    for src in dm.available_sources:
        sd = src.to_dict()
        desc = f'**{sd["id"]}) {sd["name"]}**\n'
        desc += f'> Type: {sd["type"].title()} | Method: {sd.get("fetch_method", "default").title()}\n'
        if sd.get('files'):
            desc += f'> Files: {", ".join(sd["files"])}\n'
        if sd['id'] == dm.current_source['id']:
            desc += '> **Currently Active**\n'
        description += desc + '\n'

    embed.description = description
    safe_add_field(embed, name='How to switch',
                   value='Use `/source_change <number>` to switch\nExample: `/source_change 2`', inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_status', description='Shows detailed status of all database sources')
@rate_limited('source_status')
async def source_status_cmd(interaction: discord.Interaction):
    embed = Embed(title='Database Sources Status', color=CFG.color_primary)
    embed.timestamp = utc_now()

    for src in dm.available_sources:
        sd = src.to_dict()
        status_icon = '🟢' if sd['id'] == dm.current_source['id'] else '⚪'

        desc = f'{status_icon} **{sd["id"]}) {sd["name"]}**\n'
        desc += f'> Type: `{sd["type"]}` | Fetch: `{sd.get("fetch_method", "default")}`\n'

        if sd['type'] == SourceType.JSON.value and dm.episodes:
            desc += f'> **Records:** {len(dm.episodes)} ✓ Loaded\n'
        elif sd['type'] == SourceType.CSV.value and dm.csv_data_loaded:
            desc += f'> **Records:** {len(dm.csv_stories)} ✓ Loaded\n'
        elif sd['type'] == SourceType.CSV_CHAR.value and dm.char_data_loaded:
            if sd['id'] == 3:
                desc += f'> **Records:** {len(dm.char_data)} ✓ Remote Loaded\n'
            elif sd['id'] == 4:
                desc += f'> **Records:** {len(dm.char_data)} ✓ Local Loaded\n'
        else:
            desc += '> ⏳ Not loaded (switch to enable)\n'

        if sd['id'] == dm.current_source['id']:
            desc += '> **← Currently Active**\n'

        embed.add_field(name=ZERO_WIDTH_SPACE, value=desc, inline=False)

    safe_add_field(embed, name='Quick Reference',
                   value='`/source_change <id>` to switch\n`/source_status` refreshes status',
                   inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_change', description='Changes the active database source')
@app_commands.describe(source_id='Source number to switch to (1=JSON, 2=CSV, 3=Remote Chars, 4=Local Chars)')
@admin_only()
@rate_limited('source_change', limit=3)
async def source_change_cmd(interaction: discord.Interaction, source_id: int):
    source = next((s for s in dm.available_sources if s.id == source_id), None)

    if not source:
        embed = Embed(title='Invalid Source',
                      description=f'Source ID {source_id} not found.\nUse `/source_all` to see available sources.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    sd = source.to_dict()
    old_source = dm.current_source.copy()

    try:
        if source.source_type == SourceType.CSV:
            success = await dm.load_csv_database(sd)
            if not success:
                embed = Embed(
                    title='Warning',
                    description=(
                        'Could not load CSV data.\n\n'
                        '**Troubleshooting:**\n'
                        '- Verify folder exists: `~/Doraemon-CSV-DB/database/`\n'
                        '- No internet connection (for online mode)\n\n'
                        f'Current source: **{dm.current_source["name"]}**\n\n'
                        '**Fix:** Clone the repo:\n'
                        '```\ngit clone https://github.com/star884/Doraemon-CSV-DB\n```'
                    ),
                    color=CFG.color_warning
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                await dm.safe_update_source(old_source)
                return
            await dm.safe_update_source(sd)
        elif source.source_type == SourceType.CSV_CHAR:
            success = await dm.load_char_database(sd)
            if not success:
                embed = Embed(
                    title='Warning',
                    description=(
                        'Could not load character data.\n\n'
                        '**Troubleshooting:**\n'
                        '- For remote (id 3): Check internet connection\n'
                        '- For local (id 4): Clone repo - `git clone https://github.com/star884/Database-dorav2.git ~`\n'
                        '- Try `/source_change 1` (JSON) or `/source_change 2` (Manga CSV)'
                    ),
                    color=CFG.color_warning
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                await dm.safe_update_source(old_source)
                return
            await dm.safe_update_source(sd)
        elif source.source_type == SourceType.JSON:
            success = dm.load_json_database()
            if not success:
                embed = Embed(title='Warning',
                              description='Could not load JSON data.',
                              color=CFG.color_warning)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                await dm.safe_update_source(old_source)
                return
            await dm.safe_update_source(sd)

        record_count = (len(dm.char_data) if source.source_type == SourceType.CSV_CHAR
                        else (len(dm.csv_stories) if source.source_type == SourceType.CSV
                              else len(dm.episodes)))
        embed = Embed(title='Source Changed Successfully', color=CFG.color_primary)
        embed.timestamp = utc_now()
        embed.add_field(name='Previous Source', value=old_source['name'], inline=False)
        embed.add_field(name='New Source', value=sd['name'], inline=False)
        embed.add_field(name='Records Loaded', value=str(record_count), inline=True)
        embed.set_footer(text='Use /source_status to view status')
        await interaction.response.send_message(embed=embed)

    except Exception as e:
        logger.error(f'Error during source change: {e}')
        await dm.safe_update_source(old_source)
        embed = Embed(title='Error',
                      description='An error occurred while switching sources.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name='reload', description='Force reload the current database source (Admin only)')
@admin_only()
@rate_limited('reload', limit=2)
async def reload_cmd(interaction: discord.Interaction):
    """Force reload the current database source."""
    sd = dm.current_source
    old_source = sd.copy()

    try:
        if sd['type'] == SourceType.CSV.value:
            success = await dm.load_csv_database(sd)
        elif sd['type'] == SourceType.CSV_CHAR.value:
            success = await dm.load_char_database(sd)
        else:
            success = dm.load_json_database()

        if success:
            embed = Embed(title='Reload Complete', color=CFG.color_success,
                          description=f'Successfully reloaded **{sd["name"]}**')
            embed.timestamp = utc_now()
            await interaction.response.send_message(embed=embed)
        else:
            embed = Embed(title='Reload Failed', color=CFG.color_error,
                          description=f'Failed to reload **{sd["name"]}**')
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f'Error during reload: {e}')
        embed = Embed(title='Error', description='An error occurred during reload.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================
# HEALTH CHECK COMMAND
# ============================================

@bot.tree.command(name='health', description='Show bot health diagnostics')
@rate_limited('health')
async def health_cmd(interaction: discord.Interaction):
    embed = Embed(title='Bot Health Check', color=CFG.color_primary)
    embed.timestamp = utc_now()

    embed.add_field(name='Uptime', value=metrics.get_uptime(), inline=True)
    embed.add_field(name='Cache Size', value=f'{search_cache.get_size()} entries', inline=True)
    embed.add_field(name='Last DB Load', value=metrics.last_db_load_time or 'N/A', inline=True)

    if dm.episodes_source == SourceType.CSV:
        episode_label = 'Episodes (from CSV)'
    elif dm.episodes_source == SourceType.JSON:
        episode_label = 'JSON Episodes'
    else:
        episode_label = 'Episodes'
    embed.add_field(name=episode_label, value=str(len(dm.episodes)), inline=True)
    embed.add_field(name='CSV Stories', value=str(len(dm.csv_stories)), inline=True)
    embed.add_field(name='Characters', value=str(len(dm.char_data)), inline=True)

    # Latency
    latency_ms = round(bot.latency * 1000) if bot.latency != float('inf') else -1
    embed.add_field(name='Gateway Latency', value=f'{latency_ms}ms', inline=True)

    # Memory usage (best-effort)
    try:
        import resource
        mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        embed.add_field(name='Memory (RSS)', value=f'{mem_kb / 1024:.1f} MB', inline=True)
    except Exception:
        pass

    error_summary = metrics.get_error_summary()
    error_count = sum(error_summary.values())
    embed.add_field(name='Total Errors', value=str(error_count), inline=True)

    if error_summary:
        top_errors = sorted(error_summary.items(), key=lambda x: -x[1])[:5]
        error_lines = [f'{etype}: {cnt}' for etype, cnt in top_errors]
        safe_add_field(embed, name='Top Errors', value='\n'.join(error_lines), inline=False)

    health_status = '✅ Healthy' if error_count < 10 and latency_ms >= 0 else '⚠️ Issues Detected'
    embed.description = f'**Status:** {health_status}'

    embed.set_footer(text=f'Bot v{BOT_VERSION} | Source: {dm.current_source["name"]}')
    await interaction.response.send_message(embed=embed)

# ============================================
# SEARCH COMMAND
# ============================================

@bot.tree.command(name='search', description='Search by title, episode, magazine code, or character name')
@app_commands.describe(query='Search term (e.g., nobita, Ep 7, YK, 1970-01)')
@app_commands.autocomplete(query=autocomplete_search)
@rate_limited('search')
async def search_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    coro_id = generate_correlation_id()
    q_raw = sanitize_query(query, CFG.max_query_length)
    if not q_raw:
        embed = Embed(title='Empty Query',
                      description='Please provide a search term.',
                      color=CFG.color_error)
        await interaction.followup.send(embed=embed)
        return

    if not dm.episodes and not dm.char_data:
        embed = Embed(
            title='Database Empty',
            description='No episodes loaded. Use `/source` to check status.',
            color=CFG.color_error
        )
        await interaction.followup.send(embed=embed)
        return

    q = q_raw.lower()
    csv_mode = dm.is_csv_source()
    char_mode = dm.is_char_source()
    source_type = dm.current_source['type']

    cached = search_cache.get(source_type, q)
    cache_hit = cached is not None
    if cache_hit:
        logger.info(f'[{coro_id}] Cache hit for query "{q_raw}" (source: {source_type})')
        metrics.record_search(source_type, True)
        results = cached
    else:
        results: List[dict] = []
        if char_mode:
            scored_results = search_characters_with_scores(q, dm.char_data, use_word_boundary=True, use_fuzzy=True)
            results = [c for _, c in scored_results]
        elif csv_mode:
            if len(q) <= 4 and q.upper() in KNOWN_MAGAZINES:
                results = search_index.get_magazine_bucket(q.upper())
            elif q.startswith('ep ') or (q.startswith('ep') and len(q) > 2 and q[2:3].isdigit()):
                ep_num = q.replace('ep ', '').replace('ep', '').strip()
                for ep in dm.episodes:
                    if ep_num in ep.get('1979 anime', '').lower():
                        results.append(ep)
            elif q.startswith('2005') and len(q) > 5:
                ep_num = q[5:].strip()
                for ep in dm.episodes:
                    if ep_num in ep.get('2005 anime', '').lower():
                        results.append(ep)
            elif re.match(r'^\d{4}-\d{2}$', q):
                results = search_index.get_date_bucket(q)
            if not results:
                results = search_stories(q, dm.episodes, csv_mode=True)
        else:
            if q.startswith('jp:') or q.isdigit() or re.match(r'^s\d+\s*s?$', q, re.I):
                jp_query = q.replace('jp:', '').strip()
                standalone_match = re.match(r'^s(\d+)$', jp_query, re.I)
                reg_special_match = re.match(r'^(\d+)\s*s$', jp_query, re.I)

                for ep in dm.episodes:
                    for jp in ep.get('jp_parsed', []):
                        if standalone_match:
                            if jp.get('is_standalone_special') and str(jp.get('number')) == standalone_match.group(1):
                                results.append(ep)
                                break
                        elif reg_special_match:
                            target_num = reg_special_match.group(1)
                            if (not jp.get('is_standalone_special') and
                                    jp.get('is_special') and
                                    str(jp.get('number')) == target_num):
                                results.append(ep)
                                break
                        elif jp_query.isdigit():
                            if str(jp.get('number')) == jp_query:
                                results.append(ep)
                                break
            elif re.match(r'^s\d{2}e\d{2}$', q):
                results = [e for e in dm.episodes if e.get('in_season_episode', '').lower() == q]
            elif q.startswith('spe'):
                spe_num = re.sub(r'[^0-9]', '', q.replace('spe', ''))
                results = [e for e in dm.episodes
                           if e.get('in_season_episode', '').upper().startswith('SPE')
                           and spe_num in e.get('in_season_episode', '')]
            elif q.startswith('ce') or q.startswith('classic'):
                ce_num = re.sub(r'[^0-9]', '', q.replace('ce', '').replace('classic', ''))
                results = [e for e in dm.episodes
                           if e.get('in_season_episode', '').upper().startswith('CE')
                           and ce_num in e.get('in_season_episode', '')]
            if not results:
                results = search_stories(q, dm.episodes, csv_mode=False)

        search_cache.set(source_type, q, results)
        metrics.record_search(source_type, False)

    if not results:
        tips_csv = (
            '**CSV Search Formats:**\n'
            '- Title: `nobita` or `doraemon`\n'
            '- Magazine code: `YK`, `G1`, `KG`, `G4`\n'
            '- 1979 anime: `Ep 7` or `ep7`\n'
            '- 2005 anime: `2005 16A`\n'
            '- Publication date: `1970-01`'
        )
        tips_char = (
            '**Character Search Formats:**\n'
            '- Name: `nobita`, `shizuka`, `giant`\n'
            '- Category: `human`, `robot`, `animal`\n'
            '- Any keyword from description\n\n'
            '*Word-boundary matching for names, fuzzy matching as fallback.*'
        )
        tips_json = (
            '**JSON Search Formats:**\n'
            '- JP Story #: `150` or `jp:150`\n'
            '- JP Special: `150 S` or `S16`\n'
            '- IN Episode: `s04e35`\n'
            '- Title: `nobita`'
        )

        embed = Embed(
            title='No Results Found',
            description=f'No results found for "**{q_raw}**"\n\n'
                        + (tips_char if char_mode else (tips_csv if csv_mode else tips_json))
                        + f'\n\n**Current Source:** {dm.current_source["name"]}',
            color=CFG.color_error
        )
        embed.timestamp = utc_now()
        await interaction.followup.send(embed=embed)
        return

    first = results[0]

    if char_mode:
        name = first.get('Character Name', 'Unknown') or 'Unknown'
        alt = first.get('Alternative Name', '') or ''
        cat = first.get('Category', 'Unknown') or 'Unknown'
        desc = first.get('Description', '') or ''
        refs = first.get('Source References', '') or ''

        embed = Embed(title=f'Character Found: {truncate(name, 70)}', color=CFG.color_primary)
        embed.timestamp = utc_now()

        embed.add_field(name='Alternative Name', value=alt if alt else 'N/A', inline=True)
        embed.add_field(name='Category', value=cat, inline=True)
        safe_add_field(embed, name='Description', value=truncate(desc, CFG.embed_field_value_max), inline=False)
        if refs:
            safe_add_field(embed, name='Source References',
                           value=truncate(refs, CFG.embed_field_value_max), inline=False)

        if len(results) > 1:
            build_char_more_matches_field(embed, results)

    elif csv_mode:
        title = first.get('English title', '') or first.get('Japanese title', 'Unknown') or 'Unknown'
        embed = Embed(title=f'Found: {truncate(title, 70)}', color=CFG.color_primary)
        embed.timestamp = utc_now()
        embed.description = f'**Category:** Manga Stories'
        build_csv_result_fields(embed, first)
        if len(results) > 1:
            build_more_matches_field(embed, results, csv_mode)
    else:
        in_ep = first.get('in_season_episode', 'N/A') or 'N/A'
        alt_ep = first.get('alt_in_episode', '') or ''
        jp_display = first.get('jp_story_all', 'Not indexed') or 'Not indexed'
        story_a = first.get('story_a', 'Unknown') or 'Unknown'
        story_b = first.get('story_b', '') or ''
        category = (first.get('category', 'unknown') or 'unknown').replace('_', ' ').title()
        title = (first.get('title', 'Unknown') or 'Unknown')[:70]

        embed = Embed(title=f'Episode Found: {title}', color=CFG.color_primary)
        embed.timestamp = utc_now()
        embed.description = f'**Category:** {category}'
        embed.add_field(name='Indian Episode', value=in_ep, inline=True)
        if alt_ep:
            embed.add_field(name='Alt IN Number', value=alt_ep, inline=True)
        embed.add_field(name='Japanese Story #', value=jp_display, inline=True)
        safe_add_field(embed, name='Story A',
                       value=truncate(story_a, CFG.embed_field_value_max), inline=False)
        if story_b:
            safe_add_field(embed, name='Story B',
                           value=truncate(story_b, CFG.embed_field_value_max), inline=False)
        if len(results) > 1:
            build_more_matches_field(embed, results, is_csv=False)

    # --- Send the embed with dropdown if multiple results ---
    source_type_val = dm.current_source['type']

    if len(results) > 1:
        view = ResultSelectView(results[:25], source_type_val, original_user_id=interaction.user.id)
        footer = f'Matches: {len(results)} | Source: {dm.current_source["name"]}'
        if cache_hit:
            footer += ' [Cached]'
        footer += ' | Use dropdown to view details'
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()
        return

    # Single result case
    footer = f'Matches: {len(results)} | Source: {dm.current_source["name"]}'
    if cache_hit:
        footer += ' [Cached]'
    embed.set_footer(text=footer)
    await interaction.followup.send(embed=embed)

# ============================================
# CROSS-SOURCE SEARCH COMMAND
# ============================================

@bot.tree.command(name='search_all', description='Search across all loaded sources simultaneously')
@app_commands.describe(query='Search term')
@rate_limited('search_all')
async def search_all_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    q = sanitize_query(query)
    if not q:
        await interaction.followup.send(
            embed=Embed(title='Empty Query', description='Please provide a search term.',
                        color=CFG.color_error))
        return

    results = await dm.cross_source_search(q)
    if not results:
        embed = Embed(
            title='No Results Found',
            description=f'No results found for "**{q}**" across any source.',
            color=CFG.color_error
        )
        embed.timestamp = utc_now()
        await interaction.followup.send(embed=embed)
        return

    embed = Embed(title=f'Cross-Source Search: {q}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    total = 0

    for source_name, source_results in results.items():
        total += len(source_results)
        first = source_results[0]
        if source_name == 'characters':
            label = 'Characters'
            preview = f'{first.get("Character Name", "?")} [{first.get("Category", "?")}]'
        elif source_name == 'manga_stories':
            label = 'Manga Stories'
            preview = (first.get('English title', '') or first.get('story_a', '?'))[:60]
        else:
            label = 'Episodes'
            preview = (first.get('story_a', '?') or '')[:60]
        safe_add_field(embed, name=f'{label} ({len(source_results)} match(es))',
                       value=preview, inline=False)

    embed.description = f'Found **{total}** total results across {len(results)} source(s)'
    embed.set_footer(text=f'Use the dropdown(s) below to browse all results | Source: All')

    view = CrossSourceSelectView(results, original_user_id=interaction.user.id)
    await interaction.followup.send(embed=embed, view=view)
    view.message = await interaction.original_response()

# ============================================
# JP LOOKUP COMMAND
# ============================================

@bot.tree.command(name='jp', description='Lookup by Japanese Story Number (JSON source only)')
@app_commands.describe(jp_number='Japanese Story Number (e.g., 150, 150 S, or S16)')
@rate_limited('jp')
async def jp_cmd(interaction: discord.Interaction, jp_number: str):
    if not dm.episodes:
        embed = Embed(title='Database Empty', description='No episodes loaded.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return

    if dm.is_csv_source() or dm.is_char_source():
        embed = Embed(
            title='Not Available for CSV',
            description='JP Story Number lookup requires the JSON database source.\n'
                        'Use `/source_change 1` to switch to JSON.\n\n'
                        'For CSV, use `/search <title>` or `/search_1979 <episode>` instead.\n'
                        'For characters, use `/search <character name>` instead.',
            color=CFG.color_warning
        )
        await interaction.response.send_message(embed=embed)
        return

    q = sanitize_query(jp_number)
    if not q:
        embed = Embed(title='Empty Query', description='Please provide a JP story number.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return

    results: List[dict] = []
    standalone_match = re.match(r'^s(\d+)$', q, re.I)
    reg_special_match = re.match(r'^(\d+)\s*s$', q, re.I)

    for ep in dm.episodes:
        for jp in ep.get('jp_parsed', []):
            if standalone_match:
                if jp.get('is_standalone_special') and str(jp.get('number')) == standalone_match.group(1):
                    results.append(ep)
                    break
            elif reg_special_match:
                target_num = reg_special_match.group(1)
                if (not jp.get('is_standalone_special') and
                        jp.get('is_special') and
                        str(jp.get('number')) == target_num):
                    results.append(ep)
                    break
            elif q.isdigit():
                if str(jp.get('number')) == q:
                    results.append(ep)
                    break

    if not results:
        embed = Embed(
            title='Not Found',
            description=f'No episode found for JP Story #{jp_number}\n\n'
                       f'**Current Source:** {dm.current_source["name"]}',
            color=CFG.color_error
        )
        await interaction.response.send_message(embed=embed)
        return

    first = results[0]
    embed = Embed(title=f'JP Story #{jp_number}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.add_field(name='IN Episode', value=first.get('in_season_episode', 'N/A') or 'N/A', inline=True)
    embed.add_field(name='Category', value=(first.get('category', 'unknown') or 'unknown').replace('_', ' ').title(), inline=True)
    if first.get('alt_in_episode'):
        embed.add_field(name='Alt IN', value=first.get('alt_in_episode'), inline=True)
    safe_add_field(embed, name='Story A',
                   value=truncate(first.get('story_a', 'Unknown') or 'Unknown', CFG.embed_field_value_max), inline=False)
    if first.get('story_b'):
        safe_add_field(embed, name='Story B',
                       value=truncate(first.get('story_b', ''), CFG.embed_field_value_max), inline=False)
    embed.add_field(name='All JP Stories', value=first.get('jp_story_all', '-') or '-', inline=False)
    embed.set_footer(text=f'Matches: {len(results)} | Source: {dm.current_source["name"]}')

    if len(results) > 1:
        build_more_matches_field(embed, results, is_csv=False)

    await interaction.response.send_message(embed=embed)

# ============================================
# LIST COMMAND
# ============================================

@bot.tree.command(name='list', description='List episodes or characters with pagination')
@app_commands.describe(
    filter_val='Filter by magazine code (CSV), category (Chars), or season (JSON)',
    page='Page number to jump to (default: 1)'
)
@rate_limited('list')
async def list_cmd(interaction: discord.Interaction, filter_val: Optional[str] = None, page: Optional[int] = 1):
    if not dm.episodes and not dm.char_data:
        embed = Embed(title='Database Empty', description='No data loaded. Use `/source_change` to switch sources.', color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return

    csv_mode = dm.is_csv_source()
    char_mode = dm.is_char_source()

    if char_mode:
        if not dm.char_data:
            embed = Embed(title='No Characters Loaded', description='Character database is empty. Use `/source_change 3` or `/source_change 4`.', color=CFG.color_error)
            await interaction.response.send_message(embed=embed)
            return
        filtered = list(dm.char_data)
    else:
        if not dm.episodes:
            embed = Embed(title='No Episodes Loaded', description='Episode database is empty. Use `/source_change 1` or `/source_change 2`.', color=CFG.color_error)
            await interaction.response.send_message(embed=embed)
            return
        filtered = list(dm.episodes)

    if filter_val:
        f = sanitize_query(filter_val).lower()
        if csv_mode:
            filtered = search_index.get_magazine_bucket(f.upper())
        elif char_mode:
            filtered = [e for e in filtered if (e.get('Category', '') or '').lower() == f]
        else:
            if f == 'special':
                filtered = [e for e in filtered if e.get('category') == 'special_episodes']
            elif f == 'classic':
                filtered = [e for e in filtered if e.get('category') == 'classic_doraemon']
            elif f.isdigit():
                filtered = [e for e in filtered if e.get('category') == f'season_{f}']

    if not filtered:
        embed = Embed(
            title='No Results for Filter',
            description=f'No items found matching filter "**{filter_val}**".\nTry without a filter or use /stats to see available categories.',
            color=CFG.color_warning
        )
        await interaction.response.send_message(embed=embed)
        return

    page_size = CFG.page_size
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    if page < 1:
        page = 1
    elif page > total_pages:
        page = total_pages

    source_type = dm.current_source['type']
    view = ListNavigationView(
        data=filtered,
        page=page,
        page_size=page_size,
        source_type=source_type,
        filter_val=filter_val,
        dm_ref=dm,
        channel_id=interaction.channel.id if hasattr(interaction.channel, 'id') else 0,
        message_id=None
    )
    view.original_user_id = interaction.user.id

    embed = build_list_embed(filtered, page, page_size, source_type, filter_val,
                             dm.current_source['name'])
    metrics.record_command('list')
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()

# ============================================
# RANDOM COMMAND
# ============================================

@bot.tree.command(name='random', description='Get a random story or character')
@rate_limited('random')
async def random_cmd(interaction: discord.Interaction):
    char_mode = dm.is_char_source()
    csv_mode = dm.is_csv_source()

    if char_mode and dm.char_data:
        result = random.choice(dm.char_data)
        name = result.get('Character Name', 'Unknown') or 'Unknown'
        embed = Embed(title=f'🎲 Random Character: {truncate(name, 70)}', color=CFG.color_primary)
        embed.timestamp = utc_now()
        embed.add_field(name='Category', value=result.get('Category', 'Unknown') or 'Unknown', inline=True)
        embed.add_field(name='Alternative Name', value=result.get('Alternative Name', '') or 'N/A', inline=True)
        safe_add_field(embed, name='Description', value=truncate(result.get('Description', '') or '', CFG.embed_field_value_max), inline=False)
    elif dm.episodes:
        result = random.choice(dm.episodes)
        if csv_mode:
            title = result.get('English title', '') or result.get('Japanese title', 'Unknown') or 'Unknown'
            embed = Embed(title=f'🎲 Random Story: {truncate(title, 70)}', color=CFG.color_primary)
            embed.timestamp = utc_now()
            build_csv_result_fields(embed, result)
        else:
            title = (result.get('title', 'Unknown') or 'Unknown')[:70]
            embed = Embed(title=f'🎲 Random Episode: {title}', color=CFG.color_primary)
            embed.timestamp = utc_now()
            embed.description = f'**Category:** {(result.get("category", "unknown") or "unknown").replace("_", " ").title()}'
            embed.add_field(name='Indian Episode', value=result.get('in_season_episode', 'N/A') or 'N/A', inline=True)
            embed.add_field(name='Japanese Story #', value=result.get('jp_story_all', 'N/A') or 'N/A', inline=True)
            safe_add_field(embed, name='Story A',
                           value=truncate(result.get('story_a', 'Unknown') or 'Unknown', CFG.embed_field_value_max), inline=False)
            if result.get('story_b'):
                safe_add_field(embed, name='Story B',
                               value=truncate(result.get('story_b', ''), CFG.embed_field_value_max), inline=False)
    else:
        embed = Embed(title='No Data Available',
                      description='No data loaded to pick a random entry from.',
                      color=CFG.color_error)
        embed.timestamp = utc_now()

    embed.set_footer(text=f'Source: {dm.current_source["name"]}')
    await interaction.response.send_message(embed=embed)

# ============================================
# RELATED COMMAND
# ============================================

@bot.tree.command(name='related', description='Show stories sharing the same magazine, date, or anime episode')
@app_commands.describe(query='A story title or magazine code to find related stories')
@rate_limited('related')
async def related_cmd(interaction: discord.Interaction, query: str):
    if not dm.episodes and not dm.csv_stories:
        embed = Embed(title='No Data', description='No stories loaded.', color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    await interaction.response.defer()

    q = sanitize_query(query).lower()
    csv_mode = dm.is_csv_source()

    # Find the original story first
    original: Optional[dict] = None
    data = dm.csv_stories if csv_mode else dm.episodes

    for s in data:
        title = (s.get('English title', '') or s.get('story_a', '') or '').lower()
        if q in title:
            original = s
            break

    if not original:
        # Try as magazine code
        if q.upper() in KNOWN_MAGAZINES:
            bucket = search_index.get_magazine_bucket(q.upper())
            if bucket:
                embed = Embed(title=f'Related: Magazine {q.upper()}', color=CFG.color_primary)
                embed.timestamp = utc_now()
                embed.description = f'Found **{len(bucket)}** stories in magazine {q.upper()}'
                for story in bucket[:10]:
                    t = story.get('English title', '') or story.get('story_a', '?') or '?'
                    d = story.get('Publication Date', '?') or '?'
                    safe_add_field(embed, name=f'{d} - {truncate(t, 50)}',
                                   value=ZERO_WIDTH_SPACE, inline=False)
                if len(bucket) > 10:
                    embed.set_footer(text=f'Showing 10 of {len(bucket)} | Source: {dm.current_source["name"]}')
                else:
                    embed.set_footer(text=f'Source: {dm.current_source["name"]}')
                await interaction.followup.send(embed=embed)
                return

        embed = Embed(title='No Match Found',
                      description=f'Could not find a story matching "**{query}**".',
                      color=CFG.color_error)
        await interaction.followup.send(embed=embed)
        return
    # Find related by magazine and date
    mag = (original.get('Magazine code', '') or '').upper().strip()
    date = (original.get('Publication Date', '') or '').strip()
    orig_title = original.get('English title', '') or original.get('story_a', 'Unknown') or 'Unknown'

    related: List[dict] = []
    for s in data:
        if s is original:
            continue
        s_mag = (s.get('Magazine code', '') or '').upper().strip()
        s_date = (s.get('Publication Date', '') or '').strip()
        if (mag and s_mag == mag) or (date and s_date == date):
            related.append(s)

    if not related:
        embed = Embed(
            title='No Related Stories Found',
            description=f'No other stories share the same magazine or date as "**{orig_title}**".',
            color=CFG.color_warning
        )
        await interaction.followup.send(embed=embed)
        return

    embed = Embed(title=f'Related to: {truncate(orig_title, 70)}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.description = f'Found **{len(related)}** related stories (same magazine/date)'

    for story in related[:10]:
        t = story.get('English title', '') or story.get('story_a', '?') or '?'
        m = story.get('Magazine code', '?') or '?'
        d = story.get('Publication Date', '?') or '?'
        safe_add_field(embed, name=f'[{m}] {d} - {truncate(t, 50)}',
                       value=ZERO_WIDTH_SPACE, inline=False)

    if len(related) > 10:
        embed.set_footer(text=f'Showing 10 of {len(related)} | Source: {dm.current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {dm.current_source["name"]}')
    await interaction.followup.send(embed=embed)


# ============================================
# COMPARE COMMAND
# ============================================

@bot.tree.command(name='compare', description='Compare two characters side by side')
@app_commands.describe(
    name1='First character name',
    name2='Second character name'
)
@app_commands.autocomplete(name1=autocomplete_character_names)
@app_commands.autocomplete(name2=autocomplete_character_names)
@rate_limited('compare')
async def compare_cmd(interaction: discord.Interaction, name1: str, name2: str):
    is_available, error_embed = dm.check_char_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    await interaction.response.defer()

    q1 = sanitize_query(name1).lower()
    q2 = sanitize_query(name2).lower()

    results1 = search_characters(q1, dm.char_data, use_word_boundary=True, use_fuzzy=True)
    results2 = search_characters(q2, dm.char_data, use_word_boundary=True, use_fuzzy=True)

    if not results1 or not results2:
        missing = []
        if not results1:
            missing.append(name1)
        if not results2:
            missing.append(name2)
        embed = Embed(
            title='Character Not Found',
            description=f'Could not find: {", ".join(f"**{m}**" for m in missing)}',
            color=CFG.color_error
        )
        await interaction.followup.send(embed=embed)
        return

    c1 = results1[0]
    c2 = results2[0]

    embed = Embed(title=f'Comparing: {truncate(c1.get("Character Name", "?") or "?", 30)} vs {truncate(c2.get("Character Name", "?") or "?", 30)}',
                  color=CFG.color_primary)
    embed.timestamp = utc_now()

    # Side-by-side fields
    embed.add_field(name='Name', value=c1.get('Character Name', '?') or '?', inline=True)
    embed.add_field(name=ZERO_WIDTH_SPACE, value=ZERO_WIDTH_SPACE, inline=True)
    embed.add_field(name='Name', value=c2.get('Character Name', '?') or '?', inline=True)

    embed.add_field(name='Category', value=c1.get('Category', '?') or '?', inline=True)
    embed.add_field(name=ZERO_WIDTH_SPACE, value=ZERO_WIDTH_SPACE, inline=True)
    embed.add_field(name='Category', value=c2.get('Category', '?') or '?', inline=True)

    embed.add_field(name='Alt Name', value=c1.get('Alternative Name', '') or 'N/A', inline=True)
    embed.add_field(name=ZERO_WIDTH_SPACE, value=ZERO_WIDTH_SPACE, inline=True)
    embed.add_field(name='Alt Name', value=c2.get('Alternative Name', '') or 'N/A', inline=True)

    safe_add_field(embed, name='Description (1)',
                   value=truncate(c1.get('Description', '') or '', 400), inline=False)
    safe_add_field(embed, name='Description (2)',
                   value=truncate(c2.get('Description', '') or '', 400), inline=False)

    embed.set_footer(text=f'Source: {dm.current_source["name"]}')
    await interaction.followup.send(embed=embed)

# ============================================
# TIMELINE COMMAND
# ============================================

@bot.tree.command(name='timeline', description='Show stories sorted by publication date chronologically')
@app_commands.describe(
    limit='Maximum number of stories to show (default: 20)',
    magazine='Filter by magazine code (optional)'
)
@app_commands.autocomplete(magazine=autocomplete_magazine_codes)
@rate_limited('timeline')
async def timeline_cmd(interaction: discord.Interaction, limit: Optional[int] = 20, magazine: Optional[str] = None):
    if not dm.csv_stories and not dm.episodes:
        embed = Embed(title='No Data', description='No stories loaded.', color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    await interaction.response.defer()

    csv_mode = dm.is_csv_source()
    data = dm.csv_stories if csv_mode else dm.episodes

    # Filter by magazine if provided
    if magazine:
        mag_upper = magazine.upper().strip()
        data = [s for s in data if (s.get('Magazine code', '') or '').upper().strip() == mag_upper]

    # Sort by publication date
    def sort_key(s: dict) -> str:
        d = s.get('Publication Date', '') or ''
        return d if d else '9999-99'

    sorted_data = sorted(data, key=sort_key)

    if not sorted_data:
        embed = Embed(
            title='No Timeline Data',
            description='No stories with publication dates found.',
            color=CFG.color_warning
        )
        await interaction.followup.send(embed=embed)
        return

    # Cap limit
    limit = max(1, min(limit, 25))
    sorted_data = sorted_data[:limit]

    embed = Embed(title='📚 Doraemon Manga Timeline', color=CFG.color_primary)
    embed.timestamp = utc_now()

    if magazine:
        embed.description = f'Showing {len(sorted_data)} stories from magazine **{magazine.upper()}**, sorted by publication date'
    else:
        embed.description = f'Showing {len(sorted_data)} stories sorted by publication date'

    for story in sorted_data:
        date = story.get('Publication Date', '?') or '?'
        title = story.get('English title', '') or story.get('story_a', 'Unknown') or 'Unknown'
        mag = story.get('Magazine code', '?') or '?'
        safe_add_field(embed, name=f'{date} [{mag}]',
                       value=truncate(title, 80), inline=False)

    embed.set_footer(text=f'Source: {dm.current_source["name"]}')
    await interaction.followup.send(embed=embed)

# ============================================
# STATS COMMAND
# ============================================

@bot.tree.command(name='stats', description='Show database statistics')
@rate_limited('stats')
async def stats_cmd(interaction: discord.Interaction):
    embed = Embed(title='Database Statistics', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.add_field(name='Bot Uptime', value=metrics.get_uptime(), inline=True)
    embed.add_field(name='Cache Size', value=f'{search_cache.get_size()} entries', inline=True)

    MAX_FIELDS = MAX_EMBED_FIELDS_DISPLAY

    if dm.is_char_source():
        categories: Dict[str, int] = {}
        for e in dm.char_data:
            cat = e.get('Category', 'Unknown') or 'Unknown'
            categories[cat] = categories.get(cat, 0) + 1

        url_display = truncate(dm.current_source["url"], 60) if interaction.user.id == 1236713171142840403 else '`Hidden — Admin only`'
        embed.description = (
            f'**Total Characters:** {len(dm.char_data)}\n'
            f'**Source:** {dm.current_source["name"]}\n'
            f'**URL:** {url_display}\n\n'
            f'**By Category:**'
        )

        sorted_cats = sorted(categories.items(), key=lambda x: -x[1])

        for cat, cnt in sorted_cats[:MAX_FIELDS]:
            embed.add_field(name=cat, value=f'**{cnt}**', inline=True)

        if len(sorted_cats) > MAX_FIELDS:
            other_lines = [f'{cat}: {cnt}' for cat, cnt in sorted_cats[MAX_FIELDS:]]
            other_text = '\n'.join(other_lines)
            if len(other_text) > CFG.embed_field_value_max:
                other_text = other_text[:CFG.embed_field_value_max - 1] + '…'
            safe_add_field(embed, name=f'Other ({len(sorted_cats) - MAX_FIELDS})',
                           value=other_text, inline=False)

        embed.set_footer(text=f'Generated: {utc_now().strftime("%Y-%m-%d %H:%M")} | '
                               f'Source: {dm.current_source["name"]}')
        await interaction.response.send_message(embed=embed)
        return

    if dm.is_csv_source():
        magazines: Dict[str, int] = {}
        for e in dm.episodes:
            mag = e.get('Magazine code', 'Unknown') or 'Unknown'
            magazines[mag] = magazines.get(mag, 0) + 1

        path_display = truncate(dm.current_source["url"], 60) if interaction.user.id == 1236713171142840403 else '`Hidden — Admin only`'
        embed.description = (
            f'**Total Stories:** {len(dm.episodes)}\n'
            f'**Source:** {dm.current_source["name"]}\n'
            f'**Path:** {path_display}\n\n'
            f'**By Magazine Code:**'
        )

        sorted_mags = sorted(magazines.items(), key=lambda x: -x[1])

        for mag, cnt in sorted_mags[:MAX_FIELDS]:
            embed.add_field(name=mag, value=f'**{cnt}**', inline=True)

        if len(sorted_mags) > MAX_FIELDS:
            other_lines = [f'{mag}: {cnt}' for mag, cnt in sorted_mags[MAX_FIELDS:]]
            other_text = '\n'.join(other_lines)
            if len(other_text) > CFG.embed_field_value_max:
                other_text = other_text[:CFG.embed_field_value_max - 1] + '…'
            safe_add_field(embed, name=f'Other ({len(sorted_mags) - MAX_FIELDS})',
                           value=other_text, inline=False)

        has_1979 = sum(1 for e in dm.episodes if e.get('1979 anime', '').strip())
        has_2005 = sum(1 for e in dm.episodes if e.get('2005 anime', '').strip())
        has_movie = sum(1 for e in dm.episodes if e.get('Movie', '').strip())

        embed.add_field(name='Has 1979 Anime', value=f'**{has_1979}**', inline=True)
        embed.add_field(name='Has 2005 Anime', value=f'**{has_2005}**', inline=True)
        embed.add_field(name='Has Movie', value=f'**{has_movie}**', inline=True)

        if dm.csv_data_loaded:
            embed.add_field(name='CSV Rows', value=str(len(dm.csv_stories)), inline=True)
    else:
        counts: Dict[str, int] = {}
        with_jp = 0
        jp_specials = 0
        jp_standalone = 0

        for e in dm.episodes:
            cat = e.get('category', 'unknown') or 'unknown'
            counts[cat] = counts.get(cat, 0) + 1
            if e.get('jp_story_numbers'):
                with_jp += 1
            if e.get('jp_has_specials'):
                jp_specials += 1
            if e.get('jp_has_standalone_specials'):
                jp_standalone += 1

        url_display = dm.current_source["url"] if interaction.user.id == 1236713171142840403 else '`Hidden — Admin only`'
        embed.description = (
            f'**Total Episodes:** {len(dm.episodes)}\n'
            f'**JP Indexed:** {with_jp}\n'
            f'**JP Special Stories:** {jp_specials}\n'
            f'**JP Standalone Specials:** {jp_standalone}\n\n'
            f'**Source:** {dm.current_source["name"]} ({dm.current_source["type"].title()})\n'
            f'**URL:** {url_display}\n\n'
            f'**Breakdown:**'
        )

        sorted_counts = sorted(counts.items(), key=lambda x: -x[1])

        for cat, cnt in sorted_counts[:MAX_FIELDS]:
            embed.add_field(name=cat.replace('_', ' ').title(), value=f'**{cnt}**', inline=True)

        if len(sorted_counts) > MAX_FIELDS:
            other_lines = [f'{cat.replace("_", " ").title()}: {cnt}' for cat, cnt in sorted_counts[MAX_FIELDS:]]
            other_text = '\n'.join(other_lines)
            if len(other_text) > CFG.embed_field_value_max:
                other_text = other_text[:CFG.embed_field_value_max - 1] + '…'
            safe_add_field(embed, name=f'Other ({len(sorted_counts) - MAX_FIELDS})',
                           value=other_text, inline=False)

    embed.set_footer(text=f'Generated: {utc_now().strftime("%Y-%m-%d %H:%M")} | '
                           f'Source: {dm.current_source["name"]}')
    await interaction.response.send_message(embed=embed)

# ============================================
# HELP COMMAND
# ============================================

@bot.tree.command(name='help', description='Show available commands')
async def help_cmd(interaction: discord.Interaction):
    embed = Embed(title='Doraemon Search Bot Help', color=CFG.color_primary)
    embed.timestamp = utc_now()

    if dm.is_char_source():
        embed.description = (
            '**Available Commands (Character Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by character name, alt name, category, or description\n'
            '  *Uses word-boundary matching for names, fuzzy matching as fallback*\n'
            '- `/search_all <query>` - Search across all loaded sources simultaneously\n'
            '- `/character <name>` - Dedicated character lookup\n'
            '- `/characters_by_category <category>` - Filter characters by category\n'
            '- `/compare <name1> <name2>` - Compare two characters side by side\n'
            '- `/random` - Get a random character\n'
            '- `/list [filter]` - Browse characters with interactive buttons (incl. Jump to Page)\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source *(admin only)*\n'
            '- `/reload` - Force reload current source *(admin only)*\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n'
            '- `/health` - Bot health diagnostics\n\n'
            f'**Current Source:** {dm.current_source["name"]}\n'
            f'**Total Records:** {len(dm.char_data)}'
        )
    elif dm.is_csv_source():
        embed.description = (
            '**Available Commands (CSV Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by title, magazine code, 1979/2005 anime ep, or date\n'
            '- `/search_all <query>` - Search across all loaded sources simultaneously\n'
            '- `/list [filter]` - Browse stories with interactive buttons (incl. Jump to Page)\n'
            '- `/stats` - Show database statistics\n'
            '- `/random` - Get a random story\n'
            '- `/related <query>` - Show stories sharing the same magazine/date\n'
            '- `/timeline [limit] [magazine]` - View stories chronologically by publication date\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source *(admin only)*\n'
            '- `/reload` - Force reload current source *(admin only)*\n\n'
            '**CSV-Only Commands**\n'
            '- `/search_jp_title <keyword>` - Search by Japanese title\n'
            '- `/search_magazine <code>` - Search by magazine code\n'
            '- `/search_1979 <episode>` - Search by 1979 anime episode\n'
            '- `/search_2005 <episode>` - Search by 2005 anime episode\n'
            '- `/search_volume <type> <number>` - Search by compilation volume\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n'
            '- `/health` - Bot health diagnostics\n\n'
            f'**Current Source:** {dm.current_source["name"]}\n'
            f'**Total Records:** {len(dm.episodes)}'
        )
    else:
        embed.description = (
            '**Available Commands (JSON Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by JP#, IN#, SPE, CE, or title\n'
            '- `/search_all <query>` - Search across all loaded sources simultaneously\n'
            '- `/jp <number>` - Lookup by Japanese Story Number\n'
            '- `/list [filter]` - Browse episodes with interactive buttons (incl. Jump to Page)\n'
            '- `/stats` - Show database statistics\n'
            '- `/random` - Get a random episode\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source *(admin only)*\n'
            '- `/reload` - Force reload current source *(admin only)*\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n'
            '- `/health` - Bot health diagnostics\n\n'
            f'**Current Source:** {dm.current_source["name"]}\n'
            f'**Total Records:** {len(dm.episodes)}'
        )

    embed.set_footer(text=f'Doraemon Search Bot v{BOT_VERSION}')
    await interaction.response.send_message(embed=embed)

# ============================================
# CSV-SPECIFIC COMMANDS
# ============================================

@bot.tree.command(name='search_jp_title', description='Search by Japanese title (CSV only)')
@app_commands.describe(query='Japanese title or keyword')
@rate_limited('search_jp_title')
async def search_jp_title_cmd(interaction: discord.Interaction, query: str):
    is_available, error_embed = dm.check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = sanitize_query(query).lower()
    results = [s for s in dm.csv_stories
               if q in (s.get('Japanese title', '') or '').lower() or q in (s.get('English title', '') or '').lower()]

    if not results:
        results = [s for s in dm.csv_stories
                   if fuzzy_contains(s.get('Japanese title', ''), q) or fuzzy_contains(s.get('English title', ''), q)]

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for "**{query}**"',
                      color=CFG.color_error)
        embed.timestamp = utc_now()
        await interaction.response.send_message(embed=embed)
        return

    first = results[0]
    embed = Embed(title=f'JP Title Match: {truncate(first.get("English title", "Unknown") or "Unknown", 70)}',
                  color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.add_field(name='Japanese Title', value=first.get('Japanese title', 'N/A') or 'N/A', inline=False)
    embed.add_field(name='1979 Episode', value=first.get('1979 anime', 'N/A') or 'N/A', inline=True)
    embed.add_field(name='2005 Episode', value=first.get('2005 anime', 'N/A') or 'N/A', inline=True)
    embed.add_field(name='Magazine', value=first.get('Magazine code', 'N/A') or 'N/A', inline=True)
    embed.add_field(name='Publication Date', value=first.get('Publication Date', 'N/A') or 'N/A', inline=True)

    if len(results) > 1:
        additional = '\n'.join(f'- {truncate(r.get("English title", "?") or "?", 40)}' for r in results[1:6])
        safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value=additional, inline=False)

    embed.set_footer(text=f'Source: {dm.current_source["name"]}')
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_magazine', description='Search by magazine code (CSV only)')
@app_commands.describe(code='Magazine code (e.g., YK, KG, G1, G2, G3, G4, G5, G6, BK, SD, TK, CC, CD)')
@app_commands.autocomplete(code=autocomplete_magazine_codes)
@rate_limited('search_magazine')
async def search_magazine_cmd(interaction: discord.Interaction, code: str):
    is_available, error_embed = dm.check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    code_upper = sanitize_query(code).upper()
    # Use precomputed bucket
    results = search_index.get_magazine_bucket(code_upper)

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for magazine code "**{code_upper}**"',
                      color=CFG.color_error)
        embed.timestamp = utc_now()
        await interaction.response.send_message(embed=embed)
        return

    embed = Embed(title=f'Magazine: {code_upper}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.description = f'Found **{len(results)}** stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', 'Unknown') or 'Unknown'
        date = story.get('Publication Date', '?') or '?'
        safe_add_field(embed, name=f'{date} - {truncate(title, 50)}', value=ZERO_WIDTH_SPACE, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {dm.current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {dm.current_source["name"]}')

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_1979', description='Search by 1979 anime episode (CSV only)')
@app_commands.describe(episode='Episode text (e.g., 7, Ep 7, 403, Special)')
@rate_limited('search_1979')
async def search_1979_cmd(interaction: discord.Interaction, episode: str):
    is_available, error_embed = dm.check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = sanitize_query(episode).lower().strip()
    results = search_index.get_anime_1979_bucket(q)
    if not results:
        results = [s for s in dm.csv_stories if q in (s.get('1979 anime', '') or '').lower()]

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for 1979 anime episode "**{episode}**"',
                      color=CFG.color_error)
        embed.timestamp = utc_now()
        await interaction.response.send_message(embed=embed)
        return

    embed = Embed(title=f'1979 Anime: {episode}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.description = f'Found **{len(results)}** story/stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', '?') or '?'
        jp = story.get('Japanese title', '') or ''
        desc = f'- {truncate(title, 50)}' + (f' ({truncate(jp, 30)})' if jp else '')
        safe_add_field(embed, name=truncate(desc, CFG.embed_field_name_max), value=ZERO_WIDTH_SPACE, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {dm.current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {dm.current_source["name"]}')

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_2005', description='Search by 2005 anime episode (CSV only)')
@app_commands.describe(episode='Episode text (e.g., 16A, 44, 87, 1694)')
@rate_limited('search_2005')
async def search_2005_cmd(interaction: discord.Interaction, episode: str):
    is_available, error_embed = dm.check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = sanitize_query(episode).lower().strip()
    results = search_index.get_anime_2005_bucket(q)
    if not results:
        results = [s for s in dm.csv_stories if q in (s.get('2005 anime', '') or '').lower()]

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for 2005 anime episode "**{episode}**"',
                      color=CFG.color_error)
        embed.timestamp = utc_now()
        await interaction.response.send_message(embed=embed)
        return

    embed = Embed(title=f'2005 Anime: {episode}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.description = f'Found **{len(results)}** story/stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', '?') or '?'
        jp = story.get('Japanese title', '') or ''
        desc = f'- {truncate(title, 50)}' + (f' ({truncate(jp, 30)})' if jp else '')
        safe_add_field(embed, name=truncate(desc, CFG.embed_field_name_max), value=ZERO_WIDTH_SPACE, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {dm.current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {dm.current_source["name"]}')

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_volume', description='Search by volume number across all compilation types (CSV only)')
@app_commands.describe(
    volume_type='Which compilation type to search',
    volume_number='Volume number (e.g., 1, +1, 0, L1, 197, 1-1)'
)
@app_commands.choices(volume_type=[
    app_commands.Choice(name='Tankōbon (original Japanese print)', value='tankobon'),
    app_commands.Choice(name='Complete Collection (Fujiko F. Fujio)', value='complete'),
    app_commands.Choice(name='English Kindle', value='kindle'),
    app_commands.Choice(name='Bilingual Print', value='bilingual'),
    app_commands.Choice(name='All Types', value='all'),
])
@rate_limited('search_volume')
async def search_volume_cmd(
    interaction: discord.Interaction,
    volume_type: str,
    volume_number: str
):
    is_available, error_embed = dm.check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = sanitize_query(volume_number).strip()
    if not q:
        embed = Embed(title='Empty Query',
                      description='Please provide a volume number.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return

    results: List[dict] = []

    if volume_type == 'all':
        for story in dm.csv_stories:
            for col_name in VOLUME_COLUMNS.values():
                val = story.get(col_name, '').strip()
                if val and word_boundary_match(q, val):
                    story_copy = dict(story)
                    story_copy['_matched_column'] = col_name
                    results.append(story_copy)
                    break
    else:
        col_name = VOLUME_COLUMNS.get(volume_type, 'Tankōbon')
        for story in dm.csv_stories:
            val = story.get(col_name, '').strip()
            if val and word_boundary_match(q, val):
                story_copy = dict(story)
                story_copy['_matched_column'] = col_name
                results.append(story_copy)

    if not results:
        embed = Embed(
            title='No Results Found',
            description=f'No stories found for volume "**{volume_number}**" in {volume_type.title()}.',
            color=CFG.color_error
        )
        embed.timestamp = utc_now()
        safe_add_field(embed, name='Tip', value=(
            'Try different formats:\n'
            '- Plain number: `1`, `5`, `44`\n'
            '- Plus prefix: `+1`, `+5`\n'
            '- Volume 0: `0`\n'
            '- Long Stories: `L1`, `L5`\n'
            '- Bilingual: `1-1`, `2-3`, `3-6`\n'
            '- Kindle: `197`, `174`'
        ), inline=False)
        embed.set_footer(text=f'Source: {dm.current_source["name"]}')
        await interaction.response.send_message(embed=embed)
        return

    type_display = {
        'tankobon':  'Tankōbon',
        'complete':  'Complete Collection',
        'kindle':    'English Kindle',
        'bilingual': 'Bilingual Print',
        'all':       'All Types',
    }.get(volume_type, volume_type.title())

    embed = Embed(
        title=f'Volume Search: "{volume_number}" ({type_display})',
        color=CFG.color_primary
    )
    embed.timestamp = utc_now()
    embed.description = f'Found **{len(results)}** story/stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', 'Unknown') or 'Unknown'
        jp_title = story.get('Japanese title', '') or ''
        matched_col = story.get('_matched_column', '?')

        vol_parts: List[str] = []
        for col_label, col_key in [
            ('Tankōbon', 'Tankōbon'),
            ('Complete', 'Complete Collection'),
            ('Kindle', 'English Kindle'),
            ('Bilingual', 'Bilingual Print'),
        ]:
            v = story.get(col_key, '').strip()
            if v:
                highlight = '**' if col_key == matched_col else ''
                vol_parts.append(f'{highlight}{col_label}: {v}{highlight}')

        field_name = truncate(title, CFG.embed_field_name_max)
        field_value = (f'({truncate(jp_title, 30)})\n' if jp_title else '') + ' | '.join(vol_parts)
        safe_add_field(embed, name=field_name, value=field_value, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {dm.current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {dm.current_source["name"]}')

    await interaction.response.send_message(embed=embed)

# ============================================
# CHARACTER-SPECIFIC COMMANDS
# ============================================

@bot.tree.command(name='character', description='Search for a character by name (Characters source only)')
@app_commands.describe(name='Character name or keyword')
@app_commands.autocomplete(name=autocomplete_character_names)
@rate_limited('character')
async def character_cmd(interaction: discord.Interaction, name: str):
    is_available, error_embed = dm.check_char_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    await interaction.response.defer()

    q = sanitize_query(name)
    if not q:
        embed = Embed(title='Empty Query',
                      description='Please provide a character name.',
                      color=CFG.color_error)
        await interaction.followup.send(embed=embed)
        return

    q_lower = q.lower()

    results = search_characters(q_lower, dm.char_data, use_word_boundary=True, use_fuzzy=True)

    if not results:
        embed = Embed(
            title='No Results Found',
            description=f'No characters found for "**{q}**"\n\n'
                        '**Tips:**\n'
                        '- Try full name: `nobita nobi` vs just `nobita`\n'
                        '- Use category: `robot`, `human`, `animal`\n'
                        '- Search keywords from descriptions',
            color=CFG.color_error
        )
        embed.timestamp = utc_now()
        await interaction.followup.send(embed=embed)
        return

    first = results[0]
    char_name = first.get('Character Name', 'Unknown') or 'Unknown'
    alt_name = first.get('Alternative Name', '') or ''
    category = first.get('Category', 'Unknown') or 'Unknown'
    description = first.get('Description', '') or ''
    references = first.get('Source References', '') or ''

    embed = Embed(title=f'Character: {truncate(char_name, 70)}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.add_field(name='Alternative Name', value=alt_name or 'N/A', inline=True)
    embed.add_field(name='Category', value=category, inline=True)
    safe_add_field(embed, name='Description', value=truncate(description, CFG.embed_field_value_max), inline=False)
    if references:
        safe_add_field(embed, name='Source References', value=truncate(references, CFG.embed_field_value_max), inline=False)

    if len(results) > 1:
        # Use select menu instead of plain text
        view = ResultSelectView(results[:25], SourceType.CSV_CHAR.value, original_user_id=interaction.user.id)
        additional = '\n'.join(
            f'- {r.get("Character Name", "?") or "?"} [{r.get("Category", "") or ""}]'
            for r in results[1:6]
        )
        safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value=additional, inline=False)
        embed.set_footer(text=f'Matches: {len(results)} | Use dropdown to view details | Source: {dm.current_source["name"]}')
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()
        return

    embed.set_footer(text=f'Matches: {len(results)} | Source: {dm.current_source["name"]}')
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='characters_by_category', description='List characters filtered by category (Characters source only)')
@app_commands.describe(category='Category name (e.g., human, robot, animal)')
@app_commands.autocomplete(category=autocomplete_categories)
@rate_limited('characters_by_category')
async def characters_by_category_cmd(interaction: discord.Interaction, category: str):
    is_available, error_embed = dm.check_char_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = sanitize_query(category).lower().strip()
    if not q:
        embed = Embed(title='Empty Query',
                      description='Please provide a category name.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return

    results = [c for c in dm.char_data if q == (c.get('Category', '') or '').lower().strip()]
    results.sort(key=lambda x: (x.get('Character Name', '') or '').lower())

    if not results:
        embed = Embed(
            title='No Results Found',
            description=f'No characters found in category "**{category}**"\n\n'
                        '**Available categories:**\n'
                        '- human, robot, animal, gadget\n'
                        '- Check with /stats for full breakdown',
            color=CFG.color_error
        )
        embed.timestamp = utc_now()
        await interaction.response.send_message(embed=embed)
        return

    embed = Embed(title=f'Characters: {category.title()}', color=CFG.color_primary)
    embed.timestamp = utc_now()
    embed.description = f'Found **{len(results)}** characters'

    for char in results[:10]:
        name = char.get('Character Name', '?') or '?'
        alt = char.get('Alternative Name', '') or ''
        desc_preview = truncate(char.get('Description', '') or '', 80)
        field_value = desc_preview
        if alt:
            field_value += f'\n*Alt: {alt}*'
        safe_add_field(embed, name=name, value=field_value, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {dm.current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {dm.current_source["name"]}')

    await interaction.response.send_message(embed=embed)

# ============================================
# AUTO-REFRESH BACKGROUND TASK
# ============================================

@tasks.loop(hours=AUTO_REFRESH_INTERVAL_HOURS)
async def auto_refresh_task():
    """Periodically re-fetch remote CSVs to keep data current."""
    logger.info('Auto-refresh: Checking for data updates...')

    try:
        # Refresh character data if remote-loaded
        if dm.char_data_loaded and AIOHTTP_AVAILABLE:
            success = await dm.load_char_csv_remote(CHAR_CSV_RAW_URL)
            if success:
                logger.info(f'Auto-refresh: Character data updated ({len(dm.char_data)} chars)')
                search_cache.invalidate_pattern(SourceType.CSV_CHAR.value)
                search_index.build_char_index(dm.char_data)
            else:
                logger.warning('Auto-refresh: Failed to update character data')

        # Refresh CSV story data if loaded remotely
        if dm.csv_data_loaded and AIOHTTP_AVAILABLE and dm.is_csv_source():
            success = await dm.load_csv_database_live(CFG.csv_raw_base_url)
            if success:
                await dm.safe_update_episodes(dm.convert_csv_to_episode_format(dm.csv_stories))
                logger.info(f'Auto-refresh: CSV data updated ({len(dm.episodes)} stories)')
                search_cache.invalidate_pattern(SourceType.CSV.value)
                search_index.build_story_index(dm.episodes)
            else:
                logger.warning('Auto-refresh: Failed to update CSV data')

    except Exception as e:
        logger.error(f'Auto-refresh error: {type(e).__name__}: {e}')
        metrics.record_error(type(e).__name__)

@auto_refresh_task.before_loop
async def auto_refresh_before():
    """Wait until bot is ready before starting auto-refresh."""
    await bot.wait_until_ready()

class CrossSourceSelectView(ui.View):
    """View with multiple dropdown menus — one per source type."""

    def __init__(self, results: Dict[str, List[dict]], original_user_id: Optional[int] = None):
        super().__init__(timeout=CFG.view_timeout_seconds)
        self.original_user_id = original_user_id
        self.message = None

        source_map: Dict[str, str] = {
            'characters':    SourceType.CSV_CHAR.value,
            'manga_stories': SourceType.CSV.value,
            'episodes':      'json',
        }

        for source_name, source_results in results.items():
            source_type = source_map.get(source_name, 'json')
            menu = ResultSelectMenu(source_results[:25], source_type)

            label_display = source_name.replace('_', ' ').title()
            menu.placeholder = f'Browse {label_display} ({len(source_results)} match(es))...'

            self.add_item(menu)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, 'message') and self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

# ============================================
# ERROR HANDLING
# ============================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    """Handle slash command errors gracefully."""
    logger.error(f'Slash command error: {type(error).__name__}: {error}')
    metrics.record_error(type(error).__name__)

    embed = Embed(
        title='Something went wrong',
        description='An unexpected error occurred while running this command.\n'
                    'Please try again, or contact the bot administrator if the issue persists.',
        color=CFG.color_error
    )
    embed.timestamp = utc_now()

    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f'Failed to send error message: {e}')

@bot.event
async def on_command_error(ctx, error):
    """Handle legacy prefix command errors."""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send('Command not found. Use `/help` to see available commands.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('Missing required argument. Use `/help` for usage information.')
    elif isinstance(error, commands.CheckFailure):
        logger.warning(f'Check failure: {error}')
    else:
        logger.error(f'Unhandled error: {type(error).__name__}: {error}')
        try:
            await ctx.send('An unexpected error occurred. Please contact the bot administrator.')
        except Exception:
            pass

# ============================================
# EVENT HANDLERS
# ============================================

@bot.event
async def on_ready():
    global _ready_flag

    if _ready_flag:
        logger.info('on_ready fired again (reconnect) — checking data freshness...')
        # Trigger a refresh if data might be stale (reconnected after a gap)
        if AIOHTTP_AVAILABLE:
            try:
                if dm.char_data_loaded:
                    await dm.load_char_csv_remote(CHAR_CSV_RAW_URL)
                    search_index.build_char_index(dm.char_data)
                    logger.info('Reconnect: Character data refreshed.')
                if dm.csv_data_loaded and dm.is_csv_source():
                    await dm.load_csv_database_live(CFG.csv_raw_base_url)
                    await dm.safe_update_episodes(dm.convert_csv_to_episode_format(dm.csv_stories))
                    search_index.build_story_index(dm.episodes)
                    logger.info('Reconnect: CSV data refreshed.')
            except Exception as e:
                logger.warning(f'Reconnect refresh failed: {e}')
        return

    _ready_flag = True

    logger.info(f'Logged in as {bot.user}')
    logger.info('Syncing slash commands...')
    dev_guild_id = os.getenv('DEV_GUILD_ID')
    if dev_guild_id:
        try:
            guild_obj = discord.Object(id=int(dev_guild_id))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            logger.info(f'Synced {len(synced)} commands to dev guild {dev_guild_id}')
        except Exception as e:
            logger.warning(f'Guild sync failed, falling back to global: {e}')
            synced = await bot.tree.sync()
            logger.info(f'Synced {len(synced)} GLOBAL commands')
    else:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} GLOBAL commands')

    if bot.guilds:
        logger.info(f'Bot is in {len(bot.guilds)} guild(s)')

    logger.info('\n=== Loading Initial Databases (with fallback chain) ===')

    # Use fallback chain for story data
    await dm.load_all_with_fallback()

    # Set current source based on what loaded
    if dm.episodes and not dm.is_csv_source():
        dm.current_source = dm.available_sources[0].to_dict()
        search_index.build_story_index(dm.episodes)
        logger.info(f'✓ Story database loaded ({len(dm.episodes)} records)')
    elif dm.csv_data_loaded:
        dm.current_source = dm.available_sources[1].to_dict()
        search_index.build_story_index(dm.episodes)
        logger.info(f'✓ CSV story database loaded ({len(dm.csv_stories)} stories)')

    # Try local character database FIRST
    char_local_success = dm.load_char_csv_local(
        os.path.join(CFG.char_csv_local_path, CHAR_CSV_FILENAME)
    )
    if char_local_success:
        logger.info(f'✓ Local Characters CSV loaded ({len(dm.char_data)} characters)')
        logger.info('  Use /source_change 4 to switch to local character mode')
    else:
        # If local fails, try remote
        if AIOHTTP_AVAILABLE:
            logger.info('Attempting to fetch Characters CSV from GitHub...')
            char_remote_success = await dm.load_char_csv_remote(CHAR_CSV_RAW_URL)
            if char_remote_success:
                logger.info(f'✓ Remote Characters CSV loaded ({len(dm.char_data)} characters)')
                logger.info('  Use /source_change 3 to switch to remote character mode')
            else:
                logger.info('○ Remote Characters CSV unavailable')
        else:
            logger.info('○ Cannot fetch Characters CSV - aiohttp not installed')

    # Start auto-refresh background task
    auto_refresh_task.start()
    logger.info(f'Auto-refresh task started (interval: {AUTO_REFRESH_INTERVAL_HOURS}h)')

    # Final status summary
    logger.info('\n=== Status Summary ===')
    logger.info(f'Core commands: OK')
    logger.info(f'Source commands: OK')
    logger.info(f'JSON DB: {"✓ Ready" if dm.episodes else "✗ Disabled"}')
    logger.info(f'CSV Features: {"✓ Ready" if dm.csv_data_loaded else "○ Disabled (/source_change 2)"}')
    logger.info(f'Character Features: {"✓ Ready" if dm.char_data_loaded else "○ Disabled (/source_change 3 or 4)"}')
    logger.info(f'Search indexing: Enabled')
    logger.info(f'Rate limiting: {CFG.rate_per_user}/min per user, {CFG.rate_per_guild}/min per guild')
    logger.info(f'Search cache: Enabled (TTL: {CFG.search_cache_ttl}s, Max: {CFG.cache_max_size}, SQLite: {CACHE_DB_FILENAME})')
    logger.info(f'Fuzzy matching: Enabled (threshold: {CFG.fuzzy_threshold})')
    logger.info(f'Retry logic: Enabled (max: {CFG.max_retries})')
    logger.info(f'Logging: Rotating files {"+ JSON" if CFG.log_json else ""} enabled')
    logger.info(f'Metrics: Tracking enabled')
    logger.info(f'Connection pooling: {"Enabled" if AIOHTTP_AVAILABLE else "Disabled (aiohttp not installed)"}')
    logger.info(f'Admin-only commands: /source_change, /reload')
    logger.info(f'Auto-refresh: Every {AUTO_REFRESH_INTERVAL_HOURS}h')
    logger.info(f'Graceful shutdown: SIGTERM/SIGINT handlers active')

    if not dm.episodes and not dm.char_data:
        logger.critical(
            '⚠️  NO DATA SOURCES LOADED! Bot will be non-functional. '
            'Check file paths, internet connection, and source configuration.'
        )

    logger.info('\n✅ Bot is ready!\n')

# ============================================
# MAIN EXECUTION
# ============================================

if __name__ == '__main__':
    logger.info('=' * 60)
    logger.info(f'Starting Doraemon Search Bot v{BOT_VERSION}...')
    logger.info(f'Running in: {CFG.home_dir}')
    logger.info(f'Python version: {sys.version}')
    logger.info(f'Discord.py version: {discord.__version__}')
    logger.info('=' * 60)

    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f'Fatal error starting bot: {type(e).__name__}: {e}')
        logger.error('Check your DISCORD_TOKEN and permissions.')
        sys.exit(1)
