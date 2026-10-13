#!/usr/bin/env python3
"""Compatibility Facade for audit_prompt.py directing to audit_cli micro-kernel."""

from __future__ import annotations

import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from audit_cli import (
    main,
    run_audit,
    detect_channel,
    audit_native_zh,
    audit_ref2va_en,
    compute_sha256,
)

if __name__ == "__main__":
    sys.exit(main())
