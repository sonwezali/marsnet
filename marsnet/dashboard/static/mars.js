// ── Node layout (fixed positions on 900×600 canvas) ──────────────────
const NODES = {
  rover_a: { x: 300, y: 450, label: "ROVER A", color: "#5080e0" },
  rover_b: { x: 600, y: 450, label: "ROVER B", color: "#5080e0" },
  relay:   { x: 450, y: 80,  label: "RELAY",   color: "#e0c050", isOrbiter: true },
};

const RADIUS = 18;
const SATELLITE_ARC_Y_CENTER = 160;
const SATELLITE_ARC_RX = 320;
const SATELLITE_ARC_RY = 80;

// ── State ─────────────────────────────────────────────────────────────
let activeContacts = new Set();   // contact_id strings
let failedNodes    = new Set();
let imageFragments = {};          // image_id → { received: Set<offset>, total: int }
let satelliteAngle = 0;           // radians, drives relay position
let simTime = 0;
let contactMap = {};              // contact_id → {from, to}

// ── Canvas setup ──────────────────────────────────────────────────────
const mapCanvas = document.getElementById("map");
const ctx       = mapCanvas.getContext("2d");
const imgCanvas = document.getElementById("img-preview");
const imgCtx    = imgCanvas.getContext("2d");

// ── WebSocket ─────────────────────────────────────────────────────────
const ws = new WebSocket(`ws://${location.host}/ws`);
ws.onopen  = () => document.getElementById("status").textContent = "● live";
ws.onclose = () => document.getElementById("status").textContent = "○ disconnected";

ws.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  simTime = ev.ts ?? simTime;
  handleEvent(ev);
  logEvent(ev);
};

function handleEvent(ev) {
  switch (ev.event) {
    case "contact_open":
      activeContacts.add(ev.contact_id);
      if (ev.from && ev.to) contactMap[ev.contact_id] = {from: ev.from, to: ev.to};
      break;
    case "contact_closed":
    case "contact_failed":
      activeContacts.delete(ev.contact_id);
      if (ev.event === "contact_failed") failedNodes.add(ev.node);
      break;
    case "fragment_received":
      trackFragment(ev); break;
  }
}

function trackFragment(ev) {
  const id = ev.image_id;
  if (!imageFragments[id]) imageFragments[id] = { received: new Set(), total: ev.total_size };
  imageFragments[id].received.add(ev.fragment_offset);
  updateImagePreview(id);
}

function updateImagePreview(id) {
  const f = imageFragments[id];
  const pct = f.received.size / Math.ceil(f.total / 512);
  document.getElementById("img-progress").textContent =
    `${id} — ${Math.round(pct * 100)}% received`;
  // Paint received bands green, missing grey
  imgCtx.clearRect(0, 0, 256, 256);
  const bands = Math.ceil(f.total / 512);
  const bh = 256 / bands;
  for (let i = 0; i < bands; i++) {
    imgCtx.fillStyle = f.received.has(i * 512) ? "#50c878" : "#2a2a35";
    imgCtx.fillRect(0, i * bh, 256, bh);
  }
}

function logEvent(ev) {
  const li = document.createElement("li");
  li.textContent = `[${ev.ts?.toFixed(1) ?? "?"}s] ${ev.node} ${ev.event}`;
  if (ev.event.includes("fail") || ev.event.includes("drop")) li.className = "fail";
  if (ev.event.includes("received") || ev.event.includes("open")) li.className = "ok";
  const log = document.getElementById("log");
  log.prepend(li);
  if (log.children.length > 100) log.removeChild(log.lastChild);
}

// ── Draw loop ─────────────────────────────────────────────────────────
function drawBackground() {
  ctx.fillStyle = "#0a0a0f";
  ctx.fillRect(0, 0, 900, 600);
  // Simple Mars surface gradient
  const grad = ctx.createLinearGradient(0, 350, 0, 600);
  grad.addColorStop(0, "#2a1208");
  grad.addColorStop(1, "#1a0a04");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 350, 900, 250);
}

function getSatellitePos() {
  return {
    x: 450 + SATELLITE_ARC_RX * Math.cos(satelliteAngle),
    y: SATELLITE_ARC_Y_CENTER + SATELLITE_ARC_RY * Math.sin(satelliteAngle),
  };
}

function drawNodes() {
  for (const [name, n] of Object.entries(NODES)) {
    const pos = name === "relay" ? getSatellitePos() : n;
    const failed = failedNodes.has(name);

    // Node circle
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, RADIUS, 0, Math.PI * 2);
    ctx.fillStyle = failed ? "#333" : n.color + "33";
    ctx.fill();
    ctx.strokeStyle = failed ? "#555" : n.color;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Label
    ctx.fillStyle = failed ? "#555" : "#e0d8c8";
    ctx.font = "10px monospace";
    ctx.textAlign = "center";
    ctx.fillText(n.label, pos.x, pos.y + RADIUS + 14);
  }
}

function drawContactLines() {
  // Draw a glowing line for each active contact
  for (const cid of activeContacts) {
    const info = contactMap[cid];
    if (!info) continue;
    const fromName = info.from;
    const toName = info.to;
    const fromNode = NODES[fromName];
    const toNode = NODES[toName];
    if (!fromNode || !toNode) continue;

    const fromPos = fromName === "relay" ? getSatellitePos() : fromNode;
    const toPos = toName === "relay" ? getSatellitePos() : toNode;

    ctx.strokeStyle = "rgba(80,200,120,0.6)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(fromPos.x, fromPos.y);
    ctx.lineTo(toPos.x, toPos.y);
    ctx.stroke();
  }
}

function draw() {
  drawBackground();
  drawContactLines();
  drawNodes();
  satelliteAngle += 0.005;  // slow orbit animation
  requestAnimationFrame(draw);
}

draw();
