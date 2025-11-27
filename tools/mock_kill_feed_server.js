#!/usr/bin/env node
/**
 * Simple mock WebSocket server that mimics CRCON's kill feed so you can test
 * ENABLE_KILL_FEED locally without needing a live server. The script accepts
 * the following flags (or env vars):
 *   --port <number>        PORT env var (default: 8765)
 *   --token <value>        MOCK_TOKEN env var (default: mock-token)
 *   --auto-interval <ms>   AUTO_INTERVAL env var (optional) to auto-stream kills
 *
 * Usage examples:
 *   node tools/mock_kill_feed_server.js
 *   node tools/mock_kill_feed_server.js --port 9001 --token local-dev
 *   AUTO_INTERVAL=3000 node tools/mock_kill_feed_server.js
 */

const WebSocket = require('ws');
const readline = require('readline');

function getArg(flag, defaultValue) {
  const index = process.argv.indexOf(flag);
  if (index !== -1 && index + 1 < process.argv.length) {
    return process.argv[index + 1];
  }
  return defaultValue;
}

const port = Number(process.env.PORT || getArg('--port', 8765));
const authToken = process.env.MOCK_TOKEN || getArg('--token', 'mock-token');
const autoInterval = Number(
  process.env.AUTO_INTERVAL || getArg('--auto-interval', 0)
);

const sampleKills = [
  {
    killer_name: 'Able Gunner',
    killer_team: 'Allies',
    victim_name: 'Axis Defender',
    victim_team: 'Axis',
    weapon: '75mm Cannon',
    vehicle: 'Panther',
    keyword_group: 'cannon_shells',
    keyword_match: '75mm'
  },
  {
    killer_name: 'Panzer Ace',
    killer_team: 'Axis',
    victim_name: 'Sherman Crew',
    victim_team: 'Allies',
    weapon: '88mm AP',
    vehicle: 'M4 Sherman',
    keyword_group: 'cannon_shells',
    keyword_match: '88mm'
  },
  {
    killer_name: 'Engineer Bob',
    killer_team: 'Allies',
    victim_name: 'Tiger Crew',
    victim_team: 'Axis',
    weapon: 'Satchel Charge',
    vehicle: 'Tiger',
    keyword_group: 'explosives',
    keyword_match: 'satchel'
  }
];

let killIndex = 0;

const wss = new WebSocket.Server({ port }, () => {
  console.log(`🛰️  Mock kill feed listening on ws://localhost:${port}`);
  console.log('Headers: Authorization: Bearer <token>');
  console.log('Press ENTER to send the next sample kill event.');
  console.log('Or paste raw JSON (per line) to broadcast custom payloads.');
});

function nextSampleKill() {
  const payload = sampleKills[killIndex % sampleKills.length];
  killIndex += 1;
  return {
    ...payload,
    timestamp: new Date().toISOString()
  };
}

function broadcast(data) {
  const message = typeof data === 'string' ? data : JSON.stringify(data);
  wss.clients.forEach((client) => {
    if (client.readyState === WebSocket.OPEN) {
      client.send(message);
    }
  });
  console.log(`📤 Sent event to ${wss.clients.size} subscriber(s).`);
}

wss.on('connection', (socket, req) => {
  const header = req.headers['authorization'] || '';
  const expects = `Bearer ${authToken}`;
  if (authToken && header !== expects) {
    console.warn('❌ Connection rejected: bad token');
    socket.close(4401, 'Unauthorized');
    return;
  }

  console.log('✅ Client connected to mock kill feed');
  socket.on('close', (code) => {
    console.log(`👋 Client disconnected (code ${code})`);
  });
});

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) {
    broadcast(nextSampleKill());
    return;
  }

  try {
    const customPayload = JSON.parse(trimmed);
    broadcast(customPayload);
  } catch (err) {
    console.error('⚠️  Invalid JSON. Paste a valid JSON object or press ENTER.');
  }
});

if (autoInterval > 0) {
  console.log(`⏱️  Auto mode enabled (every ${autoInterval} ms)`);
  setInterval(() => broadcast(nextSampleKill()), autoInterval).unref();
}

process.on('SIGINT', () => {
  console.log('\n🛑 Shutting down mock kill feed');
  rl.close();
  wss.close(() => process.exit(0));
});
