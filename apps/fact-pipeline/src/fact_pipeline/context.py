"""The store and model client the extraction and answer calls use (context-local)."""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fake_model import ModelClient
    from .store import Store

_STORE: contextvars.ContextVar[Store | None] = contextvars.ContextVar("fact_pipeline_store", default=None)
_CLIENT: contextvars.ContextVar[ModelClient | None] = contextvars.ContextVar("fact_pipeline_client", default=None)


def bind(store: Store, client: ModelClient) -> None:
    """Make store and client current for this context (threads started with to_thread inherit it)."""
    _STORE.set(store)
    _CLIENT.set(client)


def current_store() -> Store:
    s = _STORE.get()
    if s is None:
        raise RuntimeError("no store bound; build a Pipeline first")
    return s


def current_client() -> ModelClient:
    c = _CLIENT.get()
    if c is None:
        raise RuntimeError("no model client bound; build a Pipeline first")
    return c
