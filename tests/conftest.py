"""Shared test configuration for the EngageMate control API, worker, and scheduler."""

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_DIR = os.path.join(ROOT_DIR, "api")

# The import root belongs here, not in whichever test module happens to be
# collected first. Without this, running a single test file fails to import
# `engagemate`.
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)


import pytest


@pytest.fixture(autouse=True)
def configured_web_origin(monkeypatch):
    """Run as a configured deployment. The code has no default origin.

    Tests of the unconfigured path remove these with monkeypatch.delenv.
    """

    monkeypatch.setenv("ENGAGEMATE_WEB_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("ENGAGEMATE_CLERK_INVITE_HOST", "clerk.example.com")
    monkeypatch.delenv("ENGAGEMATE_SUPPORT_EMAIL", raising=False)
