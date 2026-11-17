# P104-100 PCIe Gen2 x4 Enforcer Daemon (Linux)

A native Linux background daemon that unlocks **PCIe Gen2 x4** on NVIDIA
**P104-100** mining GPUs (`10DE:1B87`) and keeps the link at Gen2 even when the
NVIDIA driver, a power-state transition or a bus reset drops it back to Gen1.

The optional ``--x16`` flag only updates the **x16 width declaration** on the
bus (`LnkCap maxwidth = 16`). This behaviour was carried over from the original
Windows project ([p104-unlock-v3](https://github.com/skbskhud/p104-unlock-v3));
it does not guarantee that the physical link will operate at x16. **There are
currently no confirmed facts that a P104-100 with the capacitors installed can
actually run in x16 mode.**

**Multi-GPU stability:** When multiple GPUs are present the daemon processes them
sequentially with a configurable delay between cards to prevent PCIe Root Port
AER overflows and cascade crashes.

The daemon uses only the Python 3 standard library and talks to hardware
through standard Linux interfaces:

| Resource | Linux interface |
|---|---|
| GPU discovery | `/sys/bus/pci/devices/*/vendor` and `.../device` |
| PCI configuration space | `/sys/bus/pci/devices/<BDF>/config` |
| GPU BAR0 MMIO registers | `/sys/bus/pci/devices/<BDF>/resource0` (mmap) |

No out-of-tree kernel module, no BYOVD driver and no external Python package
is required. Only **volatile** registers are written; a power cycle restores
the factory state.

## Attribution

This project is a Linux port of the Windows tool **p104-unlock-v3**:

- Original project: <https://github.com/skbskhud/p104-unlock-v3>
- The register recipes, safety rules and measured behaviour come from the
  original author's work and notes.

## How it works

The P104-100 boots with its PCIe capability pinned to Gen1 (`LCAP2 = 0x02`).
The NVIDIA driver rewrites several volatile BAR0 registers whenever it
initialises or wakes the GPU, which is what causes the link to fall back to
Gen1. The daemon applies the same "live write group" that was measured to
work on the original hardware:

```
0x8872C |= 0x0F -> 6
0x2241C |= 0x81
0x8841C &= ~0x6000
0x8C2C0 &= ~0x04
0x8C300 &= ~0x10
0x8C040 SPEED[19:18] = 1        (5.0 GT/s)
0x8C1C0[19:16] = 2              (target link speed Gen2)
0x88610 |= 0x01, clear bit 12
```

Before touching the BAR0 speed field the daemon also sets
`LNKCTL2[3:0] = 2` (target speed Gen2) on both the endpoint and its upstream
root port, so a root port that is still targeting Gen1 cannot abort the link
when the endpoint starts a Gen2 retrain.

After the declaration latches to Gen2 (`LCAP2 & 0x6 == 0x6`), the daemon:

1. Re-asserts `LNKCTL2[3:0] = 2` (target speed Gen2) on **both** the endpoint
   and its upstream root port.
2. Triggers a link retrain on the root port and on the endpoint.
3. **Optionally** arms the x16 width declaration with the safe whole-word recipe
   `0x8C040 = (cur & ~0xF8000000) | 0x80000000` — only when ``--x16`` is passed.
4. Retrains once and checks whether the physical width reached x16.

The watchdog then re-checks the link on the configured polling interval. It
only writes when a condition has actually deviated, so the registers are not
rewritten needlessly.

### Safety rules inherited from the original tool

- `0x8C040` must never be written with **bit31 = 0 and bit29 = 0**, nor with
  **bit31 = 1 and bit29 = 1**. Both combinations were observed to knock the
  card off the bus. The daemon refuses such values.
- To roll back an armed width declaration, the **whole word**
  `0x8C040 = 0x20085800` is written. Bit31 is never cleared with a mask.
- Before any configuration-space access the daemon verifies that the device is
  exactly `10DE:1B87`. Non-target hardware is never touched.
- Before writing the speed field the daemon checks the upstream root port's
  Link Capabilities. If that port only advertises 2.5 GT/s, it refuses to
  request Gen2: the retrain cannot succeed and the endpoint would drop off the
  bus.
- Any configuration-space or BAR0 read that returns all ones (`0xFFFFFFFF` /
  `0xFFFF`) is treated as "device gone". The daemon aborts immediately instead
  of writing more registers or reporting a false `PASS`.
- The Linux port does **not** perform a PCI hot-reset. If the card ever falls
  off the bus, a full AC power cycle is required.
- BAR0 is mapped while the NVIDIA driver is bound. This is the same approach
  as the original tool but is not an officially supported kernel use case;
  keep backups and use it on a machine you can safely power-cycle.

### x16 width declaration vs. physical x16

The x16 width declaration is **disabled by default**. The ``--x16`` flag only
announces a maximum width of 16 lanes on the bus; it was ported from the
original Windows project. It does not guarantee that the physical link will
actually train to x16. At the moment there are no confirmed facts that a
P104-100 with the capacitors installed can operate in x16 mode.

To enable the x16 width declaration, pass ``--x16``:

```bash
sudo python3 /usr/local/p104-100/p104-100.py --once --x16
```

The tool declares a maximum width of 16 lanes and retrains the link. Whether
the physical link actually becomes x16 depends on the board: on the original
author's card it stays x4 without hardware modifications because the width
registers are protected by a hardware privilege gate. The daemon logs the real
`LNKSTA` width and stops retraining after two consecutive misses in the same
process, so it will not fight the hardware forever.

`nvidia-smi` may report Gen1 even when the raw registers and
bandwidth show Gen2; trust `LNKSTA`/`LCAP2` and a bandwidth test instead.

## Requirements

- Linux with sysfs PCI support (any modern distribution).
- Python 3.9 or newer.
- Root privileges (raw PCI configuration-space and BAR0 access).
- The NVIDIA driver loaded with the Gen2 driver policy enabled
  (`RmForceEnableGen2=1`, `RMPcieLinkSpeed=0x1`); see
  [NVIDIA driver policy](#nvidia-driver-policy-gen2-enable). The daemon does
  not require `nvidia-smi`, but the driver must have bound the GPU for the
  link to be initialised.
- An x86_64 host is recommended. The code handles non-4 KiB page sizes, but
  the register map was verified on x86_64.
- Kernel BAR0 access: kernels built with `CONFIG_IO_STRICT_DEVMEM` (the
  default on Ubuntu and many other distributions) refuse `mmap()` of
  `resource0` while the NVIDIA driver has claimed the BAR. Add
  `iomem=relaxed` to the kernel command line; see
  [Kernel access to BAR0](#kernel-access-to-bar0-iomemrelaxed) below.

## Kernel access to BAR0 (`iomem=relaxed`)

Since Linux 5.12 (commit `636b21b50152`, "PCI: Revoke mappings like devmem"),
kernels with `CONFIG_IO_STRICT_DEVMEM=y` reject user-space mappings of a sysfs
PCI resource whose region is already owned by a kernel driver. On such a
system the daemon fails with:

```
cannot map BAR0 (resource0): resource0: mmap failed: [Errno 22] Invalid argument
```

even when run as root, because the NVIDIA driver has requested the BAR. The
supported way to allow the daemon to map it is to boot with the kernel
parameter `iomem=relaxed`:

```bash
# GRUB-based distributions (Debian, Ubuntu, Arch, Fedora)
# Add iomem=relaxed to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub, e.g.:
#   GRUB_CMDLINE_LINUX_DEFAULT="quiet splash iomem=relaxed"
sudo update-grub
sudo reboot
```

For systemd-boot or a custom bootloader, append `iomem=relaxed` to the kernel
`options`/`linux` line instead. After reboot, verify it is active:

```bash
grep -o 'iomem=relaxed' /proc/cmdline
```

`iomem=relaxed` is machine-wide: it also relaxes `/dev/mem` filtering for
other devices, so treat it as a **deliberate security trade-off**. It does not
unprotect system RAM, and it does not bypass Secure Boot or kernel lockdown.
If Secure Boot/lockdown is enabled, the mmap can still fail with `EPERM`;
disable Secure Boot or use a kernel without lockdown for this machine.

The daemon prints this hint automatically when the mmap fails with `EINVAL`
or `EPERM`.

## NVIDIA driver policy (Gen2 enable)

The Windows original writes the display driver's registry policy before every
unlock:

```
RMPcieLinkSpeed = 1
RmForceEnableGen2 = 1
RmEnableGen2 = 1
```

On Linux the same settings are passed to the proprietary NVIDIA module through
`NVreg_RegistryDwords`. Without them the driver may keep clamping the PCIe
link to Gen1 and can undo or fight the BAR0 declaration. Create
`/etc/modprobe.d/p104-100.conf`:

```bash
echo 'options nvidia NVreg_RegistryDwords="RmForceEnableGen2=1;RMPcieLinkSpeed=0x1;RmEnableGen2=1"' \
  | sudo tee /etc/modprobe.d/p104-100.conf
sudo update-initramfs -u
sudo reboot
```

Verify after reboot:

```bash
grep RegistryDwords /proc/driver/nvidia/params
```

The daemon logs a warning at startup if `RmForceEnableGen2=1` or
`RMPcieLinkSpeed=0x1` is missing from the running driver.

> **Warning:** these registry dwords are global and affect **every** NVIDIA
> GPU in the machine (they tell the driver to allow Gen2). On a mixed rig with
> newer cards, use `NVreg_RegistryDwordsPerDevice="pci=0000:01:00.0;..."` if
> your driver version supports these keys per device, or accept that all GPUs
> will be allowed to negotiate Gen2.

## Installation

1. Copy the daemon to a stable location:

   ```bash
   sudo install -d -m 0755 /usr/local/p104-100
   sudo install -m 0755 p104-100.py /usr/local/p104-100/p104-100.py
   ```

2. Verify the current link state (read-only):

   ```bash
   sudo python3 /usr/local/p104-100/p104-100.py --status
   ```

   A factory card reports `LNKSTA=0x0041` (`2.5GT/s (Gen1) x4`).

3. Try a single unlock before installing the service:

   ```bash
   sudo python3 /usr/local/p104-100/p104-100.py --once --bdf 0000:01:00.0
   ```

   The daemon logs a `PASS` line (add `--log-level DEBUG` for group steps).
   Re-run `--status` to confirm Gen2.

4. Install and start the systemd service:

   ```bash
   sudo install -m 0644 p104-100.service /etc/systemd/system/p104-100.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now p104-100.service
   ```

## Running and monitoring

```bash
# Service status and recent log lines
systemctl status p104-100.service
journalctl -u p104-100.service -f

# Follow the structured log
journalctl -u p104-100.service -n 100 --no-pager

# Current link state (read-only)
sudo python3 /usr/local/p104-100/p104-100.py --status
```

The default `INFO` level shows only card discovery, the first Gen2
confirmation per card, and problems (a link that stays below Gen2,
MMIO/config-space errors).  Retrains and recoveries are logged at `DEBUG`; use
`--log-level DEBUG` to see the full per-step register and retrain trace.

Typical successful log lines:

```
2026-11-15 05:40:13 INFO     found 4 P104-100 card(s): 0000:01:00.0, ...
2026-11-15 05:40:13 INFO     0000:01:00.0: Gen2 active (5.0GT/s (Gen2) x4)
```

## Configuration

All options are command-line flags. Edit `ExecStart` in the unit file to change
the defaults.

| Option | Default | Description |
|---|---|---|
| `--interval SECONDS` | `0.02` | Hold-loop tick. `0.02` is the 20 ms tick copied from the C# `HoldLoop`. |
| `--min-gap SECONDS` | `0.2` | Debounce between full-group replays. |
| `--retrain-gap SECONDS` | `0.02` | Debounce between TLS+retrain triggers while the link is Gen1. |
| `--speed-gap SECONDS` | `0.2` | Debounce between fast `0x8C040` speed rewrites. |
| `--group-retry SECONDS` | `6` | Replay the full group after this long in Gen1. |
| `--x16` | off | Arm the x16 width declaration on the bus (announcement only). |
| `--once` | off | Apply the full unlock once and exit. |
| `--status` | off | Print the current link state of every P104-100 and exit. |
| `--bdf BDF` | all | Only operate on this PCI address (repeatable). |
| `--force` | off | Bypass the single-instance lock (unsafe; concurrent instances can corrupt the link state). |
| `--stagger SECONDS` | `1.0` | Delay between cards during `--once` only. |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

Examples:

```bash
# Watchdog with the 20 ms hold loop (default)
sudo python3 /usr/local/p104-100/p104-100.py

# One-shot unlock of a single card
sudo python3 /usr/local/p104-100/p104-100.py --once --bdf 0000:01:00.0

# x16 width declaration (announcement only)
sudo python3 /usr/local/p104-100/p104-100.py --once --x16 --bdf 0000:01:00.0

# Retry the full group less often on a noisy link
sudo python3 /usr/local/p104-100/p104-100.py --group-retry 10

# One-shot diagnostic
sudo python3 /usr/local/p104-100/p104-100.py --once --log-level DEBUG --bdf 0000:01:00.0
```

## Multi-GPU systems

The daemon discovers all P104-100 cards at startup and keeps one instance per
card. Each 20 ms cycle visits the cards sequentially and configures only the
two ends of that card's link. New cards are not hot-added; restart the service
after changing the hardware.

Initializing several cards at once can still cause PCIe Root Port AER
overflows and cascade crashes, so `--once` staggers the full-group unlock
between cards (`--stagger`, default 1 s). The watchdog itself does not stagger:
it only performs single `0x8C040` writes and quick retrains.

**Recommended settings for a 4-card rig:**

- `--interval 0.02` (20 ms tick; this is the default)
- no `--x16` unless you want the bus width announcement
- validate `--once --bdf <addr>` on one card before running the watchdog on
  all four

## Single instance

The watchdog and `--once` take an exclusive `flock` on
`/run/lock/p104-100.lock`. A second writer exits with code `3` and logs
the PID of the running instance; the lock is released automatically on exit or
crash. `--status` is read-only and never takes the lock. Use `--force` only to
override the lock intentionally.

## systemd unit notes

The provided `p104-100.service` is intentionally hardened:

- Runs as `root` with a minimal capability bounding set
  (`CAP_SYS_ADMIN`, `CAP_SYS_RAWIO`).
- `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `PrivateDevices=yes`, `NoNewPrivileges=yes`, `MemoryDenyWriteExecute=yes`,
  `RestrictAddressFamilies=AF_UNIX`.
- `ProtectKernelTunables=no` is **required**. Enabling it remounts `/sys`
  read-only and would prevent PCI configuration-space writes.

If your distribution starts the service before the NVIDIA driver is ready,
increase `RestartSec` or add a suitable `After=` dependency. If no card is
found at startup the daemon exits with code 4 and systemd restarts it.
`RestartPreventExitStatus=3` stops the restart loop when another instance
already holds the lock; stop that instance and restart the unit manually.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `no NVIDIA P104-100 (10DE:1B87) found` | The GPU is not visible in `lspci`; check `lspci -nn | grep 10de`, the driver and the power cabling. |
| `config space is not writable` | Run as root. Check `ls -l /sys/bus/pci/devices/<BDF>/config`. |
| Writes fail with `EPERM` even as root | Kernel lockdown (Secure Boot) blocks userspace PCI config access (`LOCKDOWN_PCI_ACCESS`). Disable Secure Boot or kernel lockdown for this machine. |
| `cannot open device: ... resource0` | BAR0 is not assigned or the resource is unavailable. Check `lspci -vv` and the kernel log. |
| `mmap failed: [Errno 22] Invalid argument` on `resource0` | The kernel blocks user-space mappings of a driver-owned BAR (`CONFIG_IO_STRICT_DEVMEM`). Boot with `iomem=relaxed`; see [Kernel access to BAR0](#kernel-access-to-bar0-iomemrelaxed). |
| `mmap failed: [Errno 1] Operation not permitted` on `resource0` | Kernel lockdown (Secure Boot) or an LSM (AppArmor/SELinux) blocked the mapping. Disable Secure Boot/kernel lockdown. |
| Declaration latches but link stays Gen1 | Some systems need a retrain on the root port; check the log and try `--interval 0.02`. |
| `RESULT: FAIL` after all retries | The card or slot may not support Gen2 on this platform, or the link partner is a PCH port. |
| `NVIDIA Gen2 driver policy is not active` at startup | The `NVreg_RegistryDwords` policy is missing. Add it as described in [NVIDIA driver policy](#nvidia-driver-policy-gen2-enable) and reboot. |
| `upstream port ... only advertises 2.5GT/s` | The root port is Gen1-only; the daemon refuses to write because the retrain would drop the card. Move the GPU to a Gen2-capable slot/riser. |
| `RESULT: FAIL - configuration space returns all ones`, `BAR0 stopped responding`, or `NVRM: Xid ... GPU has fallen off the bus` | The Gen2 request dropped the link. The daemon now aborts immediately instead of writing more registers or reporting a false `PASS`. Power off, unplug AC power for 30 s, then boot again. Check the upstream port's `LnkCap` speed and the riser/slot signal quality before retrying. |
| Cards drop off sequentially on multi-GPU rig | Simultaneous initialization across all ports is causing AER overflow. Run the initial unlock with `--once --stagger 2`; if it still happens under load, check the risers/slots. |
| Link drops during CUDA work | Expected. The NVIDIA driver rewrites the volatile registers; the daemon restores Gen2 on the next 20 ms poll. |
| `nvidia-smi` shows Gen1 | `nvidia-smi` reports driver-cached values that are unreliable on this mining card. Trust `--status` and a bandwidth test. |
| Card disappeared from the bus | Power off the machine, unplug AC power for 30 seconds, then boot again. This cannot be recovered by a warm reboot. |

## Uninstall

```bash
sudo systemctl disable --now p104-100.service
sudo rm -f /etc/systemd/system/p104-100.service
sudo systemctl daemon-reload
sudo rm -rf /usr/local/p104-100
```

A power cycle is enough to revert all register changes. The daemon never
modifies the VBIOS, EEPROM or fuses.

## Disclaimer

For learning, research and your own hardware only. Modifying PCIe link
configuration can cause device loss or system instability. Use at your own
risk. No VBIOS, EEPROM or fuse-level persistent changes are made; all writes
target volatile registers and disappear after a power cycle.

This project is not affiliated with NVIDIA or with the original author of
`p104-unlock-v3`; it is an independent Linux port published under the same
spirit of open research.

This software is provided "as is", without warranty of any kind, and is
distributed under the terms of the [MIT License](LICENSE).
