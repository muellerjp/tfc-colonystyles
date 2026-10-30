"""Validate the structure packs and package them together as one NeoForge mod jar.

Usage:
  python tools/pack.py validate [--pack FOLDER ...]
  python tools/pack.py build [--pack FOLDER ...] [--version X.Y.Z] [--out dist]

Every top-level folder that contains a pack.json is a pack (except IGNORED_DIRS).
All packs go into the same jar:

  META-INF/neoforge.mods.toml    (repo root, placeholders filled in)
  pack.mcmeta                    (repo root)
  logo.png                       (from LOGO in the repo root, the mod's logo)
  blueprints/<modId>/<PackFolder>/pack.json      ("icon" set to logo.png)
  blueprints/<modId>/<PackFolder>/logo.png       (the PNG named by "icon" in the pack's pack.json)
  blueprints/<modId>/<PackFolder>/<category folders>/*.blueprint

<modId> comes from META-INF/neoforge.mods.toml; <PackFolder> is the pack's folder name as is.
Other files in a pack folder (extra logo images, notes) are not packaged.

Uses only the Python standard library.
"""

import argparse
import gzip
import json
import re
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORED_DIRS = {"mr101freaking", "old-VerdigrisFoundry", "tools", "dist", "META-INF"}  # never treated as packs
JAR_NAME = "TFCColonyStyles-{version}.jar"  # {version} comes from the git tag
MINECRAFT_VERSION = "1.21.1"
NEO_VERSION_RANGE = "[21.1,)"
MODS_TOML = "META-INF/neoforge.mods.toml"
JAR_FILES = [MODS_TOML, "pack.mcmeta"]  # same path in repo root and jar
LOGO = ROOT / "logo_square.png"  # packaged as logo.png (logoFile in neoforge.mods.toml)
PACK_ICON = "logo.png"  # name of each style pack's icon inside the jar
REQUIRED_FIELDS = {"version": int, "pack-format": int, "name": str, "desc": str, "authors": list, "mods": list}
# Mods a blueprint may need without being listed in pack.json.
BASE_MODS = {"minecraft", "structurize", "minecolonies", "domum_ornamentum"}
# Files in a pack folder that are not packaged and not warned about.
IGNORED_SUFFIXES = {".png", ".jpg", ".jpeg", ".md", ".txt"}


# --- Minimal NBT reader -------------------------------------------------------

class NbtReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ValueError("unexpected end of data")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def unpack(self, fmt: str):
        return struct.unpack(">" + fmt, self.take(struct.calcsize(">" + fmt)))[0]

    def string(self) -> str:
        return self.take(self.unpack("H")).decode("utf-8", errors="replace")

    def payload(self, tag: int):
        simple = {1: "b", 2: "h", 3: "i", 4: "q", 5: "f", 6: "d"}
        if tag in simple:
            return self.unpack(simple[tag])
        if tag == 7:
            return self.take(self.unpack("i"))
        if tag == 8:
            return self.string()
        if tag == 9:
            item_tag = self.unpack("B")
            return [self.payload(item_tag) for _ in range(self.unpack("i"))]
        if tag == 10:
            result = {}
            while (child := self.unpack("B")) != 0:
                name = self.string()
                result[name] = self.payload(child)
            return result
        if tag == 11:
            return [self.unpack("i") for _ in range(self.unpack("i"))]
        if tag == 12:
            return [self.unpack("q") for _ in range(self.unpack("i"))]
        raise ValueError(f"unknown NBT tag {tag}")

    def root(self) -> dict:
        if self.unpack("B") != 10:
            raise ValueError("root tag is not a compound")
        self.string()
        result = self.payload(10)
        if self.pos != len(self.data):
            raise ValueError("trailing data after root tag")
        return result


# --- Packs --------------------------------------------------------------------

class Pack:
    def __init__(self, folder: Path):
        self.folder = folder
        self.meta = {}

    @property
    def label(self) -> str:
        return self.folder.name

    @property
    def icon(self):
        """The pack's icon file named in pack.json, or None if not set."""
        name = self.meta.get("icon") or ""
        return self.folder / name if name else None

    def content_files(self):
        """Blueprint files that go into the jar (pack.json and the icon are written separately).

        Yields (path, rel); rel is None for unexpected files that are not packaged.
        """
        for path in sorted(self.folder.rglob("*")):
            rel = path.relative_to(self.folder)
            if not path.is_file() or any(p.startswith(".") for p in rel.parts) or rel.as_posix() == "pack.json":
                continue
            if path.suffix == ".blueprint":
                yield path, rel
            elif path.suffix.lower() not in IGNORED_SUFFIXES:
                yield path, None


def find_packs(selected=None):
    packs = []
    for folder in sorted(ROOT.iterdir()):
        if not folder.is_dir() or folder.name.startswith(".") or folder.name in IGNORED_DIRS:
            continue
        if not (folder / "pack.json").is_file():
            continue
        if selected and folder.name not in selected:
            continue
        packs.append(Pack(folder))
    return packs


def read_mod_id():
    """modId of the first [[mods]] entry in the root neoforge.mods.toml, or None."""
    try:
        text = (ROOT / MODS_TOML).read_text(encoding="utf-8")
    except OSError:
        return None
    section = text.split("[[mods]]", 1)
    if len(section) < 2:
        return None
    match = re.search(r'^\s*modId\s*=\s*"([^"]+)"', section[1], re.MULTILINE)
    return match.group(1) if match else None


# --- Checks -------------------------------------------------------------------

def validate_pack(pack: Pack) -> int:
    errors, warnings = [], []
    label = pack.label

    try:
        pack.meta = json.loads((pack.folder / "pack.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR {label}/pack.json: {exc}")
        return 1

    for field, kind in REQUIRED_FIELDS.items():
        if not isinstance(pack.meta.get(field), kind):
            errors.append(f"{label}/pack.json: '{field}' is missing or not a {kind.__name__}")
    allowed_mods = BASE_MODS | set(pack.meta.get("mods") or [])

    count = 0
    for path, rel in pack.content_files():
        shown = f"{label}/{path.relative_to(pack.folder).as_posix()}"
        if rel is None:
            warnings.append(f"{shown}: unexpected file, not packaged")
            continue
        count += 1
        try:
            nbt = NbtReader(gzip.decompress(path.read_bytes())).root()
        except (OSError, EOFError, ValueError, struct.error) as exc:
            errors.append(f"{shown}: not a valid blueprint ({exc})")
            continue
        for key in ("size_x", "size_y", "size_z"):
            if not isinstance(nbt.get(key), int) or nbt[key] <= 0:
                errors.append(f"{shown}: missing or invalid {key}")
        missing = set(nbt.get("required_mods") or []) - allowed_mods
        if missing:
            errors.append(f"{shown}: needs mods not listed in pack.json: {', '.join(sorted(missing))}")

    if count == 0:
        errors.append(f"{label}: no .blueprint files found")

    icon = pack.icon
    if icon is None:
        errors.append(f"{label}/pack.json: 'icon' is not set (it becomes the pack's {PACK_ICON})")
    elif not icon.is_file():
        errors.append(f"{label}/pack.json: icon '{pack.meta['icon']}' not found")
    elif png_size(icon) is None:
        errors.append(f"{label}/pack.json: icon '{pack.meta['icon']}' is not a PNG")

    for line in warnings:
        print(f"WARNING {line}")
    for line in errors:
        print(f"ERROR {line}")
    print(f"{label}: checked {count} blueprints: {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


def validate(packs) -> int:
    failed = 0

    for name in JAR_FILES:
        if not (ROOT / name).is_file():
            print(f"ERROR {name}: missing")
            failed = 1
    mod_id = read_mod_id()
    if (ROOT / MODS_TOML).is_file() and (not mod_id or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", mod_id)):
        print(f"ERROR {MODS_TOML}: no valid modId in [[mods]] (got {mod_id!r})")
        failed = 1
    if not LOGO.is_file():
        print(f"ERROR {LOGO.name}: missing (the mod logo)")
        failed = 1
    elif png_size(LOGO) is None:
        print(f"ERROR {LOGO.name}: not a PNG")
        failed = 1

    if not packs:
        print("ERROR no packs found (top-level folders with a pack.json)")
        return 1
    for pack in packs:
        failed |= validate_pack(pack)
    return failed


def png_size(path: Path):
    """Return (width, height) of a PNG, or None if it is not a PNG."""
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


# --- Build --------------------------------------------------------------------

def build(packs, version: str, out: str) -> int:
    if validate(packs):
        return 1
    mod_id = read_mod_id()
    values = {
        "${mod_version}": version,
        "${minecraft_version}": MINECRAFT_VERSION,
        "${neo_version_range}": NEO_VERSION_RANGE,
    }

    out_dir = ROOT / out
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / JAR_NAME.format(version=version)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as jar:
        for name in JAR_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            for key, value in values.items():
                text = text.replace(key, value)
            jar.writestr(name, text)
        jar.write(LOGO, "logo.png")
        for pack in packs:
            base = f"blueprints/{mod_id}/{pack.label}"
            meta = dict(pack.meta, icon=PACK_ICON)
            jar.writestr(f"{base}/pack.json", json.dumps(meta, indent=4, ensure_ascii=False) + "\n")
            jar.write(pack.icon, f"{base}/{PACK_ICON}")
            for path, rel in pack.content_files():
                if rel is not None:
                    jar.write(path, f"{base}/{rel.as_posix()}")

    shown = target.relative_to(ROOT) if target.is_relative_to(ROOT) else target
    print(f"Built {shown.as_posix()} with {len(packs)} pack(s): {', '.join(p.label for p in packs)} (version {version})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    validate_cmd = sub.add_parser("validate")
    build_cmd = sub.add_parser("build")
    for cmd in (validate_cmd, build_cmd):
        cmd.add_argument("--pack", action="append", help="only this pack folder (repeatable); default: all packs")
    build_cmd.add_argument("--version", default="dev")
    build_cmd.add_argument("--out", default="dist")
    args = parser.parse_args()

    packs = find_packs(args.pack)
    if args.pack:
        unknown = set(args.pack) - {p.label for p in packs}
        if unknown:
            print(f"ERROR unknown pack folder(s): {', '.join(sorted(unknown))}")
            return 1
    return validate(packs) if args.command == "validate" else build(packs, args.version, args.out)


if __name__ == "__main__":
    sys.exit(main())
