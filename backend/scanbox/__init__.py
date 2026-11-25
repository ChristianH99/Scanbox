"""Scanbox – self-hosted eSCL scanning app."""

import os

# The version comes from the release tag: CI builds the image with VERSION=<tag without "v"> and the
# Dockerfile passes it on as SCANBOX_VERSION. Local builds and runs say "dev".
__version__ = os.environ.get("SCANBOX_VERSION") or "dev"
