#!/bin/sh
# Install, upgrade or remove hlidskjalf as systemd services. Run as root from a
# checkout; running it again upgrades in place and keeps your configuration.
set -eu

usage() {
    cat <<'EOF'
Usage: sudo deploy/install.sh [options]

  (no options)   dashboard + privileged helper + RAPL udev rule
  --no-helper    no root collector: no containers, SMART, per-client GPU usage
                 or memory modules in the hardware report
  --no-rapl      no power readings (RAPL counters stay root-only)
  --no-start     install and enable, but do not start or restart anything
  --uninstall    remove code and units; keep config, state and the user
  --purge        with --uninstall: also remove /etc/hlidskjalf,
                 /var/lib/hlidskjalf and the hlidskjalf user
  -h, --help     this text

Re-running with --no-helper or --no-rapl removes what an earlier run installed
for that feature.
EOF
}

PREFIX=/usr/local/lib/hlidskjalf
UNITS=/etc/systemd/system
ETC=/etc/hlidskjalf
RAPL_RULE=/etc/udev/rules.d/90-hlidskjalf-rapl.rules
SYSUSERS=/etc/sysusers.d/hlidskjalf.conf
HELPER=1
RAPL=1
START=1
UNINSTALL=0
PURGE=0
for a in "$@"; do
    case "$a" in
        --no-helper) HELPER=0 ;;
        --no-rapl) RAPL=0 ;;
        --no-start) START=0 ;;
        --uninstall) UNINSTALL=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $a" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo deploy/install.sh)"
[ "$PURGE" -eq 0 ] || [ "$UNINSTALL" -eq 1 ] || die "--purge only works together with --uninstall"
if ! have systemctl || [ ! -d /run/systemd/system ]; then
    die "systemd is required and must be running"
fi
cd "$(dirname "$0")/.."

remove_helper() {
    systemctl disable --now hlidskjalf-helper.timer 2>/dev/null || true
    systemctl stop hlidskjalf-helper.service 2>/dev/null || true
    rm -f "$UNITS/hlidskjalf-helper.service" "$UNITS/hlidskjalf-helper.timer"
    rm -rf /run/hlidskjalf-helper
}

remove_rapl() {
    [ -e "$RAPL_RULE" ] || return 0
    rm -f "$RAPL_RULE"
    # Put the counters back to root-only now; they would stay group-readable until reboot.
    if have udevadm; then
        udevadm control --reload || true
    fi
    for f in /sys/class/powercap/*/energy_uj; do
        [ -e "$f" ] || continue
        chgrp root "$f" 2>/dev/null || true
        chmod 0400 "$f" 2>/dev/null || true
    done
}

if [ "$UNINSTALL" -eq 1 ]; then
    systemctl disable --now hlidskjalf.service 2>/dev/null || true
    remove_helper
    remove_rapl
    rm -f "$UNITS/hlidskjalf.service"
    rm -rf "$PREFIX"
    systemctl daemon-reload
    if [ "$PURGE" -eq 1 ]; then
        rm -rf "$ETC" /var/lib/hlidskjalf "$SYSUSERS"
        if id hlidskjalf >/dev/null 2>&1; then
            userdel hlidskjalf 2>/dev/null || warn "could not remove user hlidskjalf"
        fi
        echo "hlidskjalf removed, including configuration, state and the hlidskjalf user."
    else
        echo "hlidskjalf removed. Kept: $ETC, /var/lib/hlidskjalf, user hlidskjalf, $SYSUSERS"
        echo "Run again with --uninstall --purge to remove those too."
    fi
    exit 0
fi

# ---- checks before anything is changed ----
for f in hlidskjalf/__main__.py hlidskjalf/static/index.html deploy/hlidskjalf.service \
         deploy/hlidskjalf-helper.service deploy/hlidskjalf-helper.timer deploy/hlidskjalf.sysusers \
         deploy/hlidskjalf.env deploy/90-hlidskjalf-rapl.rules; do
    [ -f "$f" ] || die "$f is missing: run this from a complete checkout"
done
[ -x /usr/bin/python3 ] || die "/usr/bin/python3 not found (the units call it directly)"
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || die "python3 3.9 or newer is required, found $(/usr/bin/python3 -V 2>&1)"
have systemd-sysusers || die "systemd-sysusers not found"
VERSION=$(/usr/bin/python3 -sB -c 'import hlidskjalf; print(hlidskjalf.__version__)') \
    || die "the hlidskjalf package in this checkout does not import"

have sadf || warn "sadf not found: no 1h/24h/7d history (install sysstat, see README)"
have ip || warn "ip not found: no interface addresses (install iproute)"
have virsh || warn "virsh not found: the VMs tab stays empty (install libvirt-client)"
if [ "$HELPER" -eq 1 ]; then
    have smartctl || warn "smartctl not found: no SMART data (install smartmontools)"
    systemctl is-active --quiet podman.socket 2>/dev/null \
        || warn "podman.socket is not active: the Containers tab stays empty (systemctl enable --now podman.socket)"
fi

# ---- code: staged next to the target, then swapped in, so a failed copy never
# leaves a half-written tree behind. Root-owned, read-only for the service user.
install -d -m 0755 "$PREFIX"
STAGE="$PREFIX/.hlidskjalf.new.$$"
trap 'rm -rf "$STAGE" "$PREFIX/.hlidskjalf.old.$$"' EXIT
rm -rf "$STAGE"
cp -R hlidskjalf "$STAGE"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$STAGE"
chmod -R u=rwX,go=rX "$STAGE"
if [ -e "$PREFIX/hlidskjalf" ]; then
    mv "$PREFIX/hlidskjalf" "$PREFIX/.hlidskjalf.old.$$"
fi
if ! mv "$STAGE" "$PREFIX/hlidskjalf"; then
    [ ! -e "$PREFIX/.hlidskjalf.old.$$" ] || mv "$PREFIX/.hlidskjalf.old.$$" "$PREFIX/hlidskjalf"
    die "could not install the code into $PREFIX; the previous version was put back"
fi
rm -rf "$PREFIX/.hlidskjalf.old.$$"

# ---- user, configuration, units ----
install -D -m 0644 deploy/hlidskjalf.sysusers "$SYSUSERS"
systemd-sysusers "$SYSUSERS"
id hlidskjalf >/dev/null 2>&1 || die "user hlidskjalf was not created"

NEW_ENV=0
if [ ! -e "$ETC/hlidskjalf.env" ]; then
    install -D -m 0644 deploy/hlidskjalf.env "$ETC/hlidskjalf.env"
    NEW_ENV=1
fi

install -m 0644 deploy/hlidskjalf.service "$UNITS/"
if [ "$HELPER" -eq 1 ]; then
    install -m 0644 deploy/hlidskjalf-helper.service deploy/hlidskjalf-helper.timer "$UNITS/"
else
    remove_helper
fi
if [ "$RAPL" -eq 1 ]; then
    install -D -m 0644 deploy/90-hlidskjalf-rapl.rules "$RAPL_RULE"
    if have udevadm; then
        udevadm control --reload || warn "udevadm control --reload failed"
        udevadm trigger --subsystem-match=powercap --action=add || warn "could not apply the RAPL rule now; it applies after a reboot"
    fi
else
    remove_rapl
fi
# Files copied from a home directory can carry the wrong SELinux label.
if have restorecon; then
    restorecon -RF "$PREFIX" "$ETC" "$UNITS/hlidskjalf.service" 2>/dev/null || true
    [ "$HELPER" -eq 0 ] || restorecon -F "$UNITS/hlidskjalf-helper.service" "$UNITS/hlidskjalf-helper.timer" 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable hlidskjalf.service >/dev/null
[ "$HELPER" -eq 0 ] || systemctl enable hlidskjalf-helper.timer >/dev/null

if [ "$START" -eq 0 ]; then
    echo "hlidskjalf $VERSION installed and enabled, not started (--no-start)."
    echo "Start it with: systemctl start hlidskjalf-helper.timer hlidskjalf.service"
    exit 0
fi

if [ "$HELPER" -eq 1 ]; then
    systemctl start hlidskjalf-helper.timer
    # First collection now, so the dashboard has container and SMART data at once.
    systemctl start hlidskjalf-helper.service \
        || warn "the helper's first run failed: journalctl -u hlidskjalf-helper.service"
fi
systemctl restart hlidskjalf.service

# ---- verify: the service must stay up and answer on its port ----
PORT=$(sed -n 's/^[[:space:]]*HLID_PORT=\([0-9][0-9]*\).*/\1/p' "$ETC/hlidskjalf.env" | tail -n 1)
BIND=$(sed -n 's/^[[:space:]]*HLID_BIND=\([^[:space:]#]*\).*/\1/p' "$ETC/hlidskjalf.env" | tail -n 1)
PORT=${PORT:-8899}
case "${BIND:-0.0.0.0}" in
    0.0.0.0|"") PROBE=127.0.0.1 ;;
    ::) PROBE='[::1]' ;;
    *:*) PROBE="[$BIND]" ;;
    *) PROBE=$BIND ;;
esac
ok=0
i=0
while [ "$i" -lt 15 ]; do
    systemctl is-active --quiet hlidskjalf.service || break
    if /usr/bin/python3 -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2).read()' \
            "http://$PROBE:$PORT/api/health" 2>/dev/null; then
        ok=1
        break
    fi
    i=$((i + 1))
    sleep 1
done
if [ "$ok" -ne 1 ]; then
    systemctl --no-pager --lines=15 status hlidskjalf.service >&2 || true
    die "hlidskjalf.service did not come up on $PROBE:$PORT: journalctl -u hlidskjalf.service"
fi

echo "hlidskjalf $VERSION is running: http://$PROBE:$PORT/"
echo "Configuration: $ETC/hlidskjalf.env (restart hlidskjalf.service after editing)"
[ "$NEW_ENV" -eq 0 ] || echo "First install: set the bind address, UPS and host switcher there."
if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1 \
        && ! firewall-cmd --quiet --query-port="$PORT/tcp" 2>/dev/null; then
    echo "firewalld does not allow port $PORT yet:"
    echo "  firewall-cmd --permanent --add-port=$PORT/tcp && firewall-cmd --reload"
fi
