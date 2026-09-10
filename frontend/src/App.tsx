import { useState, useRef } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

type Ride = {
  id: string;
  status: string;
  version: number;
  fare_estimate: number | null;
};

export default function App() {
  const [email, setEmail] = useState("demo@rideflow.dev");
  const [password, setPassword] = useState("password123");
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [ride, setRide] = useState<Ride | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  const appendLog = (msg: string) => setLog((prev) => [...prev, msg]);

  async function registerAndLogin() {
    await fetch(`${API_BASE}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, full_name: "Demo Passenger", role: "passenger" }),
    }).catch(() => null); // ignore "already registered" on repeated demo runs

    const res = await fetch(`${API_BASE}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    setAccessToken(data.access_token);
    appendLog(`Logged in as ${email}`);
  }

  async function requestRide() {
    if (!accessToken) return;
    const res = await fetch(`${API_BASE}/api/v1/rides`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${accessToken}`,
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({
        pickup_lat: 12.9716,
        pickup_lng: 77.5946,
        dropoff_lat: 12.9352,
        dropoff_lng: 77.6146,
      }),
    });
    const data = await res.json();
    setRide(data);
    appendLog(`Ride requested: ${data.id} (fare est. $${data.fare_estimate})`);

    const ws = new WebSocket(`${API_BASE.replace("http", "ws")}/ws/rides/${data.id}?token=${accessToken}`);
    ws.onmessage = (event) => appendLog(`Live update: ${event.data}`);
    ws.onopen = () => appendLog("WebSocket connected for live tracking");
    wsRef.current = ws;
  }

  async function startMatching() {
    if (!accessToken || !ride) return;
    const res = await fetch(`${API_BASE}/api/v1/rides/${ride.id}/transition`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ target_status: "matching", expected_version: ride.version }),
    });
    const data = await res.json();
    setRide(data);
    appendLog(`Matching result: ${data.status}`);
  }

  return (
    <div style={{ fontFamily: "system-ui, sans-serif", maxWidth: 560, margin: "40px auto", padding: 16 }}>
      <h1>RideFlow</h1>
      <p style={{ color: "#666" }}>Minimal demo client for the RideFlow API.</p>

      <section style={{ marginBottom: 24 }}>
        <h2>1. Account</h2>
        <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email" style={{ marginRight: 8 }} />
        <input
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="password"
          type="password"
          style={{ marginRight: 8 }}
        />
        <button onClick={registerAndLogin}>Register / Login</button>
        {accessToken && <p style={{ color: "green" }}>Authenticated ✓</p>}
      </section>

      <section style={{ marginBottom: 24 }}>
        <h2>2. Ride</h2>
        <button onClick={requestRide} disabled={!accessToken} style={{ marginRight: 8 }}>
          Request ride
        </button>
        <button onClick={startMatching} disabled={!ride}>
          Start matching
        </button>
        {ride && (
          <p>
            Ride <code>{ride.id}</code> — status: <b>{ride.status}</b>
          </p>
        )}
      </section>

      <section>
        <h2>3. Live log</h2>
        <ul>
          {log.map((line, i) => (
            <li key={i} style={{ fontFamily: "monospace", fontSize: 13 }}>
              {line}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
