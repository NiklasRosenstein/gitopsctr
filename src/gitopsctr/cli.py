"""Command-line entry point for gitopsctr."""

from __future__ import annotations

from gitopsctr.controller import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
