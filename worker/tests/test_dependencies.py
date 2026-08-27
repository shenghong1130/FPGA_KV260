from __future__ import annotations

from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]


def test_worker_and_pynq_dependency_contract_stays_pinned() -> None:
    requirements = {
        line.strip()
        for line in (REPOSITORY / "worker/requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "fastapi==0.115.13" in requirements
    assert "pydantic==1.10.22" in requirements
    assert not any(line.startswith("pydantic>=") for line in requirements)

    installer = (REPOSITORY / "runtime/install_pynq.sh").read_text(
        encoding="utf-8"
    )
    assert 'PYDANTIC_VERSION="${PYDANTIC_VERSION:-1.10.22}"' in installer
    assert 'FASTAPI_VERSION="${FASTAPI_VERSION:-0.115.13}"' in installer
    assert '"$PYNQ_VENV/bin/python" -m pip check' in installer
    assert 'Requires-Dist: pydantic (>=1.9.1,<2)' in installer
    assert "PYNQ/FastAPI/Pydantic compatibility: OK" in installer
    assert installer.index("pip uninstall -y fastapi pydantic") < installer.index(
        "pip install --ignore-installed"
    )
