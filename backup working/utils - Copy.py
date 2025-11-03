"""
utils.py — Helper utilities for logging and formatting.
"""

import os
import datetime


def log_message(msg, dest_folder):
    """Append timestamped log to transfer_log.txt."""
    log_file = os.path.join(dest_folder, "transfer_log.txt")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")


def human_size(size_bytes):
    """Convert bytes to readable format."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:3.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"
