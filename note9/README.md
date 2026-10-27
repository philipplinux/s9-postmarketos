# Galaxy Note9 (SM-N960F/N, `crownlte`)

**Untested on hardware.** Same image as the S9: same kernel source, all 15 patches, same `s9-server`.

- `kernel/`: `linux-samsung-crownlte`, the S9 recipe with the crownlte defconfig (y771 touch, Wacom W9018 S Pen,
  Note9 camera/sensor options), the `*crownlte*.dtb` and patch 16 (sec_ts ABS_MT range, from pmaports). Patches
  01-15 link to `../../kernel/`.
- `device/`: `device-samsung-crownlte`, our S9 device package (hciattach, gzip initfs, rndis) with Note9 names.
- Screen: same 1440x2960 panel mode as the S9; `display.py` scales to the framebuffer, so no UI change.
- Not included: `s9-audio-fw` (S9 DSM speaker tuning), so no speaker audio.

## Build

Like the S9+ (`s9plus/README.md`), with `crownlte` names, `hostname = note9` and
`extra_packages = s9-server,s9-llama-vk,device-samsung-crownlte-nonfree-firmware,device-samsung-crownlte-nonfree-firmware-openrc`.
Config used: `~/projects/oldphones/s9/pmbootstrap-crownlte.cfg`.

TWRP: `twrp-3.7.0_9-0-crownlte.img` from <https://dl.twrp.me/crownlte/>, SHA-256
`7ea8960e5c8df86f07c6d6b3f5b4ab6fa1c533cafd46cd4e2578d32867f154af`.

Release v2026.10.27-note9: <https://huggingface.co/PhilippLinux/s9-postmarketos/tree/main/v2026.10.27-note9>
