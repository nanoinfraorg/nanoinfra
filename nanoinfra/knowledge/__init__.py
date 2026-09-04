"""The knowledge base in the workspace (#237).

Documents live in ``workspaces/<ws>/knowledge/`` and the index sits beside them in
``.index/``. Nothing here is ever injected into a prompt: the agent reaches it through the
``knowledge_search`` tool, because a knowledge base in the stable prompt block is the *31K for a
hola* problem (#203) at a larger scale -- and this one grows with the operator's own writing.

The public surface is small on purpose. ``reindex_workspace`` is what the ``knowledge-index``
system job calls, ``refresh_workspace`` and ``search_workspace`` are what the tool calls, and
``status_payload`` is what the settings panel reads.
"""

from nanoinfra.knowledge.chunking import Chunk, chunk_document
from nanoinfra.knowledge.service import (
    HYBRID_INSTALL_HINT,
    KNOWLEDGE_DIR_NAME,
    ReindexReport,
    hybrid_available,
    index_dir,
    knowledge_root,
    refresh_workspace,
    reindex_workspace,
    run_pass,
    search_workspace,
    status_payload,
)
from nanoinfra.knowledge.store import SearchHit

__all__ = [
    "HYBRID_INSTALL_HINT",
    "KNOWLEDGE_DIR_NAME",
    "Chunk",
    "ReindexReport",
    "SearchHit",
    "chunk_document",
    "hybrid_available",
    "index_dir",
    "knowledge_root",
    "refresh_workspace",
    "reindex_workspace",
    "run_pass",
    "search_workspace",
    "status_payload",
]
