#!/bin/bash
cd "$(dirname "$0")"
exec python3 ps2_native_gui.py "$@"
