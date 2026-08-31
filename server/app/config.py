from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _path_from_env(name: str, default: Path, base_dir: Path) -> Path:
    value = Path(os.getenv(name, str(default)))
    return (base_dir / value).resolve() if not value.is_absolute() else value.resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    base_dir: Path
    server_host: str
    server_port: int
    database_url: str
    artifact_root: Path
    workers_config: Path
    worker_connect_timeout: float
    worker_request_timeout: float
    worker_deploy_timeout: float
    health_interval_seconds: float
    health_failure_threshold: int
    lease_idle_timeout_seconds: float
    lease_reclaim_grace_seconds: float
    lease_reaper_interval_seconds: float
    max_bit_size: int
    max_hwh_size: int
    max_predict_image_size: int
    admin_action_token: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        base_dir = Path(__file__).resolve().parents[1]
        data_dir = base_dir / "data"
        default_database = f"sqlite:///{data_dir / 'central.db'}"
        return cls(
            base_dir=base_dir,
            server_host=os.getenv("SERVER_HOST", "127.0.0.1"),
            server_port=int(os.getenv("SERVER_PORT", "8000")),
            database_url=os.getenv("DATABASE_URL", default_database),
            artifact_root=_path_from_env(
                "ARTIFACT_ROOT", data_dir / "artifacts", base_dir
            ),
            workers_config=_path_from_env(
                "WORKERS_CONFIG", base_dir / "config/workers.json", base_dir
            ),
            worker_connect_timeout=float(
                os.getenv("WORKER_CONNECT_TIMEOUT", "2.0")
            ),
            worker_request_timeout=float(
                os.getenv("WORKER_REQUEST_TIMEOUT", "30.0")
            ),
            worker_deploy_timeout=float(os.getenv("WORKER_DEPLOY_TIMEOUT", "120.0")),
            health_interval_seconds=float(os.getenv("HEALTH_INTERVAL_SECONDS", "5.0")),
            health_failure_threshold=int(os.getenv("HEALTH_FAILURE_THRESHOLD", "3")),
            lease_idle_timeout_seconds=float(os.getenv(
                "LEASE_IDLE_TIMEOUT_SECONDS",
                os.getenv("SESSION_IDLE_TIMEOUT_SECONDS", "1800"),
            )),
            lease_reclaim_grace_seconds=float(
                os.getenv("LEASE_RECLAIM_GRACE_SECONDS", "300")
            ),
            lease_reaper_interval_seconds=float(
                os.getenv("LEASE_REAPER_INTERVAL_SECONDS", "10")
            ),
            max_bit_size=int(os.getenv("MAX_BIT_SIZE", str(128 * 1024 * 1024))),
            max_hwh_size=int(os.getenv("MAX_HWH_SIZE", str(16 * 1024 * 1024))),
            max_predict_image_size=int(
                os.getenv("MAX_PREDICT_IMAGE_SIZE", str(8 * 1024 * 1024))
            ),
            admin_action_token=os.getenv("ADMIN_ACTION_TOKEN") or None,
        )
