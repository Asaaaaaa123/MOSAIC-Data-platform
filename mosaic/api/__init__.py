"""HTTP API (FastAPI) for Mosaic: JSON endpoints, the chat event stream and the web UI.

Run it with ``mosaic serve`` (binds ``BIOPHA_API_HOST``, 127.0.0.1 by default).
"""

from .app import create_app

__all__ = ["create_app"]
