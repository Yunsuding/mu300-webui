#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download and SHA256-verify golden offline flashing & patched VPN bundles for ZTE U30 Air.
Downloads directly over HTTPS from the public GitHub Release (`Yunsuding/mu300-webui @ baseline-v2026.10.27.2`)
with zero authentication token required, and automatically falls back to mirror/API URLs if needed.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_OUT = ROOT / "device-backups" / "offline-bundles"
MANIFEST_PATH = HERE / "bundle-manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def resolve_github_token(repo: str) -> str:
    for env_key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost=github.com\npath={repo}.git\n\n",
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def fetch_release_asset_map(repo: str, tag: str, token: str) -> dict[str, str]:
    if not token or not repo or not tag:
        return {}
    api_url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "u30air-offline-bundle-fetcher",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {a["name"]: a["url"] for a in data.get("assets", []) if "name" in a and "url" in a}
    except Exception:
        return {}


def stream_url_to_file(url: str, tmp: Path, headers: dict[str, str]) -> tuple[str, int]:
    req = urllib.request.Request(url, headers=headers)
    h = hashlib.sha256()
    downloaded = 0
    with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as out:
        while True:
            buf = resp.read(1024 * 1024)
            if not buf:
                break
            out.write(buf)
            h.update(buf)
            downloaded += len(buf)
    return h.hexdigest(), downloaded


def download_item(item: dict, out_dir: Path, api_assets: dict[str, str], token: str) -> Path:
    dest = out_dir / item["name"]
    expected_sha = item["sha256"]
    if dest.exists():
        actual = sha256_file(dest)
        if actual == expected_sha:
            print(f"[SKIP] {item['name']} already verified ({dest.stat().st_size / 1048576:.2f} MB)")
            return dest
        print(f"[WARN] {item['name']} checksum mismatch, re-downloading...")

    tmp = out_dir / (item["name"] + ".partial")
    candidates: list[tuple[str, dict[str, str], str]] = []

    if item.get("url"):
        candidates.append((item["url"], {"User-Agent": "u30air-offline-bundle-fetcher"}, "public-release"))

    api_asset_url = api_assets.get(item["name"])
    if api_asset_url and token:
        candidates.append(
            (
                api_asset_url,
                {
                    "Accept": "application/octet-stream",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "u30air-offline-bundle-fetcher",
                },
                "authenticated-api",
            )
        )

    if item.get("fallback_url"):
        candidates.append((item["fallback_url"], {"User-Agent": "u30air-offline-bundle-fetcher"}, "upstream-mirror"))

    last_err: Optional[Exception] = None
    for url, headers, label in candidates:
        try:
            print(f"[DOWN] {item['name']} ({item['size_bytes'] / 1048576:.2f} MB via {label}) ...")
            actual, downloaded = stream_url_to_file(url, tmp, headers)
            if actual != expected_sha:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"SHA256 mismatch for {item['name']}: expected {expected_sha}, got {actual}"
                )
            tmp.replace(dest)
            print(f"[ OK ] {item['name']} verified ({downloaded / 1048576:.2f} MB, sha256={actual[:16]}...)")
            return dest
        except Exception as e:
            tmp.unlink(missing_ok=True)
            last_err = e
            print(f"[WARN] {label} failed for {item['name']}: {e}")

    raise RuntimeError(f"下载 {item['name']} 失败: {last_err}")


def main():
    parser = argparse.ArgumentParser(description="Fetch & verify U30 Air offline flashing & patched VPN bundles.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Target directory for offline bundles")
    parser.add_argument(
        "--essential-only",
        action="store_true",
        help="Only download the all-in-one Magisk/PC offline zip + patched VPN/Mihomo bundle (~231 MB total)",
    )
    parser.add_argument("--verify-only", action="store_true", help="Only verify already downloaded files")
    parser.add_argument("--repo", default="", help="Optional GitHub repo override for authenticated API fallback")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    bundles = manifest["bundles"]
    if args.essential_only:
        bundles = [b for b in bundles if b["group"] != "split-release-images"]

    if args.verify_only:
        ok = True
        for b in bundles:
            p = out_dir / b["name"]
            if not p.exists():
                print(f"[MISS] {b['name']}")
                ok = False
            elif sha256_file(p) != b["sha256"]:
                print(f"[FAIL] {b['name']} SHA256 mismatch")
                ok = False
            else:
                print(f"[ OK ] {b['name']}")
        sys.exit(0 if ok else 1)

    repo = args.repo or os.environ.get("MU300_RELEASE_REPO") or manifest.get("release_repo", "")
    tag = manifest.get("release_tag", "baseline-v2026.10.27.2")
    token = resolve_github_token(repo) if repo else ""
    api_assets = fetch_release_asset_map(repo, tag, token) if token else {}

    for b in bundles:
        download_item(b, out_dir, api_assets, token)

    print(f"\nAll selected bundles verified in: {out_dir}")


if __name__ == "__main__":
    main()
