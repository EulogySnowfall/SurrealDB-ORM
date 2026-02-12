"""
SurrealDB SDK Streaming Module.

Provides Live Queries and Change Feeds streaming capabilities.
"""

from .change_feed import ChangeFeedStream
from .live_query import LiveAction, LiveNotification, LiveQuery, LiveQueryManager
from .live_select import (
    LiveAction as LiveSelectAction,
)
from .live_select import (
    LiveChange,
    LiveSelectManager,
    LiveSelectStream,
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
