"""Minimal read-only Microsoft Graph client for the recordings folder."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import GRAPH_URL
from .media import ROLE_COVER, ROLE_ORIGINAL, ROLE_PROXY, classify_name, is_flight_record
from .media_backend import MediaAuthError, MediaError, MediaNotFound

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Only what normalize_item() needs; keeps delta pages small.
_SELECT = "id,name,size,webUrl,file,folder,deleted,parentReference,photo,video,image"


class GraphError(MediaError):
    """A Graph request failed."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Graph API {status}: {message}")
        self.status = status


class GraphAuthError(GraphError, MediaAuthError):
    """The token was rejected; the user has to sign in again."""


class GraphNotFound(GraphError, MediaNotFound):
    """Item or path does not exist."""


class GraphResyncRequired(GraphError):
    """The delta token expired (410); start over with a full enumeration."""


TokenProvider = Callable[[], Awaitable[str]]


class OneDriveClient:
    def __init__(self, session: aiohttp.ClientSession, token: TokenProvider) -> None:
        self._session = session
        self._token = token

    async def _request(
        self, url: str, params: dict[str, str] | None = None, *, allow_redirects: bool = True
    ) -> aiohttp.ClientResponse:
        if not url.startswith("http"):
            url = f"{GRAPH_URL}{url}"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        resp = await self._session.get(
            url, headers=headers, params=params, timeout=_TIMEOUT, allow_redirects=allow_redirects
        )
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

    async def async_list_subfolders(self, path: str) -> list[str]:
        """Names of the folders directly inside ``path`` ('' = root), sorted."""
        folder = await self.async_get_folder(path)
        names: list[str] = []
        next_url: str | None = f"/me/drive/items/{folder['id']}/children"
        params: dict[str, str] | None = {"$select": "id,name,folder", "$top": "999"}
        while next_url:
            page = await self._json(next_url, params)
            params = None  # nextLink already carries the query
            names.extend(child["name"] for child in page.get("value", []) if "folder" in child)
            next_url = page.get("@odata.nextLink")
        return sorted(names, key=str.casefold)

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
        """Short-lived pre-authenticated URL of the file content (about 1 h).

        Graph answers ``/content`` with a redirect to it. Asking for
        ``@microsoft.graph.downloadUrl`` via ``$select`` returns nothing on a
        personal OneDrive.
        """
        resp = await self._request(f"/me/drive/items/{item_id}/content", allow_redirects=False)
        resp.release()
        return resp.headers.get("Location")


def normalize_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a Graph driveItem to what the integration stores.

    Returns None for folders, deleted items and files that are neither
    media nor flight records.
    """
    if "folder" in raw or "deleted" in raw or "file" not in raw:
        return None
    name = raw.get("name", "")
    if classify_name(name) is None and not is_flight_record(name):
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


class OneDriveMedia:
    """Media backend for one folder in OneDrive (delta query where available)."""

    kind = "onedrive"
    label = "OneDrive"

    def __init__(self, client: OneDriveClient, folder_path: str) -> None:
        self.client = client
        self.folder_path = folder_path.strip().strip("/")
        self._folder_id: str | None = None
        self._delta_link: str | None = None
        self._use_delta = True

    @property
    def folder_display(self) -> str:
        return f"/{self.folder_path}"

    def load_state(self, data: dict[str, Any]) -> None:
        self._folder_id = data.get("folder_id")
        self._delta_link = data.get("delta_link")
        self._use_delta = data.get("use_delta", True)

    def dump_state(self) -> dict[str, Any]:
        return {"folder_id": self._folder_id, "delta_link": self._delta_link, "use_delta": self._use_delta}

    # -- sync ----------------------------------------------------------------

    async def async_sync(self, items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        try:
            return await self._async_sync(items)
        except aiohttp.ClientResponseError as err:
            # Raised by the token refresh when the grant was revoked.
            if err.status in (400, 401):
                raise GraphAuthError(err.status, f"token refresh failed: {err.message}") from err
            raise GraphError(err.status, err.message) from err

    async def _async_sync(self, items: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if self._folder_id is None:
            folder = await self.client.async_get_folder(self.folder_path)
            self._folder_id = folder["id"]
            self._delta_link = None

        if self._use_delta:
            try:
                raw, self._delta_link, full = await self.client.async_delta(self._folder_id, self._delta_link)
            except GraphAuthError:
                raise
            except GraphNotFound:
                # The folder was deleted or moved; resolve the path again next time.
                self._folder_id = None
                raise GraphNotFound(404, f"folder '{self.folder_path}' not found") from None
            except GraphError as err:
                _LOGGER.info("OneDrive delta not available (%s), falling back to full listings", err)
                self._use_delta = False
                self._delta_link = None
            else:
                return self._apply_delta({} if full else items, raw)

        raw = await self.client.async_list_recursive(self._folder_id)
        return self._apply_delta({}, raw)

    def _apply_delta(
        self, items: dict[str, dict[str, Any]], raw_items: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        for raw in raw_items:
            item_id = raw.get("id")
            if not item_id or item_id == self._folder_id:
                continue
            if "deleted" in raw:
                items.pop(item_id, None)
                # A deleted folder is not always followed by its children.
                for key in [k for k, v in items.items() if v.get("folder") == item_id]:
                    items.pop(key, None)
                continue
            item = normalize_item(raw)
            if item is None:
                items.pop(item_id, None)
            else:
                items[item_id] = item
        return items

    # -- thumbnails / files ----------------------------------------------------

    async def async_thumbnail(self, rec: dict[str, Any]) -> bytes | None:
        if cover := rec.get(ROLE_COVER):
            # Extracted by the sync script from the .OSV (OneDrive cannot
            # thumbnail 360° originals). Let OneDrive scale it down.
            data = await self.client.async_thumbnail(cover["item_id"]) or await self.client.async_content(
                cover["item_id"]
            )
            if data:
                return data
        for role in (ROLE_PROXY, ROLE_ORIGINAL):
            if (ref := rec.get(role)) and (data := await self.client.async_thumbnail(ref["item_id"])):
                return data
        return None

    async def async_file(self, ref: dict[str, Any]) -> str | None:
        return await self.client.async_download_url(ref["item_id"])

    async def async_read(self, item: dict[str, Any], max_bytes: int) -> bytes | None:
        return await self.client.async_content(item["id"], max_bytes)
