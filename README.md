<p align="center">
  <img src="bootlogo/postmarketos-logo.svg" width="104" height="104" alt="postmarketOS emblem">
</p>

<h1 align="center">s9-postmarketos<br><sub>Galaxy S9 Linux Server</sub></h1>
<p align="center"><strong>A pocket Linux server, with a touch dashboard on the phone.</strong><br>Nura · OpenRC · Docker · Tailscale</p>

<p align="center">
  <a href="#hardware-status"><img src="https://img.shields.io/badge/status-experimental-amber" alt="Status: experimental"></a>
  <a href="#hardware-status"><img src="https://img.shields.io/badge/device-SM--G960F-blue" alt="Device: SM-G960F"></a>
  <a href="https://github.com/philipplinux/s9-postmarketos/releases/latest"><img src="https://img.shields.io/github/v/release/philipplinux/s9-postmarketos?label=release" alt="Latest release"></a>
</p>

<p align="center">
  <a href="#get-started">Get started</a> · <a href="#on-the-phone">Phone UI</a> · <a href="#web-dashboard">Dashboard</a> · <a href="#hardware-status">Status</a> · <a href="#documentation">Docs</a> · <a href="https://huggingface.co/PhilippLinux/s9-postmarketos">🤗 Hugging Face</a> · <a href="CONTRIBUTING.md">Contribute</a>
</p>

<p align="center">
  <strong><a href="https://philipplinux.github.io/s9-postmarketos/">🌐 Browser flasher</a> · <a href="https://huggingface.co/PhilippLinux/s9-postmarketos">⬇️ Download images</a></strong>
</p>

---

A server ROM for the **Samsung Galaxy S9 SM-G960F, Exynos 9810**: Nura edge with OpenRC on a patched vendor kernel 4.9. Run Docker and Tailscale, use a local terminal or web dashboard, and manage networking, power and thermals from the screen. The shell includes bash, Starship, ble.sh and fzf. The Python dashboard uses the standard library; the touch UI has its own display and terminal dependencies.

> [!NOTE]
> **postmarketOS is now [Nura](https://nura.eco/).** The rename was announced on [27 September 2026](https://nura.eco/blog/2026/09/27/nura-rename/).
> This project retains its historical repository name, `s9-postmarketos`.

<p align="center"><img src="docs/fastfetch.png" width="600" alt="fastfetch in the on-phone terminal"></p>

<a id="get-started"></a>

## 🚀 Get started

1. **Check the device.** The tested target is the Exynos **SM-G960F (`starlte`)**. The [S9+ build](s9plus/README.md) is separate and remains untested on hardware.
2. Open the [browser flasher](https://philipplinux.github.io/s9-postmarketos/) in Chrome or Edge, or get the files from the [latest GitHub release](https://github.com/philipplinux/s9-postmarketos/releases/latest). Release images are also available on [🤗 Hugging Face](https://huggingface.co/PhilippLinux/s9-postmarketos). Read the [flasher instructions](web-flasher/README.md) before writing partitions.
3. Follow the flasher's Activity output until the final step reports Done. The flasher documentation describes Download Mode/TWRP installation and SHA-256 verification of the selected image files.
4. Log in with the documented image credentials: **`user` / `changeme`**. Change this public default password on first login using **SSH & User** on the phone. The image ships no SSH keys or Wi-Fi credentials; provision your own access settings.
5. Join Wi-Fi from the touch UI, then use the terminal, SSH or dashboard. The [network guide](docs/technical-reference.md#wi-fi-and-ssh-pages-network-tab) covers Wi-Fi, SSH, user settings and USB modes.

Experimental: a fresh release-image installation through the browser flasher has been tested on one SM-G960F. Review the hardware status and recovery notes before flashing.

<a id="on-the-phone"></a>

## 📱 On the phone

<p align="center"><img src="docs/phone-ui-tour.gif" width="240" alt="Boot logo and phone UI tab tour"> <img src="docs/phone-ui-subpages.gif" width="240" alt="Phone UI subpages"></p>

*Boot logo, tabs and subpages, animated with the real UI code on the phone. Personal values are replaced in these recordings.*

The framebuffer UI brings the server's everyday controls to the phone:

- **Overview and processes:** CPU, GPU, memory, storage, service status and a process list; tap Storage for partition details.
- **Network:** Wi-Fi scan/join and saved networks, DHCP or static addressing, SSH and user settings, Tailscale status, USB modes, Bluetooth and dashboard reachability.
- **Thermals and power:** thermal readings, CPU/GPU limits, charge cap and touch boost. Overclock is opt-in, off by default and guarded by two warnings.
- **Terminal:** a persistent login session with a touch keyboard, rotation and scrollback. The shell session survives tab changes and screen blanking.
- **Docker:** container state, details, logs and controls appropriate to the current state.

The power key cycles console, dashboard and screen off. Reboot and shutdown require confirmation. See the [complete touch UI reference](docs/technical-reference.md#framebuffer-touch-ui-display) for behavior, dependencies, deployment and known caveats.

<a id="web-dashboard"></a>

## 🖥️ Web dashboard

![Web dashboard](docs/web-dashboard.png)

`dashboard/server.py` serves the dashboard and `/api/status` on port **8090**. It shows battery, per-core CPU activity and clocks, Mali-G72 status, thermals, memory, storage, networking, services and processes. The API also supplies the on-phone UI.

Network settings control dashboard reachability over Wi-Fi, Tailscale and USB. The service remains available locally for the touch UI even when external web access is disabled. See [Bluetooth and dashboard settings](docs/technical-reference.md#bluetooth-and-web-dashboard-pages-network-tab), and the [dashboard notes](docs/technical-reference.md) for deployment paths, OpenRC service details and checks.

<a id="services-shell-and-local-models"></a>

## ⚙️ Services, shell and local models

**Docker** uses overlay2 on `/srv/docker` with iptables-legacy. **Tailscale** is available after a separate login with `sudo tailscale up`. These are ordinary services on the phone, with status surfaced in the UI.

The login shell combines **bash, Starship, ble.sh and fzf**. The boot setup supplies it to login users while preserving their own existing `.bashrc` files. Build and staging details remain in the [image build guide](docs/technical-reference.md#build-the-image-from-scratch).

The image includes patched **llama.cpp** as `s9-llama-vk`; bring your own model. CPU execution works in the documented tests. Vulkan runs on Mali but is slower in those documented measurements. See the [LLM setup and build](llm/README.md) and [benchmark notes](docs/llm-bench.md).

<a id="hardware-status"></a>

## 📋 Hardware status

Tested on **one SM-G960F**: a fresh release-image installation through the web flasher, plus an earlier development installation. Device coverage and untested features are listed below.

| Feature | Status | Note |
|---|---|---|
| Wi-Fi, SSH (dropbear) | working | set up from the touch UI or `wifi-setup` |
| USB networking | working | NCM 172.16.43.1 (default); RNDIS verified on one Windows 11 host and tried with a Linux host |
| Touch UI, terminal, web dashboard | working | |
| Bluetooth | working | power, visibility, name |
| Docker | working | overlay2 on `/srv/docker`, iptables-legacy |
| Tailscale | working | log in once with `sudo tailscale up` |
| Thermal guard, charge cap | working | |
| Overclock | working | opt-in behind two warnings, off by default |
| llama.cpp (CPU) | working | ~5 t/s on 4 big cores; bring your own model |
| llama.cpp (GPU, Vulkan) | partly | runs, but slower than the CPU (~1 t/s) |
| Audio (speaker) | works | stock Samsung DSP firmware bundled as `s9-audio-fw` |
| Reboot on freeze | not tested | no real lockup triggered yet |
| Web flasher, release image first boot | working | fresh install on one SM-G960F |
| TWRP zip | not tested | |
| Camera, flash LED | not working | per the postmarketOS wiki |
| Modem/SIM, GPS, fingerprint, NFC | not tested | out of scope for a server |
| ADB, MTP | not implemented | |

### Galaxy S9+ — SM-G965F (`star2lte`)

Choose **Galaxy S9+** in the flasher or use the `-s9plus` release. **Untested on hardware.** Speaker audio is absent because the S9 DSP tuning is not shipped for the S9+. See [S9+ recipes and build steps](s9plus/README.md).

### Known limits

WPA3-only Wi-Fi networks are unsupported. USB reconnects can trigger a vendor-kernel networking problem; the technical reference describes the guard and its recovery behavior. RNDIS has been verified on one Windows 11 host and tried with a Linux host; broader host compatibility is untested. CPU/GPU clock limits and overclock behavior depend on the vendor kernel. See the [detailed UI reference](docs/technical-reference.md#framebuffer-touch-ui-display), [USB notes](docs/technical-reference.md#wi-fi-and-ssh-pages-network-tab) and [power documentation](power/README.md).

<a id="build-and-development"></a>

## 🛠️ Build and development

Image builds use **pmbootstrap** and a pmaports checkout with the project's kernel recipe and staged `s9-*` packages. Firmware and graphics/audio components have separate input requirements. The model, Wi-Fi credentials and SSH keys are not included in the image.

Follow the [full image build procedure](docs/technical-reference.md#build-the-image-from-scratch), [kernel recipe](kernel/README.md) and component instructions rather than mixing staging, installation and flashing commands.

The browser flasher is a separate web project. Its [README](web-flasher/README.md) documents installation, checks, tests and build. For Python, display and device changes, use the [contribution guide](CONTRIBUTING.md) and report which checks were offline and which ran on hardware.

<a id="documentation"></a>

## 📚 Documentation

| Guide | What it covers |
| --- | --- |
| [Technical reference](docs/technical-reference.md) | Setup, deployment, build procedure and detailed behavior |
| [Web flasher](web-flasher/README.md) | WebUSB installation and flasher development |
| [Kernel](kernel/README.md) | Vendor-kernel configuration, patches and build inputs |
| [Power](power/README.md) | Power management and opt-in overclock |
| [Local models](llm/README.md) · [Benchmarks](docs/llm-bench.md) | llama.cpp, model requirements and recorded measurements |
| [S9+](s9plus/README.md) | Device-specific build and limitations |
| [Contributing](CONTRIBUTING.md) | Focused changes, useful bug reports and relevant verification |

<a id="contributing-and-licensing"></a>

## 🤝 Contributing and licensing

Report problems in the [XDA thread](https://xdaforums.com/t/rom-linux-pmos4-9-s9-headless-server-w-dashboard-overclock-sm-g960f-exynos-starlte.4805155/) or [Reddit discussion](https://www.reddit.com/r/mobilelinux/comments/1x6luth/samsung_galaxy_s9_exynos_on_nura_postmarketos/). For documentation and focused improvements, see the [contribution guide](CONTRIBUTING.md). Reports should include the device, version, reproduction and expected versus observed behavior, with personal data removed.

See the [licence page](LICENSE.md) for the proposed MIT licence, its scope and existing third-party terms.

---

<p align="center">Created by <strong><a href="https://github.com/philipplinux/s9-postmarketos">PhilippLinux</a></strong>.</p>

<p align="center">
  <a href="https://xdaforums.com/t/rom-linux-pmos4-9-s9-headless-server-w-dashboard-overclock-sm-g960f-exynos-starlte.4805155/">XDA thread</a> · <a href="https://www.reddit.com/r/mobilelinux/comments/1x6luth/samsung_galaxy_s9_exynos_on_nura_postmarketos/">r/mobilelinux</a>
</p>

<p align="center">
  <a href="https://www.buymeacoffee.com/philipplinux"><img src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer%3F&amp;emoji=%F0%9F%8D%BA&amp;slug=philipplinux&amp;button_colour=FFDD00&amp;font_colour=000000&amp;font_family=Cookie&amp;outline_colour=000000&amp;coffee_colour=ffffff" width="220" alt="Buy me a beer?"></a>
</p>
