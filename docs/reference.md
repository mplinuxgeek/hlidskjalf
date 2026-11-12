# hlidskjalf reference

Detail that does not fit the [README](../README.md).

- [History: sysstat at 1-minute resolution](#history-sysstat-at-1-minute-resolution)
- [Configuration](#configuration)
- [What it shows](#what-it-shows)
- [Security model](#security-model)
- [Development](#development)
- [API](#api)

## History: sysstat at 1-minute resolution

```sh
sudo dnf install -y sysstat
sudo cp -a /etc/sysconfig/sysstat /etc/sysconfig/sysstat.bak-hlidskjalf
sudo sed -i -e 's/^SADC_OPTIONS=.*/SADC_OPTIONS="-S XALL"/' \
            -e 's/^HISTORY=.*/HISTORY=28/' \
            -e 's/^COMPRESSAFTER=.*/COMPRESSAFTER=10/' /etc/sysconfig/sysstat
sudo mkdir -p /etc/systemd/system/sysstat-collect.timer.d
sudo tee /etc/systemd/system/sysstat-collect.timer.d/10-every-minute.conf >/dev/null <<'EOF'
[Timer]
OnCalendar=
OnCalendar=*:*:00
AccuracySec=1s
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now sysstat.service sysstat-collect.timer sysstat-summary.timer
```

`-S XALL` (which includes temperatures, fans and per-device stats) costs about
47 KB per sample, or roughly 70 MB per day in `/var/log/sa`. With
COMPRESSAFTER=10 and HISTORY=28 that is around 1 GB in total. The current day's
file only gains the XALL activities after the next file rotation at midnight.

## Configuration

Set in `/etc/hlidskjalf/hlidskjalf.env`, then `sudo systemctl restart hlidskjalf`.

| Variable | Default | |
|---|---|---|
| `HLID_BIND` / `HLID_PORT` | `0.0.0.0` / `8899` | listen address |
| `HLID_HOSTS` | (empty: no switcher) | host switcher, `name=url,name=url` (http/https only) |
| `HLID_NUT_HOST` / `HLID_NUT_PORT` / `HLID_NUT_UPS` | `none` / `3493` / `ups` | `none` disables the UPS tile; `auto` uses the first UPS |
| `HLID_PODMAN_SOCK` | `/run/podman/podman.sock` | read by the helper |
| `HLID_HELPER_DIR` | `/run/hlidskjalf-helper` | helper output |
| `HLID_LIBVIRT_URI` | `qemu:///system` | always used with `virsh -r` |
| `HLID_SA_DIR` | `/var/log/sa` | |
| `HLID_LINK_HOST` | (request Host header) | host used for container port links |
| `HLID_SAMPLE_INTERVAL` / `HLID_LIVE_POINTS` | `2` / `300` | live ring buffer |
| `HLID_SMART_INTERVAL`, `HLID_SMART=0`, `HLID_VMS=0` | `600` | |
| `HLID_GPU` | `1` | `0` disables GPU collection |
| `HLID_DATA_DIR` | (unit: `/var/lib/hlidskjalf`) | where GPU history is saved |
| `HOST_PROC` / `HOST_SYS` / `HOST_ROOT` | `/proc` / `/sys` / `/` | read host data from another path |

Host switcher example, the same line on every host:

```sh
HLID_HOSTS=asgard=http://192.0.2.10:8899,valhalla=http://192.0.2.11:8899
```

## What it shows

| Section | Source |
|---|---|
| Host: hostname, OS, kernel, uptime, machine, CPU model, installed RAM | `/etc/os-release`, `/proc`, DMI, `/sys/devices/system/memory` |
| CPU: model, total chart, per-core bars grouped by P- and E-cores, load, frequency | `/proc/stat`, `/sys/devices/cpu_{core,atom}`, cpufreq |
| Memory and swap: apps / shared / cache / free breakdown, containers ranked by RAM | `/proc/meminfo`, container cgroups |
| Power: CPU package, cores, iGPU (uncore), DRAM where present | RAPL `/sys/class/powercap/intel-rapl*` |
| GPU (Intel i915/xe, amdgpu fallback): per-engine busy % (Render/3D, Video, VideoEnhance, Blitter, Compute), actual vs requested vs max frequency, RC6 idle, iGPU vs package power | i915 PMU via `perf_event_open` (needs `CAP_PERFMON`), gt sysfs, RAPL |
| GPU clients: per-container / per-process engine usage | `/proc/<pid>/fdinfo` `drm-engine-*` (via the helper), deduplicated by `drm-client-id`, mapped to containers via `/proc/<pid>/cgroup` (`libpod-*`) |
| Temperatures and fans, with friendly labels (CPU package, per core, NVMe, DIMM, ACPI) | hwmon |
| UPS: status, charge, runtime, load, watts, voltages | NUT `upsd` over TCP 3493 (read-only `LIST VAR`) |
| Containers (rootful podman, including stopped): state, health, uptime, image tag/digest, CPU% and RAM sparklines, ports with UI links, quadlet unit | podman REST API (via the helper) plus container cgroups |
| VMs (libvirt, including shut-off): state, vCPU, RAM, CPU%, disks, IP/MAC, autostart | `virsh -r -c qemu:///system` |
| Filesystems: local, LVM, btrfs, NFS, with usage bars | `/proc/1/mountinfo` and `statvfs` (a hung NFS mount is marked stale and never blocks) |
| btrfs: allocation by profile, devices, error counters | `/sys/fs/btrfs` |
| md RAID: level, state, sync progress | `/sys/block/md*/md` |
| Disk I/O live, plus SMART health, temperature, wear, power-on hours, bytes written | `/proc/diskstats`, `smartctl -j -n standby` via the helper (every 10 min; never spins up sleeping HDDs) |
| Network: throughput, link speed, bridge members, IPs | `/proc/net/dev`, `/sys/class/net`, `ip -j addr` |

**Live mode** samples in-process every 2 s into a ring buffer (10 min).
**History** (1h / 24h / 7d) comes from sysstat via `sadf -j`. It is parsed
incrementally and cached in the background, so requests never wait on `sadf`.
History covers CPU, memory, load, network, disk I/O and temperatures.
sysstat has no GPU data, so GPU and RAPL power history is kept in-process as
one-minute buckets (7 days), saved to `$HLID_DATA_DIR/gpu-history.json.gz`
every 10 minutes and on SIGTERM.

**Layout.** The top row (machine tile plus CPU / memory / GPU / temperature /
network / UPS tiles) is always visible. Below it, one tab per area. Each tab
has the range selector. The selected tab is kept in the URL hash (`#gpu`) and
in localStorage. Clicking a tile opens its tab; clicking the machine tile opens
the hardware report. On phones the tab strip and the host switcher scroll
sideways.

**GPU degradation.** Without a GPU the tab says so. Without the PMU (no
`CAP_PERFMON`, SELinux label enforced, or a non-Intel GPU) engine busy % falls
back to the sum of fdinfo clients; amdgpu uses `gpu_busy_percent`.

All collectors run in background worker threads, and HTTP handlers only serve
cached JSON.

## Security model

The web-facing process never runs as root.

| | runs as | privileges | does |
|---|---|---|---|
| `hlidskjalf.service` | `hlidskjalf` (system user, locked password, `nologin` shell) | `CAP_PERFMON` only | serves the dashboard; reads `/proc`, `/sys`, sysstat, libvirt (read-only socket), NUT |
| `hlidskjalf-helper.service` (every 15 s via `.timer`) | root, capability-bounded | `CAP_SYS_RAWIO`, `CAP_SYS_ADMIN`, `CAP_SYS_PTRACE`, `CAP_DAC_READ_SEARCH` | podman API, `smartctl`, other processes' `/proc/<pid>/fd*`, the SMBIOS table; writes JSON to `/run/hlidskjalf-helper` (root:hlidskjalf 0750) |

- The dashboard has no access to the podman socket, which would be
  root-equivalent. The helper only issues fixed GET requests and accepts no
  input from the dashboard. A compromised dashboard can read the helper's
  output but cannot make it do anything.
- Both units are sandboxed: read-only filesystem (`ProtectSystem=strict`),
  syscall filter, `NoNewPrivileges`, no new namespaces, W^X memory, memory and
  task limits. The dashboard also gets a private `/dev`. The helper has no IP
  networking (`AF_UNIX` only, `IPAddressDeny=any`) and read-only access to disks.
  `systemd-analyze security` rates them 1.8 and 2.6 ("OK"); CI checks they stay
  below 3.0.
- RAPL energy counters are root-only by default because they are a weak side
  channel (CVE-2020-8694). The udev rule gives read access to the `hlidskjalf`
  group only. Install with `--no-rapl` to skip power readings.
- HTTP: GET/HEAD only, no shell-outs with request data, a strict
  Content-Security-Policy, `X-Frame-Options: DENY`, and a 30 s connection
  timeout. **There is no authentication.** Anyone who can reach the port sees
  host details, IPs, MACs, containers, VMs and disks. Bind to a management
  address, or to `127.0.0.1` behind a reverse proxy that adds authentication and
  TLS. Never expose it to the internet.

What you lose without the helper (`--no-helper`): the Containers tab, SMART,
per-client GPU usage, ports for host-network containers and the memory modules
in the hardware report. Per-container CPU
and RAM also need the container list. Without `CAP_PERFMON`, GPU engine busy
% falls back to the per-client sum; frequency and RC6 still come from sysfs.

## Development

```sh
python3 -m hlidskjalf              # as any user; reads helper data if present
sudo python3 -m hlidskjalf.helper  # one-shot privileged collection
```

`HLID_BIND=127.0.0.1 HLID_PORT=8898 python3 -m hlidskjalf` runs a second copy
beside an installed one. CI runs `pyflakes`, `node --check`, `shellcheck` on
the installer and `systemd-analyze security` on the units.

## API

All GET, all JSON.

| Endpoint | |
|---|---|
| `/api/info` | static facts, sensors, host switcher |
| `/api/live?since=<seq>` | 2 s samples |
| `/api/snapshot` | containers, VMs, storage, SMART, UPS |
| `/api/history?range=1h\|6h\|24h\|7d` | sysstat history |
| `/api/gpu/history?range=...` | GPU engines, frequency, RC6, RAPL power, top clients |
| `/api/hardware` | hardware report data |
| `/api/health` | liveness and version |
