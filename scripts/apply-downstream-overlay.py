#!/usr/bin/env python3
"""Apply the crates.io-safe downstream overlay to an OpenAI Codex checkout.

The upstream workspace keeps the original Rust dependency keys and library
crate names. Only published package names are namespaced, so existing Rust
source continues to compile while external users depend on packages named
unofficial-codex-*.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_RS = REPO_ROOT / "codex-rs"
VERSION_FILE = REPO_ROOT / "DOWNSTREAM_VERSION"
REVISION_FILE = REPO_ROOT / "UPSTREAM_REVISION"

FALLBACK_REGISTRY_VERSIONS = {
    "nucleo": "0.5.0",
    "runfiles": "0.1.0",
}

PACKAGE_SECTION_RE = re.compile(r"(?ms)^\[package\](.*?)(?=^\[|\Z)")
NAME_RE = re.compile(r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$')
INLINE_PATH_DEP_RE = re.compile(
    r'^(?P<indent>\s*)(?P<key>[A-Za-z0-9_-]+)\s*=\s*'
    r'\{(?P<body>[^{}\n]*\bpath\s*=\s*"[^"]+"[^{}\n]*)\}'
    r'(?P<tail>\s*(?:#.*)?)$',
    re.MULTILINE,
)


DEPENDENCY_TABLE_RE = re.compile(
    r"(?ms)^(?P<header>\[(?:[^\]]+\.)?(?:dependencies|dev-dependencies|build-dependencies)\.)"
    r"(?P<key>[A-Za-z0-9_-]+)\](?P<body>.*?)(?=^\[|\Z)"
)

def namespaced_name(original: str) -> str:
    slug = original.replace("_", "-")
    if slug.startswith("unofficial-codex-"):
        return slug
    if slug.startswith("codex-"):
        return f"unofficial-{slug}"
    return f"unofficial-codex-{slug}"


def dependency_target(
    key: str,
    explicit_name: str | None,
    package_map: dict[str, str],
) -> str | None:
    original = explicit_name or key
    target = package_map.get(original) or package_map.get(key)
    if target:
        return target
    if (
        original.startswith("codex-")
        or key.startswith("codex-")
        or original.endswith("_test_support")
        or key.endswith("_test_support")
    ):
        return namespaced_name(original)
    return None


def package_name(text: str) -> str | None:
    match = PACKAGE_SECTION_RE.search(text)
    if not match:
        return None
    name_match = NAME_RE.search(match.group(0))
    return name_match.group(1) if name_match else None


def package_manifests() -> list[Path]:
    return sorted(
        path
        for path in CODEX_RS.rglob("Cargo.toml")
        if "target" not in path.parts and ".git" not in path.parts
    )


def set_package_field(section: str, key: str, value: str) -> str:
    literal = value if value in {"true", "false"} else f'"{value}"'
    field_re = re.compile(rf'(?m)^(\s*{re.escape(key)}\s*=\s*).*$')
    if field_re.search(section):
        return field_re.sub(lambda match: f"{match.group(1)}{literal}", section, count=1)

    name_match = NAME_RE.search(section)
    if not name_match:
        return section
    line_end = section.find("\n", name_match.end())
    if line_end < 0:
        line_end = len(section)
    else:
        line_end += 1
    return section[:line_end] + f"{key} = {literal}\n" + section[line_end:]

def rewrite_package_manifest(
    text: str,
    package_map: dict[str, str],
    version: str,
    repository: str,
) -> str:
    section_match = PACKAGE_SECTION_RE.search(text)
    if not section_match:
        return text

    section = section_match.group(0)
    original_name = package_name(text)
    if not original_name:
        return text

    target_name = package_map[original_name]
    name_match = NAME_RE.search(section)
    if not name_match:
        return text

    section = (
        section[: name_match.start(1)]
        + target_name
        + section[name_match.end(1) :]
    )
    # Every local package in this downstream is intentionally registry-visible.
    section = set_package_field(section, "publish", "true")
    section = set_package_field(section, "repository", repository)
    if not re.search(r"(?m)^\s*description\s*=", section):
        section = set_package_field(
            section,
            "description",
            f"Unofficial downstream package of the OpenAI Codex Rust workspace ({original_name}).",
        )

    updated = text[: section_match.start()] + section + text[section_match.end() :]

    def rewrite_dependency(match: re.Match[str]) -> str:
        key = match.group("key")
        body = match.group("body")
        package_match = re.search(r'\bpackage\s*=\s*"([^"]+)"', body)
        original_dependency = package_match.group(1) if package_match else key
        target_dependency = dependency_target(key, original_dependency, package_map)
        if not target_dependency:
            return match.group(0)

        if package_match:
            body = re.sub(
                r'\bpackage\s*=\s*"[^"]+"',
                f'package = "{target_dependency}"',
                body,
                count=1,
            )
        else:
            body = f' package = "{target_dependency}",{body}'

        if re.search(r'\bversion\s*=\s*"[^"]+"', body):
            body = re.sub(
                r'\bversion\s*=\s*"[^"]+"',
                f'version = "{version}"',
                body,
                count=1,
            )
        else:
            body = f'{body}, version = "{version}"'

        return (
            f'{match.group("indent")}{key} = {{'
            f'{body}}}{match.group("tail")}'
        )

    updated = INLINE_PATH_DEP_RE.sub(rewrite_dependency, updated)
    return rewrite_dependency_tables(updated, package_map, version)


def rewrite_dependency_tables(
    text: str,
    package_map: dict[str, str],
    version: str,
) -> str:
    def rewrite(match: re.Match[str]) -> str:
        body = match.group("body")
        if not re.search(r'(?m)^\s*path\s*=', body):
            return match.group(0)

        key = match.group("key")
        package_match = re.search(r'\bpackage\s*=\s*"([^"]+)"', body)
        original_dependency = package_match.group(1) if package_match else key
        target_dependency = dependency_target(key, original_dependency, package_map)
        if not target_dependency:
            return match.group(0)

        if package_match:
            body = re.sub(
                r'\bpackage\s*=\s*"[^"]+"',
                f'package = "{target_dependency}"',
                body,
                count=1,
            )
        else:
            path_match = re.search(r'(?m)^\s*path\s*=', body)
            if not path_match:
                return match.group(0)
            body = body[:path_match.start()] + f'package = "{target_dependency}"\n' + body[path_match.start():]

        if re.search(r'\bversion\s*=\s*"[^"]+"', body):
            body = re.sub(
                r'\bversion\s*=\s*"[^"]+"',
                f'version = "{version}"',
                body,
                count=1,
            )
        else:
            body = body.rstrip("\n") + f'\nversion = "{version}"\n'

        return f'{match.group("header")}{key}]{body}'

    return DEPENDENCY_TABLE_RE.sub(rewrite, text)


def add_registry_versions_to_git_dependencies(text: str, version_map: dict[str, str]) -> str:
    for name, version in version_map.items():
        line_re = re.compile(
            r'(?m)^(\s*' + re.escape(name) + r'\s*=\s*\{)([^}\n]*\bgit\s*=\s*"[^}"\n]+"[^}\n]*)(\}\s*)$'
        )

        def rewrite(match: re.Match[str]) -> str:
            body = match.group(2)
            if re.search(r'\bversion\s*=\s*"[^"]+"', body):
                body = re.sub(
                    r'\bversion\s*=\s*"[^"]+"',
                    f'version = "{version}"',
                    body,
                    count=1,
                )
                return f'{match.group(1)}{body}{match.group(3)}'
            return f'{match.group(1)}{body}, version = "{version}"{match.group(3)}'

        text = line_re.sub(rewrite, text, count=1)
    return text


def remove_registry_incompatible_features(
    text: str,
    feature_map: dict[str, set[str]],
) -> str:
    """Drop features supplied only by upstream git patches.

    The public crates.io releases expose the standard websocket feature set;
    the upstream checkout adds proxy/deflate through OpenAI-maintained patches.
    The Codex source uses the standard websocket API, so the registry mirror
    intentionally uses the public feature names.
    """

    for dependency, features in feature_map.items():
        dependency_re = re.compile(
            rf"(?ms)^({re.escape(dependency)}\s*=\s*\{{.*?features\s*=\s*\[)(.*?)(\]\s*\}})"
        )

        def rewrite(match: re.Match[str]) -> str:
            body = match.group(2)
            for feature in features:
                body = re.sub(
                    rf'(?m)^\s*"{re.escape(feature)}",?\s*$\n?',
                    "",
                    body,
                )
                body = re.sub(
                    rf',\s*"{re.escape(feature)}"',
                    "",
                    body,
                )
            return f"{match.group(1)}{body}{match.group(3)}"

        text = dependency_re.sub(rewrite, text, count=1)
    return text


def rewrite_workspace_root(
    path: Path,
    text: str,
    package_map: dict[str, str],
    version: str,
) -> str:
    workspace_package = re.search(
        r"(?ms)^\[workspace\.package\](.*?)(?=^\[|\Z)", text
    )
    if not workspace_package:
        raise SystemExit(f"missing [workspace.package] in {path}")

    section = workspace_package.group(0)
    version_re = re.compile(r'(?m)^(\s*version\s*=\s*)"[^"]+"\s*$')
    if version_re.search(section):
        section = version_re.sub(lambda match: f'{match.group(1)}"{version}"', section, count=1)
    else:
        section = section.replace(
            "[workspace.package]\n",
            f'[workspace.package]\nversion = "{version}"\n',
            1,
        )

    updated = text[: workspace_package.start()] + section + text[workspace_package.end() :]
    updated = add_registry_versions_to_git_dependencies(
        updated, FALLBACK_REGISTRY_VERSIONS
    )
    updated = remove_registry_incompatible_features(
        updated,
        {
            "tokio-tungstenite": {"proxy"},
            "tungstenite": {"deflate", "proxy"},
        },
    )

    def rewrite_dependency(match: re.Match[str]) -> str:
        key = match.group("key")
        body = match.group("body")
        package_match = re.search(r'\bpackage\s*=\s*"([^"]+)"', body)
        original_dependency = package_match.group(1) if package_match else key
        target_dependency = dependency_target(key, original_dependency, package_map)
        if not target_dependency:
            return match.group(0)

        if package_match:
            body = re.sub(
                r'\bpackage\s*=\s*"[^"]+"',
                f'package = "{target_dependency}"',
                body,
                count=1,
            )
        else:
            body = f' package = "{target_dependency}",{body}'

        if re.search(r'\bversion\s*=\s*"[^"]+"', body):
            body = re.sub(
                r'\bversion\s*=\s*"[^"]+"',
                f'version = "{version}"',
                body,
                count=1,
            )
        else:
            body = f'{body}, version = "{version}"'

        return (
            f'{match.group("indent")}{key} = {{'
            f'{body}}}{match.group("tail")}'
        )

    updated = INLINE_PATH_DEP_RE.sub(rewrite_dependency, updated)
    return rewrite_dependency_tables(updated, package_map, version)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=None)
    parser.add_argument("--upstream-revision", default=None)
    parser.add_argument(
        "--repository",
        default="https://github.com/f4ah6o/unofficial-codex-rs",
    )
    args = parser.parse_args()

    if not CODEX_RS.is_dir():
        raise SystemExit(f"missing upstream workspace: {CODEX_RS}")

    version = args.version or VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"invalid downstream semver: {version!r}")

    manifests = package_manifests()
    package_map: dict[str, str] = {}
    for manifest in manifests:
        original = package_name(manifest.read_text(encoding="utf-8"))
        if original:
            package_map[original] = namespaced_name(original)

    if not package_map:
        raise SystemExit("no Cargo packages found")

    root = CODEX_RS / "Cargo.toml"
    for manifest in manifests:
        original_text = manifest.read_text(encoding="utf-8")
        if manifest == root:
            updated = rewrite_workspace_root(
                manifest, original_text, package_map, version
            )
        else:
            updated = rewrite_package_manifest(
                original_text,
                package_map,
                version,
                args.repository,
            )
        if updated != original_text:
            manifest.write_text(updated, encoding="utf-8")

    # Maintain the source/API and manifest adaptations alongside the mechanical
    # namespace rewrite. Fail closed on upstream drift instead of opening an
    # incompatible sync PR. Reverse-check permits an idempotent second run.
    patch = REPO_ROOT / "scripts/patches/registry-compat.patch"
    command = ["git", "apply", "--check", str(patch)]
    check = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    if check.returncode == 0:
        subprocess.run(["git", "apply", str(patch)], cwd=REPO_ROOT, check=True)
    else:
        reverse = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if reverse.returncode != 0:
            raise SystemExit("registry compatibility patch needs rebasing:\n" + check.stderr)

    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8")
    revision = args.upstream_revision
    if revision:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise SystemExit(f"invalid upstream revision: {revision!r}")
        REVISION_FILE.write_text(f"{revision}\n", encoding="utf-8")

    remaining = []
    for manifest in package_manifests():
        name = package_name(manifest.read_text(encoding="utf-8"))
        if name and not name.startswith("unofficial-codex-"):
            remaining.append(f"{manifest}: {name}")
    if remaining:
        print("un-namespaced packages remain:", file=sys.stderr)
        print("\n".join(remaining), file=sys.stderr)
        return 1

    print(f"Applied downstream overlay to {len(package_map)} Cargo packages at {version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
