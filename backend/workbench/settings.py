"""Machine-local paths and executable discovery.

Nothing in this module reads a provider secret.  The defaults deliberately keep
mutable state outside the source tree so a repository checkout remains clean and
can be moved between devices.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _default_state_dir() -> Path:
    configured = os.environ.get("DUB_WORKBENCH_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "DubWorkbench"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "dub-workbench"
    return Path.home() / ".local" / "state" / "dub-workbench"


def _default_library_dir() -> Path:
    configured = os.environ.get("DUB_WORKBENCH_LIBRARY_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Videos" / "DubWorkbench"


@dataclass(frozen=True, slots=True)
class WorkbenchSettings:
    state_dir: Path
    library_dir: Path
    database_path: Path
    ffmpeg: str
    ffprobe: str
    yt_dlp: str
    worker_id: str

    @classmethod
    def from_environment(cls) -> WorkbenchSettings:
        state_dir = _default_state_dir().resolve()
        library_dir = _default_library_dir().resolve()
        return cls(
            state_dir=state_dir,
            library_dir=library_dir,
            database_path=Path(
                os.environ.get("DUB_WORKBENCH_DB", state_dir / "workbench.sqlite3")
            ).expanduser().resolve(),
            ffmpeg=os.environ.get("DUB_FFMPEG", shutil.which("ffmpeg") or "ffmpeg"),
            ffprobe=os.environ.get("DUB_FFPROBE", shutil.which("ffprobe") or "ffprobe"),
            yt_dlp=os.environ.get("DUB_YT_DLP", shutil.which("yt-dlp") or "yt-dlp"),
            worker_id=os.environ.get("DUB_WORKER_ID", os.environ.get("COMPUTERNAME", "local-worker")),
        )

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def public_environment(self) -> dict[str, object]:
        """Return diagnostics without secrets or file contents."""
        return {
            "state_dir": str(self.state_dir),
            "library_dir": str(self.library_dir),
            "database_path": str(self.database_path),
            "worker_id": self.worker_id,
            "tools": {
                "ffmpeg": shutil.which(self.ffmpeg) or self.ffmpeg,
                "ffprobe": shutil.which(self.ffprobe) or self.ffprobe,
                "yt_dlp": shutil.which(self.yt_dlp) or self.yt_dlp,
            },
        }
