#!/usr/bin/env python3

"""
Doraemon Image Database Client
==============================

Runtime client for Build-Doraemon-Databasev3.

The image-building process lives in:

    star884/Doraemon-Image-Database

This module is READ-ONLY.

It does not:
    - search Wikimedia
    - search Wikipedia
    - search Fandom
    - download image files
    - build the image database

It only:

    1. Downloads database/images.json
    2. Caches the manifest
    3. Resolves character names / aliases
    4. Returns published GitHub image URLs
    5. Provides image-database statistics

The image repository is the single source of truth.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp


logger = logging.getLogger(
    "doraemon-bot.images"
)


# ============================================================
# CONFIGURATION
# ============================================================

IMAGE_REPOSITORY_OWNER = os.getenv(
    "IMAGE_GITHUB_OWNER",
    "star884",
)

IMAGE_REPOSITORY_NAME = os.getenv(
    "IMAGE_GITHUB_REPOSITORY",
    "Doraemon-Image-Database",
)

IMAGE_REPOSITORY_BRANCH = os.getenv(
    "IMAGE_GITHUB_BRANCH",
    "main",
)

IMAGE_MANIFEST_PATH = (
    "database/images.json"
)

IMAGE_MANIFEST_URL = (
    "https://raw.githubusercontent.com/"
    f"{IMAGE_REPOSITORY_OWNER}/"
    f"{IMAGE_REPOSITORY_NAME}/"
    f"{IMAGE_REPOSITORY_BRANCH}/"
    f"{IMAGE_MANIFEST_PATH}"
)


# ------------------------------------------------------------
# Manifest cache
# ------------------------------------------------------------

MANIFEST_CACHE_SECONDS = max(
    30,
    int(
        os.getenv(
            "IMAGE_MANIFEST_CACHE_SECONDS",
            "300",
        )
    ),
)


# ------------------------------------------------------------
# HTTP timeout
# ------------------------------------------------------------

HTTP_TIMEOUT_SECONDS = max(
    5,
    int(
        os.getenv(
            "IMAGE_HTTP_TIMEOUT",
            "20",
        )
    ),
)


# ------------------------------------------------------------
# Maximum images returned to the bot
# ------------------------------------------------------------

MAX_IMAGES_PER_GALLERY = max(
    1,
    int(
        os.getenv(
            "IMAGE_MAX_GALLERY_IMAGES",
            "10",
        )
    ),
)


# ------------------------------------------------------------
# Manifest request retries
# ------------------------------------------------------------

MANIFEST_RETRIES = max(
    1,
    int(
        os.getenv(
            "IMAGE_MANIFEST_RETRIES",
            "4",
        )
    ),
)


# ============================================================
# SUPPORTED MANIFEST SCHEMAS
# ============================================================

# Schema 2 was used by the original bot client.
#
# The new Doraemon image builder generates schema 5.
#
# The actual structure needed by this client is compatible
# between these versions because it still contains:
#
#     manifest["characters"]
#
# and character entries still contain:
#
#     name
#     aliases
#     images
#
SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {
        2,
        3,
        4,
        5,
    }
)


# ============================================================
# DATA MODEL
# ============================================================

@dataclass(frozen=True)
class DoraemonImage:
    """
    One published character image.
    """

    image_id: str

    character_id: str

    character_name: str

    filename: str

    url: str

    width: int

    height: int

    source: str = ""

    source_url: str = ""

    format: str = ""

    bytes: int = 0

    sha256: str = ""


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_name(
    value: str,
) -> str:
    """
    Normalize names consistently with the image builder.

    This intentionally mirrors the builder's normalization:

        lower
        ampersand -> "and"
        remove apostrophes
        non-alphanumeric runs -> "-"
    """

    value = str(
        value or ""
    ).strip().lower()

    value = value.replace(
        "&",
        " and ",
    )

    value = value.replace(
        "’",
        "'",
    )

    value = re.sub(
        r"[’'`]",
        "",
        value,
    )

    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value,
    )

    value = re.sub(
        r"-+",
        "-",
        value,
    )

    return value.strip("-")


# ============================================================
# IMAGE URL SECURITY
# ============================================================

def is_allowed_image_url(
    url: str,
) -> bool:
    """
    Accept only raw GitHub URLs belonging to the configured
    image repository.

    This prevents an accidental or malicious manifest entry
    from turning the bot into an arbitrary remote-image proxy.
    """

    if not isinstance(
        url,
        str,
    ):
        return False

    url = url.strip()

    if not url:
        return False

    expected_prefix = (
        "https://raw.githubusercontent.com/"
        f"{IMAGE_REPOSITORY_OWNER}/"
        f"{IMAGE_REPOSITORY_NAME}/"
        f"{IMAGE_REPOSITORY_BRANCH}/"
    )

    return url.startswith(
        expected_prefix
    )


# ============================================================
# CLIENT
# ============================================================

class DoraemonImageDatabase:
    """
    Read-only runtime client for Doraemon-Image-Database.
    """

    def __init__(
        self,
        *,
        manifest_url: str = IMAGE_MANIFEST_URL,
        cache_seconds: int = MANIFEST_CACHE_SECONDS,
    ) -> None:

        self.manifest_url = (
            manifest_url
        )

        self.cache_seconds = max(
            30,
            cache_seconds,
        )

        self._session: (
            aiohttp.ClientSession
            | None
        ) = None

        self._manifest: (
            dict[str, Any]
            | None
        ) = None

        self._loaded_at = 0.0

        self._lock = asyncio.Lock()

    # ========================================================
    # HTTP SESSION
    # ========================================================

    async def _session_get(
        self,
    ) -> aiohttp.ClientSession:

        if (
            self._session is None
            or self._session.closed
        ):

            timeout = aiohttp.ClientTimeout(
                total=HTTP_TIMEOUT_SECONDS,
                connect=10,
                sock_read=HTTP_TIMEOUT_SECONDS,
            )

            connector = aiohttp.TCPConnector(
                limit=10,
                limit_per_host=5,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )

            self._session = (
                aiohttp.ClientSession(
                    timeout=timeout,
                    connector=connector,
                    headers={
                        "User-Agent": (
                            "Doraemon-Database/"
                            "5.x Image Client"
                        ),
                        "Accept": (
                            "application/json"
                        ),
                        "Cache-Control": (
                            "no-cache"
                        ),
                    },
                )
            )

        return self._session

    # ========================================================
    # CLOSE
    # ========================================================

    async def close(
        self,
    ) -> None:

        if self._session is not None:

            try:
                await self._session.close()
            finally:
                self._session = None

    # ========================================================
    # SCHEMA VALIDATION
    # ========================================================

    @staticmethod
    def _validate_manifest(
        payload: Any,
    ) -> dict[str, Any]:

        if not isinstance(
            payload,
            dict,
        ):
            raise RuntimeError(
                "Image manifest is not "
                "a JSON object."
            )

        schema_version = payload.get(
            "schema_version"
        )

        try:
            schema_version_int = int(
                schema_version
            )
        except (
            TypeError,
            ValueError,
        ):

            raise RuntimeError(
                "Image manifest has an invalid "
                "schema_version: "
                f"{schema_version!r}"
            )

        if (
            schema_version_int
            not in SUPPORTED_SCHEMA_VERSIONS
        ):

            raise RuntimeError(
                "Unsupported image database "
                f"schema: {schema_version_int!r}. "
                "Supported schemas: "
                + ", ".join(
                    str(value)
                    for value in sorted(
                        SUPPORTED_SCHEMA_VERSIONS
                    )
                )
            )

        characters = payload.get(
            "characters"
        )

        if not isinstance(
            characters,
            dict,
        ):

            raise RuntimeError(
                "Image manifest does not contain "
                "a valid 'characters' object."
            )

        return payload

    # ========================================================
    # MANIFEST DOWNLOAD
    # ========================================================

    async def _download_manifest(
        self,
    ) -> dict[str, Any]:

        session = await self._session_get()

        last_error: (
            Exception
            | None
        ) = None

        for attempt in range(
            1,
            MANIFEST_RETRIES + 1,
        ):

            try:

                # ------------------------------------------------
                # Add a cache-busting query parameter.
                #
                # This is useful when force_refresh=True and GitHub
                # raw content has recently changed.
                # ------------------------------------------------

                params = {
                    "_ts": str(
                        int(
                            time.time()
                        )
                    )
                }

                async with session.get(
                    self.manifest_url,
                    params=params,
                    allow_redirects=True,
                ) as response:

                    if response.status != 200:

                        raise RuntimeError(
                            "Image manifest returned "
                            f"HTTP {response.status}"
                        )

                    payload = (
                        await response.json(
                            content_type=None
                        )
                    )

                    manifest = (
                        self._validate_manifest(
                            payload
                        )
                    )

                    logger.info(
                        "Loaded Doraemon image "
                        "manifest: schema=%s "
                        "characters=%d",
                        manifest.get(
                            "schema_version"
                        ),
                        len(
                            manifest.get(
                                "characters",
                                {},
                            )
                        ),
                    )

                    return manifest

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RuntimeError,
                ValueError,
            ) as exc:

                last_error = exc

                logger.warning(
                    "Image manifest request "
                    "attempt %d/%d failed: %s",
                    attempt,
                    MANIFEST_RETRIES,
                    exc,
                )

                if attempt < MANIFEST_RETRIES:

                    # Exponential but bounded retry delay.
                    await asyncio.sleep(
                        min(
                            8.0,
                            1.5 * attempt,
                        )
                    )

        raise RuntimeError(
            "Unable to load the Doraemon "
            "image database."
        ) from last_error

    # ========================================================
    # CACHED MANIFEST
    # ========================================================

    async def get_manifest(
        self,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:

        now = time.monotonic()

        if (
            not force_refresh
            and self._manifest is not None
            and (
                now - self._loaded_at
                < self.cache_seconds
            )
        ):

            return self._manifest

        async with self._lock:

            now = time.monotonic()

            if (
                not force_refresh
                and self._manifest is not None
                and (
                    now - self._loaded_at
                    < self.cache_seconds
                )
            ):

                return self._manifest

            try:

                manifest = (
                    await self._download_manifest()
                )

            except Exception:

                # If the bot already has a known-good cached
                # manifest, do not destroy it merely because
                # GitHub is temporarily unavailable.
                if self._manifest is not None:

                    logger.exception(
                        "Unable to refresh image "
                        "manifest; keeping the "
                        "previous cached manifest."
                    )

                    return self._manifest

                raise

            self._manifest = manifest

            self._loaded_at = (
                time.monotonic()
            )

            return manifest

    # ========================================================
    # FORCE REFRESH
    # ========================================================

    async def refresh(
        self,
    ) -> dict[str, Any]:

        """
        Force a fresh download of database/images.json.
        """

        return await self.get_manifest(
            force_refresh=True
        )

    # ========================================================
    # EXACT CHARACTER LOOKUP
    # ========================================================

    async def find_character(
        self,
        query: str,
    ) -> Optional[
        tuple[
            str,
            dict[str, Any],
        ]
    ]:

        query_key = normalize_name(
            query
        )

        if not query_key:
            return None

        manifest = (
            await self.get_manifest()
        )

        characters = manifest.get(
            "characters",
            {},
        )

        if not isinstance(
            characters,
            dict,
        ):
            return None

        # ----------------------------------------------------
        # 1. Exact character ID
        # ----------------------------------------------------

        if query_key in characters:

            data = characters[
                query_key
            ]

            if isinstance(
                data,
                dict,
            ):

                return (
                    query_key,
                    data,
                )

        # ----------------------------------------------------
        # 2. Generated search index
        #
        # The new image builder creates:
        #
        #     search_index.json
        #
        # but it is not required for compatibility because
        # the manifest itself contains names and aliases.
        # ----------------------------------------------------

        index = manifest.get(
            "search_index",
            {},
        )

        if isinstance(
            index,
            dict,
        ):

            # Future-compatible embedded index support.
            by_name = index.get(
                "by_name",
                {},
            )

            if isinstance(
                by_name,
                dict,
            ):

                character_id = (
                    by_name.get(
                        query_key
                    )
                )

                if (
                    character_id
                    in characters
                ):

                    data = characters[
                        character_id
                    ]

                    if isinstance(
                        data,
                        dict,
                    ):

                        return (
                            character_id,
                            data,
                        )

            by_alias = index.get(
                "by_alias",
                {},
            )

            if isinstance(
                by_alias,
                dict,
            ):

                character_id = (
                    by_alias.get(
                        query_key
                    )
                )

                if (
                    character_id
                    in characters
                ):

                    data = characters[
                        character_id
                    ]

                    if isinstance(
                        data,
                        dict,
                    ):

                        return (
                            character_id,
                            data,
                        )

        # ----------------------------------------------------
        # 3. Exact canonical name / aliases
        # ----------------------------------------------------

        for character_id, data in (
            characters.items()
        ):

            if not isinstance(
                data,
                dict,
            ):
                continue

            name = str(
                data.get(
                    "name",
                    "",
                )
            )

            if (
                normalize_name(name)
                == query_key
            ):

                return (
                    str(character_id),
                    data,
                )

            aliases = data.get(
                "aliases",
                [],
            )

            if isinstance(
                aliases,
                list,
            ):

                for alias in aliases:

                    if (
                        normalize_name(
                            str(alias)
                        )
                        == query_key
                    ):

                        return (
                            str(character_id),
                            data,
                        )

        # ----------------------------------------------------
        # 4. Partial match
        # ----------------------------------------------------

        matches: list[
            tuple[
                int,
                str,
                dict[str, Any],
            ]
        ] = []

        for character_id, data in (
            characters.items()
        ):

            if not isinstance(
                data,
                dict,
            ):
                continue

            values = [
                str(
                    data.get(
                        "name",
                        "",
                    )
                )
            ]

            aliases = data.get(
                "aliases",
                [],
            )

            if isinstance(
                aliases,
                list,
            ):

                values.extend(
                    str(alias)
                    for alias in aliases
                )

            matched = False

            for value in values:

                normalized = (
                    normalize_name(
                        value
                    )
                )

                if (
                    query_key
                    in normalized
                ):

                    matches.append(
                        (
                            len(
                                normalized
                            ),
                            str(
                                character_id
                            ),
                            data,
                        )
                    )

                    matched = True

                    break

            if matched:
                continue

        if not matches:
            return None

        matches.sort(
            key=lambda item: (
                item[0],
                item[1],
            )
        )

        return (
            matches[0][1],
            matches[0][2],
        )

    # ========================================================
    # IMAGE CONVERSION
    # ========================================================

    @staticmethod
    def _convert_image(
        character_id: str,
        character_name: str,
        item: dict[str, Any],
    ) -> Optional[
        DoraemonImage
    ]:

        url = str(
            item.get(
                "url",
                "",
            )
        ).strip()

        if not is_allowed_image_url(
            url
        ):

            logger.warning(
                "Rejected image URL for "
                "%s: %s",
                character_name,
                url,
            )

            return None

        image_id = str(
            item.get(
                "id",
                "",
            )
        ).strip()

        # ----------------------------------------------------
        # Older manifests might theoretically omit "id".
        # Construct a deterministic fallback instead of
        # throwing away an otherwise valid image.
        # ----------------------------------------------------

        if not image_id:

            filename = str(
                item.get(
                    "filename",
                    "",
                )
            ).strip()

            if filename:

                image_id = (
                    f"{character_id}-"
                    f"{normalize_name(filename)}"
                )

            else:

                image_id = (
                    f"{character_id}-"
                    f"{abs(hash(url))}"
                )

        try:

            width = int(
                item.get(
                    "width",
                    0,
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):

            width = 0

        try:

            height = int(
                item.get(
                    "height",
                    0,
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):

            height = 0

        try:

            byte_count = int(
                item.get(
                    "bytes",
                    0,
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):

            byte_count = 0

        return DoraemonImage(
            image_id=image_id,
            character_id=character_id,
            character_name=character_name,
            filename=str(
                item.get(
                    "filename",
                    "",
                )
            ),
            url=url,
            width=width,
            height=height,
            source=str(
                item.get(
                    "source",
                    "",
                )
            ),
            source_url=str(
                item.get(
                    "source_url",
                    "",
                )
            ),
            format=str(
                item.get(
                    "format",
                    "",
                )
            ),
            bytes=byte_count,
            sha256=str(
                item.get(
                    "sha256",
                    "",
                )
            ),
        )

    # ========================================================
    # GET IMAGES
    # ========================================================

    async def get_images(
        self,
        query: str,
        *,
        limit: int = MAX_IMAGES_PER_GALLERY,
    ) -> tuple[
        str,
        list[DoraemonImage],
    ]:

        limit = max(
            1,
            min(
                int(limit),
                MAX_IMAGES_PER_GALLERY,
            ),
        )

        result = (
            await self.find_character(
                query
            )
        )

        if result is None:

            raise LookupError(
                f"No image record exists for "
                f"{query!r}."
            )

        character_id, data = result

        character_name = str(
            data.get(
                "name",
                character_id,
            )
        )

        raw_images = data.get(
            "images",
            [],
        )

        if not isinstance(
            raw_images,
            list,
        ):

            raw_images = []

        images: list[
            DoraemonImage
        ] = []

        seen_urls: set[str] = set()

        for item in raw_images:

            if not isinstance(
                item,
                dict,
            ):
                continue

            image = self._convert_image(
                character_id,
                character_name,
                item,
            )

            if image is None:
                continue

            if image.url in seen_urls:
                continue

            seen_urls.add(
                image.url
            )

            images.append(
                image
            )

            if len(images) >= limit:
                break

        if not images:

            raise LookupError(
                f"{character_name} has no "
                "published images yet."
            )

        return (
            character_name,
            images,
        )

    # ========================================================
    # FIND ONE IMAGE
    # ========================================================

    async def get_first_image(
        self,
        query: str,
    ) -> DoraemonImage:

        _, images = (
            await self.get_images(
                query,
                limit=1,
            )
        )

        return images[0]

    # ========================================================
    # SEARCH MULTIPLE CHARACTERS
    # ========================================================

    async def search_characters(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[
        tuple[
            str,
            dict[str, Any],
        ]
    ]:

        query_key = normalize_name(
            query
        )

        if not query_key:
            return []

        manifest = (
            await self.get_manifest()
        )

        characters = manifest.get(
            "characters",
            {},
        )

        if not isinstance(
            characters,
            dict,
        ):
            return []

        results: list[
            tuple[
                float,
                str,
                dict[str, Any],
            ]
        ] = []

        for character_id, data in (
            characters.items()
        ):

            if not isinstance(
                data,
                dict,
            ):
                continue

            name = str(
                data.get(
                    "name",
                    "",
                )
            )

            aliases = data.get(
                "aliases",
                [],
            )

            if not isinstance(
                aliases,
                list,
            ):

                aliases = []

            names = [
                name,
                *(
                    str(alias)
                    for alias in aliases
                ),
            ]

            normalized_names = [
                normalize_name(
                    value
                )
                for value in names
                if value
            ]

            score = 0.0

            for normalized in (
                normalized_names
            ):

                if not normalized:
                    continue

                if normalized == query_key:

                    score = max(
                        score,
                        1000.0,
                    )

                elif normalized.startswith(
                    query_key
                ):

                    score = max(
                        score,
                        700.0,
                    )

                elif query_key in normalized:

                    score = max(
                        score,
                        500.0,
                    )

                else:

                    query_tokens = set(
                        query_key.split(
                            "-"
                        )
                    )

                    name_tokens = set(
                        normalized.split(
                            "-"
                        )
                    )

                    overlap = (
                        query_tokens
                        & name_tokens
                    )

                    if overlap:

                        score = max(
                            score,
                            100.0
                            * (
                                len(overlap)
                                /
                                max(
                                    1,
                                    len(
                                        query_tokens
                                    )
                                )
                            ),
                        )

            if score <= 0:
                continue

            results.append(
                (
                    score,
                    str(
                        character_id
                    ),
                    data,
                )
            )

        results.sort(
            key=lambda item: (
                -item[0],
                item[1],
            )
        )

        return [
            (
                character_id,
                data,
            )
            for _, character_id, data
            in results[:limit]
        ]

    # ========================================================
    # STATISTICS
    # ========================================================

    async def statistics(
        self,
    ) -> dict[str, int]:

        manifest = (
            await self.get_manifest()
        )

        characters = manifest.get(
            "characters",
            {},
        )

        if not isinstance(
            characters,
            dict,
        ):

            return {
                "characters": 0,
                "characters_with_images": 0,
                "characters_without_images": 0,
                "images": 0,
            }

        character_count = 0

        characters_with_images = 0

        image_count = 0

        for data in (
            characters.values()
        ):

            if not isinstance(
                data,
                dict,
            ):
                continue

            character_count += 1

            images = data.get(
                "images",
                [],
            )

            if isinstance(
                images,
                list,
            ):

                count = len(
                    images
                )

                image_count += count

                if count > 0:

                    characters_with_images += 1

        return {
            "characters": character_count,
            "characters_with_images": (
                characters_with_images
            ),
            "characters_without_images": (
                character_count
                - characters_with_images
            ),
            "images": image_count,
        }


# ============================================================
# GLOBAL INSTANCE
# ============================================================

image_database = (
    DoraemonImageDatabase()
)
