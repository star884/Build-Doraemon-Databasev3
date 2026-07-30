"""
Doraemon Search Bot — Improved Edition
A Discord bot for searching Doraemon manga stories, episodes, and characters.

Architecture: Class-based DatabaseManager with locking, caching, rate limiting,
structured logging, search indexing, and word-boundary character matching.
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
from typing import Optional, Tuple, List, Dict, Any, Callable

import discord
from discord import app_commands, Embed, Intents
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
# CONSTANTS (Moved before BotConfig to prevent NameError)
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


# ============================================
# LOGGING SETUP
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('doraemon-bot')


# ============================================
# CONFIGURATION
# ============================================

@dataclass
class BotConfig:
    """Central configuration loaded from environment or defaults."""
    # Paths
    home_dir: str = field(default_factory=lambda: os.path.expanduser("~"))
    
    # Pagination
    page_size: int = 10
    max_pages: int = 999
    
    # Cache
    search_cache_ttl: int = 300  # 5 minutes
    
    # Rate limiting
    rate_per_user: int = 10
    rate_window_seconds: int = 60
    
    # HTTP
    http_timeout_seconds: int = 30
    
    # Colors (hex)
    color_primary: int = 0x6d4aff
    color_error: int = 0xcc0000
    color_warning: int = 0xffaa00
    
    # Embed limits (Discord constants)
    embed_field_name_max: int = 256
    embed_field_value_max: int = 1024
    embed_total_max: int = 6000
    embed_field_max_count: int = 25
    
    # Search
    max_search_results: int = 10
    max_query_length: int = 200
    
    @property
    def csv_local_path(self) -> str:
        return os.path.join(self.home_dir, "Doraemon-CSV-DB", "database") + "/"
    
    @property
    def char_csv_local_path(self) -> str:  # FIXED: Now CHAR_CSV_REPO_NAME is defined
        return os.path.join(self.home_dir, CHAR_CSV_REPO_NAME) + "/"
    
    @property
    def csv_raw_base_url(self) -> str:
        return 'https://raw.githubusercontent.com/star884/Doraemon-CSV-DB/main/database/'


CFG = BotConfig()

# Constants that depend on config
CSV_FIELD_NAMES = [
    'Magazine code', 'Publication Date', 'English title', 'Japanese title',
    'English Kindle', 'Bilingual Print', 'Tankōbon', 'Complete Collection',
    '1979 anime', '1979 anime extra', '2005 anime', '2005 anime extra 1',
    '2005 anime extra 2', 'Movie', 'Movie extra', 'Short film', 'Notes'
]

VOLUME_COLUMNS = {
    'tankobon':  'Tankōbon',
    'complete':  'Complete Collection',
    'kindle':    'English Kindle',
    'bilingual': 'Bilingual Print',
}

KNOWN_MAGAZINES = {'YK', 'KG', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'BK', 'SD', 'TK', 'CC', 'CD'}

CHAR_CSV_LOCAL_PATH = os.path.join(CFG.home_dir, CHAR_CSV_REPO_NAME)
CHAR_CSV_LOCAL_FILE = os.path.join(CHAR_CSV_LOCAL_PATH, CHAR_CSV_FILENAME)

CHAR_CSV_FIELD_NAMES = [
    'Character Name',
    'Alternative Name',
    'Category',
    'Description',
    'Source References',
]


# ============================================
# INPUT SANITIZATION
# ============================================

def sanitize_query(query: str, max_length: int = 200) -> str:
    """Sanitize user input for search queries."""
    if not query:
        return ""
    query = query.strip()
    if len(query) > max_length:
        query = query[:max_length]
    return query


def validate_discord_token(token: str) -> bool:
    """Basic token format validation."""
    if not token:
        return False
    if len(token) < 50:
        return False
    return True


# ============================================
# RATE LIMITER
# ============================================

class RateLimiter:
    """Per-user, per-command sliding-window rate limiter."""
    
    def __init__(self, default_limit: int = 10, window_seconds: int = 60):
        self._usage: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        self.default_limit = default_limit
        self.window_seconds = window_seconds
    
    def check(self, user_id: str, command: str, limit: Optional[int] = None) -> bool:
        """Returns True if the request is allowed, False if rate-limited."""
        now = time.time()
        max_allowed = limit or self.default_limit
        cutoff = now - self.window_seconds
        key = (user_id, command)
        
        self._usage[key] = [t for t in self._usage[key] if t > cutoff]
        
        if len(self._usage[key]) >= max_allowed:
            return False
        
        self._usage[key].append(now)
        return True


rate_limiter = RateLimiter(
    default_limit=CFG.rate_per_user,
    window_seconds=CFG.rate_window_seconds
)


def rate_limited(command_name: str, limit: Optional[int] = None):
    """Decorator for slash commands that enforces rate limiting."""
    def decorator(func):
        @wraps(func)
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            user_id = str(interaction.user.id)
            if not rate_limiter.check(user_id, command_name, limit):
                embed = Embed(
                    title='Rate Limited',
                    description=f'You\'re using `/{command_name}` too quickly. '
                                f'Please wait {CFG.rate_window_seconds}s and try again.',
                    color=CFG.color_warning
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            return await func(interaction, *args, **kwargs)
        return wrapper
    return decorator


# ============================================
# SEARCH CACHE
# ============================================

class SearchCache:
    """TTL-based cache for search results to avoid redundant computation."""
    
    def __init__(self, ttl: int = 300):
        self._cache: Dict[str, Tuple[List[dict], float]] = {}
        self.ttl = ttl
    
    def _key(self, source_type: str, query: str) -> str:
        raw = f"{source_type}:{query}"
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
        self._cache[key] = (results, time.time())
    
    def invalidate(self) -> None:
        """Clear entire cache (call when source changes)."""
        self._cache.clear()


search_cache = SearchCache(ttl=CFG.search_cache_ttl)


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
        self._data: List[dict] = []
        self._char_data: List[dict] = []
    
    def build_story_index(self, data: List[dict]) -> None:
        """Build index for story/episode records."""
        self._data = data
        self.by_title_token.clear()
        self.by_magazine.clear()
        
        for idx, item in enumerate(data):
            title = (item.get('English title', '') or item.get('story_a', '')).lower()
            for token in re.findall(r'\w+', title):
                self.by_title_token.setdefault(token, []).append(idx)
            
            mag = item.get('Magazine code', '').upper()
            if mag:
                self.by_magazine.setdefault(mag, []).append(idx)
        
        logger.info(f'Built story index: {len(self.by_title_token)} title tokens, '
                     f'{len(self.by_magazine)} magazine codes')
    
    def build_char_index(self, data: List[dict]) -> None:
        """Build index for character records."""
        self._char_data = data
        self.by_char_name_token.clear()
        self.by_char_category.clear()
        
        for idx, item in enumerate(data):
            for field in ('Character Name', 'Alternative Name'):
                name = item.get(field, '').lower()
                for token in re.findall(r'\w+', name):
                    self.by_char_name_token.setdefault(token, []).append(idx)
            
            cat = item.get('Category', '').lower().strip()
            if cat:
                self.by_char_category.setdefault(cat, []).append(idx)
        
        logger.info(f'Built character index: {len(self.by_char_name_token)} name tokens, '
                     f'{len(self.by_char_category)} categories')
    
    def search_title(self, query: str) -> List[dict]:
        """Fast title search using the inverted index."""
        tokens = re.findall(r'\w+', query.lower())
        if not tokens:
            return []
        
        candidate_sets = []
        for token in tokens:
            if token in self.by_title_token:
                candidate_sets.append(set(self.by_title_token[token]))
            else:
                return []
        
        candidates = candidate_sets[0]
        for s in candidate_sets[1:]:
            candidates &= s
        
        return [self._data[i] for i in sorted(candidates)][:CFG.max_search_results]


search_index = SearchIndex()


# ============================================
# WORD BOUNDARY MATCHING FOR CHARACTERS
# ============================================

def word_boundary_match(query: str, text: str) -> bool:
    """Match query as a whole word/number within text using \\b boundaries."""
    if not query or not text:
        return False
    pattern = r'\b' + re.escape(query.lower()) + r'\b'
    return re.search(pattern, text.lower()) is not None


def search_characters(
    query: str,
    characters: List[dict],
    use_word_boundary: bool = True
) -> List[dict]:
    """Search characters with tiered matching and ranking."""
    q = query.lower()
    if not q:
        return []
    
    tier_1: List[dict] = []
    tier_2: List[dict] = []
    tier_3: List[dict] = []
    tier_4: List[dict] = []
    tier_5: List[dict] = []
    
    seen_ids: set = set()
    
    for char in characters:
        char_obj_id = id(char)
        if char_obj_id in seen_ids:
            continue
        
        name = char.get('Character Name', '')
        alt = char.get('Alternative Name', '')
        cat = char.get('Category', '')
        desc = char.get('Description', '')
        
        name_lower = name.lower()
        alt_lower = alt.lower()
        cat_lower = cat.lower()
        desc_lower = desc.lower()
        
        if use_word_boundary:
            if word_boundary_match(q, name_lower):
                tier_1.append(char)
                seen_ids.add(char_obj_id)
                continue
            
            if word_boundary_match(q, alt_lower):
                tier_2.append(char)
                seen_ids.add(char_obj_id)
                continue
        
        if q in cat_lower and char_obj_id not in seen_ids:
            tier_3.append(char)
            seen_ids.add(char_obj_id)
            continue
        
        if q in desc_lower and char_obj_id not in seen_ids:
            tier_4.append(char)
            seen_ids.add(char_obj_id)
            continue
        
        if q in name_lower and char_obj_id not in seen_ids:
            tier_5.append(char)
            seen_ids.add(char_obj_id)
    
    ranked = tier_1 + tier_2 + tier_3 + tier_4 + tier_5
    return ranked[:CFG.max_search_results]


# ============================================
# DATABASE MANAGER
# ============================================

class DatabaseManager:
    """Thread-safe manager for all data sources."""
    
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
            self.current_source.update(source)
    
    def is_csv_source(self) -> bool:
        return self.current_source['type'] == 'csv'
    
    def is_char_source(self) -> bool:
        return self.current_source['type'] == 'csv_char'
    
    def load_json_database(self) -> bool:
        """Load data from local JSON database."""
        try:
            db_path = 'database/search_index.json'
            with open(db_path, encoding='utf-8') as f:
                self.db = json.load(f)
            self.episodes = self.db.get('items', [])
            logger.info(f'Loaded {len(self.episodes)} episodes from JSON database')
            return True
        except FileNotFoundError:
            self.episodes = []
            logger.warning('JSON database file not found at database/search_index.json')
            return False
        except json.JSONDecodeError as e:
            self.episodes = []
            logger.error(f'Invalid JSON in database file: {e}')
            return False
        except Exception as e:
            self.episodes = []
            logger.error(f'Error loading JSON database: {e}')
            return False
    
    def _parse_csv_rows(self, reader) -> List[dict]:
        """Parse CSV rows using csv.reader with column indices."""
        stories = []
        header = next(reader, None)
        
        num_cols = len(header) if header else len(CSV_FIELD_NAMES)
        logger.info(f'CSV has {num_cols} columns')
        
        for row in reader:
            if not row or all(c.strip() == '' for c in row):
                continue
            while len(row) < len(CSV_FIELD_NAMES):
                row.append('')
            story = {}
            for i, name in enumerate(CSV_FIELD_NAMES):
                story[name] = row[i].strip() if i < len(row) else ''
            story['_raw'] = row
            stories.append(story)
        
        return stories
    
    def _parse_char_csv_rows(self, reader) -> List[dict]:
        """Parse character CSV rows using the expected header."""
        characters = []
        header = next(reader, None)
        
        if header:
            header = [h.strip() for h in header]
            logger.info(f'Character CSV header: {header}')
        else:
            header = CHAR_CSV_FIELD_NAMES
        
        for row in reader:
            if not row or all(c.strip() == '' for c in row):
                continue
            while len(row) < len(header):
                row.append('')
            entry = {}
            for i, col_name in enumerate(header):
                entry[col_name] = row[i].strip() if i < len(row) else ''
            entry['_raw'] = row
            characters.append(entry)
        
        return characters
    
    def load_csv_database_local(self, base_path: str) -> bool:
        """Load CSV data from local files."""
        try:
            logger.info(f'Loading CSV data from: {base_path}')
            
            if not os.path.isdir(base_path):
                logger.warning(f'Directory not found: {base_path}')
                logger.info('Run: git clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB')
                self.csv_stories = []
                self.csv_data_loaded = False
                return False
            
            story_file = os.path.join(base_path, 'story_index.csv')
            
            if not os.path.isfile(story_file):
                logger.warning(f'Story index not found at {story_file}')
                self.csv_stories = []
                self.csv_data_loaded = False
                return False
            
            with open(story_file, 'r', encoding='utf-8') as f:
                stories = self._parse_csv_rows(csv.reader(f))
            
            self.csv_stories = stories
            self.csv_data_loaded = True
            logger.info(f'Loaded {len(self.csv_stories)} stories from local CSV (offline mode)')
            
            if self.csv_stories:
                s = self.csv_stories[0]
                logger.info(f'  First story: {s.get("English title", "?")} | '
                             f'Magazine: {s.get("Magazine code", "?")} | '
                             f'Date: {s.get("Publication Date", "?")}')
            
            return True
        
        except PermissionError:
            logger.error(f'Permission denied accessing: {base_path}')
            self.csv_stories = []
            self.csv_data_loaded = False
            return False
        except Exception as e:
            logger.error(f'Error loading local CSV: {type(e).__name__}: {e}')
            self.csv_stories = []
            self.csv_data_loaded = False
            return False
    
    async def load_csv_database_live(self, source_url: str) -> bool:
        """Fetch CSV data from GitHub (fallback)."""
        if not AIOHTTP_AVAILABLE:
            logger.error('aiohttp not installed - remote fetching disabled')
            self.csv_stories = []
            self.csv_data_loaded = False
            return False
        
        async with self._lock:
            try:
                logger.info('Fetching CSV data from GitHub...')
                story_url = CFG.csv_raw_base_url + 'story_index.csv'
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(story_url, timeout=CFG.http_timeout_seconds) as resp:
                        if resp.status != 200:
                            logger.error(f'Failed to fetch CSV: HTTP {resp.status}')
                            self.csv_stories = []
                            self.csv_data_loaded = False
                            return False
                        
                        content = await resp.text(encoding='utf-8')
                        lines = content.splitlines()
                        
                        if not lines:
                            self.csv_stories = []
                            self.csv_data_loaded = False
                            return False
                        
                        stories = self._parse_csv_rows(csv.reader(lines))
                        
                        self.csv_stories = stories
                        self.csv_data_loaded = True
                        logger.info(f'Loaded {len(self.csv_stories)} stories from CSV (online mode)')
                        return True
            
            except Exception as e:
                logger.error(f'Error fetching CSV: {type(e).__name__}: {e}')
                self.csv_stories = []
                self.csv_data_loaded = False
                return False
    
    def load_char_csv_local(self, filepath: str) -> bool:
        """Load characters CSV from a local file."""
        try:
            if not os.path.isfile(filepath):
                logger.warning(f'Character CSV not found at {filepath}')
                self.char_data = []
                self.char_data_loaded = False
                return False
            
            with open(filepath, 'r', encoding='utf-8') as f:
                self.char_data = self._parse_char_csv_rows(csv.reader(f))
            
            self.char_data_loaded = True
            logger.info(f'Loaded {len(self.char_data)} characters from local CSV')
            if self.char_data:
                first = self.char_data[0]
                logger.info(f'  First: {first.get("Character Name", "?")} | '
                             f'Category: {first.get("Category", "?")}')
            return True
        
        except Exception as e:
            logger.error(f'Error loading local character CSV: {type(e).__name__}: {e}')
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
                logger.info(f'Fetching characters CSV from: {url}')
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=CFG.http_timeout_seconds) as resp:
                        if resp.status != 200:
                            logger.error(f'Failed to fetch character CSV: HTTP {resp.status}')
                            self.char_data = []
                            self.char_data_loaded = False
                            return False
                        
                        content = await resp.text(encoding='utf-8')
                        lines = content.splitlines()
                        
                        if not lines:
                            self.char_data = []
                            self.char_data_loaded = False
                            return False
                        
                        self.char_data = self._parse_char_csv_rows(csv.reader(lines))
                        self.char_data_loaded = True
                        logger.info(f'Loaded {len(self.char_data)} characters from remote CSV')
                        
                        if self.char_data:
                            first = self.char_data[0]
                            logger.info(f'  First: {first.get("Character Name", "?")} | '
                                         f'Category: {first.get("Category", "?")}')
                        return True
            
            except Exception as e:
                logger.error(f'Error fetching character CSV: {type(e).__name__}: {e}')
                self.char_data = []
                self.char_data_loaded = False
                return False
    
    def convert_csv_to_episode_format(self, stories: List[dict]) -> List[dict]:
        """Map CSV data to standard EPISODES format."""
        episodes = []
        for story in stories:
            anime_1979 = story.get('1979 anime', '')
            anime_2005 = story.get('2005 anime', '')
            anime_1979_extra = story.get('1979 anime extra', '')
            anime_2005_extra = story.get('2005 anime extra 1', '')
            
            anime_1979_full = f"{anime_1979}; {anime_1979_extra}" if anime_1979 and anime_1979_extra else anime_1979 or ''
            anime_2005_full = f"{anime_2005}; {anime_2005_extra}" if anime_2005 and anime_2005_extra else anime_2005 or ''
            
            in_ep = anime_1979 or anime_2005 or 'N/A'
            
            ep = {
                'in_season_episode': in_ep,
                'alt_in_episode': '',
                'jp_story_numbers': '',
                'jp_story_all': '',
                'story_a': story.get('English title', '') or story.get('Japanese title', ''),
                'story_b': story.get('Japanese title', ''),
                'category': 'manga_stories',
                'title': story.get('English title', '') or story.get('Japanese title', ''),
                'Magazine code': story.get('Magazine code', ''),
                'Publication Date': story.get('Publication Date', ''),
                'English title': story.get('English title', ''),
                'Japanese title': story.get('Japanese title', ''),
                'English Kindle': story.get('English Kindle', ''),
                'Bilingual Print': story.get('Bilingual Print', ''),
                'Tankōbon': story.get('Tankōbon', ''),
                'Complete Collection': story.get('Complete Collection', ''),
                '1979 anime': anime_1979_full,
                '2005 anime': anime_2005_full,
                'Movie': story.get('Movie', ''),
                'Short film': story.get('Short film', ''),
                'Notes': story.get('Notes', ''),
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
        """Load character database (local or remote) - ENHANCED WITH FALLBACK."""
        fetch_method = source_config.get('fetch_method', 'github_raw')
        source_url = source_config['url']
        
        if fetch_method == 'local_file':
            filename = source_config.get('files', [CHAR_CSV_FILENAME])[0]
            filepath = os.path.join(source_url.rstrip('/'), filename)
            success = self.load_char_csv_local(filepath)
            
            if not success:
                logger.info('Local load failed, attempting remote fallback...')
                success = await self.load_char_csv_remote(CHAR_CSV_RAW_URL)
        else:
            success = await self.load_char_csv_remote(source_url)
        
        if success:
            async with self._lock:
                self.char_data = self.char_data  # Fixed: Proper assignment
            logger.info(f'Character database ready with {len(self.char_data)} characters')
            search_index.build_char_index(self.char_data)
            search_cache.invalidate()
            return True
        
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
# EMBED HELPER FUNCTIONS
# ============================================

def truncate(text: str, max_len: int) -> str:
    """Safely truncate text to fit Discord limits."""
    if text and len(text) > max_len:
        return text[:max_len - 1] + '…'
    return text or ZERO_WIDTH_SPACE


def safe_add_field(embed: Embed, *, name: str, value: str = '', inline: bool = False) -> None:
    """Add a field to an embed, enforcing Discord limits and preventing empty values."""
    name = truncate(name or ZERO_WIDTH_SPACE, CFG.embed_field_name_max)
    value = value if value else ZERO_WIDTH_SPACE
    value = truncate(value, CFG.embed_field_value_max)
    if len(embed.fields) >= CFG.embed_field_max_count:
        return
    embed.add_field(name=name, value=value, inline=inline)


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
    
    extras = []
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
    
    lines = []
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
    
    lines = []
    for i, r in enumerate(results[1:max_shown + 1], start=2):
        name = r.get('Character Name', '?')
        cat = r.get('Category', '')
        lines.append(f'- {name} [{cat}]')
    
    safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value='\n'.join(lines), inline=False)


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
                        '- Verify folder exists: `~/Doraemon-CSV-DB/database/`\n'
                        '- Run: `git clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB`\n'
                        '- Then switch back: `/source_change 1`'
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
# SEARCH COMMANDS
# ============================================

@bot.tree.command(name='search', description='Search by title, episode, magazine code, or character name')
@app_commands.describe(query='Search term (e.g., nobita, Ep 7, YK, 1970-01)')
@rate_limited('search')
async def search_cmd(interaction: discord.Interaction, query: str):
    q_raw = sanitize_query(query, CFG.max_query_length)
    if not q_raw:
        embed = Embed(title='EmptyQuery',
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
    if cached is not None:
        logger.info(f'Cache hit for query "{q_raw}" (source: {source_type})')
        results = cached
    else:
        results = []
        if char_mode:
            results = search_characters(q, dm.char_data, use_word_boundary=True)
        elif csv_mode:
            if len(q) <= 4 and q.upper() in KNOWN_MAGAZINES:
                results = [ep for ep in dm.episodes if ep.get('Magazine code', '').lower() == q]
            elif q.startswith('ep ') or (q.startswith('ep') and len(q) > 2 and q[2:3].isdigit()):
                ep_num = q.replace('ep ', '').replace('ep', '').strip()
                for ep in dm.episodes:
                    val = ep.get('1979 anime', '').lower()
                    if ep_num in val:
                        results.append(ep)
            elif q.startswith('2005') and len(q) > 5:
                ep_num = q[5:].strip()
                for ep in dm.episodes:
                    val = ep.get('2005 anime', '').lower()
                    if ep_num in val:
                        results.append(ep)
            elif re.match(r'^\d{4}-\d{2}$', q):
                results = [ep for ep in dm.episodes if ep.get('Publication Date', '').lower() == q]
            if not results:
                for ep in dm.episodes:
                    story_a = ep.get('story_a', '').lower()
                    story_b = ep.get('story_b', '').lower() if ep.get('story_b') else ''
                    if q in story_a or q in story_b:
                        results.append(ep)
                results = results[:CFG.max_search_results]
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
                for ep in dm.episodes:
                    story_a = ep.get('story_a', '').lower()
                    story_b = ep.get('story_b', '').lower() if ep.get('story_b') else ''
                    if q in story_a or q in story_b:
                        results.append(ep)
                results = results[:5]
        
        search_cache.set(source_type, q, results)
    
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
            '*Word-boundary matching is used for names —*\n'
            '*so "nobita" matches "Nobita" exactly, not "Nobisuke".*\n'
            '*If no word-boundary hits, substring fallback applies.*'
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
        
        if alt:
            embed.add_field(name='Alternative Name', value=alt, inline=True)
        else:
            embed.add_field(name='Alternative Name', value='N/A', inline=True)
        
        embed.add_field(name='Category', value=cat, inline=True)
        embed.add_field(name='Description', value=truncate(desc, CFG.embed_field_value_max), inline=False)
        
        if refs:
            embed.add_field(name='Source References',
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
    
    results = []
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
# LIST COMMAND
# ============================================

@bot.tree.command(name='list', description='List episodes with pagination')
@app_commands.describe(page='Page number (1-999)', season='Filter by magazine code (CSV) or season (JSON)')
@rate_limited('list')
async def list_cmd(interaction: discord.Interaction, page: int = 1, season: str = None):
    if not dm.episodes and not dm.char_data:
        embed = Embed(title='Database Empty', description='No episodes loaded.',
                      color=CFG.color_error)
        await interaction.response.send_message(embed=embed)
        return
    
    # Fix: Use char_data when in char_mode
    if dm.is_char_source():
        filtered = dm.char_data[:]
    else:
        filtered = dm.episodes[:]
    
    if season:
        s = sanitize_query(season).lower()
        if dm.is_csv_source():
            filtered = [e for e in filtered if e.get('Magazine code', '').lower() == s]
        elif dm.is_char_source():
            filtered = [e for e in filtered if e.get('Category', '').lower() == s]
        else:
            if s == 'special':
                filtered = [e for e in filtered if e.get('category') == 'special_episodes']
            elif s == 'classic':
                filtered = [e for e in filtered if e.get('category') == 'classic_doraemon']
            elif s.isdigit():
                filtered = [e for e in filtered if e.get('category') == f'season_{s}']
    
    if page < 1:
        page = 1
    
    total_pages = (len(filtered) + CFG.page_size - 1) // CFG.page_size if filtered else 1
    
    if page > total_pages:
        embed = Embed(
            title='Page Not Found',
            description=f'Page {page} does not exist. Valid pages: 1-{total_pages}\n'
                        f'**Source:** {dm.current_source["name"]}',
            color=CFG.color_error
        )
        await interaction.response.send_message(embed=embed)
        return
    
    start = (page - 1) * CFG.page_size
    end = min(start + CFG.page_size, len(filtered))
    
    chunk = filtered[start:end]
    filter_display = season if season else 'All'
    
    if dm.is_char_source():
        embed = Embed(title=f'Characters - Page {page} ({filter_display})', color=CFG.color_primary)
    else:
        embed = Embed(title=f'Doraemon Episodes - Page {page} ({filter_display})', color=CFG.color_primary)
    
    embed.description = f'Total: **{len(filtered)}** | Showing {start + 1}-{end}'
    
    csv_mode = dm.is_csv_source()
    char_mode = dm.is_char_source()
    
    for r in chunk:
        if char_mode:
            name = r.get('Character Name', '?')
            alt = r.get('Alternative Name', '')
            cat = r.get('Category', '?')
            field_name = f'{name} [{cat}]'
            field_value = truncate(r.get('Description', ''), 50)
            if alt:
                field_value += f'\n**Alt Name:** {alt}'
            safe_add_field(embed, name=field_name, value=field_value, inline=False)
        elif csv_mode:
            mag = r.get('Magazine code', '?')
            pub_date = r.get('Publication Date', '?')
            title = r.get('English title', '') or r.get('Japanese title', 'Unknown')
            jp_title = r.get('Japanese title', '')
            anime_1979 = r.get('1979 anime', '')
            anime_2005 = r.get('2005 anime', '')
            field_name = f'[{mag}] {pub_date} | {truncate(title, 40)}'
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
    
    embed.set_footer(text=f'Page {page} of {total_pages} | Source: {dm.current_source["name"]}')
    await interaction.response.send_message(embed=embed)


# ============================================
# STATS COMMAND
# ============================================

@bot.tree.command(name='stats', description='Show database statistics')
@rate_limited('stats')
async def stats_cmd(interaction: discord.Interaction):
    embed = Embed(title='Database Statistics', color=CFG.color_primary)
    
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
        for cat, cnt in sorted(categories.items(), key=lambda x: -x[1]):
            embed.add_field(name=cat, value=f'**{cnt}**', inline=True)
        
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
        for mag, cnt in sorted(magazines.items(), key=lambda x: -x[1]):
            embed.add_field(name=mag, value=f'**{cnt}**', inline=True)
        
        # FIXED: Proper indentation - moved outside the if block
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
            f'**URL:** {dm.current_source["name"]}\n\n'
            f'**Breakdown:**'
        )
        for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            embed.add_field(name=cat.replace('_', ' ').title(), value=f'**{cnt}**', inline=True)

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
            '  *Uses word-boundary matching for names — "nobita" matches "Nobita", not "Nobisuke"*\n'
            '- `/character <name>` - Dedicated character lookup (same word-boundary matching)\n'
            '- `/characters_by_category <category>` - Filter characters by category\n'
            '- `/list [page] [category]` - Browse characters (filter by category)\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source (1=JSON, 2=Manga CSV, 3=Remote Chars, 4=Local Chars)\n\n'
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
            '- `/list [page] [magazine]` - Browse stories (filter by magazine: G1, YK, etc.)\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source (1=JSON, 2=Manga CSV, 3=Remote Chars, 4=Local Chars)\n\n'
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
            '- `/list [page] [season]` - Browse episodes with pagination\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_status` - Detailed status of all sources\n'
            '- `/source_change <id>` - Switch source (1=JSON, 2=Manga CSV, 3=Remote Chars, 4=Local Chars)\n\n'
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
    results = []
    for s in dm.csv_stories:
        val = s.get('1979 anime', '').lower()
        val_extra = s.get('1979 anime extra', '').lower()
        if q in val or q in val_extra:
            results.append(s)

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
    results = []
    for s in dm.csv_stories:
        val = s.get('2005 anime', '').lower()
        val_extra = s.get('2005 anime extra 1', '').lower()
        val_extra2 = s.get('2005 anime extra 2', '').lower()
        if q in val or q in val_extra or q in val_extra2:
            results.append(s)

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

    results = []

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

        vol_parts = []
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

        field_name = truncate(title, CFG.embed_field_value_max)
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

    results = search_characters(q_lower, dm.char_data, use_word_boundary=True)

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
    """Handle legacy prefix command errors (rarely triggered)."""
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
        except:
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

    # Step 2: Try local CSV database (non-blocking)
    csv_success = dm.load_csv_database_local(CFG.csv_local_path)
    if csv_success:
        async with dm._lock:
            dm.episodes = dm.convert_csv_to_episode_format(dm.csv_stories)
        dm.csv_data_loaded = True
        logger.info(f'✓ Local CSV loaded ({len(dm.csv_stories)} stories)')
        dm.current_source.update(dm.available_sources[1])
    else:
        logger.info('○ Local CSV not available (use /source_change 2)')

    # Step 3: Try local character database FIRST (non-blocking)
    char_local_success = dm.load_char_csv_local(
        os.path.join(CFG.char_csv_local_path, CHAR_CSV_FILENAME)
    )
    if char_local_success:
        logger.info(f'✓ Local Characters CSV loaded ({len(dm.char_data)} characters)')
        logger.info('  Use /source_change 4 to switch to local character mode')
    else:
        # Step 4: If local fails, try remote (only if aiohttp available)
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
    logger.info(f'Search cache: Enabled (TTL: {CFG.search_cache_ttl}s)')
    logger.info('\n✅ Bot is ready!\n')

# ============================================
# MAIN EXECUTION
# ============================================

if __name__ == '__main__':
    logger.info('=' * 60)
    logger.info('Starting Doraemon Search Bot...')
    logger.info(f'Running in: {CFG.home_dir}')
    logger.info(f'Python version: {sys.version}')
    logger.info('=' * 60)

    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f'Fatal error starting bot: {type(e).__name__}: {e}')
        logger.error('Check your DISCORD_TOKEN and permissions.')
        sys.exit(1)
