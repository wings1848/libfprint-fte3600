# SPDX-License-Identifier: LGPL-2.1-or-later
"""Allow running ``python3 -m fte3600_toolkit`` directly."""

from .cli.main import entrypoint

if __name__ == "__main__":
    entrypoint()
