import os
import tempfile
import unittest
from pathlib import Path

from quant_trend.env_loader import load_env_file


class EnvLoaderTests(unittest.TestCase):
    def test_loads_local_env_without_returning_values(self):
        old_key = os.environ.pop("OPENAI_API_KEY", None)
        old_model = os.environ.pop("OPENAI_MODEL", None)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / "openai.env"
                env_path.write_text(
                    "\n".join(
                        [
                            "# local only",
                            "OPENAI_API_KEY='test-openai-key'",
                            'OPENAI_MODEL="gpt-test"',
                        ]
                    ),
                    encoding="utf-8",
                )

                loaded = load_env_file(env_path)

            self.assertEqual(loaded, ["OPENAI_API_KEY", "OPENAI_MODEL"])
            self.assertEqual(os.environ["OPENAI_API_KEY"], "test-openai-key")
            self.assertEqual(os.environ["OPENAI_MODEL"], "gpt-test")
            self.assertNotIn("test-openai-key", repr(loaded))
        finally:
            if old_key is not None:
                os.environ["OPENAI_API_KEY"] = old_key
            else:
                os.environ.pop("OPENAI_API_KEY", None)
            if old_model is not None:
                os.environ["OPENAI_MODEL"] = old_model
            else:
                os.environ.pop("OPENAI_MODEL", None)

    def test_existing_environment_wins_by_default(self):
        old_model = os.environ.get("OPENAI_MODEL")
        try:
            os.environ["OPENAI_MODEL"] = "existing-model"
            with tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / "openai.env"
                env_path.write_text("OPENAI_MODEL=file-model\n", encoding="utf-8")

                loaded = load_env_file(env_path)

            self.assertEqual(loaded, [])
            self.assertEqual(os.environ["OPENAI_MODEL"], "existing-model")
        finally:
            if old_model is None:
                os.environ.pop("OPENAI_MODEL", None)
            else:
                os.environ["OPENAI_MODEL"] = old_model


if __name__ == "__main__":
    unittest.main()
