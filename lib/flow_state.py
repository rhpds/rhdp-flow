"""Persist Flow schedules + deploy results across pod restarts / rollouts.

In-cluster the writable root is read-only; without a PVC the previous
``~/.rhdp-flow/last_results.json`` path never survived ImageStream deploys.
Point ``RHDP_FLOW_DATA_DIR`` at a mounted volume (default ``/app/data``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

def data_dir() -> Path:
    return Path(os.environ.get("RHDP_FLOW_DATA_DIR", "/app/data")).expanduser()


def _schedules_path() -> Path:
    return Path(
        os.environ.get("RHDP_SCHEDULES_FILE", str(data_dir() / "last_schedules.json"))
    ).expanduser()


def _results_path() -> Path:
    return Path(
        os.environ.get(
            "RHDP_RESULTS_FILE",
            str(data_dir() / "last_results.json"),
        )
    ).expanduser()


def _meta_path() -> Path:
    return data_dir() / "last_meta.json"


def _qa_results_path() -> Path:
    return Path(
        os.environ.get(
            "RHDP_QA_RESULTS_FILE",
            str(data_dir() / "last_qa_results.json"),
        )
    ).expanduser()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _dataclass_from_dict(cls: type[T], raw: dict) -> T:
    """Construct a dataclass ignoring unknown keys (forward/backward compatible)."""
    if not is_dataclass(cls):
        raise TypeError(f"{cls} is not a dataclass")
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in allowed})  # type: ignore[arg-type]


def save_schedules(schedules: list[Any], *, filename: str = "") -> None:
    try:
        _write_json(_schedules_path(), [asdict(s) for s in schedules])
        _write_json(_meta_path(), {"filename": filename or ""})
    except OSError as exc:
        logger.warning("Could not persist schedules: %s", exc)


def load_schedules(schedule_cls: type[T]) -> tuple[list[T], str]:
    try:
        data = _read_json(_schedules_path())
        if not isinstance(data, list):
            return [], ""
        schedules = [_dataclass_from_dict(schedule_cls, row) for row in data if isinstance(row, dict)]
        meta = _read_json(_meta_path()) or {}
        filename = str(meta.get("filename") or "") if isinstance(meta, dict) else ""
        return schedules, filename
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Could not load persisted schedules: %s", exc)
        return [], ""


def save_results(results: list[Any]) -> None:
    try:
        _write_json(_results_path(), [asdict(r) for r in results])
    except OSError as exc:
        logger.warning("Could not persist results: %s", exc)


def load_results(result_cls: type[T]) -> list[T]:
    try:
        data = _read_json(_results_path())
        if not isinstance(data, list):
            return []
        return [_dataclass_from_dict(result_cls, row) for row in data if isinstance(row, dict)]
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Could not load persisted results: %s", exc)
        return []


def save_qa_results(results: list[Any]) -> None:
    """Persist QA results (list of dicts or model_dump()-able objects)."""
    try:
        payload = []
        for r in results:
            if hasattr(r, "model_dump"):
                payload.append(r.model_dump())
            elif is_dataclass(r):
                payload.append(asdict(r))
            elif isinstance(r, dict):
                payload.append(r)
            else:
                payload.append(dict(r))
        _write_json(_qa_results_path(), payload)
    except OSError as exc:
        logger.warning("Could not persist QA results: %s", exc)


def load_qa_results() -> list[dict]:
    """Load persisted QA results as plain dicts (caller validates into models)."""
    try:
        data = _read_json(_qa_results_path())
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Could not load persisted QA results: %s", exc)
        return []


def clear_persisted_state() -> None:
    """Remove schedule/result files (session clear)."""
    for path in (_schedules_path(), _results_path(), _meta_path(), _qa_results_path()):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not clear persisted state %s: %s", path, exc)
