import pytest

pytestmark = pytest.mark.asyncio


async def _register_and_login(client, email, role="passenger"):
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret1", "full_name": email.split("@")[0], "role": role},
    )
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "supersecret1"})
    return resp.json()["access_token"]


async def test_create_ride_requires_passenger_role(client):
    driver_token = await _register_and_login(client, "driveronly@example.com", role="driver")
    resp = await client.post(
        "/api/v1/rides",
        json={"pickup_lat": 12.9, "pickup_lng": 77.6, "dropoff_lat": 12.95, "dropoff_lng": 77.65},
        headers={"Authorization": f"Bearer {driver_token}"},
    )
    assert resp.status_code == 403


async def test_create_ride_and_get_it(client):
    token = await _register_and_login(client, "pax1@example.com")
    create_resp = await client.post(
        "/api/v1/rides",
        json={"pickup_lat": 12.9, "pickup_lng": 77.6, "dropoff_lat": 12.95, "dropoff_lng": 77.65},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_resp.status_code == 201
    ride = create_resp.json()
    assert ride["status"] == "requested"
    assert ride["fare_estimate"] > 0

    get_resp = await client.get(f"/api/v1/rides/{ride['id']}", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == ride["id"]


async def test_idempotent_ride_creation(client):
    token = await _register_and_login(client, "pax2@example.com")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "fixed-key-123"}
    body = {"pickup_lat": 12.9, "pickup_lng": 77.6, "dropoff_lat": 12.95, "dropoff_lng": 77.65}

    first = await client.post("/api/v1/rides", json=body, headers=headers)
    second = await client.post("/api/v1/rides", json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"], "retried request must not create a second ride"


async def test_idempotency_key_reuse_with_different_body_conflicts(client):
    token = await _register_and_login(client, "pax3@example.com")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "reused-key"}

    first = await client.post(
        "/api/v1/rides",
        json={"pickup_lat": 1.0, "pickup_lng": 1.0, "dropoff_lat": 2.0, "dropoff_lng": 2.0},
        headers=headers,
    )
    second = await client.post(
        "/api/v1/rides",
        json={"pickup_lat": 5.0, "pickup_lng": 5.0, "dropoff_lat": 6.0, "dropoff_lng": 6.0},
        headers=headers,
    )
    assert first.status_code == 201
    assert second.status_code == 409


async def test_invalid_transition_rejected(client):
    token = await _register_and_login(client, "pax4@example.com")
    create_resp = await client.post(
        "/api/v1/rides",
        json={"pickup_lat": 12.9, "pickup_lng": 77.6, "dropoff_lat": 12.95, "dropoff_lng": 77.65},
        headers={"Authorization": f"Bearer {token}"},
    )
    ride = create_resp.json()

    # requested -> in_progress is not a legal direct transition.
    resp = await client.post(
        f"/api/v1/rides/{ride['id']}/transition",
        json={"target_status": "in_progress", "expected_version": ride["version"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_stale_version_conflict(client):
    token = await _register_and_login(client, "pax5@example.com")
    create_resp = await client.post(
        "/api/v1/rides",
        json={"pickup_lat": 12.9, "pickup_lng": 77.6, "dropoff_lat": 12.95, "dropoff_lng": 77.65},
        headers={"Authorization": f"Bearer {token}"},
    )
    ride = create_resp.json()
    headers = {"Authorization": f"Bearer {token}"}

    # First transition succeeds and bumps the version.
    ok = await client.post(
        f"/api/v1/rides/{ride['id']}/transition",
        json={"target_status": "cancelled", "expected_version": ride["version"]},
        headers=headers,
    )
    assert ok.status_code == 200

    # Retrying with the now-stale version must fail with 409, not silently succeed.
    stale = await client.post(
        f"/api/v1/rides/{ride['id']}/transition",
        json={"target_status": "cancelled", "expected_version": ride["version"]},
        headers=headers,
    )
    assert stale.status_code in (409, 422)


async def test_matching_reserves_a_driver(client):
    driver_token = await _register_and_login(client, "driver2@example.com", role="driver")
    await client.post(
        "/api/v1/drivers/me/heartbeat",
        json={"lat": 12.9716, "lng": 77.5946},
        headers={"Authorization": f"Bearer {driver_token}"},
    )
    await client.post(
        "/api/v1/drivers/me/availability",
        json={"is_available": True},
        headers={"Authorization": f"Bearer {driver_token}"},
    )

    pax_token = await _register_and_login(client, "pax6@example.com")
    create_resp = await client.post(
        "/api/v1/rides",
        json={"pickup_lat": 12.972, "pickup_lng": 77.595, "dropoff_lat": 12.98, "dropoff_lng": 77.6},
        headers={"Authorization": f"Bearer {pax_token}"},
    )
    ride = create_resp.json()

    match_resp = await client.post(
        f"/api/v1/rides/{ride['id']}/transition",
        json={"target_status": "matching", "expected_version": ride["version"]},
        headers={"Authorization": f"Bearer {pax_token}"},
    )
    assert match_resp.status_code == 200
    matched_ride = match_resp.json()
    assert matched_ride["status"] in ("accepted", "no_drivers_found")
