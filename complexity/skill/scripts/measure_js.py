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
    owners = []
    for config in configs:
        directory = str(PurePosixPath(config).parent)
        if directory == "." or path.startswith(directory + "/"):
            owners.append(config)
    return max(owners, key=lambda config: len(str(PurePosixPath(config).parent)), default=None)


def strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments outside strings, then trailing commas."""
    out, i = [], 0
    while i < len(text):
        token, i = jsonc_token(text, i)
        out.append(token)
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def jsonc_token(text: str, start: int) -> tuple[str, int]:
    if text[start] == '"':
        end = quoted_end(text, start)
        return text[start:end], end
    if text.startswith("//", start):
        end = text.find("\n", start)
        return "", len(text) if end < 0 else end
    if text.startswith("/*", start):
        return "", text.find("*/", start) + 2
    return text[start], start + 1


def quoted_end(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            text[i + 1]  # Preserve the error on a dangling escape.
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return i


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
    level, options = rule_options(value)
    limit = limits["cognitive" if name.endswith("cognitive-complexity") else "cyclomatic"]
    reasons = []
    if level not in ("error", 2, "deny"):
        reasons.append(f"level {level!r}")
    if reason := maximum_reason(options, limit):
        reasons.append(reason)
    return ", ".join(reasons) or None


def rule_options(value: object) -> tuple[object, dict]:
    level = value[0] if isinstance(value, list) and value else value
    options = value[1] if isinstance(value, list) and len(value) > 1 and isinstance(value[1], dict) else {}
    return level, options


def maximum_reason(options: dict, limit: int) -> str | None:
    if not isinstance(options.get("max"), int):
        return "no explicit max (rule default)"
    if options["max"] > limit:
        return f"max {options['max']} > {limit}"
    return None


def measure_config(config: dict, limits: dict[str, int]) -> tuple[dict, dict | None, list[str], list[str]]:
    """Return the max-0 config, the repo-rules config, the plugin paths they need, and coverage notes."""
    notes: list[str] = []
    top = complexity_rules(config.get("rules", {}))
    lint_top = complexity_rules(config.get("rules", {}), transform=lambda value: value)
    notes += rule_notes(lint_top, limits, "top-level", "there")
    overrides, lint_overrides = override_configs(config.get("overrides", []), limits, notes)
    names = set(top) | {name for o in overrides for name in o["rules"]}
    plugins = default_rules(top, names, notes)
    if "extends" in config:
        notes.append("config uses `extends`; complexity rules defined in extended files were not copied")
    repo_plugins, plugin_paths = configured_plugins(config.get("jsPlugins", []), names)
    plugins += repo_plugins
    measured = {
        "categories": {category: "off" for category in CATEGORIES},
        "ignorePatterns": config.get("ignorePatterns", []),
        "rules": top,
        "overrides": overrides,
    }
    if plugins:
        measured["jsPlugins"] = plugins
    enforced = enforced_config(measured, lint_top, lint_overrides, plugins)
    return measured, enforced, plugin_paths, notes


def rule_notes(rules: dict, limits: dict[str, int], label: str, scope: str) -> list[str]:
    notes = []
    for name, value in rules.items():
        if reason := looser_than_limits(name, value, limits):
            notes.append(f"{label} `{name}` {reason}; lint does not enforce Pedro's limit {scope}")
    return notes


def override_configs(configs: list[dict], limits: dict[str, int], notes: list[str]) -> tuple[list, list]:
    overrides, lint_overrides = [], []
    for override in configs:
        rules = complexity_rules(override.get("rules", {}))
        if not rules:
            continue
        overrides.append({"files": override["files"], "rules": rules})
        lint_rules = complexity_rules(override["rules"], transform=lambda value: value)
        lint_overrides.append({"files": override["files"], "rules": lint_rules})
        notes += rule_notes(lint_rules, limits, f"override {override['files']}", "for those files")
    return overrides, lint_overrides


def default_rules(top: dict, names: set[str], notes: list[str]) -> list:
    plugins = []
    if not any(name == "complexity" or name == "eslint/complexity" for name in names):
        top["complexity"] = ["warn", {"max": 0, "variant": "classic"}]
        notes.append("config has no cyclomatic `complexity` rule; measured with oxlint classic `complexity`")
    if not any(name.endswith("cognitive-complexity") for name in names):
        top["pbp/cognitive-complexity"] = ["warn", {"max": 0}]
        plugins.append(str(PLUGIN))
        notes.append("config has no cognitive-complexity rule; measured with fleet's pbp plugin (whitepaper v1.7)")
    return plugins


def configured_plugins(configured: list, names: set[str]) -> tuple[list, list[str]]:
    plugins = plugins_for_rules(configured, names)
    plugin_paths = []
    for plugin in plugins:
        specifier = plugin if isinstance(plugin, str) else plugin.get("specifier", "")
        if specifier.startswith("."):
            plugin_paths.append(specifier)
    return plugins, plugin_paths


def plugins_for_rules(configured: list, names: set[str]) -> list:
    prefixes = {name.split("/")[0] for name in names if "/" in name}
    return [p for p in configured if not isinstance(p, dict) or p.get("name") in prefixes]


def enforced_config(measured: dict, lint_top: dict, lint_overrides: list, plugins: list) -> dict | None:
    enforced = None
    if lint_top or lint_overrides:
        enforced = {**measured, "rules": lint_top, "overrides": lint_overrides}
        repo_plugins = [p for p in plugins if p != str(PLUGIN)]
        enforced.pop("jsPlugins", None)
        if repo_plugins:
            enforced["jsPlugins"] = repo_plugins
    return enforced


def find_oxlint(root: Path, config_dir: Path) -> Path | None:
    for directory in (config_dir, *config_dir.parents):
        candidate = directory / "node_modules/.bin/oxlint"
        if candidate.exists():
            return candidate
        if directory == root:
            break
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
        parsed = scored_span(diagnostic, tree)
        if parsed is None:
            continue
        path, span, score, name = parsed
        key = (path, span["offset"], span["length"])
        if key not in functions:
            body = (tree / path).read_bytes()[span["offset"]: span["offset"] + span["length"]]
            functions[key] = Function(path, name, span["line"],
                                      span["line"] + body.count(b"\n"))
        apply_diagnostic(functions[key], diagnostic, score)
    return functions


def scored_span(diagnostic: dict, tree: Path):
    message = diagnostic.get("message", "")
    score, name, labels = SCORE.search(message), NAME.search(message), diagnostic.get("labels") or []
    if not score or not labels:
        return None
    file = Path(diagnostic["filename"])
    path = str(file.relative_to(tree)) if file.is_absolute() else diagnostic["filename"]
    return path, labels[0]["span"], score, name.group(1) if name else "<anonymous>"


def apply_diagnostic(function: Function, diagnostic: dict, score) -> None:
    code = diagnostic.get("code", "")
    metric = diagnostic_metric(code)
    if metric is None:
        return
    setattr(function, metric, int(score.group(1)))
    maximum = MAXIMUM.search(diagnostic.get("message", ""))
    function.lint = {**(function.lint or {}),
                     metric: (diagnostic.get("severity", ""), int(maximum.group(1)) if maximum else None)}


def diagnostic_metric(code: str) -> str | None:
    if "cognitive-complexity" in code:
        return "cognitive"
    if code.endswith("(complexity)"):
        return "cyclomatic"
    return None


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
    configs = repository_configs(root, head)
    groups: dict[str | None, list[str]] = {}
    for path in files:
        groups.setdefault(owning_config(path, configs), []).append(path)
    functions: list[Function] = []
    for config_path, group in groups.items():
        functions += measure_group(root, head, config_path, group, tree, notes, unmeasured, unparsed, limits)
    return functions


def repository_configs(root: Path, head: str | None) -> list[str]:
    if head:
        listing = git(root, "ls-tree", "-r", "--name-only", "-z", head)
    else:  # the working tree, including configs not committed yet
        listing = git(root, "ls-files", "-z") + git(root, "ls-files", "--others", "--exclude-standard", "-z")
    return [p for p in listing.split("\0") if PurePosixPath(p).name == ".oxlintrc.json" and "node_modules/" not in p]


def group_config(root: Path, head: str | None, path: str | None) -> tuple[str, dict, str]:
    if path:
        text = read_file(root, head, path) or "{}"
        return str(PurePosixPath(path).parent), json.loads(strip_jsonc(text)), path
    return ".", {}, "(no .oxlintrc.json)"


def link_node_modules(root: Path, tree: Path, config_dir: str) -> None:
    for directory in {".", config_dir}:
        if (root / directory / "node_modules").exists() and not (tree / directory / "node_modules").exists():
            (tree / directory).mkdir(parents=True, exist_ok=True)
            (tree / directory / "node_modules").symlink_to(root / directory / "node_modules")


def measure_group(root, head, config_path, group, tree, notes, unmeasured, unparsed, limits) -> list[Function]:
    config_dir, config, label = group_config(root, head, config_path)
    measured, enforced, plugin_paths, config_notes = measure_config(config, limits)
    notes += [f"{label}: {note}" for note in config_notes]
    plugin_dirs = [os.path.normpath(str(PurePosixPath(config_dir, p).parent)) for p in plugin_paths]
    materialize(root, head, group + plugin_dirs, tree)
    link_node_modules(root, tree, config_dir)
    oxlint = find_oxlint(root, root / config_dir)
    if oxlint is None:
        notes.append(f"{label}: no oxlint in the repository or in this skill (run `pnpm install` in "
                     f"{SKILL_DIR}); {len(group)} JS/TS file(s) not measured")
        unmeasured += group
        return []
    temp_config = tree / config_dir / ".pbp-measure.oxlintrc.json"
    temp_config.parent.mkdir(parents=True, exist_ok=True)
    temp_config.write_text(json.dumps(measured, indent=2))
    print(f"Config: {label} -> {len(group)} JS-family file(s); rules: "
          + ", ".join(sorted(set(measured["rules"]) | {n for o in measured["overrides"] for n in o["rules"]})))
    diagnostics = run_oxlint(root, oxlint, temp_config, [tree / p for p in group])
    note_parse_failures(diagnostics, tree, label, notes, unparsed)
    found = collect(diagnostics, tree)
    tag_with_oxlint(found, enforced, root, oxlint, temp_config, tree)
    note_unscored(group, found, label, notes)
    return list(found.values())


def tag_with_oxlint(found, enforced, root, oxlint, temp_config, tree) -> None:
    for function in found.values():
        function.lint = None
    if enforced is None:
        return
    temp_config.write_text(json.dumps(enforced, indent=2))
    scored = sorted({f.path for f in found.values()})
    real = collect(run_oxlint(root, oxlint, temp_config, [tree / p for p in scored]), tree) if scored else {}
    for key, function in found.items():
        function.lint = real[key].lint if key in real else {}


def note_unscored(group, found, label, notes) -> None:
    scored_paths = {f.path for f in found.values()}
    unscored = [path for path in group if path not in scored_paths]
    if unscored:
        notes.append(f"{label}: {len(unscored)} file(s) got no scores (outside the config's complexity scope, "
                     f"e.g. ignored tests, or no functions): {', '.join(unscored)}")
