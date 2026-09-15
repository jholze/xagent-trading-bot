"""Root Dockerfile must stay buildable on Railway (#397 follow-up, PR #457 broke every service).

Railway's builder rejects BuildKit `RUN --mount=type=bind,...` ("is missing a
type=cache argument (other mount types are not supported)"). The xAI auth sidecar
therefore ships its own Node image instead of baking Node into the shared image.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROOT_DOCKERFILE = ROOT / "Dockerfile"
SIDECAR_DIR = ROOT / "services" / "xai_auth_sidecar"
SIDECAR_DOCKERFILE = SIDECAR_DIR / "Dockerfile"
SIDECAR_RAILWAY_TOML = SIDECAR_DIR / "railway.toml"


class TestRootDockerfileRailwayCompat:
    def test_no_bind_mount_in_root_dockerfile(self):
        text = ROOT_DOCKERFILE.read_text(encoding="utf-8")
        assert "--mount=type=bind" not in text
        assert not re.search(r"--mount=type=(?!cache\b)", text), "Railway only supports --mount=type=cache"

    def test_root_dockerfile_is_node_free_for_runtime_stage(self):
        text = ROOT_DOCKERFILE.read_text(encoding="utf-8")
        assert "RUN_XAI_AUTH" not in text
        # Node is allowed only as the desk SPA build stage; the runtime stage copies dist/ from it.
        assert "COPY --from=desk /desk/dist" in text
        assert "xai_auth_sidecar" not in text


class TestXaiAuthSidecarImage:
    def test_dedicated_dockerfile_shape(self):
        text = SIDECAR_DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"^FROM node:22-slim", text, re.M)
        assert "--mount=type=bind" not in text
        assert "npm ci --omit=dev" in text
        assert "package-lock.json" in text
        assert not re.search(r"^FROM python", text, re.M)
        assert "pip install" not in text
        assert "requirements.txt" not in text

    def test_railway_toml_points_at_dedicated_dockerfile(self):
        text = SIDECAR_RAILWAY_TOML.read_text(encoding="utf-8")
        assert 'builder = "DOCKERFILE"' in text
        assert 'dockerfilePath = "services/xai_auth_sidecar/Dockerfile"' in text
