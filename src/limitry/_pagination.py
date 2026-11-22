"""Lists, page by page or item by item, as OpenAI's Python SDK pages them.

Every list answers ``{ items, nextCursor }``. ``client.….list()`` gives its
first page; iterating it goes through every item, fetching the next pages
as it goes::

    for member in client.workspace.members.list():  # every member
        ...
    page = client.workspace.members.list(limit=10)  # one page
    page.items, page.next_cursor, page.has_next_page(), page.get_next_page()

Async: ``async for`` goes through every item, and ``await`` gives the
first page (``await page.get_next_page()`` the next).
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Generator, Generic, Iterator, List, Mapping, Optional, TypeVar

from ._models import construct_type

_T = TypeVar("_T")


class _Page(Generic[_T]):
    items: List[_T]
    """This page's items."""
    next_cursor: Optional[str]
    """The cursor of the next page; ``None`` on the last one."""

    def __init__(self, data: Mapping[str, Any], item: Any) -> None:
        self._data = dict(data)
        self.items = [construct_type(value, item) for value in data.get("items") or []]
        self.next_cursor = data.get("nextCursor")

    def has_next_page(self) -> bool:
        """Whether there is a page after this one."""
        return bool(self.next_cursor) and len(self.items) > 0

    def _no_next(self) -> RuntimeError:
        return RuntimeError("This is the last page: check has_next_page() before get_next_page().")

    def to_dict(self) -> Dict[str, Any]:
        """The page as the API sent it."""
        return dict(self._data)

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        """The page as JSON, as the API sent it."""
        return json.dumps(self._data, indent=indent)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(items={self.items!r}, next_cursor={self.next_cursor!r})"


class SyncPage(_Page[_T]):
    """One page of a list. Iterating it goes through every item of every
    page from here on."""

    def __init__(self, data: Mapping[str, Any], item: Any, fetch: Callable[[str], "SyncPage[_T]"]) -> None:
        super().__init__(data, item)
        self._fetch = fetch

    def get_next_page(self) -> "SyncPage[_T]":
        """Fetches the next page (raises on the last one)."""
        if not self.next_cursor or not self.has_next_page():
            raise self._no_next()
        return self._fetch(self.next_cursor)

    def iter_pages(self) -> Iterator["SyncPage[_T]"]:
        """This page, then each next one."""
        page = self
        while True:
            yield page
            if not page.has_next_page():
                return
            page = page.get_next_page()

    def __iter__(self) -> Iterator[_T]:
        for page in self.iter_pages():
            yield from page.items


class AsyncPage(_Page[_T]):
    """One page of a list, asynchronously. ``async for`` goes through every
    item of every page from here on."""

    def __init__(self, data: Mapping[str, Any], item: Any, fetch: Callable[[str], Awaitable["AsyncPage[_T]"]]) -> None:
        super().__init__(data, item)
        self._fetch = fetch

    async def get_next_page(self) -> "AsyncPage[_T]":
        """Fetches the next page (raises on the last one)."""
        if not self.next_cursor or not self.has_next_page():
            raise self._no_next()
        return await self._fetch(self.next_cursor)

    async def iter_pages(self) -> AsyncIterator["AsyncPage[_T]"]:
        """This page, then each next one."""
        page = self
        while True:
            yield page
            if not page.has_next_page():
                return
            page = await page.get_next_page()

    async def __aiter__(self) -> AsyncIterator[_T]:
        async for page in self.iter_pages():
            for item in page.items:
                yield item


class AsyncPaginator(Generic[_T]):
    """What an async ``list()`` gives: ``await`` it for the first page, or
    ``async for`` over it for every item."""

    def __init__(self, fetch: Callable[[Optional[str]], Awaitable[AsyncPage[_T]]]) -> None:
        self._fetch = fetch

    def __await__(self) -> Generator[Any, None, AsyncPage[_T]]:
        return self._fetch(None).__await__()

    async def __aiter__(self) -> AsyncIterator[_T]:
        page = await self
        async for item in page:
            yield item


__all__ = ["SyncPage", "AsyncPage", "AsyncPaginator"]
