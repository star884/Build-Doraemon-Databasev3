"""
Doraemon Image Database Client
==============================

Runtime client for Build-Doraemon-Databasev3.

The image-building process lives in the separate:

    star884/Doraemon-Image-Database

repository.

This module does NOT search for images.

It does NOT download image files.

It does NOT contact Wikimedia, Wikipedia, Fandom, etc.

It ONLY downloads the generated manifest from:

    raw.githubusercontent.com

and resolves characters to their published image URLs.

This keeps the Discord bot lightweight and makes the image repository
the single source of truth for character images.
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
# Configuration
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

MANIFEST_CACHE_SECONDS = max(
    30,
    int(
        os.getenv(
            "IMAGE_MANIFEST_CACHE_SECONDS",
            "300",
        )
    ),
)

HTTP_TIMEOUT_SECONDS = max(
    5,
    int(
        os.getenv(
            "IMAGE_HTTP_TIMEOUT",
            "20",
        )
    ),
)

MAX_IMAGES_PER_GALLERY = max(
    1,
    int(
        os.getenv(
            "IMAGE_MAX_GALLERY_IMAGES",
            "10",
        )
    ),
)


# ============================================================
# Data model
# ============================================================

@dataclass(frozen=True)
class DoraemonImage:
    """One published character image."""

    image_id: str
    character_id: str
    character_name: str
    filename: str
    url: str
    width: int
    height: int
    source: str = ""


# ============================================================
# Normalization
# ============================================================

def normalize_name(
    value: str,
) -> str:
    """
    Normalize names consistently with the image builder.
    """

    value = str(
        value or ""
    ).strip().lower()

    value = value.replace(
        "&",
        " and ",
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
# Security boundary
# ============================================================

def is_allowed_image_url(
    url: str,
) -> bool:
    """
    The bot deliberately accepts ONLY raw GitHub URLs.

    This prevents the manifest from silently turning into an
    arbitrary remote-image proxy.
    """

    if not isinstance(
        url,
        str,
    ):
        return False

    return url.startswith(
        "https://raw.githubusercontent.com/"
    )


# ============================================================
# Client
# ============================================================

class DoraemonImageDatabase:
    """
    Read-only client for Doraemon-Image-Database.
    """

    def __init__(
        self,
        *,
        manifest_url: str = IMAGE_MANIFEST_URL,
        cache_seconds: int = MANIFEST_CACHE_SECONDS,
    ) -> None:

        self.manifest_url = manifest_url

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
    # HTTP session
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
            )

            self._session = (
                aiohttp.ClientSession(
                    timeout=timeout,
                    connector=connector,
                    headers={
                        "User-Agent": (
                            "Doraemon-Database/"
                            "4.x Image Client"
                        ),
                        "Accept": (
                            "application/json"
                        ),
                    },
                )
            )

        return self._session

    async def close(self) -> None:

        if self._session is not None:

            await self._session.close()

            self._session = None

    # ========================================================
    # Manifest download
    # ========================================================

    async def _download_manifest(
        self,
    ) -> dict[str, Any]:

        session = await self._session_get()

        last_error: Optional[
            Exception
        ] = None

        for attempt in range(3):

            try:

                async with session.get(
                    self.manifest_url
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

                    if not isinstance(
                        payload,
                        dict,
                    ):
                        raise RuntimeError(
                            "Image manifest is not "
                            "a JSON object."
                        )

                    schema_version = (
                        payload.get(
                            "schema_version"
                        )
                    )

                    if schema_version != 2:

                        raise RuntimeError(
                            "Unsupported image "
                            f"database schema: "
                            f"{schema_version!r}"
                        )

                    return payload

            except Exception as exc:

                last_error = exc

                logger.warning(
                    "Image manifest request "
                    "attempt %d/3 failed: %s",
                    attempt + 1,
                    exc,
                )

                if attempt < 2:

                    await asyncio.sleep(
                        1.5
                        * (attempt + 1)
                    )

        raise RuntimeError(
            "Unable to load the Doraemon "
            "image database."
        ) from last_error

    # ========================================================
    # Cached manifest
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

            manifest = (
                await self._download_manifest()
            )

            self._manifest = manifest

            self._loaded_at = (
                time.monotonic()
            )

            return manifest

    # ========================================================
    # Character resolution
    # ========================================================

    async def find_character(
        self,
        query: str,
    ) -> Optional[
        tuple[str, dict[str, Any]]
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
        # Exact ID
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
        # Exact name / aliases
        # ----------------------------------------------------

        for character_id, data in characters.items():

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
                    character_id,
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
                            character_id,
                            data,
                        )

        # ----------------------------------------------------
        # Partial fallback
        # ----------------------------------------------------

        matches = []

        for character_id, data in characters.items():

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

            for value in values:

                normalized = normalize_name(
                    value
                )

                if (
                    query_key
                    in normalized
                ):

                    matches.append(
                        (
                            len(normalized),
                            character_id,
                            data,
                        )
                    )

                    break

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
    # Get images
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

        result = await self.find_character(
            query
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

            url = str(
                item.get(
                    "url",
                    "",
                )
            )

            if not is_allowed_image_url(
                url
            ):

                logger.warning(
                    "Rejected non-GitHub "
                    "image URL for %s: %s",
                    character_name,
                    url,
                )

                continue

            if url in seen_urls:
                continue

            image_id = str(
                item.get(
                    "id",
                    "",
                )
            )

            if not image_id:
                continue

            seen_urls.add(url)

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

            images.append(
                DoraemonImage(
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
                )
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
    # Statistics
    # ========================================================

    async def statistics(
        self,
    ) -> dict[str, int]:

        manifest = await self.get_manifest()

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
                "images": 0,
            }

        character_count = 0
        image_count = 0

        for data in characters.values():

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
                image_count += len(
                    images
                )

        return {
            "characters": character_count,
            "images": image_count,
}
