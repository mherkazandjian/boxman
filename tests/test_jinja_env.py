"""
Tests for boxman.utils.jinja_env – Jinja2 template helpers for env vars.
"""

import os
import pytest
import yaml

from boxman.utils.jinja_env import env, env_required, env_is_set, create_jinja_env
from boxman.manager import BoxmanManager


class TestEnvFunction:

    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_VAR", "hello")
        assert env("BOXMAN_TEST_VAR") == "hello"

    def test_returns_empty_string_when_unset(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_VAR", raising=False)
        assert env("BOXMAN_TEST_VAR") == ""

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_VAR", raising=False)
        assert env("BOXMAN_TEST_VAR", default="fallback") == "fallback"

    def test_returns_value_over_default_when_set(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_VAR", "actual")
        assert env("BOXMAN_TEST_VAR", default="fallback") == "actual"

    def test_empty_string_env_var_returns_empty(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_VAR", "")
        assert env("BOXMAN_TEST_VAR", default="fallback") == ""


class TestEnvRequiredFunction:

    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_VAR", "secret")
        assert env_required("BOXMAN_TEST_VAR") == "secret"

    def test_raises_when_unset(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_VAR", raising=False)
        with pytest.raises(ValueError, match="not set"):
            env_required("BOXMAN_TEST_VAR")

    def test_raises_with_custom_message(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_VAR", raising=False)
        with pytest.raises(ValueError, match="provide BOXMAN_TEST_VAR"):
            env_required("BOXMAN_TEST_VAR", "provide BOXMAN_TEST_VAR")

    def test_raises_when_empty(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_VAR", "")
        with pytest.raises(ValueError):
            env_required("BOXMAN_TEST_VAR")


class TestEnvIsSetFunction:

    def test_true_when_set(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_VAR", "yes")
        assert env_is_set("BOXMAN_TEST_VAR") is True

    def test_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_VAR", raising=False)
        assert env_is_set("BOXMAN_TEST_VAR") is False

    def test_false_when_empty(self, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_VAR", "")
        assert env_is_set("BOXMAN_TEST_VAR") is False


class TestJinjaTemplateRendering:
    """Test that Jinja2 templates using env helpers render correctly."""

    def test_env_in_template(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_PASSWORD", "s3cret")
        tpl = tmp_path / "test.yml"
        tpl.write_text('password: {{ env("MY_PASSWORD") }}\n')

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("test.yml")
        rendered = template.render()
        data = yaml.safe_load(rendered)
        assert data["password"] == "s3cret"

    def test_env_with_default_in_template(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_PASSWORD", raising=False)
        tpl = tmp_path / "test.yml"
        tpl.write_text('password: {{ env("MY_PASSWORD", default="ubuntu") }}\n')

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("test.yml")
        rendered = template.render()
        data = yaml.safe_load(rendered)
        assert data["password"] == "ubuntu"

    def test_env_with_jinja_default_filter(self, tmp_path, monkeypatch):
        """Jinja2's built-in | default filter also works."""
        monkeypatch.delenv("MY_PASSWORD", raising=False)
        tpl = tmp_path / "test.yml"
        tpl.write_text('password: {{ env("MY_PASSWORD") | default("ubuntu", true) }}\n')

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("test.yml")
        rendered = template.render()
        data = yaml.safe_load(rendered)
        assert data["password"] == "ubuntu"

    def test_env_required_in_template_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_PASSWORD", "s3cret")
        tpl = tmp_path / "test.yml"
        tpl.write_text('password: {{ env_required("MY_PASSWORD") }}\n')

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("test.yml")
        rendered = template.render()
        data = yaml.safe_load(rendered)
        assert data["password"] == "s3cret"

    def test_env_required_in_template_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_PASSWORD", raising=False)
        tpl = tmp_path / "test.yml"
        tpl.write_text('password: {{ env_required("MY_PASSWORD", "set MY_PASSWORD!") }}\n')

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("test.yml")
        with pytest.raises(ValueError, match="set MY_PASSWORD!"):
            template.render()

    def test_env_is_set_in_template(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_PASSWORD", "yes")
        tpl = tmp_path / "test.yml"
        tpl.write_text('user: {{ "admin" if env_is_set("MY_PASSWORD") else "guest" }}\n')

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("test.yml")
        rendered = template.render()
        data = yaml.safe_load(rendered)
        assert data["user"] == "admin"

    def test_env_is_set_false_in_template(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_PASSWORD", raising=False)
        tpl = tmp_path / "test.yml"
        tpl.write_text('user: {{ "admin" if env_is_set("MY_PASSWORD") else "guest" }}\n')

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("test.yml")
        rendered = template.render()
        data = yaml.safe_load(rendered)
        assert data["user"] == "guest"

    def test_mixed_env_calls_in_template(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADMIN_PASS", "secret123")
        monkeypatch.delenv("OPTIONAL_VAR", raising=False)
        tpl = tmp_path / "conf.yml"
        tpl.write_text(
            "admin_pass: {{ env_required(\"ADMIN_PASS\") }}\n"
            "optional: {{ env(\"OPTIONAL_VAR\", default=\"none\") }}\n"
            "has_pass: {{ env_is_set(\"ADMIN_PASS\") }}\n"
        )

        jinja_env = create_jinja_env(str(tmp_path))
        template = jinja_env.get_template("conf.yml")
        rendered = template.render()
        data = yaml.safe_load(rendered)
        assert data["admin_pass"] == "secret123"
        assert data["optional"] == "none"
        assert data["has_pass"] is True


class TestBoxmanManagerLoadConfigWithJinja:
    """Test that BoxmanManager.load_config uses the custom Jinja env."""

    def test_load_config_resolves_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOXMAN_TEST_PASS", "mypassword")
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'admin_pass: {{ env("BOXMAN_TEST_PASS") }}\n'
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["admin_pass"] == "mypassword"

    def test_load_config_resolves_env_with_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_PASS", raising=False)
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'admin_pass: {{ env("BOXMAN_TEST_PASS", default="ubuntu") }}\n'
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["admin_pass"] == "ubuntu"

    def test_load_config_env_required_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BOXMAN_TEST_PASS", raising=False)
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'admin_pass: {{ env_required("BOXMAN_TEST_PASS") }}\n'
        )

        mgr = BoxmanManager()
        with pytest.raises(ValueError, match="not set"):
            mgr.load_config(str(conf))

    def test_load_config_conditional_with_env_is_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BOXMAN_FEATURE_FLAG", "1")
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'feature: {{ "enabled" if env_is_set("BOXMAN_FEATURE_FLAG") else "disabled" }}\n'
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["feature"] == "enabled"

    def test_load_config_old_environ_dict_still_works(self, tmp_path, monkeypatch):
        """The old {{ environ.VAR }} syntax via the environ=os.environ dict still works."""
        monkeypatch.setenv("BOXMAN_OLD_STYLE", "old_value")
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            "old_style: {{ environ.BOXMAN_OLD_STYLE }}\n"
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["project"] == "test"
        assert config["old_style"] == "old_value"

    def test_env_function_not_shadowed_by_environ_dict(self, tmp_path, monkeypatch):
        """env() callable must not be shadowed by the environ dict."""
        monkeypatch.setenv("BOXMAN_TEST_PASS", "from_func")
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'via_func: {{ env("BOXMAN_TEST_PASS") }}\n'
            "via_dict: {{ environ.BOXMAN_TEST_PASS }}\n"
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["via_func"] == "from_func"
        assert config["via_dict"] == "from_func"

    def test_boxman_conf_file_resolved(self, tmp_path, monkeypatch):
        """BOXMAN_CONF_FILE resolves to the absolute path of the config file."""
        monkeypatch.delenv("BOXMAN_CONF_FILE", raising=False)
        monkeypatch.delenv("BOXMAN_CONF_DIR", raising=False)
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'conf_file: {{ env("BOXMAN_CONF_FILE") }}\n'
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["conf_file"] == str(conf)

    def test_boxman_conf_dir_resolved(self, tmp_path, monkeypatch):
        """BOXMAN_CONF_DIR resolves to the directory containing the config file."""
        monkeypatch.delenv("BOXMAN_CONF_FILE", raising=False)
        monkeypatch.delenv("BOXMAN_CONF_DIR", raising=False)
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'conf_dir: {{ env("BOXMAN_CONF_DIR") }}\n'
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["conf_dir"] == str(tmp_path)

    def test_boxman_conf_file_env_override(self, tmp_path, monkeypatch):
        """A user-defined BOXMAN_CONF_FILE env var takes precedence."""
        monkeypatch.setenv("BOXMAN_CONF_FILE", "/custom/override/conf.yml")
        monkeypatch.delenv("BOXMAN_CONF_DIR", raising=False)
        conf = tmp_path / "conf.yml"
        conf.write_text(
            "project: test\n"
            'conf_file: {{ env("BOXMAN_CONF_FILE") }}\n'
        )

        mgr = BoxmanManager()
        config = mgr.load_config(str(conf))
        assert config["conf_file"] == "/custom/override/conf.yml"
