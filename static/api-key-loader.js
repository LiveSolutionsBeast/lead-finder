// Shared API-key loader for Lead Finder static pages.
// Loads the runtime API key from /api/config so no secret is hard-coded in HTML.
const API_BASE = '/api';
let API_KEY = null;
let _apiKeyReady = false;
let _apiKeyPromise = null;

function _loadApiKeyInner() {
  if (_apiKeyPromise) return _apiKeyPromise;
  _apiKeyPromise = fetch(API_BASE + '/config')
    .then(async r => {
      if (!r.ok) throw new Error('config fetch failed: ' + r.status);
      const d = await r.json();
      API_KEY = d.lf_api_key || '';
      if (!API_KEY) throw new Error('server returned empty API key');
      _apiKeyReady = true;
      return API_KEY;
    })
    .catch(e => {
      console.error('initApiKey error:', e);
      _apiKeyReady = false;
      throw e;
    });
  return _apiKeyPromise;
}

async function initApiKey() {
  return _loadApiKeyInner();
}

function requireApiKey() {
  if (!_apiKeyReady || !API_KEY) {
    throw new Error('API key not loaded yet');
  }
  return API_KEY;
}

function apiKeyReady() {
  return _apiKeyReady && !!API_KEY;
}
