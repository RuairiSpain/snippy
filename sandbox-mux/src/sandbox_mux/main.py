"""Entrypoint: `uvicorn --factory sandbox_mux.main:app_factory` or `python -m sandbox_mux.main`."""

from __future__ import annotations

import logging
import os

from .api import create_app


def app_factory():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    return create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("sandbox_mux.main:app_factory", factory=True,
                host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
