"""Shared bubblewrap policy, independent of controller and toolkits."""
import os
from pathlib import Path

def _existing_system_paths() -> list[str]:
    # Deliberately do not bind /home, /root, /mnt, /media, /run or the host cwd.
    candidates = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
    return [path for path in candidates if os.path.exists(path)]


def _nvidia_compute_devices() -> list[Path]:
    """Only compute nodes, freshly discovered; never mount the host /dev directory."""
    gpus = sorted(path for path in Path("/dev").glob("nvidia[0-9]*")
                  if path.name[6:].isascii() and path.name[6:].isdigit())
    if not gpus:
        raise OSError("GPU access requested but no numbered NVIDIA device nodes are present")
    devices = [Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm"), *gpus]
    for path in devices:
        if path.is_symlink() or not path.is_char_device():
            raise OSError(f"GPU access requires a host NVIDIA character device: {path}")
    return devices


def build_bwrap_command(
    workdir: Path,
    shell_command: str,
    *,
    network: bool,
    gpu: bool = False,
) -> list[str]:
    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if network:
        args.append("--share-net")

    for path in _existing_system_paths():
        args += ["--ro-bind", path, path]

    if network:
        # /etc is read-only, but resolv.conf may point outside it into omitted /run.
        # Share-net includes host loopback, so its stub resolver remains reachable.
        # Bind only the resolved regular file, freshly resolved for each command;
        # never expose the host /run directory or its service sockets.
        resolver = Path("/etc/resolv.conf")
        target = resolver.resolve(strict=True)
        if not target.is_file():
            raise OSError("host /etc/resolv.conf does not resolve to a regular file")
        if target != resolver:
            args += ["--ro-bind", str(target), str(target)]

    args += [
        "--dir", "/opt/pavlusha/bin",
        "--ro-bind", os.path.join(os.path.dirname(__file__), "workspace_tools.py"),
        "/opt/pavlusha/bin/pavlusha-tools",
        "--proc", "/proc",
        "--dev", "/dev",
    ]
    if gpu:
        for path in _nvidia_compute_devices():
            args += ["--dev-bind", str(path), str(path)]
    args += [
        "--tmpfs", "/tmp",
        "--bind", str(workdir), "/work",
        "--chdir", "/work",
        "--clearenv",
        "--setenv", "HOME", "/work",
        "--setenv", "USER", "worker",
        "--setenv", "LOGNAME", "worker",
        "--setenv", "PATH", "/opt/pavlusha/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "XDG_CACHE_HOME", "/work/.cache",
        "--setenv", "PIP_CACHE_DIR", "/work/.cache/pip",
        "--setenv", "NPM_CONFIG_CACHE", "/work/.cache/npm",
        "--setenv", "DEBIAN_FRONTEND", "noninteractive",
        "/bin/bash", "--noprofile", "--norc", "-o", "pipefail", "-lc", shell_command,
    ]
    return args
