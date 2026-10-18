"""Compatibility imports for the shared native archive implementation."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from vibe_job_radar.posix_artifacts import (MAX_BYTES, MAX_MEMBERS, TOP, require, digest,
    path_name, validate_links, inventory, archive_inventory, pack, extract_verified)
