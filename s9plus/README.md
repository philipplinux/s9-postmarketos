# Galaxy S9+ (SM-G965F, `star2lte`)

**Tested working on an S9+** (owner, 2026-10-27). Same image as the S9: same kernel source, all 15 patches, same `s9-server`.

- `kernel/`: `linux-samsung-star2lte`, the S9 recipe with the S9+ defconfig camera options (`CAMERA_STAR2`,
  `CIS_3M3_OBJ`, `OIS_USE_RUMBA_S6`) and the `*star2lte*.dtb`. Patches link to `../../kernel/`.
- `device/`: `device-samsung-star2lte`, our S9 device package (hciattach, gzip initfs, rndis) with S9+ names.
- Not included: `s9-audio-fw`. Its DSM speaker tuning comes from S9 (G960F) stock, so no speaker audio on the S9+
  until it is staged from G965F stock blobs.

## Build

Like the S9 ("Build the image from scratch" in the main README), with:

1. Copy `kernel/` (`cp -L`) to `pmaports/device/archived/linux-samsung-star2lte/` and `device/` to
   `pmaports/device/archived/device-samsung-star2lte/`.
2. In `pmbootstrap.cfg`: `device = samsung-star2lte`, `hostname = s9plus`,
   `extra_packages = s9-server,s9-llama-vk,device-samsung-star2lte-nonfree-firmware,device-samsung-star2lte-nonfree-firmware-openrc`.
3. `pmbootstrap chroot -- apk add binutils-aarch64`, then
   `pmbootstrap build linux-samsung-star2lte device-samsung-star2lte s9-server --force --lax`.
4. `pmbootstrap install --split --password changeme`, then `DEV=samsung-star2lte sh pkg/s9-server/make-zip.sh <W>`.
5. `node web-flasher/scripts/fill-s9-manifest.mjs <out> star2lte <release>`.

TWRP: `twrp-3.7.0_9-0-star2lte.img` from <https://dl.twrp.me/star2lte/>, SHA-256
`f674dab0134f3c929982077b6a3a8de7df209a45c76ead80bc009381bcd835e1`.
