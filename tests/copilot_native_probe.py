"""Geometry-only probe used by the synthetic Electron window test, never chats."""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from copilot_window_watch import WindowsAPI

parser = argparse.ArgumentParser()
parser.add_argument('--hwnd', type=int, required=True)
args = parser.parse_args()
api = WindowsAPI()
geometry = api.read_geometry(args.hwnd)
foreground_hwnd, _ = api.foreground()
print(json.dumps({'geometry': asdict(geometry), 'foreground_hwnd': foreground_hwnd}))
