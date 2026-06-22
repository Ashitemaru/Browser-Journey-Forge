"""Install distilled bucket skills for both targets (pure Python — no Node).

  - Claude Code:    <skills-root>/<name>/SKILL.md   (auto-discovered)
  - Claude Desktop: <skill-dir>/<name>.zip          (manual upload via Settings)

`name` is derived per capacity bucket as <domain>-<capacity> so skills from
different sites never collide.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from . import config


def kebab(s: str) -> str:
    s = re.sub(r"\b(anthropic|claude)\b", "", s.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")[:64].strip("-")


def _frontmatter(name: str, description: str) -> str:
    desc = re.sub(r"[`*_#>]", "", description or name).strip()
    desc = re.sub(r"\s+", " ", desc)[:1024] or name
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n"


def _wrap(skill_md: str, name: str, description: str) -> str:
    body = skill_md.strip()
    if re.match(r"^\s*---\s*\n", body):           # already has frontmatter
        return body + "\n"
    return _frontmatter(name, description) + body + "\n"


def build_zip(dest: Path, files: dict[str, str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, content in files.items():
            z.writestr(arcname, content)


def install_one(skill_md_path: Path, skills_root: Path, name: str, description: str) -> dict:
    raw = skill_md_path.read_text()
    wrapped = _wrap(raw, name, description)

    dest_dir = skills_root / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    installed = dest_dir / "SKILL.md"
    installed.write_text(wrapped)

    zip_path = skill_md_path.parent / f"{name}.zip"
    build_zip(zip_path, {"SKILL.md": wrapped})

    return {"name": name, "installed_path": str(installed), "zip_path": str(zip_path)}


def install_registry(skills_root: Path) -> list[dict]:
    """Install every distilled skill in the registry into skills_root (idempotent)."""
    reg_path = config.STATE_DIR / "registry.json"
    if not reg_path.exists():
        return []
    skills_root.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    data = json.loads(reg_path.read_text())
    for s in data.get("skills", []):
        skill_md = config.STATE_DIR / s.get("skill_path", "")
        if not skill_md.is_file():
            continue
        cap_id = s.get("capacity_id", "")
        name = kebab(cap_id.replace("::", "-")) or kebab(s.get("skill_name", "skill"))
        results.append(install_one(skill_md, skills_root, name, s.get("scope", "")))
    return results


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Install distilled bucket skills")
    ap.add_argument("--skills-root", required=True)
    args = ap.parse_args()
    for r in install_registry(Path(args.skills_root)):
        print(f"{r['name']}: {r['installed_path']}  (zip {r['zip_path']})")
