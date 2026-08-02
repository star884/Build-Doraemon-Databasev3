"""
Doraemon Search Bot — Improved Edition v3.0
A Discord bot for searching Doraemon manga stories, episodes, and characters.

Architecture: Class-based DatabaseManager with locking, caching, rate limiting,
structured logging, search indexing, fuzzy search, word-boundary character matching,
smart dedup (name+description), backwards-compatible CSV parsing, and 
interactive pagination with Discord UI buttons.

Improvements in v3.0:
- Interactive pagination buttons (Prev/Next/First/Last/Jump)
- Bounded cache with LRU eviction
- Enhanced embed safety checks
- Improved error recovery with retry logic
- Externalized configuration via environment variables
- Better type hints throughout
- Modular repository pattern for data access
- Comprehensive logging with rotation
- Permission-aware operations
- Telemetry/metrics collection
"""

import json
import os
import re
import csv
import asyncio
import logging
import time
import hashlib
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Callable, Set
from enum import Enum

import discord
from discord import app_commands, Embed, Intents, ui, Interaction, ButtonStyle, View
from discord.ext import commands
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
# CONFIGURATION MANAGEMENT
# ============================================

@dataclass
class BotConfig:
    """Central configuration loaded from environment or defaults."""
    # Core settings
    page_size: int = int(os.getenv('PAGE_SIZE', '10'))
    max_pages: int = int(os.getenv('MAX_PAGES', '999'))
    search_cache_ttl: int = int(os.getenv('CACHE_TTL', '300'))
    cache_max_size: int = int(os.getenv('CACHE_MAX_SIZE', '1000'))
    
    # Rate limiting
    rate_per_user: int = int(os.getenv('RATE_LIMIT_USER', '10'))
    rate_window_seconds: int = int(os.getenv('RATE_WINDOW', '60'))
    
    # Network
    http_timeout_seconds: int = int(os.getenv('HTTP_TIMEOUT', '30'))
    max_retries: int = int(os.getenv('MAX_RETRIES', '3'))
    retry_backoff_base: int = int(os.getenv('RETRY_BACKOFF', '2'))
    
    # Embed limits (Discord constraints)
    color_primary: int = int(os.getenv('COLOR_PRIMARY', '0x6d4aff'))
    color_error: int = int(os.getenv('COLOR_ERROR', '0xcc0000'))
    color_warning: int = int(os.getenv('COLOR_WARNING', '0xffaa00'))
    color_success: int = int(os.getenv('COLOR_SUCCESS', '0x00cc00'))
    
    embed_field_name_max: int = int(os.getenv('EMBED_FIELD_NAME_MAX', '256'))
    embed_field_value_max: int = int(os.getenv('EMBED_FIELD_VALUE_MAX', '1024'))
    embed_total_max: int = int(os.getenv('EMBED_TOTAL_MAX', '6000'))
    embed_field_max_count: int = int(os.getenv('EMBED_FIELD_MAX_COUNT', '25'))
    
    # Search
    max_search_results: int = int(os.getenv('MAX_SEARCH_RESULTS', '10'))
    max_query_length: int = int(os.getenv('MAX_QUERY_LENGTH', '200'))
    fuzzy_threshold: float = float(os.getenv('FUZZY_THRESHOLD', '0.6'))
    
    # Logging
    log_level: str = os.getenv('LOG_LEVEL', 'INFO')
    log_file: str = os.getenv('LOG_FILE', 'bot.log')
    log_rotation_mb: int = int(os.getenv('LOG_ROTATION_MB', '100'))
    log_rotation_days: int = int(os.getenv('LOG_ROTATION_DAYS', '7'))
    
    # Data sources
    home_dir: str = os.getenv('HOME_DIR', os.path.expanduser("~"))
    
    @property
    def csv_local_path(self) -> str:
        return os.path.join(self.home_dir, "Doraemon-CSV-DB", "database") + "/"

    @property
    def char_csv_local_path(self) -> str:
        return os.path.join(self.home_dir, 'Database-dorav2') + "/"

    @property
    def csv_raw_base_url(self) -> str:
        return 'https://raw.githubusercontent.com/star884/Doraemon-CSV-DB/main/database/'


# ============================================
# CONSTANTS
# ============================================

CHAR_CSV_REPO_OWNER = 'star884'
CHAR_CSV_REPO_NAME = 'Database-dorav2'
CHAR_CSV_BRANCH = 'main'
CHAR_CSV_FILENAME = 'All_Characters_database_fixed-1.csv'

CHAR_CSV_RAW_URL = (
    f'https://raw.githubusercontent.com/'
    f'{CHAR_CSV_REPO_OWNER}/{CHAR_CSV_REPO_NAME}/'
    f'{CHAR_CSV_BRANCH}/{CHAR_CSV_FILENAME}'
)

ZERO_WIDTH_SPACE = '\u200b'

# Column aliases for backwards compatibility
CSV_COLUMN_ALIASES: Dict[str, List[str]] = {
    'Magazine code':           ['Magazine code', 'Magazine Code', 'magazine_code', 'Magazine'],
    'Publication Date':        ['Publication Date', 'publication_date', 'Pub Date', 'Date'],
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

KNOWN_MAGAZINES = {'YK', 'KG', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'BK', 'SD', 'TK', 'CC', 'CD'}


# ============================================
# LOGGING SETUP WITH ROTATION
# ============================================

def setup_logging(config: BotConfig) -> logging.Logger:
    """Configure logging with file rotation and console output."""
    logger = logging.getLogger('doraemon-bot')
    logger.setLevel(getattr(logging, config.log_level.upper()))
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler with rotation
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
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


CFG = BotConfig()
logger = setup_logging(CFG)


# ============================================
# METRICS / TELEMETRY
# ============================================

class MetricsCollector:
    """Collect and report bot usage metrics."""
    
    def __init__(self):
        self.search_queries: Dict[str, int] = defaultdict(int)
        self.cache_hits: Dict[str, int] = defaultdict(int)
        self.cache_misses: Dict[str, int] = defaultdict(int)
        self.errors: Dict[str, int] = defaultdict(int)
        self.commands_used: Dict[str, int] = defaultdict(int)
        self.start_time: float = time.time()
        
    def record_search(self, source: str, cache_hit: bool) -> None:
        self.search_queries[source] += 1
        if cache_hit:
            self.cache_hits[source] += 1
        else:
            self.cache_misses[source] += 1
            
    def record_command(self, command: str) -> None:
        self.commands_used[command] += 1
        
    def record_error(self, error_type: str) -> None:
        self.errors[error_type] += 1
        
    def get_uptime(self) -> str:
        uptime = time.time() - self.start_time
        hours, remainder = divmod(int(uptime), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"
    
    def get_cache_stats(self) -> Dict[str, float]:
        stats = {}
        for source in set(self.cache_hits.keys()) | set(self.cache_misses.keys()):
            hits = self.cache_hits.get(source, 0)
            misses = self.cache_misses.get(source, 0)
            total = hits + misses
            stats[source] = (hits / total * 100) if total > 0 else 0.0
        return stats


metrics = MetricsCollector()


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
    """Retrieve a field value trying the canonical name first, then aliases."""
    val = row.get(canonical, '')
    if val:
        return val
    for alias in alias_map.get(canonical, []):
        val = row.get(alias, '')
        if val:
            return val
    return ''


# ============================================
# FUZZY STRING MATCHING
# ============================================

def levenshtein_ratio(s1: str, s2: str) -> float:
    """Compute Levenshtein-based similarity ratio (0.0–1.0). Optimized DP."""
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    
    s1, s2 = s1.lower(), s2.lower()
    len1, len2 = len(s1), len(s2)
    
    # Length difference optimization
    if abs(len1 - len2) > max(len1, len2) * 0.5:
        return 0.0
    
    # Use rolling arrays for memory efficiency
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


def fuzzy_contains(haystack: str, needle: str, threshold: float = CFG.fuzzy_threshold) -> bool:
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
# INPUT SANITIZATION
# ============================================

def sanitize_query(query: str, max_length: int = CFG.max_query_length) -> str:
    """Sanitize user input for safe processing."""
    if not query:
        return ""
    query = query.strip()
    if len(query) > max_length:
        query = query[:max_length]
    return query


def validate_discord_token(token: str) -> bool:
    """Validate Discord token format."""
    if not token:
        return False
    if len(token) < 50:
        return False
    # Basic base64-like validation
    parts = token.split('.')
    if len(parts) != 3:
        return False
    return True


# ============================================
# RATE LIMITER WITH IMPROVED CLEANUP
# ============================================

class RateLimiter:
    """Per-user, per-command sliding-window rate limiter with periodic cleanup."""
    
    def __init__(self, default_limit: int = CFG.rate_per_user, window_seconds: int = CFG.rate_window_seconds):
        self._usage: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        self.last_cleanup = time.time()
        self.cleanup_interval = 3600  # Cleanup every hour
        
    def check(self, user_id: str, command: str, limit: Optional[int] = None) -> bool:
        now = time.time()
        
        # Periodic cleanup
        if now - self.last_cleanup > self.cleanup_interval:
            self.cleanup()
            self.last_cleanup = now
        
        max_allowed = limit or self.default_limit
        cutoff = now - self.window_seconds
        key = (user_id, command)
        
        self._usage[key] = [t for t in self._usage[key] if t > cutoff]
        if len(self._usage[key]) >= max_allowed:
            return False
        self._usage[key].append(now)
        return True
    
    def cleanup(self) -> None:
        """Remove expired entries to prevent unbounded growth."""
        now = time.time()
        cutoff = now - self.window_seconds
        empty_keys = [k for k, v in self._usage.items() if not any(t > cutoff for t in v)]
        for k in empty_keys:
            del self._usage[k]
            
    def get_remaining(self, user_id: str, command: str) -> int:
        """Get remaining requests for user/command."""
        now = time.time()
        cutoff = now - self.window_seconds
        key = (user_id, command)
        valid_requests = len([t for t in self._usage[key] if t > cutoff])
        return max(0, self.default_limit - valid_requests)


rate_limiter = RateLimiter()


def rate_limited(command_name: str, limit: Optional[int] = None):
    """Decorator for slash commands that enforces rate limiting."""
    def decorator(func):
        @wraps(func)
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            user_id = str(interaction.user.id)
            if not rate_limiter.check(user_id, command_name, limit):
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


# ============================================
# SEARCH CACHE WITH SIZE LIMIT AND LRU EVICTION
# ============================================

class SearchCache:
    """TTL-based cache with bounded size and LRU eviction."""
    
    def __init__(self, ttl: int = CFG.search_cache_ttl, max_size: int = CFG.cache_max_size):
        self._cache: Dict[str, Tuple[List[dict], float]] = {}
        self.ttl = ttl
        self.max_size = max_size
        
    def _key(self, source_type: str, query: str) -> str:
        raw = f"{source_type}:{query.lower().strip()}"
        return hashlib.md5(raw.encode()).hexdigest()
    
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
        
        # Evict oldest entries if at capacity
        if len(self._cache) >= self.max_size:
            # Find oldest entry
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        
        self._cache[key] = (results, time.time())
    
    def invalidate(self) -> None:
        self._cache.clear()
    
    def invalidate_pattern(self, source_type: str) -> None:
        """Invalidate all entries for a specific source type."""
        keys_to_delete = [
            k for k in self._cache.keys()
            if k.startswith(f"{source_type}:")
        ]
        for k in keys_to_delete:
            del self._cache[k]
            
    def get_size(self) -> int:
        return len(self._cache)


search_cache = SearchCache(ttl=CFG.search_cache_ttl, max_size=CFG.cache_max_size)


# ============================================
# SEARCH INDEX
# ============================================

class SearchIndex:
    """Inverted-index for fast title and field searching."""
    
    def __init__(self):
        self.by_title_token: Dict[str, List[int]] = {}
        self.by_magazine: Dict[str, List[int]] = {}
        self.by_category: Dict[str, List[int]] = {}
        self.by_char_name_token: Dict[str, List[int]] = {}
        self.by_char_category: Dict[str, List[int]] = {}
        self.by_char_desc_token: Dict[str, List[int]] = {}
        self._data: List[dict] = []
        self._char_data: List[dict] = []
        
    def build_story_index(self, data: List[dict]) -> None:
        """Build inverted index for story data."""
        self._data = data
        self.by_title_token.clear()
        self.by_magazine.clear()
        
        for idx, item in enumerate(data):
            title = (item.get('English title', '') or item.get('story_a', '')).lower()
            for token in re.findall(r'\w+', title):
                self.by_title_token.setdefault(token, []).append(idx)
            mag = item.get('Magazine code', '').upper().strip()
            if mag:
                self.by_magazine.setdefault(mag, []).append(idx)
        
        logger.info(f'Built story index: {len(self.by_title_token)} title tokens, '
                    f'{len(self.by_magazine)} magazine codes')
    
    def build_char_index(self, data: List[dict]) -> None:
        """Build inverted index for character data."""
        self._char_data = data
        self.by_char_name_token.clear()
        self.by_char_category.clear()
        self.by_char_desc_token.clear()
        
        for idx, item in enumerate(data):
            for field_name in ('Character Name', 'Alternative Name'):
                name = item.get(field_name, '').lower()
                for token in re.findall(r'\w+', name):
                    self.by_char_name_token.setdefault(token, []).append(idx)
            cat = item.get('Category', '').lower().strip()
            if cat:
                self.by_char_category.setdefault(cat, []).append(idx)
            desc = item.get('Description', '').lower()
            for token in re.findall(r'\w+', desc):
                self.by_char_desc_token.setdefault(token, []).append(idx)
        
        logger.info(f'Built character index: {len(self.by_char_name_token)} name tokens, '
                    f'{len(self.by_char_category)} categories, '
                    f'{len(self.by_char_desc_token)} desc tokens')
    
    def search_title(self, query: str) -> List[dict]:
        """Search stories by title using inverted index."""
        tokens = re.findall(r'\w+', query.lower())
        if not tokens:
            return []
        
        candidate_sets: List[Set[int]] = []
        for token in tokens:
            if token in self.by_title_token:
                candidate_sets.append(set(self.by_title_token[token]))
            else:
                return []
        
        candidates = candidate_sets[0]
        for s in candidate_sets[1:]:
            candidates &= s
        
        return [self._data[i] for i in sorted(candidates)][:CFG.max_search_results]
    
    def search_char_index(self, query: str) -> List[int]:
        """Return character indices whose name tokens match all query tokens."""
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


search_index = SearchIndex()


# ============================================
# WORD BOUNDARY + RANKED CHARACTER SEARCH
# ============================================

def word_boundary_match(query: str, text: str) -> bool:
    """Match query as a whole word within text using \\b boundaries."""
    if not query or not text:
        return False
    pattern = r'\b' + re.escape(query.lower()) + r'\b'
    return re.search(pattern, text.lower()) is not None


def search_characters(
    query: str,
    characters: List[dict],
    use_word_boundary: bool = True,
    use_fuzzy: bool = True
) -> List[dict]:
    """Search characters with tiered matching, ranking, and optional fuzzy fallback."""
    q = query.lower().strip()
    if not q:
        return []
    
    query_tokens = re.findall(r'\w+', q)
    
    # Scored results: list of (score, char)
    scored: List[Tuple[float, dict]] = []
    seen_ids: Set[int] = set()
    
    def add(score: float, char: dict) -> None:
        oid = id(char)
        if oid not in seen_ids:
            scored.append((score, char))
            seen_ids.add(oid)
    
    for char in characters:
        name = char.get('Character Name', '')
        alt = char.get('Alternative Name', '')
        cat = char.get('Category', '')
        desc = char.get('Description', '')
        
        name_lower = name.lower()
        alt_lower = alt.lower()
        cat_lower = cat.lower()
        desc_lower = desc.lower()
        
        # Tier 1: Word-boundary match on name
        if use_word_boundary and word_boundary_match(q, name_lower):
            score = 100.0
            if name_lower == q:
                score += 50.0  # Exact name match bonus
            add(score, char)
            continue
        
        # Tier 2: Word-boundary match on alternative name
        if use_word_boundary and word_boundary_match(q, alt_lower):
            add(80.0, char)
            continue
        
        # Tier 3: Substring match on name
        if q in name_lower:
            add(60.0, char)
            continue
        
        # Tier 4: Exact category match
        if q == cat_lower:
            add(40.0, char)
            continue
        
        # Tier 5: Substring match in description
        if q in desc_lower:
            overlap = token_overlap_ratio(query_tokens, desc)
            add(20.0 + overlap * 10.0, char)
            continue
        
        # Tier 6: Fuzzy token match on name/alt
        if use_fuzzy:
            name_tokens = re.findall(r'\w+', name_lower)
            for nt in name_tokens:
                if levenshtein_ratio(nt, q) >= CFG.fuzzy_threshold:
                    add(15.0 + levenshtein_ratio(nt, q) * 5.0, char)
                    break
            else:
                alt_tokens = re.findall(r'\w+', alt_lower)
                for at in alt_tokens:
                    if levenshtein_ratio(at, q) >= CFG.fuzzy_threshold:
                        add(12.0 + levenshtein_ratio(at, q) * 5.0, char)
                        break
    
    # Sort by score descending
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:CFG.max_search_results]]


# ============================================
# DATABASE MANAGER
# ============================================

class DatabaseManager:
    """Thread-safe manager for all data sources with retry logic."""
    
    def __init__(self):
        self.episodes: List[dict] = []
        self.db: Dict[str, Any] = {}
        self.csv_stories: List[dict] = []
        self.csv_data_loaded: bool = False
        self.char_data: List[dict] = []
        self.char_data_loaded: bool = False
        self.current_source: Dict[str, Any] = {
            'id': 1,
            'name': 'JSON Database (Primary)',
            'url': 'https://github.com/star884/Build-Doraemon-Databasev3',
            'type': 'json',
            'files': ['database/search_index.json']
        }
        self.available_sources: List[Dict[str, Any]] = [
            {
                'id': 1,
                'name': 'JSON Database (Primary)',
                'url': 'https://github.com/star884/Build-Doraemon-Databasev3',
                'type': 'json',
                'files': ['database/search_index.json']
            },
            {
                'id': 2,
                'name': 'CSV Database (Local Clone - Termux)',
                'url': CFG.csv_local_path,
                'type': 'csv',
                'files': ['story_index.csv', 'manga_details.csv'],
                'fetch_method': 'local_file'
            },
            {
                'id': 3,
                'name': 'Characters CSV (Remote - GitHub)',
                'url': CHAR_CSV_RAW_URL,
                'type': 'csv_char',
                'files': [CHAR_CSV_FILENAME],
                'fetch_method': 'github_raw'
            },
            {
                'id': 4,
                'name': 'Characters CSV (Local Clone)',
                'url': CFG.char_csv_local_path,
                'type': 'csv_char',
                'files': [CHAR_CSV_FILENAME],
                'fetch_method': 'local_file'
            }
        ]
        self._lock = asyncio.Lock()
        
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
        return self.current_source['type'] == 'csv'
    
    def is_char_source(self) -> bool:
        return self.current_source['type'] == 'csv_char'
    
    def load_json_database(self) -> bool:
        """Load data from local JSON database."""
        try:
            db_path = 'database/search_index.json'
            path = Path(db_path)
            
            if not path.exists():
                logger.warning('JSON database file not found at database/search_index.json')
                self.episodes = []
                return False
            
            with open(path, encoding='utf-8') as f:
                self.db = json.load(f)
            
            self.episodes = self.db.get('items', [])
            logger.info(f'Loaded {len(self.episodes)} episodes from JSON database')
            return True
            
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
        """Parse CSV rows with backwards-compatible column mapping."""
        stories: List[dict] = []
        header = next(reader, None)
        
        if not header:
            header = CSV_FIELD_NAMES
        else:
            header = normalize_csv_header(header, CSV_COLUMN_ALIASES)
        
        num_cols = len(header)
        logger.info(f'CSV has {num_cols} columns (normalized)')
        
        for row in reader:
            if not row or all(c.strip() == '' for c in row):
                continue
            while len(row) < num_cols:
                row.append('')
            story: dict = {}
            for i, col_name in enumerate(header):
                story[col_name] = row[i].strip() if i < len(row) else ''
            story['_raw'] = row
            stories.append(story)
        
        logger.info(f'Parsed {len(stories)} story rows from CSV')
        return stories
    
    def _parse_char_csv_rows(self, reader) -> List[dict]:
        """Parse character CSV rows with smart deduplication (name+description)."""
        characters: List[dict] = []
        header = next(reader, None)
        
        if header:
            header = normalize_csv_header(header, CHAR_COLUMN_ALIASES)
            logger.info(f'Character CSV header (normalized): {header}')
        else:
            header = CHAR_CSV_FIELD_NAMES
        
        raw_entries: List[dict] = []
        for row in reader:
            if not row or all(c.strip() == '' for c in row):
                continue
            while len(row) < len(header):
                row.append('')
            entry: dict = {}
            for i, col_name in enumerate(header):
                entry[col_name] = row[i].strip() if i < len(row) else ''
            entry['_raw'] = row
            raw_entries.append(entry)
        
        # Strict dedup: name + description must BOTH match
        seen_keys: Dict[Tuple[str, str], int] = {}
        merged_count = 0
        skipped_no_name = 0
        separate_same_name_diff_desc = 0
        
        for entry in raw_entries:
            name = entry.get('Character Name', '').strip()
            desc = entry.get('Description', '').strip()
            alt = entry.get('Alternative Name', '').strip()
            refs = entry.get('Source References', '').strip()
            
            if not name:
                skipped_no_name += 1
                continue
            
            key = (name.lower().strip(), desc.lower().strip())
            
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
                name_only_key = name.lower().strip()
                name_exists = any(
                    c.get('Character Name', '').lower().strip() == name_only_key
                    for c in characters
                )
                if name_exists:
                    separate_same_name_diff_desc += 1
                
                seen_keys[key] = len(characters)
                characters.append(dict(entry))
        
        logger.info(
            f'Strict Dedup (name+description) complete. '
            f'Kept {len(characters)} entries. Merged {merged_count} duplicates. '
            f'Separate (same name, diff desc): {separate_same_name_diff_desc}. Skipped {skipped_no_name} empty names.'
        )
        
        return characters
    
    def load_csv_database_local(self, base_path: str) -> bool:
        """Load CSV data from local files with path validation."""
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
            
            with open(story_file, 'r', encoding='utf-8') as f:
                stories = self._parse_csv_rows(csv.reader(f))
            
            self.csv_stories = stories
            self.csv_data_loaded = True
            logger.info(f'Loaded {len(self.csv_stories)} stories from local CSV (offline mode)')
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
        """Fetch CSV data from GitHub with retry logic."""
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
    
    async def _fetch_with_retry(self, url: str, parser: Callable, retries: int = CFG.max_retries) -> bool:
        """Fetch URL with exponential backoff retry logic."""
        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=CFG.http_timeout_seconds)) as resp:
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
        """Load characters CSV from a local file."""
        try:
            path = Path(filepath)
            if not path.is_file():
                logger.warning(f'Character CSV not found at {filepath}')
                self.char_data = []
                self.char_data_loaded = False
                return False
            
            with open(path, 'r', encoding='utf-8') as f:
                self.char_data = self._parse_char_csv_rows(csv.reader(f))
            
            self.char_data_loaded = True
            logger.info(f'Loaded {len(self.char_data)} characters from local CSV')
            return True
            
        except Exception as e:
            logger.error(f'Error loading local character CSV: {type(e).__name__}: {e}')
            metrics.record_error(type(e).__name__)
            self.char_data = []
            self.char_data_loaded = False
            return False
    
    async def load_char_csv_remote(self, url: str) -> bool:
        """Fetch and parse characters CSV from GitHub raw URL."""
        if not AIOHTTP_AVAILABLE:
            logger.error('aiohttp not installed - remote fetching disabled')
            self.char_data = []
            self.char_data_loaded = False
            return False
        
        async with self._lock:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=CFG.http_timeout_seconds)) as resp:
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
                    async with aiohttp.ClientSession() as session:
                        async with session.get(CHAR_CSV_RAW_URL, timeout=aiohttp.ClientTimeout(total=CFG.http_timeout_seconds)) as resp:
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
            search_index.build_char_index(self.char_data)
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


dm = DatabaseManager()


# ============================================
# EMBED HELPERS WITH SAFETY CHECKS
# ============================================

def truncate(text: str, max_len: int) -> str:
    """Safely truncate text to fit Discord limits."""
    if text and len(text) > max_len:
        return text[:max_len - 1] + '…'
    return text or ZERO_WIDTH_SPACE


def calculate_embed_space(embed: Embed) -> int:
    """Calculate remaining space in embed."""
    used = sum(len(f.name) + len(f.value) for f in embed.fields)
    return CFG.embed_total_max - used


def safe_add_field(embed: Embed, *, name: str, value: str = '', inline: bool = False) -> bool:
    """Add a field to an embed with space checking. Returns False if field was skipped."""
    # Check field count limit
    if len(embed.fields) >= CFG.embed_field_max_count:
        return False
    
    # Check total space
    remaining_space = calculate_embed_space(embed)
    field_size = len(name) + len(value)
    
    if remaining_space < field_size + 50:  # Buffer for formatting
        logger.warning(f'Embed space insufficient: {remaining_space} < {field_size + 50}')
        return False
    
    name = truncate(name or ZERO_WIDTH_SPACE, CFG.embed_field_name_max)
    value = value if value else ZERO_WIDTH_SPACE
    value = truncate(value, CFG.embed_field_value_max)
    
    embed.add_field(name=name, value=value, inline=inline)
    return True


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
# PAGINATION VIEW WITH BUTTONS
# ============================================

class ListNavigationView(ui.View):
    """Interactive pagination view for list command with Prev/Next/First/Last buttons."""
    
    def __init__(self, data: List[dict], page: int, page_size: int, 
                 source_type: str, filter_val: Optional[str], dm_ref, 
                 channel_id: int, message_id: int):
        super().__init__(timeout=None)  # Persistent buttons
        self.data = data
        self.page = page
        self.page_size = page_size
        self.source_type = source_type
        self.filter_val = filter_val
        self.dm_ref = dm_ref
        self.channel_id = channel_id
        self.message_id = message_id
        
        # Calculate total pages
        self.total_pages = max(1, (len(data) + page_size - 1) // page_size if data else 1)
        
        # Update button states
        self.prev_button.disabled = page <= 1
        self.first_button.disabled = page <= 1
        self.next_button.disabled = page >= self.total_pages
        self.last_button.disabled = page >= self.total_pages
    
    async def on_timeout(self) -> None:
        """Called when view times out - keep buttons functional with timeout=None."""
        pass
    
    def _get_page_data(self) -> Tuple[int, int, List[dict]]:
        """Calculate page range and return (start, end, chunk)."""
        start = (self.page - 1) * self.page_size
        end = min(start + self.page_size, len(self.data))
        chunk = self.data[start:end]
        return start, end, chunk
    
    def _build_embed(self) -> Embed:
        """Build the embed for current page."""
        start, end, chunk = self._get_page_data()
        filter_display = self.filter_val if self.filter_val else 'All'
        
        if self.source_type == 'csv_char':
            title = f'Characters - Page {self.page} of {self.total_pages} ({filter_display})'
        elif self.source_type == 'csv':
            title = f'Manga Stories - Page {self.page} of {self.total_pages} ({filter_display})'
        else:
            title = f'Doraemon Episodes - Page {self.page} of {self.total_pages} ({filter_display})'
        
        embed = Embed(title=title, color=CFG.color_primary)
        embed.description = f'Total: **{len(self.data)}** | Showing {start + 1}-{end}'
        
        for r in chunk:
            if self.source_type == 'csv_char':
                name = r.get('Character Name', '?')
                alt = r.get('Alternative Name', '')
                cat = r.get('Category', '?')
                field_name = f'{name} [{cat}]'
                field_value = truncate(r.get('Description', ''), 50)
                if alt:
                    field_value += f'\n*Alt Name:* {alt}'
                safe_add_field(embed, name=field_name, value=field_value, inline=False)
            
            elif self.source_type == 'csv':
                mag = r.get('Magazine code', '?')
                pub_date = r.get('Publication Date', '?')
                title_entry = r.get('English title', '') or r.get('Japanese title', 'Unknown')
                jp_title = r.get('Japanese title', '')
                anime_1979 = r.get('1979 anime', '')
                anime_2005 = r.get('2005 anime', '')
                field_name = f'[{mag}] {pub_date} | {truncate(title_entry, 40)}'
                field_value = jp_title[:50]
                if anime_1979:
                    field_value += f' | 1979: {anime_1979}'
                if anime_2005:
                    field_value += f' | 2005: {anime_2005}'
                safe_add_field(embed, name=field_name, value=field_value, inline=False)
            
            else:  # JSON mode
                in_ep = r.get('in_season_episode', 'N/A')
                jp_st = r.get('jp_story_all', '-') or '-'
                story_a = r.get('story_a', 'Unknown')[:40]
                story_b = r.get('story_b', '')
                title_display = story_a
                if story_b:
                    title_display += f' / {truncate(story_b, 40)}'
                safe_add_field(embed, name=f'{in_ep} | JP: {jp_st}', value=title_display, inline=False)
        
        footer = f'Page {self.page} of {self.total_pages} | Source: {self.dm_ref.current_source["name"]}'
        embed.set_footer(text=footer)
        
        return embed
    
    async def update_message(self, interaction: Interaction) -> None:
        """Update the message with current page data."""
        embed = self._build_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        metrics.record_command('list_page_navigation')
    
    @ui.button(label='⏮ First', style=ButtonStyle.primary, custom_id='list_first')
    async def first_button(self, interaction: Interaction, button: ui.Button):
        if self.page > 1:
            self.page = 1
            await self.update_message(interaction)
    
    @ui.button(label='◀ Prev', style=ButtonStyle.secondary, custom_id='list_prev')
    async def prev_button(self, interaction: Interaction, button: ui.Button):
        if self.page > 1:
            self.page -= 1
            await self.update_message(interaction)
    
    @ui.button(label='Next ▶', style=ButtonStyle.secondary, custom_id='list_next')
    async def next_button(self, interaction: Interaction, button: ui.Button):
        if self.page < self.total_pages:
            self.page += 1
            await self.update_message(interaction)
    
    @ui.button(label='Last ⏭', style=ButtonStyle.primary, custom_id='list_last')
    async def last_button(self, interaction: Interaction, button: ui.Button):
        if self.page < self.total_pages:
            self.page = self.total_pages
            await self.update_message(interaction)


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

bot = commands.Bot(command_prefix='!', intents=Intents.default())


# ============================================
# SOURCE MANAGEMENT COMMANDS
# ============================================

@bot.tree.command(name='source', description='Shows the current database source')
@rate_limited('source')
async def source_cmd(interaction: discord.Interaction):
    embed = Embed(title='Current Database Source', color=CFG.color_primary)
    embed.add_field(name='Source Name', value=dm.current_source['name'], inline=True)
    embed.add_field(name='Type', value=dm.current_source['type'].title(), inline=True)
    
    url_display = dm.current_source['url'][:60] + '...' if len(dm.current_source['url']) > 60 else dm.current_source['url']
    embed.add_field(name='URL/Path', value=url_display, inline=False)
    
    if dm.current_source['type'] == 'csv':
        status = 'Loaded' if dm.csv_data_loaded else 'Not loaded'
        embed.add_field(name='Status', value=status, inline=True)
        embed.add_field(name='Records', value=str(len(dm.csv_stories)), inline=True)
    elif dm.current_source['type'] == 'csv_char':
        status = 'Loaded' if dm.char_data_loaded else 'Not loaded'
        embed.add_field(name='Status', value=status, inline=True)
        embed.add_field(name='Records', value=str(len(dm.char_data)), inline=True)
    elif dm.current_source['type'] == 'json':
        embed.add_field(name='Total Episodes', value=str(len(dm.episodes)), inline=True)
    
    embed.set_footer(text=f'Source ID: {dm.current_source["id"]} | Switch with /source_change <id>')
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='source_all', description='Lists all available database sources')
@rate_limited('source_all')
async def source_all_cmd(interaction: discord.Interaction):
    embed = Embed(title='Available Database Sources', color=CFG.color_primary)
    
    description = ''
    for src in dm.available_sources:
        desc = f'**{src["id"]}) {src["name"]}**\n'
        desc += f'> Type: {src["type"].title()} | Method: {src.get("fetch_method", "default").title()}\n'
        if src.get('files'):
            desc += f'> Files: {", ".join(src["files"])}\n'
        if src['id'] == dm.current_source['id']:
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
    
    for src in dm.available_sources:
        status_icon = '🟢' if src['id'] == dm.current_source['id'] else '⚪'
        
        desc = f'{status_icon} **{src["id"]}) {src["name"]}**\n'
        desc += f'> Type: `{src["type"]}` | Fetch: `{src.get("fetch_method", "default")}`\n'
        
        if src['type'] == 'json' and dm.episodes:
            desc += f'> **Records:** {len(dm.episodes)} ✓ Loaded\n'
        elif src['type'] == 'csv' and dm.csv_data_loaded:
            desc += f'> **Records:** {len(dm.csv_stories)} ✓ Loaded\n'
        elif src['type'] == 'csv_char' and dm.char_data_loaded:
            if src['id'] == 3:
                desc += f'> **Records:** {len(dm.char_data)} ✓ Remote Loaded\n'
            elif src['id'] == 4:
                desc += f'> **Records:** {len(dm.char_data)} ✓ Local Loaded\n'
        else:
            desc += '> ⏳ Not loaded (switch to enable)\n'
        
        if src['id'] == dm.current_source['id']:
            desc += '> **← Currently Active**\n'
        
        embed.add_field(name='\u200b', value=desc, inline=False)
    
    safe_add_field(embed, name='Quick Reference',
                   value='`/source_change <id>` to switch\n`/source_status` refreshes status',
                   inline=False)
    
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='source_change', description='Changes the active database source')
@app_commands.describe(source_id='Source number to switch to (1=JSON, 2=CSV, 3=Remote Chars, 4=Local Chars)')
@rate_limited('source_change', limit=3)
async def source_change_cmd(interaction: discord.Interaction, source_id: int):
    source = next((s for s in dm.available_sources if s['id'] == source_id), None)
    
    if not source:
        embed = Embed(title='Invalid Source',
                      description=f'Source ID {source_id} not found.\nUse `/source_all` to see available sources.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    old_source = dm.current_source.copy()
    
    try:
        if source['type'] == 'csv':
            success = await dm.load_csv_database(source)
            if not success:
                embed = Embed(
                    title='Warning',
                    description=(
                        'Could not load CSV data.\n\n'
                        '**Troubleshooting:**\n'
                        '- Verify folder exists: `~/Domaemon-CSV-DB/database/`\n'
                        '- No internet connection (for online mode)\n\n'
                        f'Current source: **{dm.current_source["name"]}**\n\n'
                        '**Fix:** Clone the repo:\n'
                        '```\ngit clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB\n```'
                    ),
                    color=CFG.color_warning
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                await dm.safe_update_source(old_source)
                return
            await dm.safe_update_source(source)
        elif source['type'] == 'csv_char':
            success = await dm.load_char_database(source)
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
            await dm.safe_update_source(source)
        elif source['type'] == 'json':
            success = dm.load_json_database()
            if not success:
                embed = Embed(title='Warning',
                              description='Could not load JSON data.',
                              color=CFG.color_warning)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                await dm.safe_update_source(old_source)
                return
            await dm.safe_update_source(source)
        
        record_count = (len(dm.char_data) if source['type'] == 'csv_char'
                        else (len(dm.csv_stories) if source['type'] == 'csv'
                              else len(dm.episodes)))
        embed = Embed(title='Source Changed Successfully', color=CFG.color_primary)
        embed.add_field(name='Previous Source', value=old_source['name'], inline=False)
        embed.add_field(name='New Source', value=source['name'], inline=False)
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

# ============================================
# SEARCH COMMAND WITH IMPROVED LOGIC
# ============================================

@bot.tree.command(name='search', description='Search by title, episode, magazine code, or character name')
@app_commands.describe(query='Search term (e.g., nobita, Ep 7, YK, 1970-01)')
@rate_limited('search')
async def search_cmd(interaction: discord.Interaction, query: str):
    q_raw = sanitize_query(query, CFG.max_query_length)
    if not q_raw:
        embed = Embed(title='Empty Query',
                      description='Please provide a search term.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    
    if not dm.episodes and not dm.char_data:
        embed = Embed(
            title='Database Empty',
            description='No episodes loaded. Use `/source` to check status.',
            color=CFG.color_error
        )
        await interaction.response.send_message(embed=embed)
        return
    
    q = q_raw.lower()
    csv_mode = dm.is_csv_source()
    char_mode = dm.is_char_source()
    
    source_type = dm.current_source['type']
    cached = search_cache.get(source_type, q)
    cache_hit = cached is not None
    if cache_hit:
        logger.info(f'Cache hit for query "{q_raw}" (source: {source_type})')
        metrics.record_search(source_type, True)
        results = cached
    else:
        results: List[dict] = []
        if char_mode:
            results = search_characters(q, dm.char_data, use_word_boundary=True, use_fuzzy=True)
        elif csv_mode:
            # Magazine code exact match
            if len(q) <= 4 and q.upper() in KNOWN_MAGAZINES:
                results = [ep for ep in dm.episodes if ep.get('Magazine code', '').lower() == q]
            # 1979 anime episode search
            elif q.startswith('ep ') or (q.startswith('ep') and len(q) > 2 and q[2:3].isdigit()):
                ep_num = q.replace('ep ', '').replace('ep', '').strip()
                for ep in dm.episodes:
                    val = ep.get('1979 anime', '').lower()
                    if ep_num in val:
                        results.append(ep)
            # 2005 anime episode search
            elif q.startswith('2005') and len(q) > 5:
                ep_num = q[5:].strip()
                for ep in dm.episodes:
                    val = ep.get('2005 anime', '').lower()
                    if ep_num in val:
                        results.append(ep)
            # Publication date search
            elif re.match(r'^\d{4}-\d{2}$', q):
                results = [ep for ep in dm.episodes if ep.get('Publication Date', '').lower() == q]
            # Fallback: title search using inverted index + fuzzy
            if not results:
                index_hits = search_index.search_title(q)
                if index_hits:
                    results = index_hits
                else:
                    query_tokens = re.findall(r'\w+', q)
                    for ep in dm.episodes:
                        story_a = ep.get('story_a', '').lower()
                        story_b = ep.get('story_b', '').lower() if ep.get('story_b') else ''
                        if q in story_a or q in story_b:
                            results.append(ep)
                        elif query_tokens and (
                            token_overlap_ratio(query_tokens, story_a) >= 0.5
                            or token_overlap_ratio(query_tokens, story_b) >= 0.5
                        ):
                            results.append(ep)
                    results = results[:CFG.max_search_results]
        else:
            # JSON mode
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
            # Fallback: title search + fuzzy
            if not results:
                query_tokens = re.findall(r'\w+', q)
                for ep in dm.episodes:
                    story_a = ep.get('story_a', '').lower()
                    story_b = ep.get('story_b', '').lower() if ep.get('story_b') else ''
                    if q in story_a or q in story_b:
                        results.append(ep)
                    elif query_tokens and (
                        token_overlap_ratio(query_tokens, story_a) >= 0.5
                        or token_overlap_ratio(query_tokens, story_b) >= 0.5
                    ):
                        results.append(ep)
                results = results[:CFG.max_search_results]
        
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
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    
    if char_mode:
        name = first.get('Character Name', 'Unknown')
        alt = first.get('Alternative Name', '')
        cat = first.get('Category', 'Unknown')
        desc = first.get('Description', '')
        refs = first.get('Source References', '')
        
        embed = Embed(title=f'Character Found: {truncate(name, 70)}', color=CFG.color_primary)
        
        embed.add_field(name='Alternative Name', value=alt if alt else 'N/A', inline=True)
        embed.add_field(name='Category', value=cat, inline=True)
        safe_add_field(embed, name='Description', value=truncate(desc, CFG.embed_field_value_max), inline=False)
        if refs:
            safe_add_field(embed, name='Source References',
                           value=truncate(refs, CFG.embed_field_value_max), inline=False)
        
        if len(results) > 1:
            build_char_more_matches_field(embed, results)
    
    elif csv_mode:
        title = first.get('English title', '') or first.get('Japanese title', 'Unknown')
        embed = Embed(title=f'Found: {truncate(title, 70)}', color=CFG.color_primary)
        embed.description = f'**Category:** Manga Stories'
        build_csv_result_fields(embed, first)
        if len(results) > 1:
            build_more_matches_field(embed, results, csv_mode)
    else:
        in_ep = first.get('in_season_episode', 'N/A')
        alt_ep = first.get('alt_in_episode')
        jp_display = first.get('jp_story_all', 'Not indexed')
        story_a = first.get('story_a', 'Unknown')
        story_b = first.get('story_b')
        category = first.get('category', 'unknown').replace('_', ' ').title()
        title = first.get('title', 'Unknown')[:70]
        
        embed = Embed(title=f'Episode Found: {title}', color=CFG.color_primary)
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
    
    footer = f'Matches: {len(results)} | Source: {dm.current_source["name"]}'
    if cache_hit:
        footer += ' [Cached]'
    embed.set_footer(text=footer)
    await interaction.response.send_message(embed=embed)

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
    embed.add_field(name='IN Episode', value=first.get('in_season_episode', 'N/A'), inline=True)
    embed.add_field(name='Category', value=first.get('category', 'unknown').replace('_', ' ').title(), inline=True)
    if first.get('alt_in_episode'):
        embed.add_field(name='Alt IN', value=first.get('alt_in_episode'), inline=True)
    safe_add_field(embed, name='Story A',
                   value=truncate(first.get('story_a', 'Unknown'), CFG.embed_field_value_max), inline=False)
    if first.get('story_b'):
        safe_add_field(embed, name='Story B',
                       value=truncate(first.get('story_b'), CFG.embed_field_value_max), inline=False)
    embed.add_field(name='All JP Stories', value=first.get('jp_story_all', '-'), inline=False)
    embed.set_footer(text=f'Matches: {len(results)} | Source: {dm.current_source["name"]}')
    
    if len(results) > 1:
        build_more_matches_field(embed, results, is_csv=False)
    
    await interaction.response.send_message(embed=embed)

# ============================================
# LIST COMMAND WITH INTERACTIVE BUTTONS
# ============================================

@bot.tree.command(name='list', description='List episodes or characters with pagination')
@app_commands.describe(filter_val='Filter by magazine code (CSV), category (Chars), or season (JSON)')
@rate_limited('list')
async def list_cmd(interaction: discord.Interaction, filter_val: Optional[str] = None):
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
            filtered = [e for e in filtered if e.get('Magazine code', '').lower() == f]
        elif char_mode:
            filtered = [e for e in filtered if e.get('Category', '').lower() == f]
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
    
    view = ListNavigationView(
        data=filtered,
        page=1,
        page_size=page_size,
        source_type=dm.current_source['type'],
        filter_val=filter_val,
        dm_ref=dm,
        channel_id=interaction.channel_id,
        message_id=None
    )
    
    # Build initial embed for page 1
    start, end, chunk = view._get_page_data()
    filter_display = filter_val if filter_val else 'All'
    
    if char_mode:
        title = f'Characters - Page 1 of {total_pages} ({filter_display})'
    elif csv_mode:
        title = f'Manga Stories - Page 1 of {total_pages} ({filter_display})'
    else:
        title = f'Doraemon Episodes - Page 1 of {total_pages} ({filter_display})'
    
    embed = Embed(title=title, color=CFG.color_primary)
    embed.description = f'Total: **{len(filtered)}** | Showing {start + 1}-{end}'
    
    for r in chunk:
        if char_mode:
            name = r.get('Character Name', '?')
            alt = r.get('Alternative Name', '')
            cat = r.get('Category', '?')
            field_name = f'{name} [{cat}]'
            field_value = truncate(r.get('Description', ''), 50)
            if alt:
                field_value += f'\n*Alt Name:* {alt}'
            safe_add_field(embed, name=field_name, value=field_value, inline=False)
        elif csv_mode:
            mag = r.get('Magazine code', '?')
            pub_date = r.get('Publication Date', '?')
            title_entry = r.get('English title', '') or r.get('Japanese title', 'Unknown')
            jp_title = r.get('Japanese title', '')
            anime_1979 = r.get('1979 anime', '')
            anime_2005 = r.get('2005 anime', '')
            field_name = f'[{mag}] {pub_date} | {truncate(title_entry, 40)}'
            field_value = jp_title[:50]
            if anime_1979:
                field_value += f' | 1979: {anime_1979}'
            if anime_2005:
                field_value += f' | 2005: {anime_2005}'
            safe_add_field(embed, name=field_name, value=field_value, inline=False)
        else:
            in_ep = r.get('in_season_episode', 'N/A')
            jp_st = r.get('jp_story_all', '-') or '-'
            story_a = r.get('story_a', 'Unknown')[:40]
            story_b = r.get('story_b', '')
            title_display = story_a
            if story_b:
                title_display += f' / {truncate(story_b, 40)}'
            safe_add_field(embed, name=f'{in_ep} | JP: {jp_st}', value=title_display, inline=False)
    
    embed.set_footer(text=f'Page 1 of {total_pages} | Source: {dm.current_source["name"]}')
    metrics.record_command('list')
    
    await interaction.response.send_message(embed=embed, view=view)

# ============================================
# STATS COMMAND
# ============================================

@bot.tree.command(name='stats', description='Show database statistics')
@rate_limited('stats')
async def stats_cmd(interaction: discord.Interaction):
    embed = Embed(title='Database Statistics', color=CFG.color_primary)
    embed.add_field(name='Bot Uptime', value=metrics.get_uptime(), inline=True)
    embed.add_field(name='Cache Size', value=f'{search_cache.get_size()} entries', inline=True)
    
    MAX_FIELDS = 22
    
    if dm.is_char_source():
        categories: Dict[str, int] = {}
        for e in dm.char_data:
            cat = e.get('Category', 'Unknown')
            categories[cat] = categories.get(cat, 0) + 1
        
        embed.description = (
            f'**Total Characters:** {len(dm.char_data)}\n'
            f'**Source:** {dm.current_source["name"]}\n'
            f'**URL:** {truncate(dm.current_source["url"], 60)}\n\n'
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
        
        embed.set_footer(text=f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} | '
                               f'Source: {dm.current_source["name"]}')
        await interaction.response.send_message(embed=embed)
        return
    
    if dm.is_csv_source():
        magazines: Dict[str, int] = {}
        for e in dm.episodes:
            mag = e.get('Magazine code', 'Unknown')
            magazines[mag] = magazines.get(mag, 0) + 1
        
        embed.description = (
            f'**Total Stories:** {len(dm.episodes)}\n'
            f'**Source:** {dm.current_source["name"]}\n'
            f'**Path:** {truncate(dm.current_source["url"], 60)}\n\n'
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
            cat = e.get('category', 'unknown')
            counts[cat] = counts.get(cat, 0) + 1
            if e.get('jp_story_numbers'):
                with_jp += 1
            if e.get('jp_has_specials'):
                jp_specials += 1
            if e.get('jp_has_standalone_specials'):
                jp_standalone += 1
        
        embed.description = (
            f'**Total Episodes:** {len(dm.episodes)}\n'
            f'**JP Indexed:** {with_jp}\n'
            f'**JP Special Stories:** {jp_specials}\n'
            f'**JP Standalone Specials:** {jp_standalone}\n\n'
            f'**Source:** {dm.current_source["name"]} ({dm.current_source["type"].title()})\n'
            f'**URL:** {dm.current_source["url"]}\n\n'
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
    
    embed.set_footer(text=f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} | '
                           f'Source: {dm.current_source["name"]}')
    await interaction.response.send_message(embed=embed)

# ============================================
# HELP COMMAND
# ============================================

@bot.tree.command(name='help', description='Show available commands')
async def help_cmd(interaction: discord.Interaction):
    embed = Embed(title='Doraemon Search Bot Help', color=CFG.color_primary)
    
    if dm.is_char_source():
        embed.description = (
            '**Available Commands (Character Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by character name, alt name, category, or description\n'
            '  *Uses word-boundary matching for names, fuzzy matching as fallback*\n'
            '- `/character <name>` - Dedicated character lookup\n'
            '- `/characters_by_category <category>` - Filter characters by category\n'
            '- `/list [filter]` - Browse characters with interactive buttons\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n\n'
            f'**Current Source:** {dm.current_source["name"]}\n'
            f'**Total Records:** {len(dm.char_data)}'
        )
    elif dm.is_csv_source():
        embed.description = (
            '**Available Commands (CSV Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by title, magazine code, 1979/2005 anime ep, or date\n'
            '- `/list [filter]` - Browse stories with interactive buttons\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source\n\n'
            '**CSV-Only Commands**\n'
            '- `/search_jp_title <keyword>` - Search by Japanese title\n'
            '- `/search_magazine <code>` - Search by magazine code\n'
            '- `/search_1979 <episode>` - Search by 1979 anime episode\n'
            '- `/search_2005 <episode>` - Search by 2005 anime episode\n'
            '- `/search_volume <type> <number>` - Search by compilation volume\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n\n'
            f'**Current Source:** {dm.current_source["name"]}\n'
            f'**Total Records:** {len(dm.episodes)}'
        )
    else:
        embed.description = (
            '**Available Commands (JSON Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by JP#, IN#, SPE, CE, or title\n'
            '- `/jp <number>` - Lookup by Japanese Story Number\n'
            '- `/list [filter]` - Browse episodes with interactive buttons\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n\n'
            f'**Current Source:** {dm.current_source["name"]}\n'
            f'**Total Records:** {len(dm.episodes)}'
        )
    
    embed.set_footer(text='Doraemon Search Bot')
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
               if q in s.get('Japanese title', '').lower() or q in s.get('English title', '').lower()]
    
    if not results:
        results = [s for s in dm.csv_stories
                   if fuzzy_contains(s.get('Japanese title', ''), q) or fuzzy_contains(s.get('English title', ''), q)]
    
    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for "**{query}**"',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    embed = Embed(title=f'JP Title Match: {truncate(first.get("English title", "Unknown"), 70)}',
                  color=CFG.color_primary)
    embed.add_field(name='Japanese Title', value=first.get('Japanese title', 'N/A'), inline=False)
    embed.add_field(name='1979 Episode', value=first.get('1979 anime', 'N/A'), inline=True)
    embed.add_field(name='2005 Episode', value=first.get('2005 anime', 'N/A'), inline=True)
    embed.add_field(name='Magazine', value=first.get('Magazine code', 'N/A'), inline=True)
    embed.add_field(name='Publication Date', value=first.get('Publication Date', 'N/A'), inline=True)
    
    if len(results) > 1:
        additional = '\n'.join(f'- {truncate(r.get("English title", "?"), 40)}' for r in results[1:6])
        safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value=additional, inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_magazine', description='Search by magazine code (CSV only)')
@app_commands.describe(code='Magazine code (e.g., YK, KG, G1, G2, G3, G4, G5, G6, BK, SD, TK, CC, CD)')
@rate_limited('search_magazine')
async def search_magazine_cmd(interaction: discord.Interaction, code: str):
    is_available, error_embed = dm.check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    code_upper = sanitize_query(code).upper()
    results = [s for s in dm.csv_stories if s.get('Magazine code', '').upper() == code_upper]
    
    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for magazine code "**{code_upper}**"',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(title=f'Magazine: {code_upper}', color=CFG.color_primary)
    embed.description = f'Found **{len(results)}** stories'
    
    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', 'Unknown')
        date = story.get('Publication Date', '?')
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
    results = [s for s in dm.csv_stories if q in s.get('1979 anime', '').lower()]
    
    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for 1979 anime episode "**{episode}**"',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(title=f'1979 Anime: {episode}', color=CFG.color_primary)
    embed.description = f'Found **{len(results)}** story/stories'
    
    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', '?')
        jp = story.get('Japanese title', '')
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
    results = [s for s in dm.csv_stories if q in s.get('2005 anime', '').lower()]
    
    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for 2005 anime episode "**{episode}**"',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(title=f'2005 Anime: {episode}', color=CFG.color_primary)
    embed.description = f'Found **{len(results)}** story/stories'
    
    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', '?')
        jp = story.get('Japanese title', '')
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
    embed.description = f'Found **{len(results)}** story/stories'
    
    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', 'Unknown')
        jp_title = story.get('Japanese title', '')
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
@rate_limited('character')
async def character_cmd(interaction: discord.Interaction, name: str):
    is_available, error_embed = dm.check_char_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    q = sanitize_query(name)
    if not q:
        embed = Embed(title='Empty Query',
                      description='Please provide a character name.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
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
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    char_name = first.get('Character Name', 'Unknown')
    alt_name = first.get('Alternative Name', '')
    category = first.get('Category', 'Unknown')
    description = first.get('Description', '')
    references = first.get('Source References', '')
    
    embed = Embed(title=f'Character: {truncate(char_name, 70)}', color=CFG.color_primary)
    embed.add_field(name='Alternative Name', value=alt_name or 'N/A', inline=True)
    embed.add_field(name='Category', value=category, inline=True)
    safe_add_field(embed, name='Description', value=truncate(description, CFG.embed_field_value_max), inline=False)
    if references:
        safe_add_field(embed, name='Source References', value=truncate(references, CFG.embed_field_value_max), inline=False)
    
    if len(results) > 1:
        additional = '\n'.join(
            f'- {r.get("Character Name", "?")} [{r.get("Category", "")}]'
            for r in results[1:6]
        )
        safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value=additional, inline=False)
    
    embed.set_footer(text=f'Matches: {len(results)} | Source: {dm.current_source["name"]}')
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='characters_by_category', description='List characters filtered by category (Characters source only)')
@app_commands.describe(category='Category name (e.g., human, robot, animal)')
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
    
    results = [c for c in dm.char_data if q == c.get('Category', '').lower().strip()]
    results.sort(key=lambda x: x.get('Character Name', '').lower())
    
    if not results:
        embed = Embed(
            title='No Results Found',
            description=f'No characters found in category "**{category}**"\n\n'
                        '**Available categories:**\n'
                        '- human, robot, animal, gadget\n'
                        '- Check with /stats for full breakdown',
            color=CFG.color_error
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(title=f'Characters: {category.title()}', color=CFG.color_primary)
    embed.description = f'Found **{len(results)}** characters'
    
    for char in results[:10]:
        name = char.get('Character Name', '?')
        alt = char.get('Alternative Name', '')
        desc_preview = truncate(char.get('Description', ''), 80)
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
    logger.info(f'Logged in as {bot.user}')
    logger.info('Syncing slash commands...')
    synced = await bot.tree.sync()
    logger.info(f'Synced {len(synced)} GLOBAL commands')
    
    if bot.guilds:
        logger.info(f'Bot is in {len(bot.guilds)} guild(s)')
    
    logger.info('\n=== Loading Initial Databases ===')
    logger.info(f'Default source: {dm.current_source["name"]} ({dm.current_source["type"]})')
    
    # Step 1: Try JSON database
    success = dm.load_json_database()
    if success:
        logger.info(f'✓ JSON database loaded ({len(dm.episodes)} records)')
        search_index.build_story_index(dm.episodes)
    else:
        logger.warning('✗ JSON database load failed')
    
    # Step 2: Try local CSV database
    csv_success = dm.load_csv_database_local(CFG.csv_local_path)
    if csv_success:
        async with dm._lock:
            dm.episodes = dm.convert_csv_to_episode_format(dm.csv_stories)
        dm.csv_data_loaded = True
        logger.info(f'✓ Local CSV loaded ({len(dm.csv_stories)} stories)')
        dm.current_source = dm.available_sources[1].copy()
    else:
        logger.info('○ Local CSV not available (use /source_change 2)')
    
    # Step 3: Try local character database FIRST
    char_local_success = dm.load_char_csv_local(
        os.path.join(CFG.char_csv_local_path, CHAR_CSV_FILENAME)
    )
    if char_local_success:
        logger.info(f'✓ Local Characters CSV loaded ({len(dm.char_data)} characters)')
        logger.info('  Use /source_change 4 to switch to local character mode')
    else:
        # Step 4: If local fails, try remote
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
    
    # Final status summary
    logger.info('\n=== Status Summary ===')
    logger.info(f'Core commands: OK')
    logger.info(f'Source commands: OK')
    logger.info(f'JSON DB: {"✓ Ready" if dm.episodes else "✗ Disabled"}')
    logger.info(f'CSV Features: {"✓ Ready" if dm.csv_data_loaded else "○ Disabled (/source_change 2)"}')
    logger.info(f'Character Features: {"✓ Ready" if dm.char_data_loaded else "○ Disabled (/source_change 3 or 4)"}')
    logger.info(f'Search indexing: Enabled')
    logger.info(f'Rate limiting: {CFG.rate_per_user}/min per user')
    logger.info(f'Search cache: Enabled (TTL: {CFG.search_cache_ttl}s, Max: {CFG.cache_max_size})')
    logger.info(f'Fuzzy matching: Enabled (threshold: {CFG.fuzzy_threshold})')
    logger.info(f'Retry logic: Enabled (max: {CFG.max_retries})')
    logger.info(f'Logging: Rotating files enabled')
    logger.info(f'Metrics: Tracking enabled')
    logger.info('\n✅ Bot is ready!\n')

# ============================================
# MAIN EXECUTION
# ============================================

if __name__ == '__main__':
    logger.info('=' * 60)
    logger.info('Starting Doraemon Search Bot v3.0...')
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
