"""Tests for nanoinfra.agent.tools.sandbox."""

import shlex

import pytest

from nanoinfra.agent.tools import sandbox as sandbox_module
from nanoinfra.agent.tools.sandbox import wrap_command


def _parse(cmd: str) -> list[str]:
    """Split a wrapped command back into tokens for assertion."""
    return shlex.split(cmd)


class TestBwrapBackend:
    def test_basic_structure(self, tmp_path):
        ws = str(tmp_path / "project")
        result = wrap_command("bwrap", "echo hi", ws, ws)
        tokens = _parse(result)

        assert tokens[0] == "bwrap"
        assert "--new-session" in tokens
        assert "--die-with-parent" in tokens
        assert "--ro-bind" in tokens
        assert "--proc" in tokens
        assert "--dev" in tokens
        assert "--tmpfs" in tokens

        sep = tokens.index("--")
        assert tokens[sep + 1:] == ["sh", "-c", "echo hi"]

    def test_workspace_bind_mounted_rw(self, tmp_path):
        ws = str(tmp_path / "project")
        result = wrap_command("bwrap", "ls", ws, ws)
        tokens = _parse(result)

        bind_idx = [i for i, t in enumerate(tokens) if t == "--bind"]
        assert any(tokens[i + 1] == ws and tokens[i + 2] == ws for i in bind_idx)

    def test_home_env_points_to_workspace(self, tmp_path):
        ws = str(tmp_path / "project")
        result = wrap_command("bwrap", "echo $HOME", ws, ws)
        tokens = _parse(result)

        setenv_idx = [i for i, t in enumerate(tokens) if t == "--setenv"]
        assert any(
            tokens[i + 1] == "HOME" and tokens[i + 2] == str(tmp_path / "project")
            for i in setenv_idx
        )

    def test_parent_dir_masked_with_tmpfs(self, tmp_path):
        ws = tmp_path / "project"
        result = wrap_command("bwrap", "ls", str(ws), str(ws))
        tokens = _parse(result)

        tmpfs_indices = [i for i, t in enumerate(tokens) if t == "--tmpfs"]
        tmpfs_targets = {tokens[i + 1] for i in tmpfs_indices}
        assert str(ws.parent) in tmpfs_targets

    def test_tmp_dir_mounted_as_tmpfs(self, tmp_path):
        """Regression coverage for #1948: commands need writable scratch space."""
        ws = tmp_path / "project"
        result = wrap_command("bwrap", "touch /tmp/probe", str(ws), str(ws))
        tokens = _parse(result)

        tmpfs_indices = [i for i, t in enumerate(tokens) if t == "--tmpfs"]
        tmpfs_targets = {tokens[i + 1] for i in tmpfs_indices}
        assert "/tmp" in tmpfs_targets

    def test_parent_mask_precedes_workspace_recreation(self, tmp_path):
        ws = tmp_path / "project"
        result = wrap_command("bwrap", "ls", str(ws), str(ws))
        tokens = _parse(result)

        parent_mask = next(
            i for i, t in enumerate(tokens)
            if t == "--tmpfs" and tokens[i + 1] == str(ws.parent)
        )
        workspace_dir = next(
            i for i, t in enumerate(tokens)
            if t == "--dir" and tokens[i + 1] == str(ws)
        )
        workspace_bind = next(
            i for i, t in enumerate(tokens)
            if t == "--bind" and tokens[i + 1] == str(ws) and tokens[i + 2] == str(ws)
        )
        chdir = tokens.index("--chdir")

        assert parent_mask < workspace_dir < workspace_bind < chdir

    def test_cwd_inside_workspace(self, tmp_path):
        ws = tmp_path / "project"
        sub = ws / "src" / "lib"
        result = wrap_command("bwrap", "pwd", str(ws), str(sub))
        tokens = _parse(result)

        chdir_idx = tokens.index("--chdir")
        assert tokens[chdir_idx + 1] == str(sub)

    def test_cwd_outside_workspace_falls_back(self, tmp_path):
        ws = tmp_path / "project"
        outside = tmp_path / "other"
        result = wrap_command("bwrap", "pwd", str(ws), str(outside))
        tokens = _parse(result)

        chdir_idx = tokens.index("--chdir")
        assert tokens[chdir_idx + 1] == str(ws.resolve())

    def test_command_with_special_characters(self, tmp_path):
        ws = str(tmp_path / "project")
        cmd = "echo 'hello world' && cat \"file with spaces.txt\""
        result = wrap_command("bwrap", cmd, ws, ws)
        tokens = _parse(result)

        sep = tokens.index("--")
        assert tokens[sep + 1:] == ["sh", "-c", cmd]

    def test_system_dirs_ro_bound(self, tmp_path):
        ws = str(tmp_path / "project")
        result = wrap_command("bwrap", "ls", ws, ws)
        tokens = _parse(result)

        ro_bind_indices = [i for i, t in enumerate(tokens) if t == "--ro-bind"]
        ro_targets = {tokens[i + 1] for i in ro_bind_indices}
        assert "/usr" in ro_targets

    def test_optional_dirs_use_ro_bind_try(self, tmp_path):
        ws = str(tmp_path / "project")
        result = wrap_command("bwrap", "ls", ws, ws)
        tokens = _parse(result)

        try_indices = [i for i, t in enumerate(tokens) if t == "--ro-bind-try"]
        try_targets = {tokens[i + 1] for i in try_indices}
        assert "/bin" in try_targets
        assert "/etc/ssl/certs" in try_targets

    def test_the_running_interpreter_is_bound(self, tmp_path, monkeypatch):
        """Without it the sandbox reaches a different Python than the app runs (#276).

        `python3` resolves to the base image's interpreter, so a dependency nanoinfra declares
        and installs reads as missing inside the sandbox. Asserted as `--ro-bind-try` and not
        `--ro-bind`: an install whose paths are absent must not be refused.
        """
        venv = tmp_path / "app" / ".venv"
        base = tmp_path / "pythons" / "cpython-3.13"
        venv.mkdir(parents=True)
        base.mkdir(parents=True)
        monkeypatch.setattr(sandbox_module.sys, "prefix", str(venv))
        monkeypatch.setattr(sandbox_module.sys, "base_prefix", str(base))

        ws = str(tmp_path / "project")
        tokens = _parse(wrap_command("bwrap", "python3 -c pass", ws, ws))
        try_idx = [i for i, t in enumerate(tokens) if t == "--ro-bind-try"]

        def bound(path: str) -> bool:
            return any(tokens[i + 1] == path and tokens[i + 2] == path for i in try_idx)

        assert bound(str(venv)), "the venv prefix is not bound into the sandbox"
        # The subtle half: a venv's `bin/python` is a symlink into the base prefix, so binding
        # the venv alone leaves a dangling link, which drops out of PATH resolution silently.
        assert bound(str(base)), "the base prefix is not bound, so the venv's python dangles"

    def test_an_interpreter_under_usr_is_not_bound_twice(self, tmp_path, monkeypatch):
        """`/usr` is a required bind; repeating it as optional is noise.

        This is the published image's shape: `sys.base_prefix` is `/usr/local`, already covered,
        which is exactly why binding the venv alone looks sufficient there and is not elsewhere.
        """
        venv = tmp_path / "app" / ".venv"
        venv.mkdir(parents=True)
        monkeypatch.setattr(sandbox_module.sys, "prefix", str(venv))
        monkeypatch.setattr(sandbox_module.sys, "base_prefix", "/usr/local")

        ws = str(tmp_path / "project")
        tokens = _parse(wrap_command("bwrap", "ls", ws, ws))
        try_idx = [i for i, t in enumerate(tokens) if t == "--ro-bind-try"]

        assert any(tokens[i + 1] == str(venv) for i in try_idx)
        assert not any(tokens[i + 1] == "/usr/local" for i in try_idx)

    def test_an_interpreter_above_the_workspace_is_dropped(self, tmp_path, monkeypatch):
        """It would cover the tmpfs that hides the config directory.

        Same rule the operator's own binds already follow, and the reason this goes through
        `_normalize_bind_paths` rather than appending to the list directly.
        """
        ws_parent = tmp_path / "data"
        ws = ws_parent / "project"
        monkeypatch.setattr(sandbox_module.sys, "prefix", str(ws_parent))
        monkeypatch.setattr(sandbox_module.sys, "base_prefix", str(tmp_path))

        tokens = _parse(wrap_command("bwrap", "ls", str(ws), str(ws)))
        try_idx = [i for i, t in enumerate(tokens) if t == "--ro-bind-try"]

        assert not any(tokens[i + 1] == str(ws_parent) for i in try_idx)
        assert not any(tokens[i + 1] == str(tmp_path) for i in try_idx)

    def test_media_dir_ro_bind(self, tmp_path, monkeypatch):
        """Media directory should be read-only mounted inside the sandbox."""
        fake_media = tmp_path / "media"
        fake_media.mkdir()
        monkeypatch.setattr(
            "nanoinfra.agent.tools.sandbox.get_media_dir",
            lambda: fake_media,
        )
        ws = str(tmp_path / "project")
        result = wrap_command("bwrap", "ls", ws, ws)
        tokens = _parse(result)

        try_indices = [i for i, t in enumerate(tokens) if t == "--ro-bind-try"]
        try_pairs = {(tokens[i + 1], tokens[i + 2]) for i in try_indices}
        assert (str(fake_media), str(fake_media)) in try_pairs

    def test_custom_read_only_binds_use_ro_bind_try(self, tmp_path):
        ws = tmp_path / "project"
        tool_bin = tmp_path / "home" / ".local" / "bin"

        result = wrap_command(
            "bwrap",
            "uv --version",
            str(ws),
            str(ws),
            sandbox_ro_binds=[str(tool_bin)],
        )
        tokens = _parse(result)

        try_indices = [i for i, t in enumerate(tokens) if t == "--ro-bind-try"]
        try_pairs = {(tokens[i + 1], tokens[i + 2]) for i in try_indices}
        assert (str(tool_bin.resolve(strict=False)), str(tool_bin.resolve(strict=False))) in try_pairs

    def test_custom_read_write_binds_use_bind_try(self, tmp_path):
        ws = tmp_path / "project"
        cache_dir = tmp_path / "cache"

        result = wrap_command(
            "bwrap",
            "touch cache/file",
            str(ws),
            str(ws),
            sandbox_rw_binds=[str(cache_dir)],
        )
        tokens = _parse(result)

        bind_try_indices = [i for i, t in enumerate(tokens) if t == "--bind-try"]
        bind_try_pairs = {(tokens[i + 1], tokens[i + 2]) for i in bind_try_indices}
        resolved = str(cache_dir.resolve(strict=False))
        assert (resolved, resolved) in bind_try_pairs

    def test_custom_relative_bind_paths_are_ignored(self, tmp_path):
        ws = tmp_path / "project"

        result = wrap_command(
            "bwrap",
            "ls",
            str(ws),
            str(ws),
            sandbox_ro_binds=["relative/bin"],
            sandbox_rw_binds=["relative/cache"],
        )
        tokens = _parse(result)

        assert "relative/bin" not in tokens
        assert "relative/cache" not in tokens

    def test_custom_workspace_parent_binds_are_ignored(self, tmp_path):
        ws = tmp_path / "private" / "project"
        parent = ws.parent.resolve(strict=False)

        result = wrap_command(
            "bwrap",
            "cat ../config.json",
            str(ws),
            str(ws),
            sandbox_ro_binds=[str(parent)],
            sandbox_rw_binds=[str(parent)],
        )
        tokens = _parse(result)

        ro_try_indices = [i for i, token in enumerate(tokens) if token == "--ro-bind-try"]
        ro_try_pairs = {(tokens[i + 1], tokens[i + 2]) for i in ro_try_indices}
        bind_try_indices = [i for i, token in enumerate(tokens) if token == "--bind-try"]
        bind_try_pairs = {(tokens[i + 1], tokens[i + 2]) for i in bind_try_indices}
        assert (str(parent), str(parent)) not in ro_try_pairs
        assert (str(parent), str(parent)) not in bind_try_pairs


class TestUnknownBackend:
    def test_raises_value_error(self, tmp_path):
        ws = str(tmp_path / "project")
        with pytest.raises(ValueError, match="Unknown sandbox backend"):
            wrap_command("nonexistent", "ls", ws, ws)

    def test_empty_string_raises(self, tmp_path):
        ws = str(tmp_path / "project")
        with pytest.raises(ValueError):
            wrap_command("", "ls", ws, ws)
