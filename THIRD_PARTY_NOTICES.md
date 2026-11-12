# Third-Party Open-Source Notices & Acknowledgments

This repository builds upon and interoperates with the following open-source projects. We gratefully acknowledge their authors and contributors.

---

## 1. `mu300-linux` & `mu300-linux-vpn` (MIT License)

Files under `standalone/upstream-installer/` and base system scripts/units under `standalone/device-runtime/` are derived from:
- Upstream Repository: https://github.com/dikeckaan/mu300-linux (`v2026.10.27.2`)
- Upstream VPN Toolkit: https://github.com/dikeckaan/mu300-linux-vpn (`v2.0.4`)

```text
MIT License

Copyright (c) 2026 dikeckaan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. `Mihomo` (Clash.Meta) & Ruleset Databases (GPL-3.0)

The optional offline Release archive `u30air-patched-vpn-mihomo.tar.gz` distributes an unmodified ARM64 Linux binary of **Mihomo** (`vpn-mihomo/bin/mihomo`) as an independent user-space executable alongside GeoIP/GeoSite routing databases:
- **Mihomo Engine**: https://github.com/MetaCubeX/mihomo — Licensed under the **GNU General Public License v3.0 (GPL-3.0)**. Complete source code is available at the upstream repository above.
- **Routing Databases (`Country.mmdb`, `GeoSite.dat`)**: https://github.com/MetaCubeX/meta-rules-dat — Licensed under **GPL-3.0 / CC-BY-SA**.

---

## 3. Linux Kernel (`5.4.254`) & Ubuntu `24.04 LTS` Rootfs

The offline flashing Release bundles (`mu300-kernel.tar.gz`, `mu300-ubuntu-rootfs.tar.gz`, `mu300-magisk-v2026.10.27.2-ubuntu-24.04-k5.4.zip`) contain:
- **Linux Kernel**: Licensed under **GPL-2.0-only** (source tree referenced by `dikeckaan/mu300-linux`). Note: zero proprietary ZTE/Unisoc modem or Wi-Fi firmware blobs are distributed in these archives; device-specific firmware is extracted locally from each user's own device partitions during installation.
- **Ubuntu 24.04 LTS (Noble Numbat) ARM64 Userspace**: Distributed under Canonical's respective package licenses.

---

## 4. Tailwind CSS Utility Stylesheet (MIT License)

- `u30air-webui/web/app.css`: Precompiled offline utility stylesheet generated with Tailwind CSS (https://github.com/tailwindlabs/tailwindcss) — Licensed under the **MIT License** (`Copyright (c) Tailwind Labs, Inc.`).
