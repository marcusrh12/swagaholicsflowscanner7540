// FlowScanner on-time trigger.
//
// GitHub's native `schedule` events can be delayed by hours during peak load,
// which made premarket scans fire after the open. Cloudflare Cron Triggers fire
// within seconds of their scheduled minute, so this Worker is the primary
// trigger: at each firing it resolves which session is due and calls GitHub's
// workflow_dispatch API with session_type already decided. The workflow trusts
// that value verbatim — no wall-clock guessing on the GitHub side.
//
// WHEN this Worker fires it fires punctually, but WHETHER it fires is not
// guaranteed: Cloudflare gives Cron Triggers no execution guarantee, and on
// 2026-07-17 the 13:00 UTC firing was skipped silently (see README.md). That is
// why scan.yml keeps its own `schedule` crons as a backup. Note the coupling:
// the hour gate below is only correct BECAUSE CF is punctual — a late firing
// would read the wrong Eastern hour and skip rather than misfire, which is the
// safe direction, and the GitHub backup then covers the miss.
//
// Cloudflare crons are UTC and do not follow DST, so (like GitHub) we register
// both the EDT and EST firing of each session in wrangler.toml. Here we gate on
// the ACTUAL Eastern hour, so exactly one twin of each pair proceeds and the
// other is a no-op.

const OWNER = "marcusrh12";
const REPO = "swagaholicsflowscanner7540";
const WORKFLOW = "scan.yml";
const REF = "main";

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchIfDue(env));
  },
};

async function dispatchIfDue(env) {
  const etHour = easternHour();

  let session;
  if (etHour === 9) session = "premarket";
  else if (etHour === 14) session = "pulse";
  else {
    // The DST twin firing (e.g. the EST-offset cron running while EDT is in
    // effect). Nothing to do.
    console.log(`Eastern hour ${etHour} is not a session hour — skipping.`);
    return;
  }

  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "flowscanner-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: REF, inputs: { session_type: session } }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`workflow_dispatch failed (${res.status}): ${body}`);
  }
  console.log(`Dispatched ${session} scan (Eastern hour ${etHour}).`);
}

// Current hour (0-23) in US Eastern, DST-aware.
function easternHour() {
  return Number(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hourCycle: "h23",
      hour: "2-digit",
    }).format(new Date())
  );
}
