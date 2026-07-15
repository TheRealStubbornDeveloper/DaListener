from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare DaListener redistributable local model assets")
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    destination = args.target / "Whisper" / "large-v3-turbo"
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        local_dir=destination,
    )
    print(destination)


if __name__ == "__main__":
    main()
