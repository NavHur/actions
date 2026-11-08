"""JS-family complexity: the repository's oxlint with its complexity rules at max 0, then at their real levels."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

from measured_function import Function
from revision import git, materialize, read_file, run

SKILL_DIR = Path(__file__).resolve().parent.parent
PLUGIN = SKILL_DIR / "scripts" / "oxlint-plugin" / "pbp.mjs"
CATEGORIES = ("correctness", "suspicious", "pedantic", "perf", "style", "restriction", "nursery")
SCORE = re.compile(r"[Cc]omplexity (?:of |from )?(\d+)")
MAXIMUM = re.compile(r"(?:Maximum allowed is|limit of|to the) (\d+)")
NAME = re.compile(r"`([^`]*)`")


def owning_config(path: str, configs: list[str]) -> str | None:
    best = None
    for config in configs:
        directory = str(PurePosixPath(config).parent)
        if directory == "." or path.startswith(directory + "/"):
            if best is None or len(directory) > len(str(PurePosixPath(best).parent)):
                best = config
    return best


def strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments outside strings, then trailing commas."""
    out, i, in_string = [], 0, False
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\":
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                break
            continue
        elif text.startswith("/*", i):
            i = text.find("*/", i) + 2
            continue
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def is_complexity_rule(name: str) -> bool:
    return name in ("complexity", "eslint/complexity") or name.endswith("cognitive-complexity")


def is_off(value: object) -> bool:
    level = value[0] if isinstance(value, list) and value else value
    return level in ("off", 0, "allow")


def measuring(value: object) -> object:
    if is_off(value):
        return "off"
    options = value[1] if isinstance(value, list) and len(value) > 1 and isinstance(value[1], dict) else {}
    return ["warn", {**options, "max": 0}]


def complexity_rules(rules: dict, transform=measuring) -> dict:
    return {name: transform(value) for name, value in rules.items() if is_complexity_rule(name)}


def looser_than_limits(name: str, value: object, limits: dict[str, int]) -> str | None:
    """Describe how a repo rule definition lets an excess over Pedro's `limits` through."""
    if is_off(value):
        return "turns it off"
    level = value[0] if isinstance(value, list) and value else value
    options = value[1] if isinstance(value, list) and len(value) > 1 and isinstance(value[1], dict) else {}
    limit = limits["cognitive" if name.endswith("cognitive-complexity") else "cyclomatic"]
    reasons = []
    if level not in ("error", 2, "deny"):
        reasons.append(f"level {level!r}")
    if not isinstance(options.get("max"), int):
        reasons.append("no explicit max (rule default)")
    elif options["max"] > limit:
        reasons.append(f"max {options['max']} > {limit}")
    return ", ".join(reasons) or None


def measure_config(config: dict, limits: dict[str, int]) -> tuple[dict, dict | None, list[str], list[str]]:
    """Return the max-0 config, the repo-rules config, the plugin paths they need, and coverage notes."""
    notes: list[str] = []
    top = complexity_rules(config.get("rules", {}))
    lint_top = complexity_rules(config.get("rules", {}), transform=lambda value: value)
    overrides, lint_overrides = [], []
    for name, value in lint_top.items():
        if reason := looser_than_limits(name, value, limits):
            notes.append(f"top-level `{name}` {reason}; lint does not enforce Pedro's limit there")
    for override in config.get("overrides", []):
        rules = complexity_rules(override.get("rules", {}))
        if rules:
            overrides.append({"files": override["files"], "rules": rules})
            lint_rules = complexity_rules(override["rules"], transform=lambda value: value)
            lint_overrides.append({"files": override["files"], "rules": lint_rules})
            for name, value in lint_rules.items():
                if reason := looser_than_limits(name, value, limits):
                    notes.append(f"override {override['files']} `{name}` {reason}; "
                                 "lint does not enforce Pedro's limit for those files")
    names = set(top) | {name for o in overrides for name in o["rules"]}
    plugins, plugin_paths = [], []
    if not any(name == "complexity" or name == "eslint/complexity" for name in names):
        top["complexity"] = ["warn", {"max": 0, "variant": "classic"}]
        notes.append("config has no cyclomatic `complexity` rule; measured with oxlint classic `complexity`")
    if not any(name.endswith("cognitive-complexity") for name in names):
        top["pbp/cognitive-complexity"] = ["warn", {"max": 0}]
        plugins.append(str(PLUGIN))
        notes.append("config has no cognitive-complexity rule; measured with fleet's pbp plugin (whitepaper v1.7)")
    if "extends" in config:
        notes.append("config uses `extends`; complexity rules defined in extended files were not copied")
    prefixes = {name.split("/")[0] for name in names if "/" in name}
    for plugin in config.get("jsPlugins", []):
        specifier = plugin if isinstance(plugin, str) else plugin.get("specifier", "")
        if isinstance(plugin, dict) and plugin.get("name") not in prefixes:
            continue
        plugins.append(plugin)
        if specifier.startswith("."):
            plugin_paths.append(specifier)
    measured = {
        "categories": {category: "off" for category in CATEGORIES},
        "ignorePatterns": config.get("ignorePatterns", []),
        "rules": top,
        "overrides": overrides,
    }
    if plugins:
        measured["jsPlugins"] = plugins
    enforced = None
    if lint_top or lint_overrides:
        enforced = {**measured, "rules": lint_top, "overrides": lint_overrides}
        repo_plugins = [p for p in plugins if p != str(PLUGIN)]
        enforced.pop("jsPlugins", None)
        if repo_plugins:
            enforced["jsPlugins"] = repo_plugins
    return measured, enforced, plugin_paths, notes


def find_oxlint(root: Path, config_dir: Path) -> Path | None:
    directory = config_dir
    while True:
        candidate = directory / "node_modules/.bin/oxlint"
        if candidate.exists():
            return candidate
        if directory == root or directory == directory.parent:
            break
        directory = directory.parent
    own = SKILL_DIR / "node_modules/.bin/oxlint"
    return own if own.exists() else None


def run_oxlint(root: Path, oxlint: Path, config: Path, files: list[Path]) -> list[dict]:
    result = run([str(oxlint), "-c", str(config), "--format", "json", *map(str, files)], root)
    try:
        return json.loads(result.stdout[result.stdout.find("{"):])["diagnostics"]
    except (json.JSONDecodeError, KeyError):
        sys.stderr.write(result.stdout + result.stderr)
        sys.exit(f"oxlint did not return JSON (exit {result.returncode}) for config {config}")


def collect(diagnostics: list[dict], tree: Path) -> dict[tuple[str, int, int], Function]:
    functions: dict[tuple[str, int, int], Function] = {}
    for diagnostic in diagnostics:
        code, message = diagnostic.get("code", ""), diagnostic.get("message", "")
        score, name, labels = SCORE.search(message), NAME.search(message), diagnostic.get("labels") or []
        if not score or not labels:
            continue
        span = labels[0]["span"]
        file = Path(diagnostic["filename"])
        path = str(file.relative_to(tree)) if file.is_absolute() else diagnostic["filename"]
        key = (path, span["offset"], span["length"])
        if key not in functions:
            body = (tree / path).read_bytes()[span["offset"]: span["offset"] + span["length"]]
            functions[key] = Function(path, name.group(1) if name else "<anonymous>", span["line"],
                                      span["line"] + body.count(b"\n"))
        metric = "cognitive" if "cognitive-complexity" in code else "cyclomatic" if code.endswith("(complexity)") else None
        if metric is None:
            continue
        setattr(functions[key], metric, int(score.group(1)))
        maximum = MAXIMUM.search(message)
        functions[key].lint = {**(functions[key].lint or {}),
                               metric: (diagnostic.get("severity", ""), int(maximum.group(1)) if maximum else None)}
    return functions


def note_parse_failures(diagnostics: list[dict], tree: Path, label: str, notes: list[str],
                        unparsed: list[str]) -> None:
    """A file oxlint cannot parse gets a codeless error diagnostic; it was not measured."""
    failed = sorted({str(Path(d["filename"]).relative_to(tree)) if Path(d["filename"]).is_absolute()
                     else d["filename"] for d in diagnostics if d.get("severity") == "error" and not d.get("code")})
    if failed:
        notes.append(f"{label}: oxlint could not parse {', '.join(failed)}; not measured")
        unparsed += failed


def measure_js(root: Path, head: str | None, files: list[str], tree: Path, notes: list[str],
               unmeasured: list[str], unparsed: list[str], limits: dict[str, int]) -> list[Function]:
    if head:
        listing = git(root, "ls-tree", "-r", "--name-only", "-z", head)
    else:  # the working tree, including configs not committed yet
        listing = git(root, "ls-files", "-z") + git(root, "ls-files", "--others", "--exclude-standard", "-z")
    configs = [p for p in listing.split("\0") if PurePosixPath(p).name == ".oxlintrc.json" and "node_modules/" not in p]
    groups: dict[str | None, list[str]] = {}
    for path in files:
        groups.setdefault(owning_config(path, configs), []).append(path)
    functions: list[Function] = []
    for config_path, group in groups.items():
        if config_path:
            config_dir = str(PurePosixPath(config_path).parent)
            text = read_file(root, head, config_path) or "{}"
            config = json.loads(strip_jsonc(text))
            label = config_path
        else:
            config_dir, config, label = ".", {}, "(no .oxlintrc.json)"
        measured, enforced, plugin_paths, config_notes = measure_config(config, limits)
        notes += [f"{label}: {note}" for note in config_notes]
        plugin_dirs = [os.path.normpath(str(PurePosixPath(config_dir, p).parent)) for p in plugin_paths]
        materialize(root, head, group + plugin_dirs, tree)
        for directory in {".", config_dir}:
            if (root / directory / "node_modules").exists() and not (tree / directory / "node_modules").exists():
                (tree / directory).mkdir(parents=True, exist_ok=True)
                (tree / directory / "node_modules").symlink_to(root / directory / "node_modules")
        oxlint = find_oxlint(root, root / config_dir)
        if oxlint is None:
            notes.append(f"{label}: no oxlint in the repository or in this skill (run `pnpm install` in "
                         f"{SKILL_DIR}); {len(group)} JS/TS file(s) not measured")
            unmeasured += group
            continue
        temp_config = tree / config_dir / ".pbp-measure.oxlintrc.json"
        temp_config.parent.mkdir(parents=True, exist_ok=True)
        temp_config.write_text(json.dumps(measured, indent=2))
        print(f"Config: {label} -> {len(group)} JS-family file(s); rules: "
              + ", ".join(sorted(set(measured["rules"]) | {n for o in measured["overrides"] for n in o["rules"]})))
        diagnostics = run_oxlint(root, oxlint, temp_config, [tree / p for p in group])
        note_parse_failures(diagnostics, tree, label, notes, unparsed)
        found = collect(diagnostics, tree)
        for function in found.values():
            function.lint = None
        if enforced is not None:  # second pass: the repository's real levels and maxes
            temp_config.write_text(json.dumps(enforced, indent=2))
            scored = sorted({f.path for f in found.values()})
            real = collect(run_oxlint(root, oxlint, temp_config, [tree / p for p in scored]), tree) if scored else {}
            for key, function in found.items():
                function.lint = real[key].lint if key in real else {}
        functions += found.values()
        unscored = [path for path in group if path not in {f.path for f in found.values()}]
        if unscored:
            notes.append(f"{label}: {len(unscored)} file(s) got no scores (outside the config's complexity scope, "
                         f"e.g. ignored tests, or no functions): {', '.join(unscored)}")
    return functions
