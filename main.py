#!/usr/bin/env python3

import sys
from pathlib import Path
from typing import Dict, List, Any

SYS_BLOCK = Path("/sys/block")
PROC_MOUNTS = Path("/proc/mounts")


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return default


def read_int(path: Path, default: int = 0) -> int:
    text = read_text(path, "")
    try:
        return int(text)
    except ValueError:
        return default


def read_bool(path: Path, default: bool = False) -> bool:
    text = read_text(path, "")
    return text == "1" if text else default


def human_size(num_bytes: int) -> str:
    units = ["B", "K", "M", "G", "T", "P"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0:
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}E"


def get_mounts() -> Dict[str, List[str]]:
    mounts: Dict[str, List[str]] = {}
    try:
        with PROC_MOUNTS.open() as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    device = parts[0]
                    mountpoint = parts[1]
                    if device.startswith("/dev/"):
                        try:
                            resolved = Path(device).resolve()
                            if resolved != Path(device):
                                device = str(resolved)
                        except OSError:
                            pass
                    mounts.setdefault(device, []).append(mountpoint)
    except OSError:
        pass
    return mounts


def get_device_slaves(dev_path: Path) -> List[str]:
    slaves: List[str] = []
    slaves_path = dev_path / "slaves"
    if slaves_path.exists():
        for slave in sorted(slaves_path.iterdir(), key=lambda p: p.name):
            if slave.is_dir():
                slaves.append(slave.name)
    return slaves


def device_size_bytes(dev_path: Path) -> int:
    sectors = read_int(dev_path / "size", 0)
    return sectors * 512


def get_device_type(dev_path: Path) -> str:
    if (dev_path / "partition").exists():
        return "part"
    if (dev_path / "dm").exists():
        return "dm"
    if (dev_path / "md").exists():
        return "raid"
    if (dev_path / "loop").exists():
        return "loop"
    return "disk"


def get_parent_from_sysfs(dev_name: str) -> str:
    dev_path = SYS_BLOCK / dev_name
    
    if not (dev_path / "partition").exists():
        return ""
    
    try:
        for parent_name in SYS_BLOCK.iterdir():
            if not parent_name.is_dir():
                continue
            
            parent_path = SYS_BLOCK / parent_name.name
            for child in parent_path.iterdir():
                if child.is_dir() and child.name == dev_name:
                    return parent_name.name
    except OSError:
        pass
    
    return ""


def get_device_info(dev_name: str) -> Dict[str, Any]:
    dev_path = SYS_BLOCK / dev_name
    
    info: Dict[str, Any] = {
        "name": dev_name,
        "size": human_size(device_size_bytes(dev_path)),
        "type": get_device_type(dev_path),
        "ro": read_bool(dev_path / "ro"),
        "rm": read_bool(dev_path / "removable"),
        "slaves": get_device_slaves(dev_path),
        "children": [],
        "parent": "",
    }
    
    parent = get_parent_from_sysfs(dev_name)
    if parent:
        info["parent"] = parent
    
    return info


def build_topology(devices: Dict[str, Any]) -> List[Dict[str, Any]]:
    for dev_info in devices.values():
        dev_info["children"] = []
    
    for dev_name, dev_info in devices.items():
        if dev_info["type"] == "part" and not dev_info["parent"]:
            for candidate in devices:
                candidate_path = SYS_BLOCK / candidate
                if (candidate_path / dev_name).exists():
                    dev_info["parent"] = candidate
                    break
    
    for dev_info in devices.values():
        if dev_info["parent"]:
            continue
        for slave_name in dev_info["slaves"]:
            if slave_name in devices:
                dev_info["parent"] = slave_name
                break
    
    for dev_info in devices.values():
        parent = dev_info.get("parent")
        if parent and parent in devices:
            devices[parent]["children"].append(dev_info)
    
    roots = [dev_info for dev_info in devices.values() if not dev_info.get("parent") or dev_info["parent"] not in devices]
    
    for dev_info in devices.values():
        dev_info["children"].sort(key=lambda x: x["name"])
    
    roots.sort(key=lambda x: x["name"])
    return roots


def get_mount_string(mounts: Dict[str, List[str]], dev_name: str) -> str:
    dev_path = Path("/dev") / dev_name
    try:
        resolved = str(dev_path.resolve())
        if resolved in mounts:
            return mounts[resolved][0]
    except OSError:
        pass
    
    kernel_name = f"/dev/{dev_name}"
    if kernel_name in mounts:
        return mounts[kernel_name][0]
    
    return ""


def format_tree(devices: List[Dict[str, Any]], mounts: Dict[str, List[str]], prefix: str = "") -> List[str]:
    lines: List[str] = []
    
    for idx, dev in enumerate(devices):
        is_last = (idx == len(devices) - 1)
        
        if prefix:
            connector = "└─" if is_last else "├─"
            display_name = f"{prefix}{connector}{dev['name']}"
        else:
            display_name = dev["name"]
        
        mountpoint = get_mount_string(mounts, dev["name"])
        
        rm_flag = "1" if dev.get("rm", False) else "0"
        ro_flag = "1" if dev.get("ro", False) else "0"
        
        line = f"{display_name:<24} {dev['size']:<8} {dev['type']:<8} {ro_flag:<2} {rm_flag:<2} {mountpoint}"
        lines.append(line.rstrip())
        
        if dev.get("children"):
            if prefix:
                child_prefix = prefix + ("    " if is_last else "│   ")
            else:
                child_prefix = "    " if is_last else "│   "
            child_lines = format_tree(dev["children"], mounts, child_prefix)
            lines.extend(child_lines)
    
    return lines


def print_tree(devices: List[Dict[str, Any]], mounts: Dict[str, List[str]]) -> None:
    print(f"{'NAME':<24} {'SIZE':<8} {'TYPE':<8} {'RO':<2} {'RM':<2} MOUNTPOINT")
    lines = format_tree(devices, mounts)
    for line in lines:
        print(line)


def get_all_devices() -> List[str]:
    devices: List[str] = []
    try:
        for entry in SYS_BLOCK.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                devices.append(entry.name)
    except OSError as e:
        print(f"Error reading /sys/block: {e}", file=sys.stderr)
        return []
    return sorted(devices)


def filter_excluded_devices(devices: List[str]) -> List[str]:
    excluded = ["loop", "ram"]
    filtered: List[str] = []
    for dev in devices:
        should_exclude = False
        for ex in excluded:
            if dev.startswith(ex):
                should_exclude = True
                break
        if not should_exclude:
            filtered.append(dev)
    return filtered


def main() -> int:
    mounts = get_mounts()
    
    all_devices = get_all_devices()
    if not all_devices:
        return 1
    
    filtered_devices = filter_excluded_devices(all_devices)
    
    devices_dict: Dict[str, Any] = {}
    for dev_name in filtered_devices:
        try:
            devices_dict[dev_name] = get_device_info(dev_name)
        except Exception as e:
            print(f"Error processing device {dev_name}: {e}", file=sys.stderr)
            continue
    
    root_devices = build_topology(devices_dict)
    print_tree(root_devices, mounts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
