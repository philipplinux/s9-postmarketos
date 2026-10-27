// Galaxy S9 (SM-G960F, starlte), S9+ (SM-G965F, star2lte) and Note9 (SM-N960F/N, crownlte), Exynos. One pin block per model: fill it with
// `node scripts/fill-s9-manifest.mjs <out-dir> <model> [release]` once the image build exists.
// Entries with size 0 or an empty sha256 make Section 2 refuse to run (fail closed).
export const models = {
  starlte: {
    label: 'Galaxy S9 · SM-G960F',
    device: 'starlte',
    model: 'Samsung Galaxy S9 SM-G960F (Exynos)',
    // Section 1: TWRP 3.7.0 for starlte, from ~/projects/oldphones/s9/twrp/twrp.img.sha256
    twrp: {
      name: 'twrp-3.7.0_9-0-starlte.img',
      partition: 'RECOVERY',
      size: 42665984,
      sha256: '905f81903849a5b981fe8915c91d2ba025358f79324e2427c2b704a4f9e46a4e'
    },
    // Section 2: written from TWRP over ADB. `device` paths are the only write targets.
    release: 'v2026.10.16-s9',
    // Shown under the build number in Section 2; update with each release.
    changes: 'Wi-Fi works on every S9/S9+ Wi-Fi module (fixes "failed to power up wifi chip"). Flash button now says which step is missing.',
    // CORS-enabled mirror, one folder per release: <mirror><release>/<name>. Tried when the file is not next to the page.
    mirror: 'https://huggingface.co/PhilippLinux/s9-postmarketos/resolve/main/',
    byName: '/dev/block/platform/11120000.ufs/by-name',
    images: [
      { name: 'samsung-starlte-root.img', partition: 'SYSTEM', size: 1322254336, sha256: '94e7a21f8043115971d28b62bc91b9769ff435e93aa860631936c560d6e70da2' },
      // full-size pmOS boot partition image (512 MiB; the shrunk one was only a heimdall workaround)
      { name: 'samsung-starlte-boot.img', partition: 'VENDOR', size: 536870912, sha256: 'c27db3f87a7b83ade6dc372d51030ef242a3bffd28205a949fbe92bc073bd50d' },
      { name: 'boot.img', partition: 'BOOT', size: 38752256, sha256: 'cded0cee6c8ea60c3260e18c39a6fa9799bad5610a7509b0e83e464897d329f3' }
    ],
    userdata: { partition: 'USERDATA', label: 's9-srv' }
  },
  // Owner-tested on an S9+ (2026-10-27): same build as the S9 with the star2lte kernel config/DTB and TWRP.
  star2lte: {
    label: 'Galaxy S9+ · SM-G965F',
    device: 'star2lte',
    model: 'Samsung Galaxy S9+ SM-G965F (Exynos)',
    twrp: {
      name: 'twrp-3.7.0_9-0-star2lte.img',
      partition: 'RECOVERY',
      size: 42670080,
      sha256: 'f674dab0134f3c929982077b6a3a8de7df209a45c76ead80bc009381bcd835e1'
    },
    release: 'v2026.10.16-s9plus',
    // Shown under the build number in Section 2; update with each release.
    changes: 'Wi-Fi works on every S9/S9+ Wi-Fi module (fixes "failed to power up wifi chip"). Flash button now says which step is missing.',
    mirror: 'https://huggingface.co/PhilippLinux/s9-postmarketos/resolve/main/',
    byName: '/dev/block/platform/11120000.ufs/by-name',
    images: [
      { name: 'samsung-star2lte-root.img', partition: 'SYSTEM', size: 1319108608, sha256: 'efbf3b0710bd9eab12bcc8336fcf9918df8cc17926bcb570443e5d6320ff65ce' },
      { name: 'samsung-star2lte-boot.img', partition: 'VENDOR', size: 536870912, sha256: 'a9e77d80928f69b21fa97719a5031050a712e796bb31f8276fed30b53a8638b0' },
      { name: 'boot.img', partition: 'BOOT', size: 38756352, sha256: 'e5d45313ef8ceb966c7175cdcaddcb1e9ff40057db4c29ce3d9c98890bceda0f' }
    ],
    userdata: { partition: 'USERDATA', label: 's9-srv' }
  },
  // Untested on hardware: S9 build with the crownlte kernel config/DTB and TWRP; same SoC, so same UFS by-name path.
  crownlte: {
    label: 'Galaxy Note9 · SM-N960F/N (untested)',
    device: 'crownlte',
    model: 'Samsung Galaxy Note9 SM-N960F/N (Exynos)',
    twrp: {
      name: 'twrp-3.7.0_9-0-crownlte.img',
      partition: 'RECOVERY',
      size: 42801152,
      sha256: '7ea8960e5c8df86f07c6d6b3f5b4ab6fa1c533cafd46cd4e2578d32867f154af'
    },
    release: 'v2026.10.27-note9',
    // Shown under the build number in Section 2; update with each release.
    changes: 'First Note9 build: S9 image with the Note9 kernel (touch, S Pen), all Wi-Fi modules and USB networking. No speaker audio.',
    mirror: 'https://huggingface.co/PhilippLinux/s9-postmarketos/resolve/main/',
    byName: '/dev/block/platform/11120000.ufs/by-name',
    images: [
      { name: 'samsung-crownlte-root.img', partition: 'SYSTEM', size: 1321205760, sha256: 'c0153a2970a5f10f98ec73b4c0a89407148f896e7ff8ae103b862a60e435e2c8' },
      { name: 'samsung-crownlte-boot.img', partition: 'VENDOR', size: 536870912, sha256: '210c0d0a571b54ee5f9c485d4179b7d9c46b6e6fc4e3cee830e4956243fd2e3a' },
      { name: 'boot.img', partition: 'BOOT', size: 39299072, sha256: 'dc574b0576d6b47c930e22cf519834c60e4c9a829245a553f69adc867c33b7e2' }
    ],
    userdata: { partition: 'USERDATA', label: 's9-srv' }
  }
};

/** The active model; every check reads it at call time. */
export const s9 = { ...models.starlte };

/** @param {string|null} key unknown keys keep the S9 */
export function useModel(key) {
  if (key && Object.hasOwn(models, key)) Object.assign(s9, models[key]);
  return s9;
}
