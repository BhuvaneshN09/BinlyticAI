// Vercel endpoint: mirrors the live bin state from the PRIVATE
// BhuvaneshN09/binlytic-state repo. cloud_sync.py pushes bins.json and
// learning.json there; this function fetches them at request time using
// a read-only token from the STATE_REPO_TOKEN environment variable
// (set in the Vercel project settings — never committed to git).
// Falls back to the bundled snapshot if the token is missing or GitHub
// is unreachable.
const fallback = require("./state-data.json");

const CONTENTS_BASE =
  "https://api.github.com/repos/BhuvaneshN09/binlytic-state/contents";

async function fetchPrivateJson(name, token) {
  const response = await fetch(`${CONTENTS_BASE}/${name}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github.raw+json",
      "User-Agent": "binlytic-dashboard",
    },
  });
  if (!response.ok) throw new Error(`${name}: HTTP ${response.status}`);
  return response.json();
}

module.exports = async (req, res) => {
  let state = fallback;
  let learning = { references: {}, candidates: [] };
  let mirrorFresh = false;

  const token = process.env.STATE_REPO_TOKEN;
  if (token) {
    try {
      state = await fetchPrivateJson("bins.json", token);
      mirrorFresh = true;
      learning = await fetchPrivateJson("learning.json", token);
    } catch (error) {
      // keep whatever we managed to load; fall back otherwise
    }
  }

  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.status(200).json({
    ...state,
    server_time: new Date().toISOString(),
    confirmation_window_seconds: 30,
    controller: {
      port: "COM5",
      online: false,
      mode: "mirror",
      mirror_fresh: mirrorFresh,
    },
    learning_summary: {
      learned_classes: Object.keys(learning.references || {}).length,
      collecting_examples: (learning.candidates || []).length,
    },
  });
};
