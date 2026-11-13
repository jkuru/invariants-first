"""The store and model client the extraction and answer calls use, as inv.Dependency values.

propose_fact and compose_answer are module-level model calls: no self, and the rules' chunk and span
providers take only binding paths (args.chunk_id, reply.start), so the store they read and the client
the body calls are dependencies, bound around the call:

    with STORE.bound(store), CLIENT.bound(client):
        propose_fact(...)

Pipeline does this around every stage it runs (Pipeline.bound()). Bindings are context-local: tasks
and asyncio.to_thread workers started inside the block see them; another thread or request does not.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Iterator

import invariants as inv

if TYPE_CHECKING:
    from .fake_model import ModelClient
    from .store import Store

STORE: inv.Dependency[Store] = inv.Dependency("fact_pipeline_store")
CLIENT: inv.Dependency[ModelClient] = inv.Dependency("fact_pipeline_client")


@contextlib.contextmanager
def bound(store: Store, client: ModelClient) -> Iterator[None]:
    """Bind store and client for the block, then restore what was bound before."""
    with STORE.bound(store), CLIENT.bound(client):
        yield


def bind(store: Store, client: ModelClient) -> None:
    """Bind store and client for the rest of this context (Pipeline() does, so a model call made
    right after building one, outside its methods, still finds them)."""
    STORE.set(store)
    CLIENT.set(client)


def current_store() -> Store:
    """The bound store; LookupError when none is (a provider turns it into a rule_error)."""
    return STORE.get()


def current_client() -> ModelClient:
    """The bound model client; LookupError when none is."""
    return CLIENT.get()
