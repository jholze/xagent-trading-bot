"""Compat: ``python -m services.santiment_sidecar``."""
from services.santiment.sidecar.__main__ import *  # noqa: F403
from services.santiment.sidecar.__main__ import main

if __name__ == "__main__":
    main()
