"""Release folder/catalog/entry identity and user-facing navigation contracts."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def test_catalog_versions_agree_with_entrypoints():
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    mismatches = []
    for product in catalog["products"]:
        skill = ROOT / product["folder"] / "SKILL.md"
        assert skill.is_file(), product["folder"]
        front = skill.read_text(encoding="utf-8").split("---", 2)[1]
        match = re.search(r"(?m)^\s*version:\s*[\"']?([^\s\"']+)", front)
        if not match or match.group(1) != product["version"]:
            mismatches.append((product["folder"], match.group(1) if match else None))
    assert not mismatches, mismatches

def test_readme_skill_links_resolve_to_catalog_entries():
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    expected = {p["folder"] + "/SKILL.md" for p in catalog["products"]}
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    actual = set(re.findall(r"\]\(([^)]+/SKILL\.md)\)", text))
    assert actual == expected, (sorted(expected-actual), sorted(actual-expected))
    assert all((ROOT / path).is_file() for path in actual)


def test_pipeline_blocks_use_the_catalog_skill_version():
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    problems = []
    for product in catalog["products"]:
        text = (ROOT / product["folder"] / "SKILL.md").read_text(encoding="utf-8")
        for version in re.findall(r"(?m)^skill_version:\s*(\S+)", text):
            version = version.strip("\"'")
            if version != product["version"]:
                problems.append((product["folder"], version))
    assert not problems, problems

def test_h3_current_version_config_matches_catalog():
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    product = next(p for p in catalog["products"] if p["id"] == "minimax-h3-craft")
    config = json.loads((ROOT / product["folder"] / "config/version.json").read_text(encoding="utf-8"))
    assert config["version"] == product["version"]
