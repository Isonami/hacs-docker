import importlib.util
import io
import json
import tempfile
import zipfile
import unittest
import sys
import subprocess
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("plugins", Path(__file__).parents[1] / "scripts/plugins.py")
plugins = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = plugins
SPEC.loader.exec_module(plugins)


class ConfigTests(unittest.TestCase):
    def make_config(self, root, name="my-plugin", **changes):
        data = {
            "repository": "owner/repo",
            "version": "v1.2.3-rc.1",
            "artifact": "auto",
            "source_path": "custom_components/my_domain",
            "domain": "my_domain",
        }
        data.update(changes)
        path = Path(root) / name / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_valid_config_and_hyphenated_tag(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = plugins.load_plugin(self.make_config(root), Path(root))
            self.assertEqual(plugin.tag, "my-plugin-v1.2.3-rc.1")

    def test_defaults_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.make_config(root)
            data = json.loads(path.read_text())
            del data["artifact"]
            path.write_text(json.dumps(data))
            self.assertEqual(plugins.load_plugin(path, Path(root)).artifact, "auto")

    def test_rejects_invalid_fields(self):
        cases = [
            {"repository": "not-a-repository"},
            {"version": "bad/version"},
            {"artifact": "tarball"},
            {"source_path": "../secret"},
            {"domain": "Bad-Domain"},
            {"artifact": "tag-source", "asset_pattern": "*.zip"},
            {"unexpected": True},
        ]
        for change in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(plugins.PluginError):
                    plugins.load_plugin(self.make_config(root, **change), Path(root))

    def test_plan_is_exact_and_stable(self):
        with tempfile.TemporaryDirectory() as root:
            first = plugins.load_plugin(self.make_config(root, "a-plugin", version="v1"), Path(root))
            second = plugins.load_plugin(self.make_config(root, "b-plugin", version="v2"), Path(root))
            self.assertEqual(plugins.missing_tags([first, second], ["a-plugin-v1-extra"]), ["a-plugin-v1", "b-plugin-v2"])
            self.assertEqual(plugins.missing_tags([first, second], [first.tag, second.tag]), [])
            self.assertEqual(plugins.missing_tags([first, second], [first.tag]), [second.tag])

    def test_existing_published_configuration_is_immutable(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            path = self.make_config(repo / "plugins", "my-plugin", version="v1")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            plugin = plugins.load_plugin(path, repo / "plugins")
            subprocess.run(["git", "tag", plugin.tag], cwd=repo, check=True)
            plugins.check_existing_tag_configs([plugin], repo)
            data = json.loads(path.read_text())
            data["repository"] = "owner/changed"
            path.write_text(json.dumps(data))
            changed = plugins.load_plugin(path, repo / "plugins")
            with self.assertRaisesRegex(plugins.PluginError, "immutable"):
                plugins.check_existing_tag_configs([changed], repo)

    def test_lookup_uses_complete_configured_tag(self):
        with tempfile.TemporaryDirectory() as root:
            plugin = plugins.load_plugin(self.make_config(root), Path(root))
            self.assertIs(plugins.find_plugin_for_tag([plugin], plugin.tag), plugin)
            for tag in ("my-plugin-v1.2.3", "my-v1.2.3-rc.1", plugin.tag + "-extra"):
                with self.assertRaises(plugins.PluginError):
                    plugins.find_plugin_for_tag([plugin], tag)


class FakeClient:
    def __init__(self, responses=None, errors=None):
        self.responses = responses or {}
        self.errors = errors or {}
        self.api_url = "https://api.github.test"

    def api_json(self, path):
        if path in self.errors:
            raise self.errors[path]
        return self.responses[path]


def make_zip(files, symlink=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "target")
    return stream.getvalue()


class ArtifactTests(unittest.TestCase):
    def plugin(self, artifact="auto", pattern=None):
        return plugins.Plugin("my-plugin", "owner/repo", "v1", artifact, "custom_components/my_domain", "my_domain", pattern)

    def release_path(self):
        return "/repos/owner/repo/releases/tags/v1"

    def test_auto_priority_and_fallback(self):
        release = {"assets": [{"name": "plugin.zip", "browser_download_url": "https://download/plugin.zip"}], "zipball_url": "https://download/source.zip"}
        client = FakeClient({self.release_path(): release})
        resolved = plugins.resolve_artifact(client, self.plugin())
        self.assertEqual((resolved.kind, resolved.url), ("release-asset", "https://download/plugin.zip"))
        release["assets"] = []
        self.assertEqual(plugins.resolve_artifact(client, self.plugin()).kind, "release-source")
        missing = FakeClient(errors={self.release_path(): plugins.NotFound("missing")})
        self.assertEqual(plugins.resolve_artifact(missing, self.plugin()).kind, "tag-source")

    def test_explicit_artifact_modes(self):
        release = {"assets": [], "zipball_url": "https://download/source.zip"}
        client = FakeClient({self.release_path(): release})
        self.assertEqual(plugins.resolve_artifact(client, self.plugin("release-source")).kind, "release-source")
        self.assertEqual(plugins.resolve_artifact(client, self.plugin("tag-source")).kind, "tag-source")
        with self.assertRaises(plugins.PluginError):
            plugins.resolve_artifact(client, self.plugin("release-asset"))

    def test_asset_pattern_and_ambiguity(self):
        release = {"assets": [
            {"name": "one.zip", "browser_download_url": "https://download/one"},
            {"name": "two.zip", "browser_download_url": "https://download/two"},
        ], "zipball_url": "https://download/source"}
        client = FakeClient({self.release_path(): release})
        with self.assertRaisesRegex(plugins.PluginError, "ambiguous"):
            plugins.resolve_artifact(client, self.plugin())
        resolved = plugins.resolve_artifact(client, self.plugin(pattern="two*"))
        self.assertEqual(resolved.asset_name, "two.zip")

    def test_auto_does_not_hide_non_404_errors(self):
        client = FakeClient(errors={self.release_path(): plugins.PluginError("rate limited")})
        with self.assertRaisesRegex(plugins.PluginError, "rate limited"):
            plugins.resolve_artifact(client, self.plugin())

    def test_resolve_commit_validates_provenance(self):
        path = "/repos/owner/repo/commits/v1"
        sha = "a" * 40
        self.assertEqual(plugins.resolve_commit(FakeClient({path: {"sha": sha}}), self.plugin()), sha)
        with self.assertRaises(plugins.PluginError):
            plugins.resolve_commit(FakeClient({path: {"sha": "not-a-sha"}}), self.plugin())

    def test_prepare_source_archive_and_license(self):
        archive = make_zip({
            "wrapper/LICENSE": "license text",
            "wrapper/custom_components/my_domain/manifest.json": json.dumps({"domain": "my_domain"}),
            "wrapper/custom_components/my_domain/__init__.py": "# plugin",
        })
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "out"
            plugins.prepare_zip(archive, self.plugin(), output)
            self.assertTrue((output / "manifest.json").is_file())
            self.assertEqual((output / "__init__.py").read_text(), "# plugin")
            self.assertEqual((output / ".hacs-docker/licenses/LICENSE").read_text(), "license text")

    def test_prepare_direct_release_asset(self):
        archive = make_zip({"manifest.json": json.dumps({"domain": "my_domain"}), "translations/en.json": "{}"})
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "out"
            plugins.prepare_zip(archive, self.plugin(), output)
            self.assertTrue((output / "translations/en.json").is_file())

    def test_prepare_rejects_bad_archives(self):
        valid_manifest = json.dumps({"domain": "my_domain"})
        cases = [
            make_zip({"../manifest.json": valid_manifest}),
            make_zip({"manifest.json": valid_manifest}, symlink="link"),
            make_zip({"one/manifest.json": valid_manifest, "two/manifest.json": valid_manifest}),
            make_zip({"manifest.json": json.dumps({"domain": "other"})}),
            b"not zip",
        ]
        for archive in cases:
            with self.subTest(size=len(archive)), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(plugins.PluginError):
                    plugins.prepare_zip(archive, self.plugin(), Path(root) / "out")


if __name__ == "__main__":
    unittest.main()
