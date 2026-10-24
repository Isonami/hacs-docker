#!/usr/bin/env python3
"""Validate plugin configs, plan tags, and prepare HACS integration artifacts."""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

PLUGIN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DOMAIN_RE = re.compile(r"^[a-z0-9_]+$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ARTIFACTS = {"auto", "release-asset", "release-source", "tag-source"}
LICENSE_NAMES = {"license", "license.md", "license.txt", "copying", "copying.md", "copying.txt", "notice", "notice.md", "notice.txt"}


class PluginError(RuntimeError):
    pass


class NotFound(PluginError):
    pass


@dataclass(frozen=True)
class ResolvedArtifact:
    kind: str
    url: str
    asset_name: str | None = None


@dataclass(frozen=True)
class Plugin:
    name: str
    repository: str
    version: str
    artifact: str
    source_path: str
    domain: str
    asset_pattern: str | None = None

    @property
    def tag(self) -> str:
        return f"{self.name}-{self.version}"

    def as_dict(self) -> dict[str, Any]:
        result = {
            "name": self.name,
            "repository": self.repository,
            "version": self.version,
            "artifact": self.artifact,
            "source_path": self.source_path,
            "domain": self.domain,
            "tag": self.tag,
        }
        if self.asset_pattern is not None:
            result["asset_pattern"] = self.asset_pattern
        return result


def _safe_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise PluginError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PluginError(f"{field} must be a normalized POSIX relative path")
    return str(path)


def load_plugin(config_path: Path, plugins_dir: Path | None = None) -> Plugin:
    config_path = config_path.resolve()
    root = (plugins_dir or config_path.parent.parent).resolve()
    try:
        relative = config_path.relative_to(root)
    except ValueError as exc:
        raise PluginError(f"config is outside plugins directory: {config_path}") from exc
    if relative.parts != (relative.parts[0], "config.json"):
        raise PluginError(f"config must be plugins/<name>/config.json: {config_path}")
    name = relative.parts[0]
    if not PLUGIN_RE.fullmatch(name):
        raise PluginError(f"invalid plugin directory name: {name}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginError(f"cannot read {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginError(f"configuration must be an object: {config_path}")
    allowed = {"repository", "version", "artifact", "asset_pattern", "source_path", "domain"}
    unknown = set(data) - allowed
    if unknown:
        raise PluginError(f"unknown configuration keys: {', '.join(sorted(unknown))}")
    missing = {"repository", "version", "source_path", "domain"} - set(data)
    if missing:
        raise PluginError(f"missing configuration keys: {', '.join(sorted(missing))}")
    repository = data["repository"]
    version = data["version"]
    artifact = data.get("artifact", "auto")
    domain = data["domain"]
    asset_pattern = data.get("asset_pattern")
    if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
        raise PluginError("repository must be owner/repo")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise PluginError("version is not safe for Git tags and OCI image tags")
    if artifact not in ARTIFACTS:
        raise PluginError(f"artifact must be one of: {', '.join(sorted(ARTIFACTS))}")
    if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(domain):
        raise PluginError("domain must contain lowercase letters, digits, and underscores")
    if asset_pattern is not None and (not isinstance(asset_pattern, str) or not asset_pattern):
        raise PluginError("asset_pattern must be a non-empty string")
    if asset_pattern is not None and artifact not in {"auto", "release-asset"}:
        raise PluginError("asset_pattern is only valid for auto or release-asset")
    return Plugin(name, repository, version, artifact, _safe_relative_path(data["source_path"], "source_path"), domain, asset_pattern)


def load_plugins(plugins_dir: Path) -> list[Plugin]:
    configs = sorted(plugins_dir.glob("*/config.json"))
    if not configs:
        raise PluginError(f"no plugin configurations found in {plugins_dir}")
    plugins = [load_plugin(path, plugins_dir) for path in configs]
    tags = [plugin.tag for plugin in plugins]
    if len(tags) != len(set(tags)):
        raise PluginError("duplicate plugin tags")
    return plugins


def check_existing_tag_configs(plugins: Iterable[Plugin], repository: Path = Path(".")) -> None:
    for plugin in plugins:
        exists = subprocess.run(
            ["git", "rev-parse", "--quiet", "--verify", f"refs/tags/{plugin.tag}"],
            cwd=repository,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if exists.returncode != 0:
            continue
        shown = subprocess.run(
            ["git", "show", f"{plugin.tag}:plugins/{plugin.name}/config.json"],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            raise PluginError(f"existing tag {plugin.tag} does not contain its plugin configuration")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / plugin.name / "config.json"
            path.parent.mkdir()
            path.write_bytes(shown.stdout)
            tagged = load_plugin(path, root)
        if tagged != plugin:
            raise PluginError(f"configuration for published tag {plugin.tag} is immutable; choose a new version")


def find_plugin_for_tag(plugins: Iterable[Plugin], tag: str) -> Plugin:
    matches = [plugin for plugin in plugins if plugin.tag == tag]
    if len(matches) != 1:
        raise PluginError(f"tag {tag!r} does not exactly match one configured plugin/version")
    return matches[0]


def missing_tags(plugins: Iterable[Plugin], existing: Iterable[str]) -> list[str]:
    present = set(existing)
    return [plugin.tag for plugin in plugins if plugin.tag not in present]


class GitHubClient:
    def __init__(self, token: str | None = None, api_url: str = "https://api.github.com"):
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _request(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        headers = {"Accept": accept, "User-Agent": "hacs-docker"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound(url) from exc
            raise PluginError(f"GitHub request failed ({exc.code}) for {url}") from exc
        except urllib.error.URLError as exc:
            raise PluginError(f"GitHub request failed for {url}: {exc.reason}") from exc

    def api_json(self, path: str) -> Any:
        return json.loads(self._request(f"{self.api_url}{path}"))

    def download(self, url: str) -> bytes:
        # Release assets prefer octet-stream; GitHub source archive endpoints reject
        # that as the sole media type, so retain the API media type as a fallback.
        return self._request(url, "application/octet-stream, application/vnd.github+json")


def _release(client: GitHubClient, plugin: Plugin) -> dict[str, Any]:
    owner_repo = "/".join(urllib.parse.quote(part, safe="") for part in plugin.repository.split("/"))
    version = urllib.parse.quote(plugin.version, safe="")
    data = client.api_json(f"/repos/{owner_repo}/releases/tags/{version}")
    if not isinstance(data, dict):
        raise PluginError("invalid release response")
    return data


def _release_asset(plugin: Plugin, release: dict[str, Any]) -> ResolvedArtifact:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise PluginError("invalid release assets response")
    zip_assets = [asset for asset in assets if isinstance(asset, dict) and isinstance(asset.get("name"), str) and asset["name"].lower().endswith(".zip")]
    if plugin.asset_pattern:
        zip_assets = [asset for asset in zip_assets if fnmatch.fnmatchcase(asset["name"], plugin.asset_pattern)]
    if not zip_assets:
        raise NotFound("no matching release ZIP asset")
    if len(zip_assets) != 1:
        names = ", ".join(sorted(asset["name"] for asset in zip_assets))
        raise PluginError(f"release ZIP asset selection is ambiguous: {names}")
    asset = zip_assets[0]
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise PluginError("selected release asset has no secure download URL")
    return ResolvedArtifact("release-asset", url, asset["name"])


def resolve_artifact(client: GitHubClient, plugin: Plugin) -> ResolvedArtifact:
    release: dict[str, Any] | None = None
    release_missing = False
    if plugin.artifact in {"auto", "release-asset", "release-source"}:
        try:
            release = _release(client, plugin)
        except NotFound:
            release_missing = True
            if plugin.artifact != "auto":
                raise PluginError(f"release {plugin.version} does not exist")
    if plugin.artifact in {"auto", "release-asset"} and release is not None:
        try:
            return _release_asset(plugin, release)
        except NotFound:
            if plugin.artifact == "release-asset":
                raise PluginError("no matching release ZIP asset")
    if plugin.artifact in {"auto", "release-source"} and release is not None:
        url = release.get("zipball_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise PluginError("release has no secure source ZIP URL")
        return ResolvedArtifact("release-source", url)
    if plugin.artifact in {"auto", "tag-source"}:
        owner_repo = "/".join(urllib.parse.quote(part, safe="") for part in plugin.repository.split("/"))
        version = urllib.parse.quote(plugin.version, safe="")
        return ResolvedArtifact("tag-source", f"{client.api_url}/repos/{owner_repo}/zipball/{version}")
    if release_missing:
        raise PluginError(f"release {plugin.version} does not exist")
    raise PluginError("could not resolve artifact")


def _zip_paths(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    result = []
    for info in archive.infolist():
        raw = info.filename
        if "\\" in raw:
            raise PluginError(f"unsafe ZIP path: {raw}")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise PluginError(f"unsafe ZIP path: {raw}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise PluginError(f"ZIP symlinks are not allowed: {raw}")
        result.append((info, path))
    return result


def prepare_zip(data: bytes, plugin: Plugin, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PluginError("download is not a valid ZIP archive") from exc
    with archive:
        entries = _zip_paths(archive)
        files = {path: info for info, path in entries if not info.is_dir()}
        manifest_dirs = {path.parent for path in files if path.name == "manifest.json"}
        source_parts = PurePosixPath(plugin.source_path).parts
        candidates = []
        for directory in manifest_dirs:
            if not directory.parts or directory.parts == source_parts or directory.parts[-len(source_parts):] == source_parts:
                candidates.append(directory)
        candidates = sorted(set(candidates), key=str)
        if len(candidates) != 1:
            raise PluginError(f"expected exactly one integration manifest, found {len(candidates)}")
        source = candidates[0]
        manifest_path = source / "manifest.json"
        try:
            manifest = json.loads(archive.read(files[manifest_path]))
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PluginError("integration manifest.json is invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("domain") != plugin.domain:
            raise PluginError(f"manifest domain does not match configured domain {plugin.domain!r}")
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        copied = 0
        for path, info in files.items():
            try:
                relative = path.relative_to(source)
            except ValueError:
                continue
            if not relative.parts:
                continue
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            copied += 1
        if not copied:
            raise PluginError("integration directory is empty")
        licenses = [(path, info) for path, info in files.items() if path.name.lower() in LICENSE_NAMES and source not in path.parents]
        if licenses:
            license_dir = destination / ".hacs-docker" / "licenses"
            license_dir.mkdir(parents=True)
            for index, (path, info) in enumerate(sorted(licenses, key=lambda item: str(item[0]))):
                prefix = f"{index + 1}-" if len(licenses) > 1 else ""
                (license_dir / f"{prefix}{path.name}").write_bytes(archive.read(info))


def resolve_commit(client: GitHubClient, plugin: Plugin) -> str:
    owner_repo = "/".join(urllib.parse.quote(part, safe="") for part in plugin.repository.split("/"))
    version = urllib.parse.quote(plugin.version, safe="")
    data = client.api_json(f"/repos/{owner_repo}/commits/{version}")
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise PluginError("GitHub returned an invalid commit SHA")
    return sha.lower()


def github_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(value)


def command_validate(args: argparse.Namespace) -> None:
    plugins = load_plugins(Path(args.plugins_dir))
    payload = [plugin.as_dict() for plugin in plugins]
    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        for plugin in plugins:
            print(plugin.tag)


def command_check_tags(args: argparse.Namespace) -> None:
    check_existing_tag_configs(load_plugins(Path(args.plugins_dir)), Path(args.repository))


def command_plan(args: argparse.Namespace) -> None:
    plugins = load_plugins(Path(args.plugins_dir))
    existing = Path(args.existing_tags).read_text(encoding="utf-8").splitlines() if args.existing_tags else []
    tags = missing_tags(plugins, existing)
    github_output("tags", json.dumps(tags, separators=(",", ":")))
    github_output("has_tags", "true" if tags else "false")


def command_lookup(args: argparse.Namespace) -> None:
    plugin = find_plugin_for_tag(load_plugins(Path(args.plugins_dir)), args.tag)
    payload = plugin.as_dict()
    if args.github_output:
        for key, value in payload.items():
            github_output(key, str(value))
    else:
        print(json.dumps(payload, separators=(",", ":")))


def command_prepare(args: argparse.Namespace) -> None:
    plugin = find_plugin_for_tag(load_plugins(Path(args.plugins_dir)), args.tag)
    client = GitHubClient(os.environ.get("GITHUB_TOKEN"), args.api_url)
    artifact = resolve_artifact(client, plugin)
    data = client.download(artifact.url)
    prepare_zip(data, plugin, Path(args.output))
    commit = resolve_commit(client, plugin)
    provenance = {
        **plugin.as_dict(),
        "artifact_kind": artifact.kind,
        "asset_name": artifact.asset_name,
        "resolved_commit": commit,
    }
    provenance_path = Path(args.provenance)
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        for key, value in provenance.items():
            if value is not None:
                github_output(key, str(value))
    else:
        print(json.dumps(provenance, separators=(",", ":")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugins-dir", default="plugins")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=command_validate)
    check_tags = commands.add_parser("check-tags")
    check_tags.add_argument("--repository", default=".")
    check_tags.set_defaults(func=command_check_tags)
    plan = commands.add_parser("plan")
    plan.add_argument("--existing-tags")
    plan.set_defaults(func=command_plan)
    lookup = commands.add_parser("lookup")
    lookup.add_argument("--tag", required=True)
    lookup.add_argument("--github-output", action="store_true")
    lookup.set_defaults(func=command_lookup)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--tag", required=True)
    prepare.add_argument("--output", default="build/plugin")
    prepare.add_argument("--provenance", default="build/provenance.json")
    prepare.add_argument("--api-url", default="https://api.github.com")
    prepare.add_argument("--github-output", action="store_true")
    prepare.set_defaults(func=command_prepare)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except PluginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
