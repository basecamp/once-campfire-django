"""Independent Rails outputs, including malformed tokens and old installations."""

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "campfire.settings")
os.environ.setdefault("SECRET_KEY_BASE", "tests-only")
import django

django.setup()
from django.test import override_settings

from campfire import rails

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "vectors/rails_compat.json").read_text()
)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class RailsVectors(unittest.TestCase):
    def setUp(self):
        self.settings = override_settings(SECRET_KEY=VECTORS["secret_key_base"])
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.clock = patch(
            "campfire.models.now", return_value=timestamp(VECTORS["now"])
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def check_verification(self, examples, verify, expected="expected"):
        for example in examples:
            with self.subTest(case=example["case"]):
                with patch(
                    "campfire.models.now",
                    return_value=timestamp(example.get("now", VECTORS["now"])),
                ):
                    try:
                        actual = verify(example)
                    except ValueError:
                        actual = None
                    self.assertEqual(actual, example[expected])

    def test_key_generator(self):
        for example in VECTORS["key_generator"]:
            with self.subTest(salt=example["salt"]):
                self.assertEqual(
                    rails.key(example["salt"], example["length"]).hex(),
                    example["key_hex"],
                )

    def test_signed_cookie_generation(self):
        for example in VECTORS["signed_cookies"]["generate"]:
            with self.subTest(name=example["name"], value=example["value"]):
                self.assertEqual(
                    rails.sign_cookie(
                        example["name"],
                        example["value"],
                        timestamp(example["expires_at"]),
                    ),
                    example["raw"],
                )

    def test_signed_cookie_verification(self):
        self.check_verification(
            VECTORS["signed_cookies"]["verify"],
            lambda e: rails.verify_cookie(e["name"], e["raw"]),
        )

    def test_encrypted_cookie_generation(self):
        for example in VECTORS["encrypted_cookies"]["generate"]:
            with self.subTest(name=example["name"], value=example["value"]):
                nonce = rails.decode64(unquote(example["raw"]).split("--")[1])
                self.assertEqual(
                    rails.encrypt_cookie(
                        example["name"],
                        example["value"],
                        timestamp(example["expires_at"]),
                        nonce=nonce,
                    ),
                    example["raw"],
                )

    def test_encrypted_cookie_verification(self):
        self.check_verification(
            VECTORS["encrypted_cookies"]["verify"],
            lambda e: rails.decrypt_cookie(e["name"], e["raw"]),
        )

    def test_signed_id_generation(self):
        for example in VECTORS["signed_ids"]["generate"]:
            with self.subTest(
                model=example["model"], id=example["id"], purpose=example["purpose"]
            ):
                self.assertEqual(
                    rails.signed_id(
                        example["model"],
                        example["id"],
                        example["purpose"],
                        timestamp(example["expires_at"]),
                    ),
                    example["signed_id"],
                )

    def test_signed_id_verification(self):
        examples = [
            dict(
                e,
                cast_expected=int(e["expected"])
                if isinstance(e["expected"], (str, int))
                else None,
            )
            for e in VECTORS["signed_ids"]["verify"]
        ]
        self.check_verification(
            examples,
            lambda e: rails.verify_id(e["model"], e["signed_id"], e["purpose"]),
            expected="cast_expected",
        )

    def test_sgid_generation(self):
        for example in VECTORS["sgids"]["generate"]:
            self.assertEqual(
                rails.sign(
                    example["data"],
                    "signed_global_ids",
                    example["purpose"],
                    timestamp(example["expires_at"]),
                    urlsafe=True,
                ),
                example["sgid"],
            )

    def test_sgid_verification(self):
        self.check_verification(
            VECTORS["sgids"]["verify"],
            lambda e: rails.verify_sgid(e["sgid"], e["purpose"]),
        )

    def test_app_verifier_generation(self):
        for example in VECTORS["app_verifiers"]["generate"]:
            with self.subTest(name=example["name"], purpose=example["purpose"]):
                self.assertEqual(
                    rails.sign(
                        json.loads(example["data_json"]),
                        example["name"],
                        example["purpose"],
                        timestamp(example["expires_at"]),
                    ),
                    example["message"],
                )

    def test_app_verifier_verification(self):
        examples = [
            dict(
                e,
                expected=json.loads(e["expected_json"])
                if e["expected_json"] is not None
                else None,
            )
            for e in VECTORS["app_verifiers"]["verify"]
        ]
        self.check_verification(
            examples, lambda e: rails.verify(e["message"], e["name"], e["purpose"])
        )

    def test_turbo_stream_generation(self):
        for example in VECTORS["turbo_stream_names"]["generate"]:
            self.assertEqual(
                rails.sign_stream(example["stream_name"]), example["signed"]
            )

    def test_turbo_stream_verification(self):
        self.check_verification(
            VECTORS["turbo_stream_names"]["verify"],
            lambda e: rails.verify_stream(e["signed"]),
        )

    def test_csrf_validity(self):
        raw = rails.decode64(VECTORS["csrf"]["session_token"])
        for example in VECTORS["csrf"]["validity"]:
            with self.subTest(
                case=example["case"], path=example["path"], method=example["method"]
            ):
                self.assertEqual(
                    rails.valid_csrf(
                        raw, example["token"], example["path"], example["method"]
                    ),
                    example["expected"],
                )

    def test_session_continuity(self):
        example = VECTORS["session"]
        self.assertEqual(
            rails.decrypt_cookie("_campfire_session", example["session_cookie_raw"]),
            example["session"],
        )
        self.assertEqual(
            rails.verify_cookie("session_token", example["session_token_raw"]),
            example["session_token_value"],
        )

    def test_user_only_legacy_mention_exception(self):
        for example in VECTORS["unverified_sgids"]:
            expected = example["expected"]
            expected_id = (
                int(expected.rsplit("/", 1)[1])
                if isinstance(expected, str) and "/User/" in expected
                else None
            )
            # This helper extracts identity; the richtext caller performs the lookup.
            if example["case"] == "missing user":
                expected_id = 999
            with self.subTest(case=example["case"]):
                self.assertEqual(
                    rails.unverified_user_gid(example["sgid"]), expected_id
                )
        self.assertIsNone(
            rails.unverified_user_gid(rails.sgid("ActiveStorage::Blob", 1))
        )

    def test_invalid_encryption_is_value_error(self):
        token = rails.encrypt_cookie("_campfire_session", {"_csrf_token": "old tab"})
        corrupted = ("A" if token[0] != "A" else "B") + token[1:]
        with self.assertRaises(ValueError):
            rails.decrypt_cookie("_campfire_session", corrupted)
        for token in ["garbage", "!!!", "a--b--c"]:
            with self.assertRaises(ValueError):
                rails.decrypt_cookie("_campfire_session", token)

    def test_secret_rotation_does_not_reuse_cached_key(self):
        token = rails.sign_cookie("session_token", "existing-session")
        with override_settings(SECRET_KEY=VECTORS["rotated_secret_key_base"]):
            with self.assertRaises(ValueError):
                rails.verify_cookie("session_token", token)

    def test_active_support_json_line_separators(self):
        # load_defaults 8.1 leaves JS separators literal but escapes HTML entities.
        self.assertEqual(
            rails.encode("<>&\u2028\u2029"),
            '"\\u003c\\u003e\\u0026\u2028\u2029"'.encode(),
        )


if __name__ == "__main__":
    unittest.main()
