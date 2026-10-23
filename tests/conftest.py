"""Hermetic test setup: no live AWS/Bedrock calls can ever happen.

Autouse fixture (a) replaces AWS credentials in the environment with obviously
fake values and removes session/profile/bearer variables, and (b) monkeypatches
boto3 client construction to raise unless a test explicitly opts in with a
stubbed client passed directly to BedrockJudge.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import boto3.session
import pytest


@pytest.fixture(autouse=True)
def hermetic_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing-not-a-real-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing-not-a-real-value")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for var in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def forbidden_client(*args: object, **kwargs: object) -> object:
        raise RuntimeError(
            "tests must not construct real boto3 clients; pass a stubbed client "
            "to BedrockJudge(client=...) instead"
        )

    monkeypatch.setattr(boto3, "client", forbidden_client)
    monkeypatch.setattr(boto3.session.Session, "client", forbidden_client)


@pytest.fixture()
def fixture_dbs(tmp_path: Path) -> dict[str, Path]:
    from tests.fixtures.build_fixture_db import build_fixture_store

    return build_fixture_store(tmp_path / "stores")
