"""Allow running as `python -m antigravity_matrix`."""

from .daemon import run_daemon

if __name__ == "__main__":
    run_daemon()
