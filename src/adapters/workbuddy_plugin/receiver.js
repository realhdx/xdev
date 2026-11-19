// This module runs through WorkBuddy's documented function-hook host, not in an
// independent CLI session. No provider/gateway credentials are read or copied.
const bindings = new Map();
const safeId = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const sessionId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const schema = "xdev.workbuddy-bridge.v1";

async function json($, path) {
  return JSON.parse(await $.fs.read(path));
}

async function save($, path, value) {
  await $.fs.write(path, JSON.stringify(value));
}

async function inspectHost($, id) {
  const tools = await $.tool.list();
  const names = tools.map(t => t.name).filter(n => typeof n === "string" && /xdev/i.test(n));
  const who = {};
  try { who.local = await $.mcp.call("xdev", "xdev_who", {host: "local"}); }
  catch (e) { who.local = {error: String(e)}; }
  return {session_id: id, tools: names, who};
}

async function bind($) {
  const id = await $.session.id();
  if (!sessionId.test(id || "") || bindings.has(id)) return;
  const local = await $.env.get("LOCALAPPDATA");
  if (!local || /[\r\n\0]/.test(local)) return;
  const base = `${local.replace(/\\/g, "/")}/CrossDeviceCollab/state/workbuddy-sessions/${id}`;
  const binding = {running: false, base, id, timer: undefined, inspector: undefined};
  bindings.set(id, binding);
  try {
    // The host performs these reads over its own existing MCP connection.
    binding.inspector = await inspectHost($, id);
    await save($, `${base}/requests/.keep.json`, {kind: "directory-marker"});
    await save($, `${base}/admissions/.keep.json`, {kind: "directory-marker"});
    await save($, `${base}/bridge.json`, {
      schema, session_id: id, plugin: "xdev-session-receiver", plugin_version: "0.1.2",
      observed_at_ms: await $.clock.now(), state: "loaded", inspection: binding.inspector
    });
    const poll = async () => {
      if (binding.running) return;
      binding.running = true;
      try {
        // A shared WorkBuddy worker can change its current conversation. Fail
        // closed rather than submit a notice to whichever one happens to be open.
        if (await $.session.id() !== id) return;
        await save($, `${base}/bridge.json`, {
          schema, session_id: id, plugin: "xdev-session-receiver", plugin_version: "0.1.2",
          observed_at_ms: await $.clock.now(), state: "loaded", inspection: binding.inspector
        });
        const entries = (await $.fs.list(`${base}/requests`))
          .filter(x => x.kind === "file" && !x.isLink && x.name.endsWith(".json") && !x.name.startsWith("."))
          .sort((a,b) => a.name.localeCompare(b.name));
        let attempted = 0;
        for (const entry of entries) {
          // Finished requests must not occupy the per-round submission limit.
          const filenameId = entry.name.slice(0, -5);
          if (!safeId.test(filenameId) || await $.fs.exists(`${base}/admissions/${filenameId}.json`)) continue;
          // A sender can die after exclusive creation but before finishing JSON.
          // Isolate malformed/partial requests so one cannot block later notices.
          let request;
          try {
            if (typeof entry.size === "number" && entry.size > 8192) continue;
            request = await json($, `${base}/requests/${entry.name}`);
          } catch { continue; }
          if (!request || typeof request !== "object" || Array.isArray(request)) continue;
          if (request.schema !== schema || request.session_id !== id || !safeId.test(request.notice_id || "") ||
              typeof request.message_id !== "string" || !request.message_id || request.message_id.length > 256 ||
              entry.name !== `${request.notice_id}.json` || typeof request.notice !== "string" ||
              !request.notice.includes(request.message_id) || request.notice.length > 1200) continue;
          const admissionPath = `${base}/admissions/${request.notice_id}.json`;
          if (await $.fs.exists(admissionPath)) continue;
          if (attempted++ >= 16) break;
          const receipt = {schema, session_id: id, message_id: request.message_id, notice_id: request.notice_id, state: "dispatching",
            observed_at_ms: await $.clock.now()};
          // A reservation survives reload and prevents retries after an uncertain
          // native submit. Do not confuse this reservation with native acceptance.
          await save($, admissionPath, receipt);
          if (await $.session.id() !== id) {
            await save($, admissionPath, {...receipt, state: "not_submitted", reason: "session_changed"});
            continue;
          }
          try {
            const result = await $.prompt.submit({text: request.notice});
            await save($, admissionPath, {...receipt,
              state: typeof result?.drop === "string" ? "rejected" :
                result?.accepted === true && result?.text === request.notice ? "input_surface_accepted" : "unknown",
              reason: result?.drop, observed_at_ms: await $.clock.now()});
          } catch (e) {
            await save($, admissionPath, {...receipt, state: "unknown", reason: String(e),
              observed_at_ms: await $.clock.now()});
          }
        }
      } catch (e) {
        // Queue files remain durable. No autonomous model retry or empty prompt.
        await save($, `${base}/last-error.json`, {schema, session_id: id, error: String(e),
          observed_at_ms: await $.clock.now()}).catch(() => {});
      } finally { binding.running = false; }
    };
    binding.timer = $.clock.every(2000, poll);
  } catch (e) {
    bindings.delete(id);
    console.error(`[xdev-session-receiver] load failed: ${String(e)}`);
  }
}

export function register(on) {
  for (const event of ["session.start", "session.attach"]) {
    on(event, async ($, e, next) => {
      const result = await next(e);
      await bind($);
      return result;
    });
  }
}

export default {register};
