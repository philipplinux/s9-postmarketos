# Technical reference

Headless server ROM for the Samsung Galaxy S9 (SM-G960F, Exynos 9810): postmarketOS edge (OpenRC) on our patched vendor kernel 4.9, with an on-phone touch UI, a web dashboard, Docker, Tailscale, opt-in overclock and a bash + starship + ble.sh + fzf shell. Stdlib-only Python.

**Install:** flash it from the browser at <https://philipplinux.github.io/s9-postmarketos/> (Chrome/Edge), or grab the files from the [latest release](https://github.com/philipplinux/s9-postmarketos/releases/latest). Login `user` / `changeme`.

**Discussion:** [XDA thread](https://xdaforums.com/t/rom-linux-pmos4-9-s9-headless-server-w-dashboard-overclock-sm-g960f-exynos-starlte.4805155/) · [r/mobilelinux](https://www.reddit.com/r/mobilelinux/comments/1x6luth/samsung_galaxy_s9_exynos_on_nura_postmarketos/)

[![Buy me a beer?](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer%3F&emoji=%F0%9F%8D%BA&slug=philipplinux&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/philipplinux)

<p align="center"><img src="fastfetch.png" width="600" alt="fastfetch in the on-phone terminal"></p>

<p align="center"><img src="phone-ui-tour.gif" width="240" alt="Boot logo and phone UI tab tour"> <img src="phone-ui-subpages.gif" width="240" alt="Phone UI subpages"></p>

The on-phone touch UI (`display/display.py`): boot logo and tab tour, and every subpage. Frames are rendered with the real UI code on the phone (private values replaced).

![Web dashboard](web-dashboard.png)

- `dashboard/server.py` serves `/` and `/api/status` on `0.0.0.0:8090`. Only write action: same-origin POST to kill the user's own processes.
- Shows battery (max77705), CPU per core + LITTLE/big clocks + busy % (load average is ~13 permanently because of D-state kernel threads), Mali-G72 clock/governor/load, thermal zones (BIG, LITTLE, G3D), memory, storage, Wi-Fi link, Bluetooth, SSH (dropbear), USB, processes.
- Install: `sh deploy.sh [ssh-host]` -> `/opt/s9-dashboard`, OpenRC service `s9-dashboard` (supervise-daemon, runlevel default, log `/var/log/s9-dashboard.log`).
- Self-checks: `python3 dashboard/battery.py`, `services.py`.

LLM: the image ships llama.cpp (`s9-llama-vk`, run with `mali-run`; bring your own model) with our patch to make it somewhat Mali-compatible, build steps and model link in [llm/README.md](../llm/README.md); speed in [docs/llm-bench.md](llm-bench.md).

## Status

Tested on one SM-G960F: a fresh install of the release image via the web flasher, plus the earlier dev install.

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

## Galaxy S9+ (SM-G965F, `star2lte`)

Same image built for the S9+: pick **Galaxy S9+** at the top of the web flasher, or use the `-s9plus` release.
**Untested on hardware**, and no speaker audio (the S9 DSP tuning is not shipped for the S9+). Recipes and
build steps: [s9plus/README.md](../s9plus/README.md).

## Web flasher

[`web-flasher/`](../web-flasher/README.md): WebUSB installer for this phone only (SM-G960F / Exynos). Section 1
installs TWRP over Odin (Download Mode); section 2 writes BOOT/SYSTEM/VENDOR from TWRP over ADB and verifies
them on the phone. Build and run: `cd web-flasher && npm ci --ignore-scripts && npm run check && npm test &&
npm run build`, then serve `dist/` on `127.0.0.1:8085`. Hosted copy: <https://philipplinux.github.io/s9-postmarketos/>
(Chrome/Edge). The download buttons fetch the images from next to `index.html` or else from the
[Hugging Face mirror](https://huggingface.co/PhilippLinux/s9-postmarketos), and select them; or pick them from disk. Either way every file
is SHA-256-checked against `site/s9-manifest.js` before anything is written. Flashing takes several minutes: scroll down to
Activity and wait until the final step (22) says Done; if it stops earlier, reload, reconnect and flash again.
Experimental; tested end to end on one SM-G960F.

## Build the image from scratch

Needs [pmbootstrap](https://docs.postmarketos.org/pmbootstrap/) and a pmaports checkout with the kernel
`device/archived/linux-samsung-starlte` (copy [`kernel/`](../kernel/README.md) there: APKBUILD, config, all 15 patches) and `main/s9-*` (written by the stage scripts).
`<W>` is the pmbootstrap workdir (default `~/projects/oldphones/s9`).

1. `pmbootstrap.cfg` in `<W>` (no `user`, no `ssh_keys`: the image ships `user` / `changeme` and no keys):
   ```ini
   [pmbootstrap]
   aports = <W>/pmaports
   work = <W>/work
   device = samsung-starlte
   extra_packages = s9-server,device-samsung-starlte-nonfree-firmware,s9-llama-vk,s9-audio-fw
   boot_size = 512
   hostname = s9
   service_manager = openrc
   ui = none
   ```
2. Stage the packages: `sh pkg/s9-server/stage.sh <W>`; `sh pkg/s9-mali/stage.sh <W>` (needs the stock
   G960FXXUHFVG4 blobs in `<W>/android-blobs`); `sh pkg/s9-llama-vk/stage.sh <W>` (needs the NDK build, see
   [llm/README.md](../llm/README.md)); `sh pkg/s9-audio-fw/stage.sh <W>` (same stock blobs).
3. Build: `pmbootstrap build linux-samsung-starlte s9-mali s9-llama-vk s9-audio-fw s9-server --force`.
4. Install: `pmbootstrap install --split --password changeme` (the boot partition goes to VENDOR, the rootfs to SYSTEM).
5. Pack: `sh pkg/s9-server/make-zip.sh <W>` → `<W>/out/`: `boot.img`, `samsung-starlte-root.img`,
   `samsung-starlte-boot.img`, `SHA256SUMS`, and the TWRP zips (`s9-server-twrp.zip`; `-srv` also formats USERDATA for `/srv`).
6. Pin the release in the web flasher: `node web-flasher/scripts/fill-s9-manifest.mjs <W>/out <release>`.

Shell: `shell/skel` (bash, starship, ble.sh, fzf) goes to `/etc/skel`, ble.sh to `/usr/share/blesh`; on boot
The `s9-shell-env` boot service gives every login user that setup and bash as login shell (own `~/.bashrc` files are left alone).
Overclock is off until turned on (behind two warnings) on the phone's CPU & GPU limits page.

Not in the image: the LLM model, Wi-Fi credentials and SSH keys.

## Framebuffer touch UI (`display/`)

`display/display.py` draws the status pages (Overview, Processes, Network, Thermals & perf, Power) on `/dev/fb0` (mmap, native 1440x2960 RGBX, ~115 ms/frame) from the dashboard's `/api/status`. Touch: `sec_touchscreen` (ABS 0..4095 scaled to the layout); power key (`gpio_keys`, grabbed so acpid cannot power off) cycles **console → dashboard → off** (a tap while off wakes to the console); volume keys scroll the process list. Reboot/Shut down need a confirm tap.

Deploy: `sh display/deploy.sh [host]` (needs `apk add py3-pillow py3-pyte font-opensans font-droid font-nerd-fonts-symbols`; installs to `/usr/local/lib/s9-display`, service `s9-display`, runs as root, log `/var/log/s9-display.log`; leave `msm-fb-refresher` running). Checks: `python3 display.py --selftest`, `--preview out.png`, `--readback out.png` (reads fb0 back).

**Console (tty1):** the kernel has fbcon (patched decon blit ops); tty1 autologins `user` in Terminus 32 with `buffyboard` as touch keyboard; the boot screen. The dashboard runs on tty7.

**Terminal tab** (6th tab): a pyte terminal backed by `/bin/login -f <user>` (autologin; the root display service starts it). The session survives tab switches and screen blanking, and respawns after logout. Ordinary text uses DroidSansMono; Starship icons use the installed Nerd Font mono symbols. The keyboard is `buffyboard` docked above the tabs (display.py leaves that strip of the framebuffer to it and reads its uinput keys); **Rotate 90°** rotates the output and falls back to the drawn keyboard. **Hide keyboard** gives output more space; either control resizes the active PTY. Drag over output for row-by-row scrollback with a short coast, or use volume keys for paging. Battery & power → Terminal options controls display-only kernel-message filters (Knox, disk probes, panel already-on, network devices, USB addresses, USB driver, Wi-Fi driver/DHCP; all hidden by default); full kernel logs remain available. The battery page also controls the wireless receiver and remembers its setting. Keyboard taps include letters/shift, digits, symbol pages, Esc, Tab, one-shot Ctrl, arrows, Enter and Del. Checks: `display.py --selftest`; `sudo display.py --selftest-terminal out.png` exercises an autologin PTY and framebuffer readback.

**`display/wifi-setup`** (installed to `/usr/local/bin`): `wifi-setup SSID [PASSWORD]` rewrites `/etc/wpa_supplicant/wpa_supplicant.conf` (0600, `wpa_passphrase`, open network without a password), kills wpa_supplicant/udhcpc on wlan0 and runs `/etc/local.d/wifi.start`. It drops the Wi-Fi link, so use it from the on-screen terminal, not over SSH. Test overrides: `WPA_CONF=/tmp/x WIFI_RESTART=0`.

**Partitions subpage**: tap the Storage tile on Overview for one row per partition from `/api/status` `storage.partitions` (mounted first: mountpoint, fstype, used bar; then unmounted ones). Drag or volume keys scroll; `< Back` (or any tab) closes.

**CPU & GPU limits subpage**: on the Thermals tab, `Limits >` opens it (`< Back` closes). `-`/`+` step each cluster's `scaling_min_freq`/`scaling_max_freq` through `scaling_available_frequencies`, and the Mali `dvfs_max_lock`/`dvfs_min_lock` through `dvfs_table` (writing the top clock as max, or the bottom as min, releases the lock; `-1` is rejected). Min follows max (and vice versa) so min <= max. Every write is re-read and the real value shown plus a toast; the big cluster often refuses anything above 1.79 GHz ("asked X, kernel limit"), and the kernel's multi-core boost cap is inconsistent between attempts. `/sys/power/cpufreq_max_limit` does cap real clocks (limit 1000000 -> cpu4 ran at 962 MHz while `scaling_max_freq` still read 1794000) but is only shown as a note, not a control. `Off` (top right) removes every limit so the kernel decides: cpufreq min/max back to the `cpuinfo` range, both Mali locks released, `cpufreq_max_limit` set to `-1` (same as a fresh boot; nothing is written at boot). The big-cluster max then follows the Exynos hotplug governor, which switches idle big cores off to boost busy ones: 2.70 GHz with 1 busy core, 2.31 with 2, 1.79 with 3-4 (DT `hotplug_governor`). `cpufreq_max_limit` reading 1.79 GHz is just the top of Samsung's table, i.e. no cap. Governor switching is hidden (only schedutil). Resets at reboot. Rows are grouped in Little / Big / GPU cards (value, position in the available range, `−`/`+`); Big has a **4-core cap** row (quad_freq, 1.79-2.70 GHz) and a **Throttle at** row (BIG `trip_point_2_temp`, 83-100 °C); both only change while overclock is on, otherwise they show *Needs Overclock*. Throttle at also starts locked: tap the lock, confirm, then it stays editable until 10 s after the last change. Tapping a range bar jumps to that value (a slider). The page refreshes every second. Below them, an opt-in **Overclock** toggle (two red warning pop-ups to turn on, the second with Accept and Cancel swapped so a double tap cancels; none to turn off; kept after reboot), see `power/README.md`. Both `s9-display` and `s9-dashboard` run pinned to the little cores (`taskset 0x0f` in their init scripts).

**Network subpages / Tailscale / charge cap:** each Network-tab row (and the Wi-Fi card) opens a read-only detail page (`< Back` returns): Wi-Fi, SSH, Tailscale (state, tailnet IP, peers), USB (ncm0 + gadget state), Bluetooth, Web dashboard (Audio is an editable page: sound on/off, speaker/headphones, volume, test tone via `/etc/s9/audio`). Tailscale is `apk add tailscale tailscale-openrc` + `rc-update add tailscale default`; log in once with `sudo tailscale up` (the page shows "Logged out" until then). `firewall/rules.v4` accepts `tailscale0`. The Power tab has a **Charge cap (stand mode)** toggle: it writes `/etc/s9/store_mode` (0/1, default on), read at boot by `power/store_mode.start` (install to `/etc/local.d/`). The Samsung driver only accepts `store_mode=1`, so OFF takes effect after the next reboot. **Reboot on freeze** (Power tab, **untested**: no real lockup triggered yet) toggles `kernel.softlockup_panic` + `kernel.panic_on_rcu_stall` and `kernel.panic=10` (off: stock 0 / 120), persisted in `/etc/sysctl.d/90-s9-panic.conf`. Hard lockups and oopses always panic on this kernel; `sec_debug.level=0` makes a panic reboot instead of entering upload mode.

**Boot logo (`bootlogo/`):** no plymouth, and the fbcon console only appears once tty1 is up, so the panel stayed black from the Samsung logo until then. The sysinit service `s9-bootlogo` (`python3 -S bootlogo/show.py`, stdlib only, ~20 ms) mmaps `/dev/fb0` (write() to it returns 0), copies the pre-rendered postmarketOS emblem + wordmark band (`logo.raw.gz`, RGBX, black background so only the centre band is written) and pans; `s9-display` then repaints over it. Rebuild the asset with `python3 bootlogo/make-logo.py` (ImageMagick + Pillow; emblem from postmarketos-artwork). Install: `sh bootlogo/deploy.sh`. The kernel/initramfs stage before sysinit stays black (boot partition is full and untouched).

**Docker tab** (7th tab, "Dock"): list of containers (name, state, image/status, "N / M running") -> tap a row for a detail page (state, health, uptime, image, restarts, CPU/memory bars, last log lines, refreshed every 5 s). Buttons depend on state: running = Pause + Stop, paused = Unpause, exited = Start; holding anywhere on the detail page restarts it (`docker restart -t 10`, run as root by the UI). The other actions POST to the dashboard's `/api/docker/<action>` (with `Origin`), which enforces the states. The result shows as a toast. Overview shows four chips (SSH, Tail, USB, BT) and a Docker row ("N/M running", green, amber if one is stopped, red if unhealthy; tap = Dock tab); Web stays on the Network tab.

**Terminal keyboard latency:** keys fire on touch-down (the tab bar still acts on release), the pressed key is highlighted, and `main()` waits up to 20 ms for the shell echo so one frame shows highlight + character. `Display.show()` now pushes only the changed 185 px bands (diff against the previous frame; readback is bit-identical to a full render) and the keyboard strip is cached per (mode, ctrl, pressed). Measured on the phone (cold CPU, service stopped, key tap to char drawn): before 397 ms after release, after about 150 ms after touch-down. **Touch boost:** every touch-down sets the little cluster's `scaling_min_freq` to its max for 1 s, then restores the previous min (on page change and exit too; never on the Limits page). Opt out with the **Touch boost** switch on the Battery page (`/etc/s9/touch-boost`, `0` = off). The UI stays pinned to the small cores on purpose, so only they are boosted. A tapped key stays highlighted for at least 100 ms (`PRESS_S`): a quick tap's touch-down and lift arrive in one event batch, which used to clear the highlight before any frame drew it. **Edge keys:** at startup display.py sends `dead_zone_enable,0` to the touch IC (`/sys/class/sec/tsp/cmd`), so Samsung's grip rejection no longer swallows touches on the curved edges, and a touch in a row's side gap (the centred 9-key row) or past the last key hits that row's nearest key (`kb_key_at`). Typing with 0.3 s gaps otherwise finds the little cores back at 455 MHz: frame 140 ms at 455 MHz, 95 ms at 1.25 GHz, 70 ms at 1.79 GHz (render + show, measured with `taskset 0x0f`).

## Wi-Fi and SSH pages (Network tab)

Wi-Fi: scan and join (wpa_cli), saved networks and forget, IP and DNS settings (DHCP or static). Static/DNS settings live in `/etc/s9/wifi-ip`
and are applied by `wifi-ip apply` (called from `/etc/local.d/wifi.start`, copy in `display/wifi.start`), so they survive reboots.
A change that leaves the gateway unreachable for 60 s is reverted automatically. WPA3-only (SAE) networks are not supported.
SSH & User: dropbear on/off (OpenRC, persistent), password change for the login user (uid 10000, never hardcoded) via `chpasswd`, and a username rename (on-screen keyboard, `^[a-z_][a-z0-9_-]{0,31}$`, refuses existing names, confirm step) via `display/user-apply OLD NEW` (busybox has no usermod): edits passwd/shadow/group/gshadow incl. member lists, moves the home, `/etc/*.s9bak` backups first.

USB (Network tab row): mode in `/etc/s9/usb` (`MODE=network|rndis|charge`), applied at boot by `usb-net apply` (from `usb-net.start`). Network = NCM + serial on 172.16.43.1 (default), Windows network = RNDIS + serial (same IP/DHCP; verified on one Windows 11 host and tried with a Linux host), Charge only = gadget off (charging is independent of the gadget). Network <-> Charge switch live (the ncm link is kept; re-linking ncm after it was bound panics the kernel); anything involving the other function type shows "Applies after reboot" + Reboot button (`usb-net apply` exits 3). The stock initramfs gadget already links rndis, so `usb-net` tracks what it bound in `/run/usb-net.built`. `firewall/rules.v4` accepts `rndis0`. Replugging the cable can jam the stock NCM tx path (NET_TX softirq storm, ksoftirqd pins CPU0); `usb-net guard` watches NET_TX in `/proc/stat`, re-binds the gadget (UDC) on a storm and takes USB down if it recurs within 60 s (`logger -t usb-net`). ADB (`adbd`) and MTP (`umtprd`) exist in the repos but are not implemented (need functionfs plumbing, untested against the panic).

## Bluetooth and Web dashboard pages (Network tab)

Bluetooth: power, visibility (default off, also after reboot unless turned on) and pairing name, stored in `/etc/s9/bluetooth` and applied by `bt-apply` (boot: `display/bluetooth.start` in `/etc/local.d/`).
Web dashboard: reachability per path (Wi-Fi, Tailscale, USB) in `/etc/s9/web`, applied by `web-apply` as DROP rules for port 8090 in an `S9WEB` chain (called from `firewall/firewall.start`). A master `WEB=on|off` toggle at the top (off = `! -i lo` DROP, unreachable from every interface, `ssh -L` tunnels still work; per-interface rows grey out; the Network row shows Off). All off = SSH only (`ssh -L 8090:127.0.0.1:8090`). The dashboard service itself keeps running because the phone UI reads its API on 127.0.0.1. Port 22 is never touched. `display/deploy.sh` installs all helpers.

## Release goal

The deliverable is a shareable S9 ROM. A fresh installation of the release image
through the web flasher has been validated on one SM-G960F. Broader hardware
acceptance remains open. Release work also covers reproducible build inputs,
clean defaults and first-boot setup, credential/identity and firmware redistribution
review, and flashing/recovery instructions. Track release status in the Hardware
Vault tasks linked below.

Current development handoff: [2026-10-15](2026-10-15-handoff.md).

---

If this saves you time: [![Buy me a beer?](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer%3F&emoji=%F0%9F%8D%BA&slug=philipplinux&button_colour=FFDD00&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/philipplinux)
