"""Minimal read-only Microsoft Graph client for the recordings folder."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import GRAPH_URL
from .media import classify_name

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Only what normalize_item() needs; keeps delta pages small.
_SELECT = "id,name,size,webUrl,file,folder,deleted,parentReference,photo,video,image"


class GraphError(Exception):
    """A Graph request failed."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Graph API {status}: {message}")
        self.status = status


class GraphAuthError(GraphError):
    """The token was rejected; the user has to sign in again."""


class GraphNotFound(GraphError):
    """Item or path does not exist."""


class GraphResyncRequired(GraphError):
    """The delta token expired (410); start over with a full enumeration."""


TokenProvider = Callable[[], Awaitable[str]]


class OneDriveClient:
    def __init__(self, session: aiohttp.ClientSession, token: TokenProvider) -> None:
        self._session = session
        self._token = token

    async def _request(self, url: str, params: dict[str, str] | None = None) -> aiohttp.ClientResponse:
        if not url.startswith("http"):
            url = f"{GRAPH_URL}{url}"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        resp = await self._session.get(url, headers=headers, params=params, timeout=_TIMEOUT)
        if resp.status < 400:
            return resp
        try:
            body = await resp.json(content_type=None)
            message = (body or {}).get("error", {}).get("message") or resp.reason or ""
        except (ValueError, aiohttp.ContentTypeError):
            message = resp.reason or ""
        finally:
            resp.release()
        if resp.status == 401:
            raise GraphAuthError(resp.status, message)
        if resp.status == 404:
            raise GraphNotFound(resp.status, message)
        if resp.status == 410:
            raise GraphResyncRequired(resp.status, message)
        raise GraphError(resp.status, message)

    async def _json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        resp = await self._request(url, params)
        async with resp:
            return await resp.json()

    # -- drive / folder ----------------------------------------------------

    async def async_get_drive(self) -> dict[str, Any]:
        return await self._json("/me/drive")

    async def async_get_folder(self, path: str) -> dict[str, Any]:
        """Resolve a folder path relative to the drive root ('' = root)."""
        path = path.strip().strip("/")
        if not path:
            return await self._json("/me/drive/root")
        item = await self._json(f"/me/drive/root:/{quote(path)}")
        if "folder" not in item:
            raise GraphNotFound(404, f"{path} is not a folder")
        return item

    # -- enumeration -------------------------------------------------------

    async def async_delta(
        self, folder_id: str, delta_link: str | None
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """Changes below ``folder_id`` since ``delta_link``.

        Returns ``(items, new_delta_link, full)``; ``full`` means the result is
        a complete enumeration and the caller should drop what it had before.
        Raises :class:`GraphError` if delta is not available for this folder
        (OneDrive for Business only supports it on the drive root).
        """
        full = delta_link is None
        url: str | None = delta_link or f"/me/drive/items/{folder_id}/delta"
        params: dict[str, str] | None = None if delta_link else {"$select": _SELECT}
        try:
            return await self._collect_delta(url, params, full)
        except GraphResyncRequired:
            _LOGGER.info("OneDrive delta token expired, enumerating the folder again")
            return await self._collect_delta(f"/me/drive/items/{folder_id}/delta", {"$select": _SELECT}, True)

    async def _collect_delta(
        self, url: str, params: dict[str, str] | None, full: bool
    ) -> tuple[list[dict[str, Any]], str, bool]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url:
            page = await self._json(next_url, params)
            params = None  # nextLink/deltaLink already carry the query
            items.extend(page.get("value", []))
            if "@odata.deltaLink" in page:
                return items, page["@odata.deltaLink"], full
            next_url = page.get("@odata.nextLink")
        raise GraphError(500, "delta response without deltaLink")

    async def async_list_recursive(self, folder_id: str, max_folders: int = 2000) -> list[dict[str, Any]]:
        """Full listing without delta (fallback for OneDrive for Business)."""
        items: list[dict[str, Any]] = []
        queue = [folder_id]
        seen = 0
        while queue and seen < max_folders:
            current = queue.pop()
            seen += 1
            next_url: str | None = f"/me/drive/items/{current}/children"
            params: dict[str, str] | None = {"$select": _SELECT, "$top": "999"}
            while next_url:
                page = await self._json(next_url, params)
                params = None
                for child in page.get("value", []):
                    if "folder" in child:
                        queue.append(child["id"])
                    else:
                        items.append(child)
                next_url = page.get("@odata.nextLink")
        return items

    # -- content -----------------------------------------------------------

    async def async_thumbnail(self, item_id: str, size: str = "medium") -> bytes | None:
        """JPEG thumbnail rendered by OneDrive, or None if it has none."""
        try:
            resp = await self._request(f"/me/drive/items/{item_id}/thumbnails/0/{size}/content")
        except GraphNotFound:
            return None
        async with resp:
            return await resp.read()

    async def async_content(self, item_id: str, max_bytes: int = 5 * 1024 * 1024) -> bytes | None:
        """Download a small file (cover images)."""
        resp = await self._request(f"/me/drive/items/{item_id}/content")
        async with resp:
            if resp.content_length and resp.content_length > max_bytes:
                return None
            return await resp.read()

    async def async_download_url(self, item_id: str) -> str | None:
        """Short-lived pre-authenticated URL of the file content (about 1 h)."""
        item = await self._json(f"/me/drive/items/{item_id}", {"$select": "id,@microsoft.graph.downloadUrl"})
        return item.get("@microsoft.graph.downloadUrl")


def normalize_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a Graph driveItem to what the integration stores.

    Returns None for folders, deleted items and files that are not media.
    """
    if "folder" in raw or "deleted" in raw or "file" not in raw:
        return None
    if classify_name(raw.get("name", "")) is None:
        return None
    video = raw.get("video") or {}
    image = raw.get("image") or {}
    return {
        "id": raw["id"],
        "name": raw["name"],
        "size": raw.get("size"),
        "web_url": raw.get("webUrl"),
        "folder": (raw.get("parentReference") or {}).get("id"),
        "taken_at": (raw.get("photo") or {}).get("takenDateTime"),
        "duration_ms": video.get("duration"),
        "width": video.get("width") or image.get("width"),
        "height": video.get("height") or image.get("height"),
    }
