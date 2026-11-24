# SPDX-License-Identifier: LGPL-2.1-or-later
"""Enrollment, verification feedback, and diagnostics for FTE3600 / GPD readers.

Talks exclusively to fprintd over D-Bus and never touches /dev/spidev, GPIO, or UIO.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
