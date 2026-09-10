import pytest

pytestmark = pytest.mark.asyncio


async def test_register_and_login(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "alice@example.com",
            "password": "supersecret1",
            "full_name": "Alice Passenger",
            "role": "passenger",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["email"] == "alice@example.com"

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "supersecret1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body


async def test_duplicate_registration_rejected(client):
    payload = {
        "email": "bob@example.com",
        "password": "supersecret1",
        "full_name": "Bob",
        "role": "passenger",
    }
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409


async def test_login_with_wrong_password_rejected(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "carol@example.com", "password": "correct-password", "full_name": "Carol"},
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "carol@example.com", "password": "wrong-password"}
    )
    assert resp.status_code == 401


async def test_refresh_token_rotation_and_reuse_detection(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "dave@example.com", "password": "supersecret1", "full_name": "Dave"},
    )
    login_resp = await client.post(
        "/api/v1/auth/login", json={"email": "dave@example.com", "password": "supersecret1"}
    )
    old_refresh = login_resp.json()["refresh_token"]

    refresh_resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh_resp.status_code == 200
    assert refresh_resp.json()["refresh_token"] != old_refresh

    # Reusing the now-rotated (revoked) token must fail.
    reuse_resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse_resp.status_code == 401


async def test_me_requires_valid_token(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401

    await client.post(
        "/api/v1/auth/register",
        json={"email": "erin@example.com", "password": "supersecret1", "full_name": "Erin"},
    )
    login_resp = await client.post(
        "/api/v1/auth/login", json={"email": "erin@example.com", "password": "supersecret1"}
    )
    access_token = login_resp.json()["access_token"]

    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "erin@example.com"
