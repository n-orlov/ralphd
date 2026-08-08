"""Run-dir state: paths, atomic JSON writes, events log."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


@dataclass
class RunDir:
    """Layout of /run and helpers over it. Engine-owned."""

    root: Path
    _events_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _event_id: int = 0

    def __post_init__(self) -> None:
        for sub in ("steering", "iterations", "approaches", "artifacts"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        last = 0
        try:
            for line in (self.root / "events.jsonl").read_text().splitlines():
                last = max(last, json.loads(line).get("id", 0))
        except FileNotFoundError:
            pass
        self._event_id = last

    # -- well-known files ------------------------------------------------
    @property
    def status_file(self) -> Path:
        return self.root / "status.json"

    @property
    def tasks_file(self) -> Path:
        return self.root / "tasks.json"

    @property
    def prd_file(self) -> Path:
        return self.root / "prd.md"

    @property
    def composite_prd_file(self) -> Path:
        return self.root / "composite-prd.md"

    @property
    def notes_file(self) -> Path:
        return self.root / "notes.md"

    @property
    def findings_file(self) -> Path:
        return self.root / "review-findings.md"

    @property
    def steering_dir(self) -> Path:
        return self.root / "steering"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    def iteration_dir(self, n: int) -> Path:
        d = self.root / "iterations" / f"{n:04d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- status ----------------------------------------------------------
    def read_status(self) -> dict:
        return read_json(self.status_file, {})

    def update_status(self, **patch: Any) -> dict:
        status = self.read_status()
        status.update(patch)
        status["updatedAt"] = utcnow()
        atomic_write_json(self.status_file, status)
        return status

    def read_tasks(self) -> dict:
        return read_json(self.tasks_file, {})

    # -- events ----------------------------------------------------------
    def emit(self, type_: str, **data: Any) -> dict:
        with self._events_lock:
            self._event_id += 1
            event = {"id": self._event_id, "ts": utcnow(), "type": type_, **data}
            with open(self.root / "events.jsonl", "a") as f:
                f.write(json.dumps(event) + "\n")
        return event

    # -- steering --------------------------------------------------------
    def add_steering(self, message: str, name: str | None = None) -> str:
        existing = sorted(self.steering_dir.glob("[0-9][0-9][0-9]-*.md"))
        seq = int(existing[-1].name[:3]) + 1 if existing else 1
        fname = f"{seq:03d}-{name or 'steering'}.md"
        atomic_write(self.steering_dir / fname, message.rstrip() + "\n")
        self.emit("steering.received", file=fname)
        return fname

    def consumed_marker(self) -> Path:
        return self.steering_dir / ".consumed.json"

    def pending_steering(self) -> list[Path]:
        consumed = set(read_json(self.consumed_marker(), []))
        return [p for p in sorted(self.steering_dir.glob("[0-9][0-9][0-9]-*.md"))
                if p.name not in consumed]

    def consume_steering(self, files: list[Path], iteration: int) -> None:
        consumed = read_json(self.consumed_marker(), [])
        consumed.extend(p.name for p in files)
        atomic_write_json(self.consumed_marker(), consumed)
        for p in files:
            self.emit("steering.consumed", file=p.name, iteration=iteration)
