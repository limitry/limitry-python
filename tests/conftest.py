"""The package in src/ under test."""

import importlib
import pathlib
import sys

import pytest

MODULE, CLIENT, KEY_ENV = "limitry", "Limitry", "LIMITRY_API_KEY"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(scope="session")
def sdk():
    return importlib.import_module(MODULE)


@pytest.fixture(scope="session")
def names():
    return {"client": CLIENT, "key_env": KEY_ENV}
