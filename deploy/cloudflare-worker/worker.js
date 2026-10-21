export default {
  async scheduled(event, env, ctx) {
    const response = await fetch(
      `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/reports.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "FullgoalQuant-Scheduler",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main", inputs: { mode: "scheduled" } }),
      },
    );
    if (response.status !== 204) {
      throw new Error(`GitHub dispatch failed: HTTP ${response.status}`);
    }
  },
};
