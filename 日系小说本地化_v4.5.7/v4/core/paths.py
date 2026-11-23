from pathlib import Path


V4_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = V4_ROOT.parent
DOCS_ROOT = PROJECT_ROOT / "docs"
CONTRACT_ROOT = V4_ROOT / "contracts"

MANIFEST_SCHEMA = DOCS_ROOT / "v4-manifest-schema.json"
ARTIFACT_SCHEMA = DOCS_ROOT / "v4-artifact-schemas.json"
RULES_PATH = V4_ROOT / "rules" / "dictionary.json"
TONE_RULES_PATH = V4_ROOT / "rules" / "tone-scan.json"
STRUCTURAL_RULES_PATH = V4_ROOT / "rules" / "structural-writing-checks.json"
