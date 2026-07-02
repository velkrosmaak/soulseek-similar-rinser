#!/usr/bin/env python3
"""Shared configuration for the Soulseek similar rinser scripts."""

import os

from slskd_api import SlskdClient


def _getenv(name: str, default: str) -> str:
    return os.environ.get(name, default)


SLSKD_URL = _getenv("SLSKD_URL", "http://your.slskd.server:5030")
SLSKD_API_KEY = _getenv("SLSKD_API_KEY", "your slskd api key")

PLEX_URL = _getenv("PLEX_URL", "http://your.plex.server:32400")
PLEX_TOKEN = _getenv("PLEX_TOKEN", "your plex token")

LASTFM_BASE_URL = _getenv("LASTFM_BASE_URL", "http://ws.audioscrobbler.com/2.0/")
LASTFM_API_KEY = _getenv("LASTFM_API_KEY", "last fm api key")

FLARESOLVERR_URL = _getenv("FLARESOLVERR_URL", "http://docker:8191/v1")


def create_slskd_client() -> SlskdClient:
    """Create a Slskd client using the shared configuration."""
    return SlskdClient(SLSKD_URL, SLSKD_API_KEY)

