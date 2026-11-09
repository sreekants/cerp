"""MPC001-070: every locked dependency is pinned with hashes; crypto comes from vetted libraries only."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Low-level primitives that would mean a local crypto implementation; use cryptography / PyJWT instead.
FORBIDDEN_IMPORTS = {"Crypto", "Cryptodome", "rsa", "ecdsa", "nacl.bindings"}


def test_lockfile_pins_every_package_with_hashes() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    missing = []
    for pkg in lock["package"]:
        source = pkg.get("source", {})
        if "registry" not in source:
            continue  # the project itself, editable or path sources
        artifacts = [pkg.get("sdist", {}), *pkg.get("wheels", [])]
        artifacts = [a for a in artifacts if a]
        if not artifacts or any(not str(a.get("hash", "")).startswith("sha256:") for a in artifacts):
            missing.append(f"{pkg['name']} {pkg.get('version')}")
    assert missing == []


def test_jwt_and_crypto_are_direct_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = " ".join(project["dependencies"]).lower()
    assert "pyjwt[crypto]" in deps


def test_no_local_crypto_primitives() -> None:
    found = []
    for f in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            found += [
                f"{f.name}: {n}"
                for n in names
                if n.split(".")[0] in FORBIDDEN_IMPORTS or n in FORBIDDEN_IMPORTS
            ]
    assert found == []
