"""Generate the webos model registry from the application tree (MPC002-006).

Reads every ``controller.wsm`` (and other ``*.wsm``) under ``<portal>/objects/modules`` and
``<portal>/services/modules``, the WSIDL files they name, the datasource map (``datasources.xml``) and,
with ``--dbroot``, the live table schemas. Writes one deterministic YAML file that the server loads at
startup as its only source of model metadata.

The command is read-only: databases are opened with ``mode=ro`` and nothing is written under ``--portal``
or ``--dbroot``. Usage::

    cerp registry generate --portal G:/cluster/wwwroot/portal \\
        --datasources G:/devel/cortex/web/config/Inetpub/sys/config/datasources.xml \\
        --dbroot data/template --out registry/webos.yaml
    cerp registry generate ... --check      # exit 1 if registry/webos.yaml is out of date
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from cerp import __version__
from cerp.data.registry import SCHEMA_VERSION, table_name_for

TREES = ("objects", "services")
_TAG = re.compile(r"<(\w+)>(.*?)</\1>", re.S)
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_INTERFACE = re.compile(r"interface\s+(\w+)\s*\{(.*?)\}\s*;?", re.S)
_METHOD = re.compile(r"([A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*;")
_DBQ = re.compile(r"DBQ=([^;]+)", re.I)

# CWebBusinessObject's generic web methods (appbobj.cpp handler map plus Add/Update/Delete/DoDelete).
GENERIC_METHODS = (
    "Add",
    "Update",
    "Delete",
    "DoDelete",
    "ToString",
    "FromString",
    "CreateFromString",
    "GetCount",
    "GetID",
    "GetObjectList",
    "GetHash",
    "Describe",
    "GetObjectsAsString",
)


def privilege_verb(method: str) -> str | None:
    """The privilege AuthenticationAspect::GetPrivilegeForMethod requires, or None if unclassified."""
    if method in ("Add", "CreateFromXML"):
        return "Add"
    if method in ("Update", "FromXML") or method.startswith("Set"):
        return "Edit"
    if method == "ToXML" or method.startswith("Get"):
        return "View"
    if method == "Delete":
        return "Delete"
    return None


def camel(column: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in column.split("_") if p)


@dataclass
class Gen:
    portal: Path
    datasources_file: Path
    dbroot: Path | None
    hashes: dict[str, str] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)

    def warn(self, code: str, model: str, detail: str) -> None:
        self.warnings.append({"code": code, "model": model, "detail": detail})

    def read(self, path: Path, label: str) -> str:
        data = path.read_bytes()
        self.hashes[label] = hashlib.sha256(data).hexdigest()
        return data.decode("utf-8", errors="replace")


def parse_manifest(text: str) -> dict[str, str]:
    inner = re.sub(r"</?WebServiceManifest>", "", text)
    return {tag: value.strip() for tag, value in _TAG.findall(inner)}


def parse_wsidl(text: str) -> list[dict[str, Any]] | None:
    body = _COMMENT.sub("", text)
    m = _INTERFACE.search(body)
    if not m:
        return None
    methods = []
    for ret, name, params in _METHOD.findall(m.group(2)):
        plist = []
        for item in params.split(","):
            param = item.strip()
            if not param or param == "void":
                continue
            parts = param.split()
            if len(parts) != 2:  # noqa: PLR2004
                return None
            plist.append({"type": parts[0], "name": parts[1]})
        methods.append({"name": name, "returns": ret, "params": plist})
    return methods


def parse_datasources(text: str) -> dict[str, str]:
    root = ET.fromstring(text)  # noqa: S314 - developer-supplied local file, not network input
    out: dict[str, str] = {}
    for ds in root.iter("DataSource"):
        name = (ds.findtext("Name") or "").strip()
        conn = (ds.findtext("ConnectString") or "").strip()
        m = _DBQ.search(conn)
        if name and m:
            out[name] = m.group(1).strip()
    return out


def read_columns(db: Path, table: str) -> list[dict[str, Any]] | None:
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as con:
        rows = con.execute(
            'SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?) ORDER BY cid', (table,)
        ).fetchall()
    if not rows:
        return None
    return [
        {"name": n, "type": t, "not_null": bool(nn), "default": d, "primary_key": bool(pk)}
        for n, t, nn, d, pk in rows
    ]


def _split_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _defaults(value: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for item in _split_list(value):
        k, sep, v = item.partition("=")
        out[k.strip()] = v.strip() if sep else None
    return out


def _deleted_state(value: str) -> dict[str, Any] | None:
    if not value:
        return None
    k, _, v = value.partition("=")
    try:
        parsed: Any = int(v)
    except ValueError:
        parsed = v.strip()
    return {"field": k.strip(), "value": parsed}


def _access(raw: str, model_name: str | None) -> dict[str, Any]:
    if not raw:
        return {"raw": None, "privilege_model": None, "enforced": False}
    _, _, value = raw.partition("=")
    value = value.strip().replace("$Model$", model_name or "")
    return {"raw": raw, "privilege_model": value or None, "enforced": bool(value)}


def _model_key(rel: PurePosixPath) -> tuple[str, str, str]:
    # objects/modules/<module>/<model>/controller/<file>.wsm  (services/modules/... likewise)
    tree, _, module, model = rel.parts[:4]
    stem = rel.stem
    key = f"{tree}/{module}/{model}" + ("" if stem == "controller" else f":{stem}")
    return key, module, model


def build(portal: Path, datasources: Path, dbroot: Path | None) -> dict[str, Any]:
    g = Gen(portal, datasources, dbroot)
    ds_map = parse_datasources(g.read(datasources, f"datasources/{datasources.name}"))
    models: dict[str, Any] = {}
    class_ids: Counter[str] = Counter()

    manifests = sorted(p for tree in TREES for p in (portal / tree / "modules").rglob("*.wsm") if p.is_file())
    for path in manifests:
        rel = PurePosixPath(path.relative_to(portal).as_posix())
        key, module, model_dir = _model_key(rel)
        m = parse_manifest(g.read(path, f"portal/{rel}"))
        mtype = m.get("Type", "")
        model_name = m.get("Model") or None
        entry: dict[str, Any] = {
            "identity": {
                "tree": rel.parts[0],
                "module": module,
                "model_dir": model_dir,
                "manifest": str(rel),
                "endpoint": str(rel.with_suffix("")),
                "class_id": m.get("ClassID") or None,
                "description": m.get("Description") or None,
                "type": mtype or None,
                "model": model_name,
            },
            "access": _access(m.get("AccessPrivilege", ""), model_name),
        }
        if entry["identity"]["class_id"]:
            class_ids[entry["identity"]["class_id"]] += 1
        else:
            g.warn("missing_class_id", key, "manifest has no ClassID")
        if not entry["access"]["enforced"]:
            g.warn("access_not_enforced", key, "no non-empty AccessPrivilege: webos skips the check")

        if mtype == "Database.Table":
            if not model_name:
                g.warn("missing_model", key, "Database.Table manifest has no Model")
            deleted = _deleted_state(m.get("DeletedState", ""))
            entry["behaviour"] = {
                "model_defaults": _defaults(m.get("ModelDefaults", "")),
                "deleted_state": deleted,
                "delete": "soft" if deleted else "hard",
                "relationships": _split_list(m.get("Relationships", "")),
                "convert_time_fields": _split_list(m.get("ConvertTimeFields", "")),
                "timezone_context": m.get("TimezoneContext") or None,
                "disabled_methods": _split_list(m.get("DisabledMethod", "")),
                "preloaded": {"true": True, "false": False}.get(m.get("Preloaded", "").lower()),
                "aspect": m.get("Aspect") or None,
            }
            if not deleted:
                g.warn("hard_delete", key, "no DeletedState: Delete removes rows")
            entry["storage"] = _storage(g, key, m, model_name, ds_map)
            columns = entry["storage"].pop("_columns", None)
            if columns is not None:
                entry["columns"] = columns
            entry["methods"] = _table_methods(g, key, entry, columns)
        elif mtype == "Script":
            entry["script"] = {
                "language": m.get("Language") or None,
                "source": m.get("Source") or None,
                "wsidl": m.get("WSIDL") or None,
            }
            entry["methods"] = _script_methods(g, key, path=path, rel=rel, manifest=m, entry=entry)
        models[key] = entry

    for cid, n in sorted(class_ids.items()):
        if n > 1:
            owners = sorted(k for k, e in models.items() if e["identity"]["class_id"] == cid)
            g.warn("duplicate_class_id", ",".join(owners), f"ClassID {cid} used {n} times")

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": "cerp.registry.generate", "version": __version__},
        "sources": {
            "portal": portal.as_posix(),
            "datasources": datasources.as_posix(),
            "dbroot": dbroot.as_posix() if dbroot else None,
            "sha256": dict(sorted(g.hashes.items())),
        },
        "datasources": {name: {"file": f} for name, f in sorted(ds_map.items())},
        "models": dict(sorted(models.items())),
        "warnings": sorted(g.warnings, key=lambda w: (w["code"], w["model"], w["detail"])),
    }


def _storage(
    g: Gen, key: str, m: dict[str, str], model_name: str | None, ds_map: dict[str, str]
) -> dict[str, Any]:
    ds = m.get("DataSource") or None
    explicit = m.get("Table") or None
    table = explicit.lower() if explicit else (table_name_for(model_name) if model_name else None)
    out: dict[str, Any] = {
        "datasource": ds,
        "file": ds_map.get(ds) if ds else None,
        "table": table,
        "table_source": "explicit" if explicit else "derived",
    }
    if ds and ds not in ds_map:
        g.warn("datasource_unmapped", key, f"datasource {ds!r} is not in the datasource map")
    if g.dbroot is not None and out["file"] and table:
        db = Path(out["file"].replace("$(DBROOT)", str(g.dbroot)))
        if not db.is_file():
            g.warn("database_missing", key, f"{PurePosixPath(out['file']).name} not found under dbroot")
        else:
            cols = read_columns(db, table)
            if cols is None:
                g.warn("table_not_found", key, f"{out['table_source']} table {table!r} not in {db.name}")
            out["_columns"] = cols
    return out


def _with_privilege(g: Gen, key: str, access: dict[str, Any], method: dict[str, Any]) -> dict[str, Any]:
    verb = privilege_verb(method["name"])
    if verb is None:
        method["privilege"] = "unclassified"
        g.warn("unclassified_method", key, f"{method['name']} has no privilege mapping")
    elif access["enforced"]:
        method["privilege"] = f"{access['privilege_model']}.{verb}"
    else:
        method["privilege"] = f"(not enforced).{verb}"
    return method


def _table_methods(
    g: Gen, key: str, entry: dict[str, Any], columns: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    access = entry["access"]
    disabled = set(entry["behaviour"]["disabled_methods"])
    names = list(GENERIC_METHODS)
    for c in columns or []:
        names += [f"Get{camel(c['name'])}", f"Set{camel(c['name'])}"]
    return [_with_privilege(g, key, access, {"name": n}) for n in names if n not in disabled]


def _script_methods(
    g: Gen, key: str, *, path: Path, rel: PurePosixPath, manifest: dict[str, str], entry: dict[str, Any]
) -> list[dict[str, Any]]:
    wsidl = manifest.get("WSIDL")
    if not wsidl:
        return []
    wpath = path.parent / wsidl
    if not wpath.is_file():
        g.warn("wsidl_missing", key, f"{wsidl} not found next to {rel.name}")
        return []
    parsed = parse_wsidl(g.read(wpath, f"portal/{rel.parent / wsidl}"))
    if parsed is None:
        g.warn("wsidl_parse_failed", key, f"{wsidl} could not be parsed")
        return []
    return [_with_privilege(g, key, entry["access"], meth) for meth in parsed]


def render(registry: dict[str, Any]) -> bytes:
    text = yaml.safe_dump(registry, sort_keys=True, allow_unicode=True, default_flow_style=False, width=110)
    return ("# Generated by `cerp registry generate` (MPC002-006). Do not edit by hand.\n" + text).encode()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cerp registry generate", description=__doc__.splitlines()[0])
    ap.add_argument("--portal", type=Path, required=True, help="webos application root")
    ap.add_argument("--datasources", type=Path, required=True, help="datasources.xml")
    ap.add_argument("--dbroot", type=Path, help="folder of module .s3db files, to read live schemas")
    ap.add_argument("--out", type=Path, required=True, help="registry YAML to write")
    ap.add_argument("--check", action="store_true", help="exit 1 if --out differs; write nothing")
    args = ap.parse_args(argv)

    for label, p in (("--portal", args.portal), ("--dbroot", args.dbroot)):
        if p is not None and not p.is_dir():
            ap.error(f"{label} {p} is not a directory")
    if not args.datasources.is_file():
        ap.error(f"--datasources {args.datasources} is not a file")

    data = render(build(args.portal, args.datasources, args.dbroot))
    if args.check:
        current = args.out.read_bytes() if args.out.is_file() else b""
        if current != data:
            print(f"{args.out} is out of date; run without --check to regenerate", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    reg = yaml.safe_load(data)
    print(f"wrote {args.out}: {len(reg['models'])} models, {len(reg['warnings'])} warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
