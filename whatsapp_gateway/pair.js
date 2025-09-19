// Create a public HTTPS URL for the local gateway and register it back to the server.
const lt = require('localtunnel');
const axios = require('axios');

function getArg(name, def = null) {
  const idx = process.argv.indexOf(`--${name}`);
  if (idx >= 0 && idx + 1 < process.argv.length) return process.argv[idx + 1];
  return process.env[name.toUpperCase()] || def;
}

(async () => {
  const port = parseInt(process.env.GATEWAY_PORT || '3000', 10);
  const pairPath = getArg('pair', null); // e.g., /ui/local/register/<token>
  if (!pairPath) {
    console.error('PAIR_URL not provided. Usage: PAIR_URL="/ui/local/register/<token>" npm run pair');
    process.exit(2);
  }

  // Build full register URL. Assume same host that served the UI,
  // by default use blank-project.onrender.com unless overridden.
  const host = getArg('host', process.env.PAIR_HOST || 'https://blank-project.onrender.com');
  let registerUrl = pairPath;
  if (!/^https?:\\/\\//i.test(registerUrl)) {
    registerUrl = host.replace(/\\/$/, '') + (pairPath.startsWith('/') ? pairPath : '/' + pairPath);
  }

  console.log('[pair] Opening tunnel for local gateway on port', port);
  const tunnel = await lt({ port });
  const publicUrl = tunnel.url.replace(/\\/$/, '');

  console.log('[pair] Public URL:', publicUrl);
  console.log('[pair] Registering to:', registerUrl);

  try {
    await axios.post(registerUrl, { base_url: publicUrl }, { timeout: 10000 });
    console.log('[pair] Registered successfully.');
  } catch (e) {
    console.error('[pair] Failed to register:', e.response ? e.response.data : e.message);
    // keep running tunnel anyway
  }

  console.log('[pair] Tunnel is active. Keep this process running. Press Ctrl+C to stop.');
  // Keep alive
  // The localtunnel client keeps the process open; if it closes, exit.
  tunnel.on('close', () => {
    console.log('[pair] Tunnel closed.');
    process.exit(0);
  });
})();