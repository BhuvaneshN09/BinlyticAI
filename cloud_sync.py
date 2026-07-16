"""Keep the Vercel mirror fresh by pushing bin state to the PRIVATE repo.

Run this alongside dashboard_server.py during demos:

    python cloud_sync.py

Whenever dashboard/data/bins.json or learning.json changes, the files are
copied into the private sync clone (C:\\Users\\bnall\\.binlytic-state) and
pushed to github.com/BhuvaneshN09/binlytic-state (private). This script
NEVER commits to the public BinlyticAI repo.
"""

import shutil
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SYNC_CLONE = Path.home() / ".binlytic-state"
STATE_FILES = [
    PROJECT_ROOT / "dashboard" / "data" / "bins.json",
    PROJECT_ROOT / "dashboard" / "data" / "learning.json",
]
SYNC_INTERVAL_SECONDS = 45


def run_git(*arguments, check=True):
    return subprocess.run(
        ["git", *arguments],
        cwd=SYNC_CLONE,
        capture_output=True,
        text=True,
        check=check,
    )


def copy_state_into_clone():
    for source in STATE_FILES:
        if source.exists():
            shutil.copy2(source, SYNC_CLONE / source.name)


def clone_has_changes():
    return bool(run_git("status", "--porcelain").stdout.strip())


def main():
    if not (SYNC_CLONE / ".git").exists():
        raise SystemExit(
            f"Sync clone not found at {SYNC_CLONE}. Clone the private repo "
            "there first: git clone https://github.com/BhuvaneshN09/binlytic-state "
            f'"{SYNC_CLONE}"'
        )

    print("Binlytic cloud sync: pushing bin state to the private repo.")
    print(f"Watching: {', '.join(str(f) for f in STATE_FILES)}")
    print("Press Ctrl+C to stop.")
    while True:
        try:
            copy_state_into_clone()
            if clone_has_changes():
                run_git("add", "-A")
                run_git("commit", "-q", "-m", "sync: bin state")
                run_git("push", "-q")
                print(f"{time.strftime('%H:%M:%S')} state pushed")
        except subprocess.CalledProcessError as error:
            print(f"sync failed: {error.stderr or error}")
        except KeyboardInterrupt:
            return
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
