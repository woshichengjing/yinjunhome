#!/usr/bin/env python3
"""Stage the complete state-machine page for the existing WebUI restore timer."""

import argparse
import os
from pathlib import Path
import re
import tempfile


FRONTEND = Path(__file__).resolve().parent / "frontend"
PAGE = "state-machine-dashboard.html"
ALIASES = (PAGE, "state-machine-dashboard-v5.html")


def replace_once(html: str, pattern: str, replacement: str) -> str:
    html, count = re.subn(pattern, lambda _: replacement, html)
    if count != 1:
        raise ValueError(f"Expected exactly one match for {pattern}, got {count}")
    return html


def bundle(frontend: Path = FRONTEND) -> str:
    html = (frontend / PAGE).read_text(encoding="utf-8")
    for name in ("weather-forecast.css", "wall-theme.css"):
        css = (frontend / name).read_text(encoding="utf-8")
        html = replace_once(html, rf'<link rel="stylesheet" href="/static/{re.escape(name)}\?[^"\n]+">',
                            f'<style data-source="{name}">\n{css}</style>')

    controls = (frontend / "control-switches.js").read_text(encoding="utf-8")
    html = replace_once(html, r'<script src="/static/control-switches\.js\?[^"\n]+"></script>',
                        f'<script data-source="control-switches.js">\n{controls}</script>')

    forecast = (frontend / "weather-forecast.js").read_text(encoding="utf-8")
    html = replace_once(html, r'<script src="/static/weather-forecast\.js\?[^"\n]+" defer></script>', "")
    return replace_once(html, r'</body>', f'<script data-source="weather-forecast.js">\n{forecast}</script>\n</body>')


def publish(destination: Path) -> None:
    payload = bundle().encode("utf-8")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ALIASES:
        target = destination / name
        if target.is_file() and target.read_bytes() == payload:
            continue
        with tempfile.NamedTemporaryFile(dir=destination, prefix=".state-machine-", delete=False) as handle:
            stage = Path(handle.name)
        try:
            stage.write_bytes(payload)
            stage.chmod(0o644)
            os.replace(stage, target)
        finally:
            stage.unlink(missing_ok=True)
        print(f"Published {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path.home() / ".hermes" / "dashboards")
    publish(parser.parse_args().destination)
