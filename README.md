# ParkProxy — Private Audio Calling

A zero-dependency, privacy-focused WebRTC audio calling application built with FastAPI, Jinja2, and native browser WebRTC. Two people communicate over a web browser using a unique room URL — no phone numbers, IP addresses, or personal data are revealed.

## Features

- **Audio-only calls** — video explicitly disabled (`video: false`)
- **WebRTC signaling** via WebSockets (SDP offers, answers, ICE candidates)
- **Room limit** — maximum of 2 active WebSocket connections per room ("Room Full" enforcement)
- **NAT traversal** — public Google STUN servers + self-hosted coturn TURN/TURNS servers
- **Time-limited TURN credentials** — coturn `use-auth-secret` mode, short-lived HMAC credentials issued by the backend
- **Graceful error handling** — room full, WebSocket disconnections, microphone permission denials, call termination
- **Responsive single-page frontend** — vanilla JS, `async/await`, native `RTCPeerConnection` API
- **Privacy** — no accounts, no phone numbers, no personal data collected

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | FastAPI, Uvicorn, Jinja2 |
| Signaling | WebSockets |
| Frontend | HTML5, CSS3, Vanilla JS, WebRTC API |
| NAT Traversal | STUN (Google), TURN/TURNS (coturn) |
| Auth | Time-limited HMAC TURN credentials |

## Project Structure

```
park-assist/
├── main.py              # FastAPI app, WebSocket signaling, TURN credentials endpoint
├── requirements.txt     # Python dependencies
├── templates/
│   └── index.html       # Responsive SPA: WebRTC client + UI
└── README.md
```

## Local Development

### 1. Clone and create virtual environment

```powershell
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 2. Run coturn locally (WSL2 Ubuntu)

coturn does not run natively on Windows. Use WSL2:

```bash
# Inside WSL2 Ubuntu
sudo apt update
sudo apt install coturn
```

Generate a self-signed cert for local `turns:` testing:

```bash
sudo mkdir -p /etc/turnserver_certs
sudo openssl req -x509 -newkey rsa:2048 -keyout /etc/turnserver_certs/key.pem -out /etc/turnserver_certs/cert.pem -days 365 -nodes -subj "/CN=localhost"
```

Edit `/etc/turnserver.conf`:

```ini
listening-port=3478
tls-listening-port=5349
listening-ip=0.0.0.0
realm=localhost
server-name=localhost
use-auth-secret=changeme-turn-secret
cert=/etc/turnserver_certs/cert.pem
pkey=/etc/turnserver_certs/key.pem
no-stun
min-port=49152
max-port=65535
```

Fix permissions and start:

```bash
sudo chmod 644 /etc/turnserver_certs/key.pem /etc/turnserver_certs/cert.pem
sudo systemctl restart coturn
sudo ss -tulpn | grep -E '3478|5349'
```

### 3. Run FastAPI

```powershell
$env:TURN_SECRET="changeme-turn-secret"
$env:TURN_REALM="localhost"
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in two browsers (or two devices on the same network). Enter the same room name to start a call.

## Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `TURN_SECRET` | HMAC secret shared with coturn (auth-secret mode) | `changeme-turn-secret` | Yes (production) |
| `TURN_REALM` | TURN realm (your coturn domain) | `localhost` | Yes (production) |
| `TURN_TTL_SECONDS` | TURN credential time-to-live in seconds | `86400` | No |
| `PORT` | Application port | `8000` | No |

## Production Deployment

### Option A: Render + Separate coturn VPS (Free)

Render hosts FastAPI; coturn runs on a free Linux VPS (e.g., Oracle Cloud Always Free).

**1. coturn VPS**
- Provision an Ubuntu VPS with a public IP.
- Open firewall ports: UDP/TCP 3478, UDP/TCP 5349, UDP 49152–65535.
- Install coturn, obtain a Let's Encrypt cert for `turn.yourdomain.com`.
- Configure `/etc/turnserver.conf` with `use-auth-secret` and TLS certs.

**2. FastAPI on Render**
- Connect GitHub repo to Render Web Service.
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Environment variables: `TURN_SECRET`, `TURN_REALM=turn.yourdomain.com`

**3. Frontend**
In `templates/index.html`, update `buildRtcConfig()` to point at your coturn domain:

```js
iceServers.push(
  { urls: "turn:turn.yourdomain.com:3478", username, credential },
  { urls: "turns:turn.yourdomain.com:5349?transport=tcp", username, credential }
);
```

### Option B: Single Cheap VPS (~$5/mo)

Run both FastAPI and coturn on one Linux VPS (Hetzner, DigitalOcean, AWS Lightsail). Use a `docker-compose.yml` or systemd units to manage both services.

## How It Works

1. **Room creation**: User enters a room name and clicks "Join Call".
2. **Media**: Browser requests microphone access (`audio: true, video: false`).
3. **Signaling**: Client connects to `ws://host/ws/{room_id}`. Server assigns a WebRTC role (`offerer` / `answerer`) to prevent offer/answer glare.
4. **TURN credentials**: Client fetches short-lived credentials from `GET /turn-credentials` (HMAC-based, expires after TTL).
5. **WebRTC handshake**: Offerer creates an SDP offer; answerer responds with an SDP answer. ICE candidates are relayed via WebSocket.
6. **Media flow**: Audio streams flow peer-to-peer when possible; coturn relays media when NAT/firewall blocks direct connection.
7. **Termination**: Either peer sends a `bye` message; both sides clean up streams and close connections.

## Security & Privacy

- No phone numbers, email addresses, or personal data are collected.
- Room IDs are client-generated random strings.
- TURN credentials are time-limited and single-purpose (HMAC auth-secret).
- Media is encrypted via WebRTC's mandatory SRTP.
- Self-signed certs are for local testing only; production requires valid TLS certs.

## Limitations

- Render free tier spins down after inactivity (cold start ~30–60s).
- Browsers reject self-signed certs for `turns:`; use valid certs in production.
- Maximum 2 participants per room by design.

## License

MIT