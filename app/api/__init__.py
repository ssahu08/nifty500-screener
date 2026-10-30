"""HTTP API layer (FastAPI routers).

The UI talks only to this API, keeping the strategy engine decoupled from
presentation (NFR-2). See :mod:`app.api.routes` for endpoints.
"""

from app.api.routes import router

__all__ = ["router"]
