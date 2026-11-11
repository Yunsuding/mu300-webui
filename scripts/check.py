#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run offline syntax, inline JS, runtime manifest SHA256, and hygiene checks
without requiring a connected U30 Air device or network access.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")


class InlineScriptExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inside = not dict(attrs).get("src")

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def verify_python_syntax():
    py_targets = [
        *ROOT.glob("*.py"),
        *ROOT.glob("scripts/*.py"),
        *ROOT.glob("standalone/*.py"),
        ROOT / "u30air-webui" / "server.py",
        ROOT / "u30air-webui" / "bin" / "mu300-mihomo",
    ]
    checked = 0
    for path in py_targets:
        if path.exists():
            compile(path.read_bytes(), str(path), "exec")
            checked += 1
    print(f"[OK] Python syntax verified ({checked} files).")


def verify_webui_inline_js():
    index_html = ROOT / "u30air-webui" / "web" / "index.html"
    parser = InlineScriptExtractor()
    parser.feed(index_html.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="mu300-webui-js-") as temp:
        js_file = Path(temp) / "inline.js"
        js_file.write_text("\n".join(parser.parts), encoding="utf-8")
        subprocess.run(["node", "--check", str(js_file)], check=True)
    print("[OK] WebUI inline JavaScript syntax verified.")


def verify_runtime_manifest():
    rt_dir = ROOT / "standalone" / "device-runtime"
    manifest_path = rt_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    file_count = 0
    for entry in manifest.get("entries", []):
        if entry.get("type") != "file":
            continue
        fp = rt_dir / entry["repo_path"]
        if not fp.exists():
            raise SystemExit(f"[FAIL] Missing runtime file: {entry['repo_path']}")
        raw = fp.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if len(raw) != entry["size"] or sha != entry["sha256"]:
            raise SystemExit(
                f"[FAIL] Runtime SHA256/size mismatch on {entry['repo_path']} (CRLF corruption?)"
            )
        file_count += 1
    print(f"[OK] Standalone device-runtime manifest verified ({file_count} files byte-exact).")


def verify_bundle_manifest():
    bm = ROOT / "standalone" / "bundle-manifest.json"
    if not bm.exists():
        return
    data = json.loads(bm.read_text(encoding="utf-8"))
    bundles = data.get("bundles", [])
    assert len(bundles) >= 9, "Expected at least 9 offline bundles in bundle-manifest.json"
    for b in bundles:
        assert len(b["sha256"]) == 64 and b["size_bytes"] > 0 and b["url"].startswith("https://")
    print(f"[OK] Offline bundle manifest schema verified ({len(bundles)} bundles).")


def main():
    verify_python_syntax()
    verify_webui_inline_js()
    verify_runtime_manifest()
    verify_bundle_manifest()
    print("All offline checks passed.")


if __name__ == "__main__":
    main()
