"""Tests for Oura ring-model derivation from ring_configuration."""

from app.services.providers.oura.data_247 import Oura247Data


def test_derive_ring_model_basic() -> None:
    raw = {"data": [{"hardware_type": "gen3", "design": "horizon", "set_up_at": "2024-01-01T00:00:00+00:00"}]}
    assert Oura247Data._derive_ring_model(raw) == "Oura Ring Gen3 Horizon"


def test_derive_ring_model_picks_most_recent_ring() -> None:
    raw = {
        "data": [
            {"hardware_type": "gen2", "set_up_at": "2020-01-01T00:00:00+00:00"},
            {"hardware_type": "gen4", "design": "heritage", "set_up_at": "2025-01-01T00:00:00+00:00"},
        ]
    }
    assert Oura247Data._derive_ring_model(raw) == "Oura Ring Gen4 Heritage"


def test_derive_ring_model_hardware_only() -> None:
    assert Oura247Data._derive_ring_model({"data": [{"hardware_type": "gen3"}]}) == "Oura Ring Gen3"


def test_derive_ring_model_empty() -> None:
    assert Oura247Data._derive_ring_model({}) is None
    assert Oura247Data._derive_ring_model({"data": []}) is None
    assert Oura247Data._derive_ring_model({"data": [{}]}) == "Oura Ring"
