// SPDX-License-Identifier: MPL-2.0
const assert = require("node:assert/strict");
const { chromium } = require("playwright-core");

const fixture = `<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Review</title>
<form hidden>
  <label>Review note <input name="note"></label>
  <button type="submit">Confirm note</button>
</form>
<p role="status">Loading</p>
<script>
  const form = document.querySelector("form");
  const statusLine = document.querySelector('[role="status"]');
  form.addEventListener("submit", event => {
    event.preventDefault();
    statusLine.textContent = form.elements.note.value;
  });
  setTimeout(() => {
    form.hidden = false;
    statusLine.textContent = "Ready";
  }, 400);
</script></html>`;

async function check(channel) {
  assert.ok(["chrome", "msedge"].includes(channel), "Choose chrome or msedge");
  const browser = await chromium.launch({ channel, headless: true, chromiumSandbox: true });
  try {
    const context = await browser.newContext({ offline: true, serviceWorkers: "block" });
    context.setDefaultTimeout(10000);
    const first = await context.newPage();
    const second = await context.newPage();
    await Promise.all([first.setContent(fixture), second.setContent(fixture)]);
    const note = "Ready for review";
    await first.getByLabel("Review note").fill(note);
    await first.getByRole("button", { name: "Confirm note" }).click();
    assert.equal(await first.getByRole("status").innerText(), note);
    await second.getByRole("button", { name: "Confirm note" }).waitFor();
    assert.equal(await second.getByLabel("Review note").inputValue(), "");
    assert.equal(await second.getByRole("status").innerText(), "Ready");
    await first.close();
    assert.equal(context.pages().length, 1);
    assert.equal(context.pages()[0], second);
    assert.equal(await second.getByRole("status").innerText(), "Ready");
    console.log(`${channel}: page A confirmed the note; page B stayed ready and open.`);
  } finally {
    await browser.close();
  }
}

check(process.argv[2]).catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});
