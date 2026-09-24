"""Reader: turns a repo into a system map (code structure + infrastructure + database)."""
import json
from pathlib import Path

import hcl2
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY = Language(tspython.language())
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".ccopy"}


def _files(root: Path, suffix: str):
    for p in root.rglob(f"*{suffix}"):
        if not SKIP.intersection(p.parts):
            yield p


def _python_symbols(path: Path, root: Path) -> dict:
    parser = Parser(PY)
    src = path.read_bytes()
    tree = parser.parse(src)
    functions, routes, sql_tables, env_vars = [], [], set(), set()

    def walk(node, decorators=()):
        if node.type == "decorated_definition":
            decs = [src[d.start_byte : d.end_byte].decode() for d in node.children if d.type == "decorator"]
            for c in node.children:
                if c.type in ("function_definition", "class_definition"):
                    walk(c, decs)
            return
        if node.type == "function_definition":
            name = src[node.child_by_field_name("name").start_byte : node.child_by_field_name("name").end_byte].decode()
            functions.append({"name": name, "line": node.start_point[0] + 1})
            for d in decorators:
                for verb in ("get", "post", "put", "patch", "delete"):
                    if f".{verb}(" in d:
                        path_ = d.split("(", 1)[1].split(",")[0].strip("\"') ")
                        routes.append({"method": verb.upper(), "path": path_, "handler": name})
        if node.type == "string":
            s = src[node.start_byte : node.end_byte].decode().lower()
            for kw in ("from ", "into ", "update ", "join "):
                if kw in s:
                    word = s.split(kw, 1)[1].split()[0].strip("\"'(),;") if s.split(kw, 1)[1].split() else ""
                    if word.isidentifier():
                        sql_tables.add(word)
        if node.type == "subscript" or node.type == "call":
            t = src[node.start_byte : node.end_byte].decode()
            if t.startswith("os.environ"):
                key = t.split("(" if "(" in t else "[", 1)[1].split(",")[0].strip("\"'[]) ")
                if key:
                    env_vars.add(key)
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return {
        "file": str(path.relative_to(root)),
        "functions": functions,
        "routes": routes,
        "sql_tables": sorted(sql_tables),
        "env_vars": sorted(env_vars),
    }


def _unquote(v):
    if isinstance(v, str) and len(v) >= 2 and v[0] == v[-1] == '"':
        return v[1:-1]
    if isinstance(v, dict):
        return {k: _unquote(x) for k, x in v.items() if not k.startswith("__")}
    if isinstance(v, list):
        return [_unquote(x) for x in v]
    return v


def _terraform(root: Path) -> list[dict]:
    resources = []
    for tf in _files(root, ".tf"):
        with tf.open() as f:
            doc = hcl2.load(f)
        for block in doc.get("resource", []):
            for rtype, named in block.items():
                for name, attrs in named.items():
                    clean = {k: _unquote(v) for k, v in attrs.items() if not k.startswith("__")}
                    resources.append({"file": str(tf.relative_to(root)), "type": _unquote(rtype), "name": _unquote(name), "attrs": clean})
    return resources


def _schema(root: Path) -> list[str]:
    return [str(p.relative_to(root)) for p in _files(root, ".sql")]


def scan(root: Path) -> dict:
    root = root.resolve()
    code = [_python_symbols(p, root) for p in _files(root, ".py")]
    system_map = {
        "repo": root.name,
        "code": code,
        "routes": [r | {"file": c["file"]} for c in code for r in c["routes"]],
        "tables": sorted({t for c in code for t in c["sql_tables"]}),
        "env_vars": sorted({e for c in code for e in c["env_vars"]}),
        "infrastructure": (infra := _terraform(root)),
        "schema_files": _schema(root),
        "has_dockerfile": (root / "Dockerfile").exists(),
        "aws_services": sorted({r["type"].split("_")[1] for r in infra if r["type"].startswith("aws_")}),
    }
    out = root / ".ccopy" / "map.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(system_map, indent=2, default=str))
    return system_map
