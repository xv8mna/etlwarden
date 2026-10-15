"""Entry point of the standalone etlwarden.exe. PyInstaller needs a plain script to start from."""

from etlwarden.cli import main

raise SystemExit(main())
