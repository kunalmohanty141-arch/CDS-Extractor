"""Streamlit UI.

Launched with ``cdc-forge ui``, which shells out to ``streamlit run`` — the app
module is a Streamlit script and cannot be imported and called like a function.

Streamlit is an optional dependency; the CLI and the offline core do not need
it and must never import from here at module level.
"""

from pathlib import Path

APP = Path(__file__).with_name("app.py")

__all__ = ["APP"]
