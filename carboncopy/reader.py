"""Reader: turns a repo into a system map (code structure + infrastructure + database)."""
import re
import subprocess
from pathlib import Path

import hcl2
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY = Language(tspython.language())
SKIP = {".git", ".venv", "node_modules", "__pycache__", ".ccopy", ".next", "dist", "build"}
JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _ignored(root: Path) -> bool:
    """True when root sits inside another repo's ignored folder (e.g. Carbon Copy's own data dir)."""
    return subprocess.run(["git", "check-ignore", "-q", "."], cwd=root, capture_output=True).returncode == 0


def _listing(root: Path) -> list[Path]:
    """Files the customer actually has: tracked plus untracked, minus .gitignore (local data, secrets)."""
    r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root, capture_output=True, text=True)
    if r.returncode == 0 and not _ignored(root):
        return [root / f for f in r.stdout.split("\0") if f and not SKIP.intersection(Path(f).parts)]
    return [p for p in root.rglob("*") if p.is_file() and not SKIP.intersection(p.relative_to(root).parts)]


def _files(root: Path, *suffixes: str):
    for p in _listing(root):
        if p.suffix in suffixes and p.is_file():
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


STRING = re.compile(r"`[^`]*`|\"[^\"\n]*\"|'[^'\n]*'", re.S)
SQL_TABLE = re.compile(r"\b(?:from|into|update|join)\s+([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)", re.I)
CTE = re.compile(r"\b([a-z_][a-z0-9_]*)\s+as\s*\(", re.I)
DDL_TABLE = re.compile(r"create\s+(?:temp(?:orary)?\s+)?(?:table|materialized\s+view|view)\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)", re.I)
JS_ENV = re.compile(r"process\.env(?:\.([A-Z_][A-Z0-9_]*)|\[[\"']([A-Z_][A-Z0-9_]*)[\"']\])")
JS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)", re.M)
JS_METHOD = re.compile(r"export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE)\b")


def _next_path(rel: Path) -> str | None:
    """Next.js route for app/**/route.ts or app/**/page.tsx; None for other files."""
    parts = rel.parts
    if "app" not in parts:
        return None
    segs = [p for p in parts[parts.index("app") + 1 : -1] if not (p.startswith("(") and p.endswith(")"))]
    return "/" + "/".join(":" + p.strip("[].") if p.startswith("[") else p for p in segs)


def _js_symbols(path: Path, root: Path) -> dict:
    src = path.read_text(errors="replace")
    rel = path.relative_to(root)
    functions = [{"name": m[1], "line": src.count("\n", 0, m.start()) + 1} for m in JS_FUNC.finditer(src)]
    routes = []
    if (route := _next_path(rel)) is not None:
        if rel.stem == "route":
            routes = [{"method": m, "path": route, "handler": m} for m in dict.fromkeys(JS_METHOD.findall(src))]
        elif rel.stem == "page":
            routes = [{"method": "PAGE", "path": route, "handler": "page"}]
    tables = set()
    for lit in STRING.findall(src):
        ctes = {c.lower() for c in CTE.findall(lit)}
        tables |= {t.lower() for t in SQL_TABLE.findall(lit)} - ctes
    env = {a or b for a, b in JS_ENV.findall(src)}
    return {"file": str(rel), "functions": functions, "routes": routes, "sql_tables": sorted(tables), "env_vars": sorted(env)}


def _sql_kind(path: Path) -> dict:
    """Which engine a .sql file targets and whether it changes schema/data or is a saved query."""
    text = path.read_text(errors="replace")
    body = re.sub(r"--[^\n]*", "", text).lower()
    engine = "clickhouse" if ("clickhouse" in path.name.lower() or re.search(r"engine\s*=\s*\w*mergetree|materialized view", body)) else "postgres"
    kind = "schema" if re.search(r"\b(create|alter|insert|drop|truncate)\b", body) else "query"
    return {"engine": engine, "kind": kind}


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


def _schema(root: Path) -> dict[str, dict]:
    files = {str(p.relative_to(root)): p for p in sorted(_files(root, ".sql"))}
    out = {f: _sql_kind(p) for f, p in files.items()}
    ch_tables = {t.lower() for f, p in files.items() if out[f]["engine"] == "clickhouse" for t in DDL_TABLE.findall(p.read_text(errors="replace"))}
    for f, p in files.items():  # a saved query that reads a ClickHouse table runs on ClickHouse
        if out[f]["kind"] == "query" and ch_tables & {t.lower() for t in SQL_TABLE.findall(p.read_text(errors="replace"))}:
            out[f]["engine"] = "clickhouse"
    return out


def _ddl_tables(root: Path, schema: dict[str, dict]) -> set[str]:
    return {t.lower() for f, k in schema.items() if k["kind"] == "schema" for t in DDL_TABLE.findall((root / f).read_text(errors="replace"))}


def scan(root: Path) -> dict:
    root = root.resolve()
    code = [_python_symbols(p, root) for p in _files(root, ".py")] + [_js_symbols(p, root) for p in _files(root, *JS_SUFFIXES)]
    schema = _schema(root)
    ddl = _ddl_tables(root, schema)
    return {
        "repo": root.name,
        "code": code,
        "routes": [r | {"file": c["file"]} for c in code for r in c["routes"]],
        # Tables the code touches; when the repo ships DDL, only names it defines (drops prose and CTEs).
        "tables": sorted({t for c in code for t in c["sql_tables"] if not ddl or t in ddl or t.split(".")[-1] in ddl}),
        "env_vars": sorted({e for c in code for e in c["env_vars"]}),
        "infrastructure": (infra := _terraform(root)),
        "schema_files": list(schema),
        "sql": schema,
        "has_dockerfile": (root / "Dockerfile").exists(),
        "has_package_json": (root / "package.json").exists(),
        "aws_services": sorted({r["type"].split("_")[1] for r in infra if r["type"].startswith("aws_")}),
    }
