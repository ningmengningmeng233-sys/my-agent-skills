#!/usr/bin/env python3
"""Root-Reachable Reference Graph Validator for MiniMax H3 Craft (Micro-kernel Edition).

Performs strict BFS/DFS reachability traversal from authoritative root entry points:
  - SKILL.md
  - AGENTS.md
  - scripts/router.py

Algorithm:
  1. Discover all active references under `references/` (excluding `legacy/`).
  2. Parse explicit Markdown links `[text](path)` and relative file references.
  3. Normalize all paths relative to repository root with forward slashes.
  4. Build directed adjacency graph with UTF-8 strict encoding.
  5. Traverse BFS from root nodes.
  6. Detect:
     - orphan files: active references not reachable from roots
     - broken links: references pointing to non-existent files
  7. Return exit code 0 if orphans == 0 and broken_links == 0, else 1.
"""

from __future__ import annotations

import os
import re
import sys
from collections import deque
from typing import Dict, List, Set, Tuple

ROOT_FILES = [
    "SKILL.md",
    "AGENTS.md",
    "scripts/router.py",
]


def normalize_rel_path(base_file: str, target: str, repo_root: str) -> str | None:
    """Normalize link target relative to repository root."""
    target = target.strip()
    target = re.sub(r"[#?].*$", "", target)
    if not target or target.startswith(("http://", "https://", "mailto:", "ftp://")):
        return None

    if os.path.isabs(target):
        try:
            rel = os.path.relpath(target, repo_root)
            return rel.replace("\\", "/")
        except ValueError:
            return None

    base_dir = os.path.dirname(os.path.join(repo_root, base_file))
    abs_target = os.path.normpath(os.path.join(base_dir, target))
    try:
        rel = os.path.relpath(abs_target, repo_root)
        return rel.replace("\\", "/")
    except ValueError:
        return None


def extract_links_from_file(file_path: str, repo_root: str) -> Set[str]:
    """Extract all explicit outgoing references from a file using strict UTF-8 decoding."""
    abs_path = os.path.join(repo_root, file_path)
    if not os.path.isfile(abs_path):
        return set()

    try:
        with open(abs_path, "r", encoding="utf-8", errors="strict") as f:
            content = f.read()
    except Exception as e:
        print(f"[ERROR] Failed to read {file_path} with strict UTF-8: {e}", file=sys.stderr)
        return set()

    links = set()

    # 1. Standard markdown link syntax: [label](path)
    for m in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", content):
        target = m.group(2)
        norm = normalize_rel_path(file_path, target, repo_root)
        if norm:
            links.add(norm)

    # 2. Direct repo-relative paths inside backticks / quotes: `references/...`
    for m in re.finditer(r"[`'\"](references/[a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]+)[`'\"]", content):
        target = m.group(1).strip()
        # target is already repo-relative
        clean_target = os.path.normpath(target).replace("\\", "/")
        links.add(clean_target)

    return links


def build_graph(repo_root: str) -> Tuple[Set[str], Dict[str, Set[str]]]:
    """Build directed reachability graph and collect all active reference files."""
    active_references = set()
    ref_dir = os.path.join(repo_root, "references")

    if os.path.isdir(ref_dir):
        for root, dirs, files in os.walk(ref_dir):
            rel_root = os.path.relpath(root, repo_root).replace("\\", "/")
            if "legacy" in rel_root.split("/"):
                continue
            for f in files:
                rel_file = os.path.relpath(os.path.join(root, f), repo_root).replace("\\", "/")
                active_references.add(rel_file)

    adj: Dict[str, Set[str]] = {}

    # Read from root files
    for rf in ROOT_FILES:
        adj[rf] = extract_links_from_file(rf, repo_root)

    # Read from all active references recursively
    for ref in active_references:
        adj[ref] = extract_links_from_file(ref, repo_root)

    return active_references, adj


def traverse_reachable(roots: List[str], adj: Dict[str, Set[str]], repo_root: str) -> Tuple[Set[str], List[Tuple[str, str]]]:
    """Execute BFS traversal from roots, returning reachable nodes and broken links."""
    visited: Set[str] = set()
    queue = deque()
    broken_links: List[Tuple[str, str]] = []

    for r in roots:
        if os.path.exists(os.path.join(repo_root, r)):
            visited.add(r)
            queue.append(r)
        elif r.startswith("references/"):
            broken_links.append(("ROOT_CONFIG", r))

    while queue:
        curr = queue.popleft()
        outgoing = adj.get(curr, set())
        for neighbor in outgoing:
            abs_neighbor = os.path.join(repo_root, neighbor)
            if not os.path.exists(abs_neighbor):
                broken_links.append((curr, neighbor))
                continue
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return visited, broken_links


def run_reference_graph_audit(repo_root: str | None = None) -> Tuple[bool, int, int, int]:
    """Run root-reachable graph audit."""
    if repo_root is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    active_refs, adj = build_graph(repo_root)
    reachable_nodes, broken_links = traverse_reachable(ROOT_FILES, adj, repo_root)

    orphans = active_refs - reachable_nodes

    print("=== Root-Reachable Reference Graph Audit ===")
    print(f"Active References Count: {len(active_refs)}")
    print(f"Reachable References Count: {len(active_refs - orphans)}")
    print(f"Orphan References Count: {len(orphans)}")
    print(f"Broken Links Count: {len(broken_links)}")

    if broken_links:
        print("\n[BROKEN LINKS DETECTED]:")
        for src, dst in broken_links:
            print(f"  {src} -> {dst}")

    if orphans:
        print("\n[ORPHAN REFERENCES DETECTED]:")
        for o in sorted(orphans):
            print(f"  {o}")

    success = (len(orphans) == 0 and len(broken_links) == 0)
    if success:
        print("\nPASS: All active reference documents are root-reachable without broken links.")
    else:
        print("\nFAIL: Unreachable orphan references or broken links detected.")
    print(f"REFERENCE_GRAPH summary: status={'PASS' if success else 'FAIL'} "
          f"orphans={len(orphans)} broken={len(broken_links)}")

    return success, len(active_refs), len(orphans), len(broken_links)


if __name__ == "__main__":
    passed, _, o_cnt, b_cnt = run_reference_graph_audit()
    sys.exit(0 if passed else 1)
