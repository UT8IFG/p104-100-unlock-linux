#!/usr/bin/env python3
"""P104-100 (GP104) PCIe Gen2 keeper.

The unlock core is a line-for-line port of the proven reference sequence:

    0x8C040(speed) -> 0x8872C=6 -> 0x2241C -> 0x8841C -> 0x8C2C0
    -> 0x8C300 -> 0x8C1C0 -> 0x88610, then TLS=Gen2 + retrain

Do not add safety reads or reorder the steps.  Every deviation that was tried
before (extra read before 0x8C040, latch-first, split passes) dropped the card
off the bus.  The working script is the reference.

Usage:
    sudo python3 p104-100.py --status
    sudo python3 p104-100.py --once
    sudo python3 p104-100.py --once --bdf 0000:01:00.0
    sudo python3 p104-100.py --x16

The x16 width declaration is opt-in (--x16) and off by default: most
P104-100 cards lack the AC coupling capacitors for stable x16 operation.
"""
import argparse
import fcntl
import logging
import mmap
import os
import signal
import struct
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

VENDOR_NVIDIA = 0x10DE
DEVICE_P104 = 0x1B87
SYSFS_PCI = Path("/sys/bus/pci/devices")
LOCK_PATH = Path("/run/lock/p104-100.lock")
EXIT_ALREADY_RUNNING = 3

OFF_LINKCFG0 = 0x8C040
OFF_LTSSM = 0x8872C
OFF_PTOP_FS4 = 0x2241C
OFF_PRIV_MISC1 = 0x8841C
OFF_CYA0 = 0x8C2C0
OFF_CYA1 = 0x8C300
OFF_LCTRL2 = 0x8C1C0
OFF_VSEC_HIER = 0x88610

PCIE_CAP_ID = 0x10
PCIE_LNK_CAP = 0x0C
PCIE_LNK_CTL = 0x10
PCIE_LNK_STA = 0x12
PCIE_LNK_CAP2 = 0x2C
PCIE_LNK_CTL2 = 0x30

BAR0_MAP = 16 * 1024 * 1024

GROUP_STEPS = (
    (OFF_LINKCFG0, 0xC0000, 0x40000, "0x8C040(speed)"),
    (OFF_LTSSM, 0xF, 6, "0x8872C"),
    (OFF_PTOP_FS4, 0x81, 0x81, "0x2241C"),
    (OFF_PRIV_MISC1, 0x6000, 0, "0x8841C"),
    (OFF_CYA0, 0x4, 0, "0x8C2C0"),
    (OFF_CYA1, 0x10, 0, "0x8C300"),
    (OFF_LCTRL2, 0xF0000, 0x20000, "0x8C1C0"),
    (OFF_VSEC_HIER, 0x1001, 0x1, "0x88610"),
)

SPEED_MASK = 0xC0000
SPEED_GEN2 = 1 << 18          # Pascal encoding: SPEED[19:18] = 1 -> 5.0 GT/s

# x16 width declaration (opt-in).  The hardware drops off the bus when
# bit31 and bit29 are both zero or both one, so every 0x8C040 write must
# keep at least one of them set (whole-word recipe only).
LINKCFG0_ARM_BIT = 0x80000000
LINKCFG0_BIT29 = 0x20000000
LINKCFG0_SAFE = 0x20085800    # factory-safe whole word: bit31=0, bit29=1
WIDTH_RETRY_GAP = 0.5         # re-arm debounce when the driver clears it
WIDTH_RETRAIN_GAP = 3.0       # minimum gap between physical width retrains
WIDTH_RETRAIN_OBSERVE = 2.0   # observation window after a width retrain
MAX_WIDTH_FAILS = 2           # stop retraining width after this many misses
RECOVERY_WARN_SEC = 10.0      # warn when the link stays below Gen2 this long
RECOVERY_WARN_GAP = 60.0      # repeat that warning at most this often

# Hold-loop timings, copied from ref/P104Gen2x4.cs (HoldLoop, lines 2229-2338).
TICK_DEFAULT = 0.02           # T_HOLD_PERIOD_MS = 20
RETRAIN_GAP = 0.02            # retrain debounce inside the hold loop
SPEED_GAP = 0.20              # 0x8C040 speed rewrite debounce
GROUP_GAP = 0.20              # full group debounce when LCAP2 is lost
GROUP_RETRY = 6.0             # re-run the full group after ~6 s of Gen1

LOG = logging.getLogger("p104-gen2")


def speed_name(value: int) -> str:
    return {1: "2.5GT/s (Gen1)", 2: "5.0GT/s (Gen2)", 3: "8.0GT/s (Gen3)",
            4: "16GT/s (Gen4)", 5: "32GT/s (Gen5)", 6: "64GT/s (Gen6)"}.get(
        value, f"speed={value}")


def width_name(value: int) -> str:
    return f"x{value}" if value else "x?"


def link_name(lnk_sta: int) -> str:
    return f"{speed_name(lnk_sta & 0xF)} {width_name((lnk_sta >> 4) & 0x3F)}"


def is_valid(value: int, bits: int) -> bool:
    return value != ((1 << bits) - 1) and value != 0xFFFFFFFF


class InstanceLock:
    """Advisory single-instance lock backed by flock(2)."""

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self.fd: Optional[int] = None

    def acquire(self, force: bool = False) -> bool:
        if force:
            LOG.warning("instance lock bypassed (--force) - concurrent writes "
                        "may corrupt the link state")
            return True
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = ""
            try:
                holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
            except OSError:
                pass
            os.close(fd)
            if holder:
                LOG.error("another instance is already running (pid %s) - exiting",
                          holder)
            else:
                LOG.error("another instance is already running - exiting")
            return False
        except BaseException:
            os.close(fd)
            raise
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        except OSError:
            pass
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None


class PciConfig:
    def __init__(self, dev_dir: Path, bdf: str):
        self.dev_dir = dev_dir
        self.bdf = bdf
        self.fd: Optional[int] = None

    def open(self) -> None:
        self.close()
        self.fd = os.open(str(self.dev_dir / "config"), os.O_RDWR | os.O_CLOEXEC)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def read(self, offset: int, size: int) -> int:
        if self.fd is None:
            raise OSError("config space is not open")
        return int.from_bytes(os.pread(self.fd, size, offset), "little")

    def write(self, offset: int, value: int, size: int) -> None:
        if self.fd is None:
            raise OSError("config space is not open")
        os.pwrite(self.fd, value.to_bytes(size, "little"), offset)

    def find_pcie_cap(self) -> int:
        if self.fd is None:
            return 0
        pos = self.read(0x34, 1) & 0xFC
        for _ in range(48):
            if not pos or pos >= 0x100:
                break
            header = self.read(pos, 4)
            if (header & 0xFF) == PCIE_CAP_ID:
                return pos
            pos = (header >> 8) & 0xFC
        return 0


class Bar0:
    def __init__(self, dev_dir: Path, bdf: str):
        self.dev_dir = dev_dir
        self.bdf = bdf
        self.fd: Optional[int] = None
        self.mm: Optional[mmap.mmap] = None

    def open(self) -> None:
        self.close()
        path = self.dev_dir / "resource0"
        self.fd = os.open(str(path), os.O_RDWR | os.O_CLOEXEC)
        size = os.fstat(self.fd).st_size
        length = min(size if size else BAR0_MAP, BAR0_MAP)
        length = (length // mmap.PAGESIZE) * mmap.PAGESIZE
        self.mm = mmap.mmap(self.fd, length, access=mmap.ACCESS_WRITE, offset=0)
        LOG.debug("%s: BAR0 mapped, %d bytes", self.bdf, length)

    def close(self) -> None:
        if self.mm is not None:
            try:
                self.mm.close()
            except Exception:
                pass
            self.mm = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def read32(self, offset: int) -> int:
        if self.mm is None:
            raise OSError("BAR0 is not mapped")
        return struct.unpack_from("<I", self.mm, offset)[0]

    def write32(self, offset: int, value: int) -> None:
        if self.mm is None:
            raise OSError("BAR0 is not mapped")
        self.mm[offset:offset + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")
        self.mm.flush()


class Device:
    def __init__(self, bdf: str):
        self.bdf = bdf
        self.dev_dir = SYSFS_PCI / bdf
        self.cfg = PciConfig(self.dev_dir, bdf)
        self.bar = Bar0(self.dev_dir, bdf)
        self.root: Optional[PciConfig] = None
        self.pcie_cap = 0
        self.root_cap = 0

    def open_cfg(self) -> None:
        """Open config space only (no BAR0 mapping) - used for state polls."""
        self.cfg.open()
        self.pcie_cap = self.cfg.find_pcie_cap()
        if not self.pcie_cap:
            raise RuntimeError("PCIe capability not found")

    def open(self) -> None:
        # Same order as the working script: cfg, root, then BAR0.
        self.open_cfg()
        parent = self.dev_dir.resolve().parent
        root_dir = None
        while parent and parent != parent.parent:
            if (parent / "config").exists():
                root_dir = parent
                break
            parent = parent.parent
        if root_dir is not None:
            self.root = PciConfig(root_dir, root_dir.name)
            try:
                self.root.open()
                self.root_cap = self.root.find_pcie_cap()
            except OSError:
                self.root = None
        self.bar.open()

    def close(self) -> None:
        self.bar.close()
        self.cfg.close()
        if self.root is not None:
            self.root.close()
            self.root = None

    def alive(self) -> bool:
        try:
            return self.cfg.read(0, 4) == ((DEVICE_P104 << 16) | VENDOR_NVIDIA)
        except OSError:
            return False

    def lnk_cap(self) -> int:
        return self.cfg.read(self.pcie_cap + PCIE_LNK_CAP, 4)

    def lnk_sta(self) -> int:
        return self.cfg.read(self.pcie_cap + PCIE_LNK_STA, 2)

    def lnk_cap2(self) -> int:
        return self.cfg.read(self.pcie_cap + PCIE_LNK_CAP2, 4)

    def lnk_ctl2(self) -> int:
        return self.cfg.read(self.pcie_cap + PCIE_LNK_CTL2, 4)

    def gen2_declared(self) -> bool:
        try:
            value = self.lnk_cap2()
            return is_valid(value, 32) and (value & 0x6) == 0x6
        except OSError:
            return False

    def gen2_link(self) -> bool:
        try:
            value = self.lnk_sta()
            return is_valid(value, 16) and (value & 0xF) >= 2
        except OSError:
            return False

    def state_line(self) -> str:
        try:
            return (f"LNKCAP=0x{self.lnk_cap():08X} "
                    f"LNKSTA=0x{self.lnk_sta():04X} ({link_name(self.lnk_sta())}) "
                    f"LCAP2=0x{self.lnk_cap2():08X} "
                    f"TLS={self.lnk_ctl2() & 0xF}")
        except OSError as exc:
            return f"state read failed: {exc}"


def apply_group(dev: Device) -> bool:
    """Run the exact proven register group (no TLS/retrain)."""
    if not dev.alive():
        LOG.error("%s: device does not answer in config space", dev.bdf)
        return False

    LOG.debug("%s: baseline 0x8C040=0x%08X 0x8872C=0x%08X alive=%s",
             dev.bdf,
             _safe_read(dev.bar, OFF_LINKCFG0),
             _safe_read(dev.bar, OFF_LTSSM),
             dev.alive())

    for offset, mask, value, name in GROUP_STEPS:
        try:
            old = dev.bar.read32(offset)
        except OSError as exc:
            LOG.error("%s: %s read failed: %s", dev.bdf, name, exc)
            return False
        if old == 0xFFFFFFFF:
            LOG.error("%s: %s read 0xFFFFFFFF -> link is down", dev.bdf, name)
            return False
        new = (old & ~mask) | (value & mask)
        if new == old:
            LOG.debug("%s: %s no-op (0x%08X)", dev.bdf, name, old)
            time.sleep(0.05)
            continue
        try:
            dev.bar.write32(offset, new)
        except OSError as exc:
            LOG.error("%s: %s write failed: %s", dev.bdf, name, exc)
            return False
        time.sleep(0.02)
        try:
            readback = dev.bar.read32(offset)
        except OSError:
            readback = 0xFFFFFFFF
        ok = (readback & mask) == (value & mask)
        LOG.debug("%s: %s 0x%08X -> 0x%08X rb=0x%08X %s alive=%s",
                 dev.bdf, name, old, new, readback,
                 "OK" if ok else "FAIL", dev.alive())
        if readback == 0xFFFFFFFF or not dev.alive():
            LOG.error("%s: link dropped during %s", dev.bdf, name)
            return False
        time.sleep(0.05)

    return True


def unlock(dev: Device) -> bool:
    """Full unlock: script group + TLS/retrain + verdict."""
    LOG.debug("%s: unlock start: %s", dev.bdf, dev.state_line())
    if not apply_group(dev):
        return False
    _tls_and_retrain(dev)
    try:
        sta = dev.lnk_sta()
        cap2 = dev.lnk_cap2()
    except OSError:
        sta = 0xFFFFFFFF
        cap2 = 0xFFFFFFFF
    if not is_valid(sta, 16):
        LOG.error("%s: FAIL - config space is gone after unlock", dev.bdf)
        return False
    if (sta & 0xF) >= 2:
        LOG.info("%s: PASS %s (LCAP2=0x%08X, TLS=%d)",
                 dev.bdf, link_name(sta), cap2, dev.lnk_ctl2() & 0xF)
        return True
    LOG.error("%s: FAIL - still %s (LCAP2=0x%08X)",
              dev.bdf, link_name(sta), cap2)
    return False


def _safe_read(bar: Bar0, offset: int) -> int:
    try:
        return bar.read32(offset)
    except OSError:
        return 0xFFFFFFFF


def _write_tls(dev: Device) -> None:
    """Set LNKCTL2.TLS=Gen2 on both ends, only where it is not already set."""
    endpoint_cap = dev.pcie_cap
    root = dev.root
    root_cap = dev.root_cap if root is not None else 0
    for cfg, cap, name in ((dev.cfg, endpoint_cap, "endpoint"),
                           (root, root_cap, "root")):
        if cfg is None or not cap:
            continue
        try:
            current = cfg.read(cap + PCIE_LNK_CTL2, 4)
            if (current & 0xF) != 2:
                cfg.write(cap + PCIE_LNK_CTL2, (current & ~0xF) | 2, 4)
                LOG.debug("%s: %s TLS 0x%08X -> 0x%08X",
                         dev.bdf, name, current, (current & ~0xF) | 2)
        except OSError as exc:
            LOG.debug("%s: %s TLS write failed: %s", dev.bdf, name, exc)


def _trigger_retrain(dev: Device) -> None:
    """Set LNKCTL.Retrain on the root port, then on the endpoint."""
    endpoint_cap = dev.pcie_cap
    root = dev.root
    root_cap = dev.root_cap if root is not None else 0
    for cfg, cap, name in ((root, root_cap, "root"),
                           (dev.cfg, endpoint_cap, "endpoint")):
        if cfg is None or not cap:
            continue
        try:
            current = cfg.read(cap + PCIE_LNK_CTL, 4)
            if current != 0xFFFFFFFF:
                cfg.write(cap + PCIE_LNK_CTL, current | (1 << 5), 4)
        except OSError as exc:
            LOG.debug("%s: %s retrain failed: %s", dev.bdf, name, exc)


def _tls_and_retrain(dev: Device) -> None:
    LOG.debug("%s: TLS+retrain (endpoint cap=0x%02X root cap=0x%02X)",
             dev.bdf, dev.pcie_cap,
             dev.root_cap if dev.root is not None else 0)
    _write_tls(dev)
    _trigger_retrain(dev)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if dev.gen2_link():
            break
    try:
        LOG.debug("%s: after retrain %s", dev.bdf, dev.state_line())
    except OSError:
        pass


def arm_width(dev: Device) -> bool:
    """Arm the x16 width declaration and verify that LNKCAP reports x16.

    Only a whole-word 0x8C040 write is used: bit31=0/bit29=0 drops the card
    off the bus, so the legality check is mandatory.  A no-op write is
    skipped because it is fatal on this hardware.
    """
    try:
        current = dev.bar.read32(OFF_LINKCFG0)
    except OSError as exc:
        LOG.error("%s: cannot read 0x8C040: %s", dev.bdf, exc)
        return False
    if current == 0xFFFFFFFF:
        LOG.error("%s: 0x8C040 reads 0xFFFFFFFF - refusing to arm width", dev.bdf)
        return False
    try:
        cap = dev.lnk_cap()
    except OSError:
        cap = 0
    if ((current & LINKCFG0_ARM_BIT) and is_valid(cap, 32)
            and ((cap >> 4) & 0x3F) == 16):
        return True

    new = (current & ~0xF8000000) | LINKCFG0_ARM_BIT
    if not (new & (LINKCFG0_ARM_BIT | LINKCFG0_BIT29)):
        LOG.error("%s: refusing illegal 0x8C040=0x%08X", dev.bdf, new)
        return False
    if new != current:
        try:
            dev.bar.write32(OFF_LINKCFG0, new)
            time.sleep(0.05)
        except OSError as exc:
            LOG.error("%s: cannot arm width: %s", dev.bdf, exc)
            return False

    try:
        readback = dev.bar.read32(OFF_LINKCFG0)
    except OSError as exc:
        LOG.error("%s: cannot read back 0x8C040: %s", dev.bdf, exc)
        return False
    if readback == 0xFFFFFFFF or (readback & LINKCFG0_ARM_BIT) == 0:
        LOG.error("%s: width arm did not latch (0x8C040=0x%08X)",
                  dev.bdf, readback)
        return False

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            cap = dev.lnk_cap()
        except OSError:
            return False
        if not is_valid(cap, 32):
            LOG.error("%s: LNKCAP=0x%08X after width arm - link is down",
                      dev.bdf, cap)
            return False
        if ((cap >> 4) & 0x3F) == 16:
            LOG.info("%s: x16 width declaration active (LNKCAP=0x%08X)",
                     dev.bdf, cap)
            return True
        time.sleep(0.1)

    try:
        current = dev.bar.read32(OFF_LINKCFG0)
        if current == 0xFFFFFFFF:
            LOG.error("%s: link is down - not restoring 0x8C040", dev.bdf)
            return False
        if current != LINKCFG0_SAFE:
            dev.bar.write32(OFF_LINKCFG0, LINKCFG0_SAFE)
            LOG.warning("%s: width arm did not change LNKCAP - restored "
                        "0x8C040=0x%08X", dev.bdf, LINKCFG0_SAFE)
    except OSError as exc:
        LOG.error("%s: cannot restore 0x8C040: %s", dev.bdf, exc)
    return False


def retrain_width(dev: Device) -> bool:
    """Retrain once and check whether the physical link reached x16."""
    LOG.debug("%s: width retrain (TLS=Gen2 + retrain, observing %.0f s)",
             dev.bdf, WIDTH_RETRAIN_OBSERVE)
    _write_tls(dev)
    _trigger_retrain(dev)
    time.sleep(0.1)
    deadline = time.monotonic() + WIDTH_RETRAIN_OBSERVE
    while time.monotonic() <= deadline:
        try:
            sta = dev.lnk_sta()
        except OSError as exc:
            LOG.warning("%s: cannot read LNKSTA after width retrain: %s",
                        dev.bdf, exc)
            return False
        if not is_valid(sta, 16):
            LOG.error("%s: LNKSTA=0x%04X after width retrain - link is down",
                      dev.bdf, sta)
            return False
        if (sta & 0xF) < 2:
            LOG.debug("%s: link dropped to Gen1 during width retrain", dev.bdf)
            return False
        if ((sta >> 4) & 0x3F) == 16:
            LOG.info("%s: physical link width reached x16", dev.bdf)
            return True
        time.sleep(0.1)
    try:
        width = (dev.lnk_sta() >> 4) & 0x3F
    except OSError:
        width = 0
    LOG.info("%s: width retrain result x%d "
             "(x4 is normal without AC coupling capacitors)", dev.bdf, width)
    return False


def discover() -> List[str]:
    found: List[str] = []
    for entry in sorted(SYSFS_PCI.iterdir()):
        try:
            vendor = int((entry / "vendor").read_text().strip(), 16)
            device = int((entry / "device").read_text().strip(), 16)
        except (OSError, ValueError):
            continue
        if vendor == VENDOR_NVIDIA and device == DEVICE_P104:
            found.append(entry.name)
    return found


def filter_bdf(found: List[str], wanted: List[str]) -> List[str]:
    if not wanted:
        return found
    return [bdf for bdf in found if bdf in wanted]


def read_state(bdf: str) -> Optional[str]:
    dev = Device(bdf)
    try:
        dev.open_cfg()
        if not dev.alive():
            return f"{bdf}: not responding (off the bus?)"
        return f"{bdf}: {dev.state_line()}"
    except (OSError, RuntimeError) as exc:
        return f"{bdf}: {exc}"
    finally:
        dev.close()


def repair(bdf: str, x16: bool = False) -> bool:
    dev = Device(bdf)
    try:
        dev.open()
        ok = unlock(dev)
        if ok and x16 and arm_width(dev):
            retrain_width(dev)
        return ok
    except (OSError, RuntimeError) as exc:
        LOG.error("%s: %s", bdf, exc)
        return False
    finally:
        dev.close()


def cmd_status() -> int:
    found = discover()
    if not found:
        LOG.error("no NVIDIA P104-100 (10DE:1B87) found")
        return 4
    for bdf in found:
        print(read_state(bdf))
    return 0


def run_once(bdfs: List[str], stagger: float, x16: bool = False) -> int:
    if not bdfs:
        LOG.error("no NVIDIA P104-100 (10DE:1B87) found")
        return 4
    result = 0
    for index, bdf in enumerate(bdfs):
        if index and stagger > 0:
            time.sleep(stagger)
        if not repair(bdf, x16):
            result = 6
    return result


class Keeper:
    """Hold-loop equivalent of ref/P104Gen2x4.cs:2229-2338.

    The full register group is only replayed when the Gen2 declaration is
    lost (or after ~6 s of Gen1).  A driver speed reset only needs a single
    0x8C040 write, and a retrain is triggered every 20 ms.
    """

    def __init__(self, bdf: str, tick: float, group_gap: float,
                 retrain_gap: float, speed_gap: float, group_retry: float,
                 x16: bool = False):
        self.bdf = bdf
        self.tick = tick
        self.group_gap = group_gap
        self.retrain_gap = retrain_gap
        self.speed_gap = speed_gap
        self.group_retry = group_retry
        self.x16 = x16
        self.dev: Optional[Device] = None
        self.lost = 0
        self.next_open = 0.0
        self.last_group = 0.0
        self.last_speed = 0.0
        self.last_retrain = 0.0
        self.last_width = 0.0
        self.last_width_retrain = 0.0
        self.width_fails = 0
        self.low_since: Optional[float] = None
        self.was_gen2: Optional[bool] = None
        self.gen2_logged = False
        self.recovery_since: Optional[float] = None
        self.last_recovery_warn = 0.0
        self.mmio_warned = False
        self.declared_lost = False
        self.open()

    def open(self) -> bool:
        self.close()
        dev = Device(self.bdf)
        try:
            dev.open()
        except (OSError, RuntimeError) as exc:
            LOG.error("%s: cannot open: %s", self.bdf, exc)
            return False
        self.dev = dev
        self.lost = 0
        return True

    def close(self) -> None:
        if self.dev is not None:
            self.dev.close()
            self.dev = None

    def full_group(self, reason: str = "declaration lost") -> None:
        """Replay the proven write group in a fresh device open."""
        LOG.debug("%s: replaying full group (%s)", self.bdf, reason)
        self.close()
        dev = Device(self.bdf)
        try:
            dev.open()
            apply_group(dev)
        except (OSError, RuntimeError) as exc:
            LOG.error("%s: full group failed: %s", self.bdf, exc)
        finally:
            dev.close()
        self.open()

    def poll(self) -> None:
        now = time.monotonic()
        if self.dev is None:
            if now < self.next_open:
                return
            if not self.open():
                self.next_open = now + 2.0
                return
        assert self.dev is not None
        try:
            cap2 = self.dev.lnk_cap2()
            sta = self.dev.lnk_sta()
        except OSError:
            self.was_gen2 = None
            self.open()
            return
        declared = (cap2 & 0x6) == 0x6
        gen2 = (sta & 0xF) >= 2
        now = time.monotonic()

        if self.was_gen2 is not gen2:
            if gen2:
                if not self.gen2_logged:
                    LOG.info("%s: Gen2 active (%s)", self.bdf, link_name(sta))
                    self.gen2_logged = True
                else:
                    LOG.debug("%s: Gen2 recovered (%s)", self.bdf, link_name(sta))
            else:
                LOG.debug("%s: link is %s - recovering", self.bdf, link_name(sta))
            self.was_gen2 = gen2

        if not gen2:
            if self.recovery_since is None:
                self.recovery_since = now
            elif (now - self.recovery_since >= RECOVERY_WARN_SEC
                  and now - self.last_recovery_warn >= RECOVERY_WARN_GAP):
                self.last_recovery_warn = now
                LOG.warning("%s: still %s after %.0f s - recovery failing",
                            self.bdf, link_name(sta),
                            now - self.recovery_since)
        else:
            self.recovery_since = None
            self.last_recovery_warn = 0.0

        if not declared:
            if not self.declared_lost:
                LOG.debug("%s: Gen2 declaration lost - replaying full group",
                          self.bdf)
                self.declared_lost = True
            if now - self.last_group >= self.group_gap:
                self.last_group = now
                self.full_group()
            return
        self.declared_lost = False

        try:
            current = self.dev.bar.read32(OFF_LINKCFG0)
        except OSError:
            current = 0xFFFFFFFF
        if current == 0xFFFFFFFF:
            self.lost += 1
            if self.lost >= 3:
                if not self.mmio_warned:
                    LOG.warning("%s: MMIO reads 0xFFFFFFFF x3 - remapping",
                                self.bdf)
                    self.mmio_warned = True
                self.was_gen2 = None
                self.close()
                time.sleep(1.5)
                self.open()
            return
        self.lost = 0
        self.mmio_warned = False

        # An armed x16 declaration blocks speed negotiation.  Roll back to
        # the factory-safe whole word before recovering a Gen1 link; the
        # width is re-armed once Gen2 is stable again.
        if not gen2 and (current & LINKCFG0_ARM_BIT):
            try:
                self.dev.bar.write32(OFF_LINKCFG0, LINKCFG0_SAFE)
                current = LINKCFG0_SAFE
                LOG.debug("%s: Gen1 with x16 armed - restored 0x8C040=0x%08X",
                         self.bdf, LINKCFG0_SAFE)
            except OSError as exc:
                LOG.error("%s: cannot disarm x16: %s", self.bdf, exc)
                return

        # Fast path: restore only the speed request (C# WriteSpeedGen2).
        # Read -> write happens without any logging in between.
        if (current & SPEED_MASK) != SPEED_GEN2 and now - self.last_speed >= self.speed_gap:
            new = (current & ~SPEED_MASK) | SPEED_GEN2
            try:
                self.dev.bar.write32(OFF_LINKCFG0, new)
                self.last_speed = now
                LOG.debug("%s: 0x8C040 speed 0x%08X -> 0x%08X (fast path)",
                         self.bdf, current, new)
            except OSError as exc:
                LOG.error("%s: 0x8C040 speed write failed: %s", self.bdf, exc)

        if not gen2:
            if self.low_since is None:
                self.low_since = now
                self.last_retrain = 0.0
            if now - self.last_retrain >= self.retrain_gap:
                _write_tls(self.dev)
                _trigger_retrain(self.dev)
                self.last_retrain = now
                LOG.debug("%s: Gen1 - TLS+retrain (0x%04X)", self.bdf, sta)
            if now - self.low_since >= self.group_retry:
                self.low_since = now
                self.last_group = now
                self.full_group(f"Gen1 for {self.group_retry:.0f}s")
        else:
            self.low_since = None
            if self.x16:
                self.maybe_arm_width(now)

    def maybe_arm_width(self, now: float) -> None:
        """Arm and retrain the opt-in x16 declaration once Gen2 is stable."""
        if self.width_fails >= MAX_WIDTH_FAILS:
            return
        if now - self.last_width < WIDTH_RETRY_GAP:
            return
        try:
            cap = self.dev.lnk_cap()
        except OSError:
            return
        if is_valid(cap, 32) and ((cap >> 4) & 0x3F) == 16:
            return
        self.last_width = now
        if not arm_width(self.dev):
            self.width_fails += 1
            if self.width_fails >= MAX_WIDTH_FAILS:
                LOG.warning("%s: x16 arm failed %d times - disabling for this process",
                            self.bdf, self.width_fails)
            return
        if now - self.last_width_retrain < WIDTH_RETRAIN_GAP:
            return
        self.last_width_retrain = now
        if retrain_width(self.dev):
            self.width_fails = 0
        else:
            self.width_fails += 1
            if self.width_fails >= MAX_WIDTH_FAILS:
                LOG.warning("%s: physical x16 not reached %d times - keeping the "
                            "declaration armed, stopping width retraining",
                            self.bdf, self.width_fails)


def run_watchdog(bdfs: List[str], tick: float, group_gap: float,
                 retrain_gap: float, speed_gap: float, group_retry: float,
                 stop: threading.Event, x16: bool = False) -> int:
    if not bdfs:
        LOG.error("no NVIDIA P104-100 (10DE:1B87) found")
        return 4
    keepers = [Keeper(bdf, tick, group_gap, retrain_gap, speed_gap, group_retry,
                      x16) for bdf in bdfs]
    LOG.debug("watchdog: %s, tick %.0f ms, retrain gap %.0f ms, speed gap %.0f ms, x16=%s",
             ", ".join(bdfs), tick * 1000.0, retrain_gap * 1000.0,
             speed_gap * 1000.0, "on" if x16 else "off")
    while not stop.is_set():
        cycle_start = time.monotonic()
        for keeper in keepers:
            if stop.is_set():
                break
            try:
                keeper.poll()
            except Exception as exc:  # keep the loop alive
                LOG.error("%s: poll failed: %s", keeper.bdf, exc)
        elapsed = time.monotonic() - cycle_start
        stop.wait(max(0.0, tick - elapsed))
    for keeper in keepers:
        keeper.close()
    LOG.info("watchdog stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P104-100 PCIe Gen2 unlock and keep-alive daemon")
    parser.add_argument("--status", action="store_true",
                        help="print link state for every P104-100 and exit")
    parser.add_argument("--once", action="store_true",
                        help="run the unlock group once per card and exit")
    parser.add_argument("--interval", type=float, default=TICK_DEFAULT,
                        help="hold-loop tick in seconds (default 0.02 = 20 ms)")
    parser.add_argument("--min-gap", type=float, default=GROUP_GAP,
                        help="minimum seconds between full-group replays (default 0.2)")
    parser.add_argument("--retrain-gap", type=float, default=RETRAIN_GAP,
                        help="minimum seconds between retrain triggers (default 0.02)")
    parser.add_argument("--speed-gap", type=float, default=SPEED_GAP,
                        help="minimum seconds between 0x8C040 speed rewrites (default 0.2)")
    parser.add_argument("--group-retry", type=float, default=GROUP_RETRY,
                        help="replay the full group after this many seconds of Gen1 (default 6)")
    parser.add_argument("--x16", action="store_true",
                        help="also arm the x16 width declaration (default off; "
                             "unstable without AC coupling capacitors)")
    parser.add_argument("--stagger", type=float, default=1.0,
                        help="delay between cards during --once (default 1.0)")
    parser.add_argument("--bdf", action="append", default=[],
                        help="restrict to this BDF (repeatable)")
    parser.add_argument("--force", action="store_true",
                        help="run even if another instance holds the instance "
                             "lock (unsafe)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    if args.status:
        return cmd_status()

    lock = InstanceLock()
    try:
        if not lock.acquire(args.force):
            return EXIT_ALREADY_RUNNING
    except OSError as exc:
        LOG.error("cannot acquire instance lock %s: %s", lock.path, exc)
        return EXIT_ALREADY_RUNNING

    try:
        bdfs = filter_bdf(discover(), args.bdf)
        if bdfs:
            LOG.info("found %d P104-100 card(s): %s", len(bdfs), ", ".join(bdfs))
        if args.once:
            return run_once(bdfs, args.stagger, args.x16)

        stop = threading.Event()

        def _stop(signum: int, _frame: object) -> None:
            LOG.info("received signal %d - stopping", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        return run_watchdog(bdfs, args.interval, args.min_gap, args.retrain_gap,
                            args.speed_gap, args.group_retry, stop, args.x16)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
