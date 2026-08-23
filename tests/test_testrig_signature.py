"""Tests for the signature-compatibility rule behind the base-contract check.

This is the logic that would have caught the stale ``connect()`` signature that
shipped for months, so it is tested locally even though the check itself only
runs on the remote host against the real Hermes.
"""

from __future__ import annotations

from testrig.remote_probe import _signature_compatible


def test_identical_signatures_are_compatible():
    def base(self, *, is_reconnect: bool = False): ...
    def impl(self, *, is_reconnect: bool = False): ...

    assert _signature_compatible(base, impl)


def test_missing_keyword_only_param_is_the_historical_bug():
    """The exact drift that shipped: gateway calls connect(is_reconnect=...)."""

    def base(self, *, is_reconnect: bool = False): ...
    def impl(self): ...

    assert not _signature_compatible(base, impl)


def test_kwargs_absorbs_keyword_only_params():
    def base(self, *, is_reconnect: bool = False): ...
    def impl(self, **kwargs): ...

    assert _signature_compatible(base, impl)


def test_extra_optional_param_on_impl_is_fine():
    def base(self, *, is_reconnect: bool = False): ...
    def impl(self, *, is_reconnect: bool = False, extra: int = 0): ...

    assert _signature_compatible(base, impl)


def test_missing_positional_param_is_incompatible():
    def base(self, chat_id, text): ...
    def impl(self, chat_id): ...

    assert not _signature_compatible(base, impl)


def test_renamed_param_is_incompatible():
    """A rename breaks keyword calls, so it must be reported."""

    def base(self, chat_id): ...
    def impl(self, chat): ...

    assert not _signature_compatible(base, impl)


def test_base_varargs_do_not_require_impl_params():
    def base(self, *args, **kwargs): ...
    def impl(self): ...

    assert _signature_compatible(base, impl)
