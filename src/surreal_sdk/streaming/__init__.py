"""
SurrealDB SDK Streaming Module.

Provides Live Queries and Change Feeds streaming capabilities.
"""

from .change_feed import ChangeFeedStream
from .live_query import LiveQuery, LiveQueryManager, LiveNotification, LiveAction
from .live_select import (
    LiveSelectStream,
    LiveSelectManager,
    LiveChange,
    LiveAction as LiveSelectAction,
    LiveSubscriptionParams,
)

__all__ = [
    # Change Feeds
    "ChangeFeedStream",
    # Live Query (callback-based)
    "LiveQuery",
    "LiveQueryManager",
    "LiveNotification",
    "LiveAction",
    # Live Select (async iterator)
    "LiveSelectStream",
    "LiveSelectManager",
    "LiveChange",
    "LiveSelectAction",
    "LiveSubscriptionParams",
]
