"""
quant_foundry.modules.sources.dedup — cross-source deduplication utility.

Helper functions (not a module) that deduplicate :class:`MediaItem`
objects across sources.  The same story is often syndicated across
multiple news sources or reposted across social platforms, so the
composer calls :func:`deduplicate_items` after merging items from all
source adapters.

Deduplication keys (in priority order):
1. Exact ``item_id`` match — same item returned twice.
2. Content hash — ``sha256(headline + body[:500])[:16]`` catches the
   same story syndicated across sources with different item IDs.
3. URL match — if both items share the same non-None ``url``.

The first occurrence of each duplicate is kept (order preserved).  A
``content_hash`` field is added to the surviving item's ``metadata`` so
the dedup decision is traceable.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any

from quant_foundry.modules.registry import MediaItem


def compute_content_hash(item: MediaItem) -> str:
    """Compute a stable content hash for a MediaItem.

    The hash is ``sha256(headline + body[:500])`` truncated to 16 hex
    chars.  This catches the same story syndicated across sources even
    when the ``item_id`` differs.
    """
    payload = f"{item.headline}{item.body[:500]}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def deduplicate_items(items: list[MediaItem]) -> list[MediaItem]:
    """Remove duplicate MediaItems across sources.

    Deduplicates on (in order): exact ``item_id``, content hash, and
    URL (when both items have the same non-None url).  Returns items
    in original order, keeping the first occurrence of each duplicate.
    A ``content_hash`` field is added to each surviving item's
    ``metadata`` so the dedup decision is traceable.

    Parameters
    ----------
    items
        List of :class:`MediaItem` objects potentially containing
        duplicates across sources.

    Returns
    -------
    list[MediaItem]
        Deduplicated list preserving original order.  Each item's
        ``metadata`` is augmented with a ``content_hash`` key.
    """
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    seen_urls: set[str] = set()

    result: list[MediaItem] = []
    for item in items:
        # Key 1: exact item_id match.
        if item.item_id in seen_ids:
            continue

        # Key 2: content hash match (syndicated stories).
        chash = compute_content_hash(item)
        if chash in seen_hashes:
            continue

        # Key 3: URL match (when both items have the same non-None url).
        if item.url is not None and item.url in seen_urls:
            continue

        # Not a duplicate — keep it and record its keys.
        seen_ids.add(item.item_id)
        seen_hashes.add(chash)
        if item.url is not None:
            seen_urls.add(item.url)

        # Attach content_hash to metadata for traceability.  MediaItem
        # is frozen, so we rebuild it with the augmented metadata.
        new_metadata: dict[str, str] = dict(item.metadata)
        new_metadata["content_hash"] = chash
        result.append(dataclasses.replace(item, metadata=new_metadata))

    return result


__all__ = ["compute_content_hash", "deduplicate_items"]
