"""Resolution order (DB → env → default), Fernet storage, and the system checks."""

from __future__ import annotations

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from llmhub import checks, conf, crypto
from llmhub.models import LLMSettings

from .helpers import DB_KEY, ENV_KEY, GATEWAY, ISOLATED, HubTestCase, reset_state, save_gateway

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@override_settings(**ISOLATED)
class ModeResolutionTests(HubTestCase):
    def setUp(self):
        reset_state()

    def test_default_when_nothing_set(self):
        with override_settings(LLMHUB_MODE=""):
            snap = conf.snapshot(fresh=True)
        assert (snap.mode, snap.mode_source) == ("direct", "default")
        assert snap.saved is False

    def test_env_beats_default(self):
        with override_settings(LLMHUB_MODE="gateway"):
            snap = conf.snapshot(fresh=True)
        assert (snap.mode, snap.mode_source) == ("gateway", "env")

    def test_db_beats_env(self):
        LLMSettings(pk=1, mode="gateway").save()
        with override_settings(LLMHUB_MODE="direct"):
            snap = conf.snapshot(fresh=True)
        assert (snap.mode, snap.mode_source) == ("gateway", "db")

    def test_blank_db_mode_follows_env(self):
        LLMSettings(pk=1, mode="", lane_map={"chat_default": "x"}).save()
        with override_settings(LLMHUB_MODE="gateway"):
            snap = conf.snapshot(fresh=True)
        assert (snap.mode, snap.mode_source) == ("gateway", "env")
        assert snap.saved is True


@override_settings(**ISOLATED)
class GatewayEndpointResolutionTests(HubTestCase):
    def setUp(self):
        reset_state()

    def test_base_url_db_then_env_then_unset(self):
        save_gateway(base="https://db.test/")
        with override_settings(LLMHUB_GATEWAY_BASE_URL="https://env.test"):
            snap = conf.snapshot(fresh=True)
            assert (snap.base_url.value, snap.base_url.source) == ("https://db.test", "db")
            LLMSettings.objects.filter(pk=1).update(gateway_base_url="")
            snap = conf.snapshot(fresh=True)
            assert (snap.base_url.value, snap.base_url.source) == ("https://env.test", "env")
        snap = conf.snapshot(fresh=True)
        assert (snap.base_url.value, snap.base_url.source) == ("", "unset")

    def test_key_db_beats_env(self):
        save_gateway(key=DB_KEY)
        with override_settings(LLMHUB_GATEWAY_API_KEY=ENV_KEY, LLMHUB_GATEWAY_BASE_URL=GATEWAY):
            snap = conf.snapshot(fresh=True)
        assert (snap.api_key.value, snap.api_key.source) == (DB_KEY, "db")

    def test_env_key_used_only_while_fallback_on(self):
        save_gateway(key=None)
        with override_settings(LLMHUB_GATEWAY_API_KEY=ENV_KEY, LLMHUB_GATEWAY_BASE_URL=GATEWAY):
            snap = conf.snapshot(fresh=True)
            assert (snap.api_key.value, snap.api_key.source) == (ENV_KEY, "env")
            LLMSettings.objects.filter(pk=1).update(fallback_to_env=False)
            snap = conf.snapshot(fresh=True)
            assert (snap.api_key.value, snap.api_key.source) == ("", "unset")

    def test_no_row_env_only(self):
        with override_settings(LLMHUB_MODE="gateway", LLMHUB_GATEWAY_BASE_URL=GATEWAY, LLMHUB_GATEWAY_API_KEY=ENV_KEY):
            snap = conf.snapshot(fresh=True)
        assert snap.gateway and snap.base_url.source == "env" and snap.api_key.source == "env"


@override_settings(**ISOLATED)
class DirectEndpointResolutionTests(HubTestCase):
    def setUp(self):
        reset_state()

    def test_direct_never_uses_db_token(self):
        LLMSettings(pk=1, mode="direct", gateway_api_key_enc=crypto.encrypt(DB_KEY)).save()
        snap = conf.snapshot(fresh=True)
        assert snap.mode == "direct"
        assert snap.api_key.value == "" and snap.api_key.source == "unset"
        with override_settings(OPENAI_API_KEY="sk-openai", OPENAI_BASE_URL="https://prov.test"):
            snap = conf.snapshot(fresh=True)
        assert (snap.api_key.value, snap.api_key.source) == ("sk-openai", "env")
        assert (snap.base_url.value, snap.base_url.source) == ("https://prov.test", "env")

    def test_direct_base_url_default(self):
        snap = conf.snapshot(fresh=True)
        assert (snap.base_url.value, snap.base_url.source) == ("https://api.openai.com", "default")

    def test_route_for_sources(self):
        with override_settings(
            LLMHUB_PURPOSES={"chat_default": {"label": "Chat", "direct_route": "gpt-x", "gateway_route": "lane-x"}}
        ):
            snap = conf.snapshot(fresh=True)
            r = conf.route_for("chat_default", snap)
            assert (r.value, r.source) == ("gpt-x", "default")
            LLMSettings(pk=1, lane_map={"chat_default": "gpt-y"}).save()
            r = conf.route_for("chat_default", conf.snapshot(fresh=True))
            assert (r.value, r.source) == ("gpt-y", "db")


@override_settings(**ISOLATED)
class CryptoTests(SimpleTestCase):
    def test_round_trip_derived(self):
        assert crypto.key_is_derived()
        token = crypto.encrypt("secret-value")
        assert token != "secret-value"
        assert crypto.decrypt(token) == "secret-value"

    def test_round_trip_dedicated(self):
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            assert not crypto.key_is_derived()
            token = crypto.encrypt("secret-value")
            assert crypto.decrypt(token) == "secret-value"
        # A dedicated key is not the derived one.
        with self.assertRaises(crypto.InvalidToken):
            crypto.decrypt(token)

    def test_setting_a_dedicated_key_keeps_the_stored_token(self):
        # Adding LLMHUB_FERNET_KEY to an install must not lose a token stored
        # under the derived key; new writes use the dedicated key only.
        stored = crypto.encrypt("secret-value")
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            assert crypto.decrypt(stored) == "secret-value"
            rewritten = crypto.encrypt("secret-value")
        assert Fernet(KEY_A.encode()).decrypt(rewritten.encode()) == b"secret-value"
        with override_settings(LLMHUB_FERNET_KEY=KEY_B):
            # Another dedicated key cannot read KEY_A's token.
            with self.assertRaises(crypto.InvalidToken):
                crypto.decrypt(rewritten)

    def test_derived_key_changes_with_secret_key(self):
        token = crypto.encrypt("secret-value")
        with override_settings(SECRET_KEY="a-completely-different-secret-key"):
            with self.assertRaises(crypto.InvalidToken):
                crypto.decrypt(token)

    def test_empty_stays_empty(self):
        assert crypto.encrypt("") == ""
        assert crypto.decrypt("") == ""

    def test_invalid_dedicated_key_is_improperly_configured(self):
        with override_settings(LLMHUB_FERNET_KEY="not-a-key"):
            with self.assertRaises(ImproperlyConfigured):
                crypto.encrypt("x")


@override_settings(**ISOLATED)
class UndecryptableKeyResolutionTests(HubTestCase):
    def setUp(self):
        reset_state()

    def test_undecryptable_is_unset_and_env_used(self):
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            save_gateway(key=DB_KEY)
        with override_settings(
            LLMHUB_FERNET_KEY=KEY_B, LLMHUB_GATEWAY_API_KEY=ENV_KEY, LLMHUB_GATEWAY_BASE_URL=GATEWAY
        ):
            snap = conf.snapshot(fresh=True)
        assert snap.key_undecryptable is True
        assert (snap.api_key.value, snap.api_key.source) == (ENV_KEY, "env")

    def test_undecryptable_without_fallback_is_unset(self):
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            save_gateway(key=DB_KEY, fallback_to_env=False)
        with override_settings(
            LLMHUB_FERNET_KEY=KEY_B, LLMHUB_GATEWAY_API_KEY=ENV_KEY, LLMHUB_GATEWAY_BASE_URL=GATEWAY
        ):
            snap = conf.snapshot(fresh=True)
        assert snap.key_undecryptable is True
        assert snap.api_key.source == "unset"


class FingerprintTests(SimpleTestCase):
    def test_long_value_shows_last_four(self):
        assert conf.fingerprint(DB_KEY) == "…" + DB_KEY[-4:]

    def test_short_value_is_fully_masked(self):
        assert conf.fingerprint("short") == "…****"

    def test_empty(self):
        assert conf.fingerprint("") == ""


class FernetCheckTests(SimpleTestCase):
    def test_w001_when_unset(self):
        with override_settings(LLMHUB_FERNET_KEY=""):
            ids = [w.id for w in checks.fernet_key_check(None)]
        assert ids == ["llmhub.W001"]

    def test_no_w001_when_set(self):
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            assert checks.fernet_key_check(None) == []

    def test_w002_base_url_outside_allowlist(self):
        with override_settings(LLMHUB_GATEWAY_BASE_URL="https://a.test", LLMHUB_ALLOWED_BASE_URLS=["https://b.test"]):
            assert [w.id for w in checks.allowlist_check(None)] == ["llmhub.W002"]
        with override_settings(LLMHUB_GATEWAY_BASE_URL="https://a.test/", LLMHUB_ALLOWED_BASE_URLS=["https://a.test"]):
            assert checks.allowlist_check(None) == []


class DerivedTokenRotationTests(HubTestCase):
    """A token stored under the SECRET_KEY-derived key moves under the
    dedicated key the first time it is read (review 2026-10-17): after that a
    SECRET_KEY rotation cannot void it."""

    def test_rotated(self):
        stored = crypto.encrypt("secret-value")
        assert crypto.rotated(stored) is None  # no dedicated key: nothing to move to
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            moved = crypto.rotated(stored)
            assert moved and crypto.rotated(moved) is None
            assert crypto.decrypt(moved) == "secret-value"
            assert crypto.rotated("") is None
            assert crypto.rotated("garbage") is None
        assert Fernet(KEY_A.encode()).decrypt(moved.encode()) == b"secret-value"

    def test_resolving_the_endpoint_moves_the_stored_token(self):
        row = save_gateway(key=DB_KEY)
        derived = row.gateway_api_key_enc
        with override_settings(LLMHUB_FERNET_KEY=KEY_A):
            reset_state()
            snap = conf.snapshot(fresh=True)
            assert (snap.api_key.value, snap.api_key.source) == (DB_KEY, "db")
            stored = LLMSettings.objects.get(pk=1).gateway_api_key_enc
            assert stored != derived
            assert Fernet(KEY_A.encode()).decrypt(stored.encode()) == DB_KEY.encode()
            # Idempotent: a second read writes nothing.
            reset_state()
            conf.snapshot(fresh=True)
            assert LLMSettings.objects.get(pk=1).gateway_api_key_enc == stored
            with override_settings(SECRET_KEY="rotated-secret-key"):
                reset_state()
                assert conf.snapshot(fresh=True).api_key.value == DB_KEY
