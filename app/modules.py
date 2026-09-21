# SPDX-License-Identifier: MIT
"""Optional backend modules (docs/module-protocol.md): discovery and validation of `module.json` manifests. A module is a folder with a manifest and executables that
speak the file protocol; the core never imports or links module code.

    python3 app/modules.py          # list the modules found and why any of them is not usable
"""
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

PROTOCOL = 1
BUILTIN_KEYS = {"none", "nis", "fsr", "rtx_vsr", "dlssg", "fsr3", "blend"}


@dataclass
class Module:
    name: str
    title: str
    license: str
    root: str
    framegen: list = field(default_factory=list)     # manifest entries (dicts)
    upscalers: list = field(default_factory=list)
    check: list = field(default_factory=list)
    problems: list = field(default_factory=list)      # why it is not usable (empty = usable)

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def command(self, entry: dict) -> list:
        exe = entry["exec"]
        return [self.path(exe[0]), *exe[1:]]


def search_dirs() -> list:
    dirs = [os.path.join(paths.ROOT, "modules")]
    dirs += [d for d in os.environ.get("NS_MODULES_PATH", "").split(":") if d]
    dirs.append(os.path.expanduser("~/.local/share/ns-dlss/modules"))
    return dirs


def _load(root: str) -> Module:
    with open(os.path.join(root, "module.json"), encoding="utf-8") as f:
        m = json.load(f)
    mod = Module(name=str(m.get("name", os.path.basename(root))), title=str(m.get("title", m.get("name", ""))), license=str(m.get("license", "?")), root=root,
                 framegen=list(m.get("framegen", [])), upscalers=list(m.get("upscalers", [])), check=list(m.get("check", [])))
    if m.get("module_version") != PROTOCOL:
        mod.problems.append(f"module_version {m.get('module_version')!r} is not supported (this core speaks {PROTOCOL})")
        return mod
    for kind, entries in (("framegen", mod.framegen), ("upscalers", mod.upscalers)):
        for e in entries:
            if not isinstance(e.get("key"), str) or not isinstance(e.get("exec"), list) or not e["exec"] or not isinstance(e.get("title"), str):
                mod.problems.append(f"{kind}: every entry needs string `key` and `title` and a non-empty `exec` list")
                continue
            if e["key"] in BUILTIN_KEYS:
                mod.problems.append(f"{kind} key {e['key']!r} collides with a built-in backend")
            elif not os.access(mod.path(e["exec"][0]), os.X_OK):
                mod.problems.append(f"{kind} {e['key']!r}: executable {e['exec'][0]} is missing (build the module: see its README)")
    if not mod.problems and mod.check:
        try:
            r = subprocess.run([mod.path(mod.check[0]), *mod.check[1:]], cwd=root, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                lines = (r.stdout + r.stderr).strip().splitlines()
                mod.problems.append("check failed: " + (lines[0] if lines else f"exit {r.returncode}"))
        except (OSError, subprocess.SubprocessError) as exc:
            mod.problems.append(f"check could not run: {exc}")
    return mod


def discover() -> list:
    """Every module found (usable or not), first occurrence of a name wins."""
    seen, out = set(), []
    for base in search_dirs():
        if not os.path.isdir(base):
            continue
        for d in sorted(os.listdir(base)):
            root = os.path.abspath(os.path.join(base, d))
            if not os.path.isfile(os.path.join(root, "module.json")):
                continue
            try:
                mod = _load(root)
            except (OSError, ValueError) as exc:
                mod = Module(name=d, title=d, license="?", root=root, problems=[f"unreadable manifest: {exc}"])
            if mod.name not in seen:
                seen.add(mod.name)
                out.append(mod)
    return out


_CACHE = None


def usable() -> list:
    global _CACHE
    if _CACHE is None:
        _CACHE = [m for m in discover() if not m.problems]
    return _CACHE


def main() -> int:
    mods = discover()
    if not mods:
        print("no modules found in: " + ", ".join(search_dirs()))
    for m in mods:
        keys = [e["key"] for e in m.framegen if "key" in e] + [e["key"] for e in m.upscalers if "key" in e]
        print(f"[{'ok' if not m.problems else 'UNUSABLE'}] {m.name} ({m.license}) at {m.root}: {', '.join(keys) or 'no backends'}")
        for p in m.problems:
            print("      -", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
