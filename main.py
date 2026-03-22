#!/usr/bin/env python3

import sys
import json
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional, Iterator
from dataclasses import dataclass, field, asdict
from functools import lru_cache

SYS_BLOCK = Path("/sys/block")
PROC_MOUNTS = Path("/proc/mounts")


@dataclass
class Device:
    name: str
    size: int = 0
    type: str = "disk"
    ro: bool = False
    rm: bool = False
    model: str = ""
    label: str = ""
    uuid: str = ""
    parent: str = ""
    slaves: List[str] = field(default_factory=list)
    children: List['Device'] = field(default_factory=list)
    
    @property
    def size_human(self) -> str:
        if self.size == 0:
            return "0B"
        units = ["B", "K", "M", "G", "T", "P"]
        size = float(self.size)
        for unit in units:
            if size < 1024.0:
                return f"{size:.1f}{unit}"
            size /= 1024.0
        return f"{size:.1f}E"
    
    @property
    def flags(self) -> str:
        flags = []
        if self.ro:
            flags.append("ro")
        if self.rm:
            flags.append("rm")
        return ",".join(flags) if flags else "-"


class BlockDeviceScanner:
    def __init__(self, exclude_patterns: List[str] = None):
        self.exclude_patterns = exclude_patterns or ["loop", "ram"]
        self._devices: Dict[str, Device] = {}
    
    def scan(self) -> Dict[str, Device]:
        for dev_name in self._iter_block_devices():
            if self._should_exclude(dev_name):
                continue
            self._devices[dev_name] = self._read_device(dev_name)
        
        self._build_relationships()
        return self._devices
    
    def _iter_block_devices(self) -> Iterator[str]:
        try:
            for entry in SYS_BLOCK.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    yield entry.name
        except OSError as e:
            print(f"Error reading /sys/block: {e}", file=sys.stderr)
    
    def _should_exclude(self, name: str) -> bool:
        return any(name.startswith(p) for p in self.exclude_patterns)
    
    def _read_device(self, name: str) -> Device:
        path = SYS_BLOCK / name
        return Device(
            name=name,
            size=self._read_size(path),
            type=self._read_type(path),
            ro=self._read_bool(path / "ro"),
            rm=self._read_bool(path / "removable"),
            model=self._read_text(path / "device/model"),
            slaves=self._read_slaves(path),
        )
    
    def _read_size(self, path: Path) -> int:
        sectors = self._read_int(path / "size")
        return sectors * 512
    
    def _read_type(self, path: Path) -> str:
        if (path / "partition").exists():
            return "part"
        if (path / "dm").exists():
            return "dm"
        if (path / "md").exists():
            return "raid"
        return "disk"
    
    def _read_text(self, path: Path, default: str = "") -> str:
        try:
            return path.read_text().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return default
    
    def _read_int(self, path: Path, default: int = 0) -> int:
        text = self._read_text(path)
        try:
            return int(text)
        except ValueError:
            return default
    
    def _read_bool(self, path: Path) -> bool:
        return self._read_text(path) == "1"
    
    def _read_slaves(self, path: Path) -> List[str]:
        slaves_path = path / "slaves"
        if not slaves_path.exists():
            return []
        return sorted(
            s.name for s in slaves_path.iterdir() if s.is_dir()
        )
    
    def _build_relationships(self) -> None:
        for dev in self._devices.values():
            for slave in dev.slaves:
                if slave in self._devices:
                    dev.parent = slave
                    break
        
        for dev in self._devices.values():
            if dev.type == "part" and not dev.parent:
                for parent in self._devices.values():
                    if (SYS_BLOCK / parent.name / dev.name).exists():
                        dev.parent = parent.name
                        break
        
        for dev in self._devices.values():
            if dev.parent and dev.parent in self._devices:
                self._devices[dev.parent].children.append(dev)
        
        for dev in self._devices.values():
            dev.children.sort(key=lambda d: d.name)


class MountScanner:
    def __init__(self):
        self._mounts: Dict[str, List[str]] = {}
    
    def scan(self) -> Dict[str, List[str]]:
        try:
            with PROC_MOUNTS.open() as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        device = self._resolve_device(parts[0])
                        self._mounts.setdefault(device, []).append(parts[1])
        except OSError:
            pass
        return self._mounts
    
    def _resolve_device(self, device: str) -> str:
        if not device.startswith("/dev/"):
            return device
        try:
            resolved = Path(device).resolve()
            return str(resolved) if resolved != Path(device) else device
        except OSError:
            return device


class TreeFormatter:
    def __init__(self, mounts: Dict[str, List[str]], width: int = 24):
        self.mounts = mounts
        self.width = width
    
    def format(self, devices: List[Device], prefix: str = "") -> List[str]:
        lines = []
        for i, dev in enumerate(devices):
            is_last = i == len(devices) - 1
            line = self._format_device(dev, prefix, is_last)
            lines.append(line)
            
            if dev.children:
                child_prefix = prefix + ("    " if is_last else "│   ")
                lines.extend(self.format(dev.children, child_prefix))
        return lines
    
    def _format_device(self, dev: Device, prefix: str, is_last: bool) -> str:
        if prefix:
            connector = "└─" if is_last else "├─"
            name_part = f"{prefix}{connector}{dev.name}"
        else:
            name_part = dev.name
        
        mount = self._get_mount(dev.name)
        flags = dev.flags
        
        return (
            f"{name_part:<{self.width}} "
            f"{dev.size_human:>8} "
            f"{dev.type:<6} "
            f"{flags:<4} "
            f"{mount}"
        ).rstrip()
    
    def _get_mount(self, name: str) -> str:
        dev_path = Path("/dev") / name
        try:
            resolved = str(dev_path.resolve())
            if resolved in self.mounts:
                return self.mounts[resolved][0]
        except OSError:
            pass
        return self.mounts.get(f"/dev/{name}", [""])[0]


class JsonFormatter:
    def __init__(self, mounts: Dict[str, List[str]]):
        self.mounts = mounts
    
    def format(self, devices: List[Device], pretty: bool = False) -> str:
        data = [self._device_to_dict(d) for d in devices]
        indent = 2 if pretty else None
        return json.dumps(data, indent=indent)
    
    def _device_to_dict(self, dev: Device) -> Dict[str, Any]:
        result = {
            "name": dev.name,
            "size": dev.size,
            "size_human": dev.size_human,
            "type": dev.type,
            "read_only": dev.ro,
            "removable": dev.rm,
            "mountpoints": self._get_mounts(dev.name),
        }
        if dev.model:
            result["model"] = dev.model
        if dev.label:
            result["label"] = dev.label
        if dev.uuid:
            result["uuid"] = dev.uuid
        if dev.children:
            result["children"] = [self._device_to_dict(c) for c in dev.children]
        return result
    
    def _get_mounts(self, name: str) -> List[str]:
        dev_path = Path("/dev") / name
        try:
            resolved = str(dev_path.resolve())
            if resolved in self.mounts:
                return self.mounts[resolved]
        except OSError:
            pass
        return self.mounts.get(f"/dev/{name}", [])


class SimpleFormatter:
    def __init__(self, mounts: Dict[str, List[str]]):
        self.mounts = mounts
    
    def format(self, devices: List[Device]) -> List[str]:
        return [f"/dev/{d.name}" for d in devices if not d.parent]


def get_roots(devices: Dict[str, Device]) -> List[Device]:
    roots = [d for d in devices.values() if not d.parent]
    roots.sort(key=lambda d: d.name)
    return roots


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lsblk",
        description="List block devices",
        epilog="Examples:\n  lsblk              Show device tree\n  lsblk -o json      JSON output\n  lsblk -s 1G        Show devices >= 1GB\n  lsblk -t disk      Show only disks"
    )
    
    parser.add_argument("-o", "--output", choices=["tree", "json", "simple"], default="tree")
    parser.add_argument("-p", "--pretty", action="store_true", help="Pretty print JSON")
    parser.add_argument("-s", "--min-size", type=str, help="Minimum size (e.g., 1G, 500M)")
    parser.add_argument("-t", "--type", action="append", dest="types", help="Filter by type (disk, part, dm, raid)")
    parser.add_argument("-x", "--exclude", action="append", default=["loop", "ram"], help="Exclude devices by prefix")
    parser.add_argument("-w", "--width", type=int, default=24, help="Name column width")
    parser.add_argument("--no-headers", action="store_true", help="Suppress headers")
    
    return parser.parse_args()


def parse_size(size_str: str) -> int:
    if not size_str:
        return 0
    size_str = size_str.upper().strip()
    multipliers = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    for suffix, mult in multipliers.items():
        if size_str.endswith(suffix):
            try:
                return int(float(size_str[:-len(suffix)]) * mult)
            except ValueError:
                pass
    try:
        return int(size_str)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid size: {size_str}")


def filter_by_size(devices: List[Device], min_size: int) -> List[Device]:
    def should_include(dev: Device) -> bool:
        if dev.size >= min_size:
            return True
        if dev.children:
            filtered_children = [c for c in dev.children if should_include(c)]
            if filtered_children:
                dev.children = filtered_children
                return True
        return False
    
    return [d for d in devices if should_include(d)]


def filter_by_type(devices: List[Device], types: List[str]) -> List[Device]:
    def should_include(dev: Device) -> bool:
        if dev.type in types:
            return True
        if dev.children:
            filtered_children = [c for c in dev.children if should_include(c)]
            if filtered_children:
                dev.children = filtered_children
                return True
        return False
    
    return [d for d in devices if should_include(d)]


def print_headers(formatter_type: str) -> None:
    if formatter_type == "tree":
        print(f"{'NAME':<24} {'SIZE':>8} {'TYPE':<6} {'FLAGS':<4} MOUNTPOINT")


def main() -> int:
    args = parse_args()
    
    scanner = BlockDeviceScanner(exclude_patterns=args.exclude)
    devices = scanner.scan()
    
    mount_scanner = MountScanner()
    mounts = mount_scanner.scan()
    
    roots = get_roots(devices)
    
    if args.min_size:
        min_bytes = parse_size(args.min_size)
        roots = filter_by_size(roots, min_bytes)
    
    if args.types:
        roots = filter_by_type(roots, args.types)
    
    if args.output == "json":
        formatter = JsonFormatter(mounts)
        print(formatter.format(roots, pretty=args.pretty))
        return 0
    
    if args.output == "simple":
        formatter = SimpleFormatter(mounts)
        for line in formatter.format(roots):
            print(line)
        return 0
    
    if not args.no_headers:
        print_headers("tree")
    
    formatter = TreeFormatter(mounts, width=args.width)
    for line in formatter.format(roots):
        print(line)
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
