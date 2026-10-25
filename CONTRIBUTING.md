<p align="center">
  <img src="bootlogo/postmarketos-logo.svg" width="104" height="104" alt="postmarketOS emblem">
</p>

<h1 align="center">Contributing</h1>
<p align="center"><code>s9-postmarketos</code></p>
<p align="center"><strong>Help improve the Galaxy S9 Linux Server.</strong><br>Documentation · bug reports · focused improvements</p>

<p align="center">
  <a href="README.md">Project overview</a> · <a href="#keep-changes-focused">Report a problem</a> · <a href="#find-the-component">Components</a> · <a href="#verify-the-changed-surface">Checks</a> · <a href="#submit-a-pull-request">Pull requests</a>
</p>

---

<a id="keep-changes-focused"></a>

## 🎯 Keep changes focused

Report problems in the [XDA thread](https://xdaforums.com/t/rom-linux-pmos4-9-s9-headless-server-w-dashboard-overclock-sm-g960f-exynos-starlte.4805155/) or [Reddit discussion](https://www.reddit.com/r/mobilelinux/comments/1x6luth/samsung_galaxy_s9_exynos_on_nura_postmarketos/). Keep each report or pull request focused on one problem. Small documentation fixes can be proposed directly. For a broad redesign or behavior change, describe the problem and proposed scope first. Check existing reports before adding a duplicate.

A useful bug report includes:

- Device model, OS/image version and source revision when known.
- A small reproduction with the exact command or UI sequence.
- Expected behavior, observed behavior and relevant error output.
- Whether the result came from static inspection, an offline test, a rendered UI, or the real phone.

Do not include passwords, tokens, private keys, VPN profiles, personal hostnames, MAC addresses, ARP tables or full network exports. Redact screenshots and use documentation addresses such as `192.0.2.20` for examples. Keep enough context for the failure to be reproducible.

<a id="find-the-component"></a>

## 🧭 Find the component

| Path | Area |
| --- | --- |
| `dashboard/` | Python metrics collectors and HTTP dashboard |
| `display/` | Framebuffer touch UI and system-setting helpers |
| `pkg/` | Packaging and image staging |
| `kernel/` | Vendor-kernel configuration and patches |
| `web-flasher/` | Browser installer |
| `power/`, `usbnet/`, `wifi/`, `firewall/` | Device and network behavior |

Read the relevant component's instructions before changing it. The [technical reference](docs/technical-reference.md) describes deployment and image builds; it also records known hardware limitations. This guide does not require a device deployment for a documentation-only change.

<a id="prepare-a-development-environment"></a>

## 🛠️ Prepare a development environment

Work on a branch from the current project baseline. Use Python 3 for Python components and install only the dependencies required by the component under test. The display uses Pillow; terminal checks also use pyte and the documented fonts. Phone-specific services depend on Linux/OpenRC and device interfaces that are absent from a typical desktop.

Do not run deployment helpers or system-setting actions merely to check syntax. Commands that write partitions, restart network services or change VPN routes need an appropriate device and recovery path. State explicitly when a required runtime or device is unavailable.

<a id="verify-the-changed-surface"></a>

## 🧪 Verify the changed surface

Run checks from the repository root unless stated otherwise. Report the exact command and result. A passing mock test or UI render is not evidence of working hardware or VPN traffic.

### 📝 Documentation

Check relative links, section anchors, image paths, command accuracy and consistency with the source. Run:

```sh
git diff --check
```

### 🐍 Existing Python and touch UI behavior

Use the relevant component's existing tests and documented self-checks. Documented component checks:

```sh
python3 dashboard/battery.py
python3 dashboard/services.py
python3 display/display.py --selftest
```

Collectors may depend on Linux/device state. The display selftest requires its documented fonts and dependencies. Compare a failure with the unchanged baseline before attributing it to your change. Record unrun or environment-dependent checks instead of changing expected results to conceal a failure.

### 🌐 Web flasher

Use the existing project commands from `web-flasher/`:

```sh
npm ci --ignore-scripts
npm run check
npm test
npm run build
```

These validate the web project; they do not establish a successful phone flash. Flashing and image acceptance require separate hardware evidence.

<a id="submit-a-pull-request"></a>

## 📦 Submit a pull request

Describe the problem, resulting behavior and why the change addresses it. Include a small before/after example or synthetic screenshot when useful, the checks you ran, any baseline failures, and the checks you could not run. Link the relevant XDA or Reddit discussion when one exists.

Keep generated output and private device data out of the change unless the component specifically requires a reviewed public artifact. Follow neighboring code conventions. Include a clear description and keep the change focused.

See [licensing](LICENSE.md) for the proposed terms and scope. Keep existing third-party notices and licences intact.
