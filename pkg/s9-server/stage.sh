#!/bin/sh
# Assemble the s9-server package from the repo components and hand it to pmbootstrap.
# Usage: sh pkg/s9-server/stage.sh [pmbootstrap-workdir]   (default ~/projects/oldphones/s9)
# Re-run after changing dashboard/ display/ etc., then `pmbootstrap build s9-server --force` and reinstall.
set -eu
R=$(cd "$(dirname "$0")/../.." && pwd)
P=$R/pkg/s9-server
W=${1:-$HOME/projects/oldphones/s9}
A=$W/pmaports/main/s9-server
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
D=$T/root

i() { m=$1; src=$2; dst=$3; install -Dm"$m" "$R/$src" "$D$dst"; }
for f in server.py services.py battery.py gpu.py; do i 644 dashboard/$f /opt/s9-dashboard/$f; done
i 644 dashboard/index.html /opt/s9-dashboard/index.html
i 755 dashboard/s9-dashboard.initd /etc/init.d/s9-dashboard
i 755 display/display.py /usr/local/lib/s9-display/display.py
i 755 display/s9-display.initd /etc/init.d/s9-display
# mirrors display/deploy.sh
for f in wifi-setup wifi-ip bt-apply web-apply user-apply; do i 755 display/$f /usr/local/bin/$f; done
i 755 display/bluetooth.start /etc/local.d/bluetooth.start
i 755 wifi/wifi.start /etc/local.d/wifi.start
i 755 wifi/dhd-iovar.py /usr/local/bin/dhd-iovar
i 755 ttyd/s9-ttyd /etc/init.d/s9-ttyd
i 644 bootlogo/show.py /usr/local/lib/s9-bootlogo/show.py
i 644 bootlogo/logo.raw.gz /usr/local/lib/s9-bootlogo/logo.raw.gz
i 755 bootlogo/s9-bootlogo.initd /etc/init.d/s9-bootlogo
i 755 docker/schedtune-off /etc/init.d/schedtune-off
i 755 docker/s9-srv.initd /etc/init.d/s9-srv
i 440 tailscale/sudoers /etc/sudoers.d/s9-tailscale
i 755 wireguard/s9-wireguard /usr/local/sbin/s9-wireguard
i 440 wireguard/sudoers /etc/sudoers.d/s9-wireguard
i 644 wireguard/config.example /usr/share/s9-server/wireguard.conf.example
i 644 docker/daemon.json /etc/docker/daemon.json
i 755 usbnet/usb-net /usr/local/sbin/usb-net
i 755 usbnet/usb-net.start /etc/local.d/usb-net.start
i 755 firewall/firewall.start /etc/local.d/firewall.start
i 755 power/store_mode.start /etc/local.d/store_mode.start
i 755 power/wireless-charging.start /etc/local.d/wireless-charging.start
i 755 power/irq-affinity.start /etc/local.d/irq-affinity.start
i 755 power/overclock.start /etc/local.d/overclock.start
i 755 power/usb-input.start /etc/local.d/usb-input.start
i 755 audio/audio.start /etc/local.d/audio.start
i 755 audio/s9-audio /usr/local/bin/s9-audio
i 755 audio/s9-chime /usr/local/bin/s9-chime
i 644 audio/mixer-path.py /usr/local/lib/s9-audio/mixer-path.py
i 644 thermal-guard/thermal-guard.py /usr/local/lib/thermal-guard/thermal-guard.py
i 755 thermal-guard/thermal-guard.initd /etc/init.d/thermal-guard
i 755 ttyd/prepare-index.py /usr/local/libexec/s9-ttyd-prepare-index
i 644 firewall/rules.v6 /etc/s9-firewall/rules.v6
# shell: bash + starship + ble.sh + fzf for every login user (s9-shell-env, boot runlevel)
i 755 shell/s9-shell-env.initd /etc/init.d/s9-shell-env
(cd "$R/shell/skel" && find . -type f) | while read -r f; do i 644 "shell/skel/$f" "/etc/skel/$f"; done
mkdir -p "$D/usr/share" && tar -C "$D/usr/share" -xJf "$R/shell/blesh.tar.xz"
# generic (shareable) rules; firewall/rules.v4 is the owner's 10.0.0.0/24 variant
i 644 pkg/s9-server/files/rules.v4 /etc/s9-firewall/rules.v4
i 644 pkg/s9-server/files/store_mode /etc/s9/store_mode
i 755 pkg/s9-server/files/s9-autologin /usr/local/bin/s9-autologin
i 644 pkg/s9-server/files/61-s9-touch.rules /etc/udev/rules.d/61-s9-touch.rules
# copied over files owned by other packages by the post-install script
i 644 docker/conf.d-docker /usr/share/s9-server/conf.d-docker
i 644 tailscale/conf.d-tailscale /usr/share/s9-server/conf.d-tailscale
i 644 pkg/s9-server/files/interfaces /usr/share/s9-server/interfaces
i 600 pkg/s9-server/files/wpa_supplicant.conf /usr/share/s9-server/wpa_supplicant.conf
mkdir -p "$D/usr/local/libexec/iptables-legacy"
for b in iptables iptables-save iptables-restore ip6tables ip6tables-save ip6tables-restore; do
	case $b in
	*-save) t=${b%-save}-legacy-save ;;
	*-restore) t=${b%-restore}-legacy-restore ;;
	*) t=$b-legacy ;;
	esac
	ln -s /sbin/$t "$D/usr/local/libexec/iptables-legacy/$b"
done

mkdir -p "$A"
tar -C "$T" --owner=0 --group=0 --numeric-owner -czf "$A/s9-server-root.tar.gz" root
cp "$P/APKBUILD" "$P/s9-server.post-install" "$P/s9-server.post-upgrade" "$A/"
cd "$W" && pmbootstrap -c pmbootstrap.cfg checksum s9-server
