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


def test_all_skills_use_kebab_case_english_names_and_required_metadata():
    import yaml

    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    kebab = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    problems = []
    for product in catalog["products"]:
        skill = ROOT / product["folder"] / "SKILL.md"
        fm = yaml.safe_load(skill.read_text(encoding="utf-8").split("---", 2)[1])
        sname = str(fm.get("name") or "")
        if not kebab.match(sname):
            problems.append((product["folder"], f"non-kebab name: {sname!r}"))
        if "skill_name" in product and product["skill_name"] != sname:
            problems.append((product["folder"], f"catalog skill_name mismatch: {product['skill_name']} != {sname}"))
        meta = fm.get("metadata") or {}
        for req in ("author", "license", "description"):
            if not (fm.get(req) or meta.get(req)):
                problems.append((product["folder"], f"missing {req}"))
        if not meta.get("display_name"):
            problems.append((product["folder"], "missing metadata.display_name"))
    assert not problems, problems


def test_root_ci_covers_all_17_products_and_no_nested_github_dirs():
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    missing_ci = [p["folder"] for p in catalog["products"] if f'"{p["folder"]}"' not in ci_text]
    nested_gh = [p["folder"] for p in catalog["products"] if (ROOT / p["folder"] / ".github").exists()]
    assert not missing_ci, f"根 CI 遗漏技能目录：{missing_ci}"
    assert not nested_gh, f"存在残留子 .github 目录：{nested_gh}"
