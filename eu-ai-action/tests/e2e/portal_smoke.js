// Browser smoke test for the portal. Run: node tests/e2e/portal_smoke.js <base-url> <screenshot-dir>
// Needs a freshly started dev server: the flow expects no inputs saved yet.
// Requires the dev server (python scripts/dev_server.py) and Playwright.
const { chromium } = require("playwright");
const assert = require("assert");
const [base, shots] = [process.argv[2], process.argv[3] || "."];
const T = "00000000-0000-0000-0000-000000000001";

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // the blocked submit is expected to return 409, which the browser logs as a console error
  page.on("console", (m) => m.type() === "error" && !m.text().includes("409") && errors.push(m.text()));

  await page.goto(`${base}/?tenant=${T}`);
  await page.click("#scan");
  await page.waitForSelector("table tbody tr");
  assert.strictEqual(await page.locator("table tbody tr").count(), 10);
  assert.ok(await page.locator("text=Organisation profile incomplete").count() === 1, "profile banner");
  await page.screenshot({ path: `${shots}/1-agents.png` });

  await page.click("text=Public Help Bot");
  await page.waitForSelector("h2:has-text('Public Help Bot')");
  assert.ok(await page.locator(".pill.fail").count() >= 1, "disclosure check should fail for this agent");

  // submit is rejected until human inputs are supplied
  await page.locator("tr:has-text('register-entry'):has-text('en') button:has-text('submit')").first().click();
  await page.waitForFunction(() => document.getElementById("status").textContent.includes("human input missing"));
  assert.ok(await page.locator("#status.error").count() === 1);
  await page.screenshot({ path: `${shots}/2-agent-blocked.png` });

  // fill every input and save (inputs are grouped per document in collapsed sections)
  await page.evaluate(() => document.querySelectorAll("details").forEach((d) => { d.open = true; }));
  for (const name of ["business_purpose", "intended_users", "processing_purposes", "data_subjects", "recipients", "third_country_transfers", "retention_period"]) {
    await page.fill(`textarea[name=${name}]`, `Test ${name}`);
  }
  await page.click("text=Save inputs");
  await page.waitForFunction(() => document.getElementById("status").textContent === "Saved.");

  // controller defaults live in Settings
  await page.click("nav >> text=Settings");
  await page.fill("input[name=controller_name]", "Contoso SA");
  await page.fill("input[name=controller_contact]", "dpo@contoso.example");
  await page.click("button:has-text('Save')");
  await page.waitForFunction(() => document.getElementById("status").textContent === "Saved.");

  await page.goto(`${base}/?tenant=${T}#/agent/agent-public-nodisclosure`);
  await page.waitForSelector("h2:has-text('Public Help Bot')");
  const row = page.locator("tr:has-text('register-entry'):has-text('en')").first();
  await row.locator("button:has-text('submit')").click();
  await page.waitForFunction(() => document.getElementById("status").textContent.includes("submit done"));
  await page.locator("tr:has-text('register-entry'):has-text('en')").first().locator("button:has-text('approve')").click();
  await page.waitForFunction(() => document.getElementById("status").textContent.includes("approve done"));
  await page.waitForSelector("tr:has-text('register-entry'):has-text('approved')");
  await page.screenshot({ path: `${shots}/3-agent-approved.png`, fullPage: true });

  // organisation profile enables sector documents
  await page.click("nav >> text=Settings");
  await page.selectOption("select[name=is_financial_entity]", "yes");
  await page.fill("input[name=country]", "ES");
  await page.click("button:has-text('Save profile')");
  await page.waitForFunction(() => document.getElementById("status").textContent === "Saved.");

  // loan agent: incident documents appear per incident, with computed deadlines in the files
  await page.goto(`${base}/?tenant=${T}#/agent/agent-loan-prescreen`);
  await page.waitForSelector("h2:has-text('Loan Pre-screening')");
  assert.ok(await page.locator("tr:has-text('dora-incident-report'):has-text('INC-200')").count() >= 1);
  assert.ok(await page.locator("tr:has-text('serious-incident-report'):has-text('INC-200')").count() >= 1);
  await page.screenshot({ path: `${shots}/4-loan-agent.png`, fullPage: true });

  // risk tier and scope inputs
  await page.selectOption("select[name=risk_tier]", "high");
  await page.selectOption("select[name=decisions_about_persons]", "yes");
  await page.fill("input[name=explanation_requests]", "REQ-1");
  await page.click("text=Save inputs");
  await page.waitForFunction(() => document.getElementById("status").textContent === "Saved.");
  await page.waitForSelector("tr:has-text('decision-explanation'):has-text('REQ-1')");
  await page.waitForSelector("tr:has-text('affected-person-notice')");

  // audit view: verify an approved document is intact
  await page.goto(`${base}/?tenant=${T}#/audit`);
  await page.waitForSelector("button:has-text('verify')");
  await page.locator("button:has-text('verify')").first().click();
  await page.waitForSelector("span.pass:has-text('intact')");
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("button:has-text('Download bundle')")]);
  assert.strictEqual(download.suggestedFilename(), "eu-ai-action-evidence.zip");
  await page.screenshot({ path: `${shots}/5-audit.png`, fullPage: true });

  // markup in tenant data must not be interpreted: no injected elements
  assert.strictEqual(await page.locator("script, img[onerror]").count(), 1, "only the app script may exist");
  assert.deepStrictEqual(errors, []);
  await browser.close();
  console.log("portal smoke test passed");
})().catch((e) => { console.error(e); process.exit(1); });
