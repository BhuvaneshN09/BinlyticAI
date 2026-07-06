// Read-only demo endpoint for the Vercel static build. Serves a fixed
// snapshot of dashboard/data/bins.json — no writes, no live hardware.
const snapshot = require("./state-data.json");

module.exports = (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.status(200).json({
    ...snapshot,
    server_time: new Date().toISOString(),
    confirmation_window_seconds: 30,
    controller: { port: "COM5", online: false, description: null },
    learning_summary: { learned_classes: 0, collecting_examples: 0 },
  });
};
