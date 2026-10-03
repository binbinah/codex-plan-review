#!/usr/bin/env python3
"""Dependency-free entry point for plugin hooks and explicit plan review."""

from plan_review.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
