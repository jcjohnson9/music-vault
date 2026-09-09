"""Select explicit, isolated diagnostics before importing the desktop application."""

import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--verify-acquisition":
        from tools.dev.verify_acquisition import main as verify_acquisition

        return verify_acquisition(sys.argv[2:])
    from music_vault.app import main as run_application

    return run_application()

if __name__ == "__main__":
    raise SystemExit(main())
