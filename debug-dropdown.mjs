import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

// Navigate and wait for the app to load
await page.goto('http://127.0.0.1:8081/', { waitUntil: 'networkidle' });
await page.waitForTimeout(3000);

// Take initial screenshot
await page.screenshot({ path: '/tmp/metube-1-initial.png', fullPage: true });

// Check console logs for custom_dirs
page.on('console', msg => {
  if (msg.text().includes('custom_dirs') || msg.text().includes('customDir')) {
    console.log('CONSOLE:', msg.text());
  }
});

// Look for the download folder dropdown area
const advancedToggle = await page.$('text=Show advanced options');
const optionsToggle = await page.$('[data-bs-toggle="collapse"]');
const qualitySelect = await page.$('text=Download Folder');

console.log('advancedToggle:', !!advancedToggle);
console.log('optionsToggle:', !!optionsToggle);
console.log('qualitySelect (Download Folder label):', !!qualitySelect);

// Try to find and expand the options area
// First, let's see what's on the page
const allText = await page.textContent('body');
console.log('\n--- Page text snippet ---');
console.log(allText.substring(0, 2000));

// Look for any collapse/accordion triggers
const collapseButtons = await page.$$('[data-bs-toggle="collapse"], .btn-link, details summary, .accordion-button');
console.log('\nCollapse buttons found:', collapseButtons.length);
for (const btn of collapseButtons) {
  const text = await btn.textContent();
  console.log('  Button text:', text.trim().substring(0, 80));
}

// Try clicking to expand options
const optionsBtns = await page.$$('a, button');
for (const btn of optionsBtns) {
  const text = (await btn.textContent()).trim();
  if (text.toLowerCase().includes('option') || text.toLowerCase().includes('advanced') || text.toLowerCase().includes('folder')) {
    console.log('Clicking:', text.substring(0, 60));
    await btn.click();
    await page.waitForTimeout(500);
  }
}

await page.screenshot({ path: '/tmp/metube-2-expanded.png', fullPage: true });

// Now look for ng-select or the download folder dropdown
const ngSelects = await page.$$('ng-select');
console.log('\nng-select elements found:', ngSelects.length);

for (let i = 0; i < ngSelects.length; i++) {
  const sel = ngSelects[i];
  const placeholder = await sel.getAttribute('placeholder');
  const cls = await sel.getAttribute('class');
  console.log(`  ng-select[${i}]: placeholder="${placeholder}", class="${cls}"`);

  // Check if items are bound
  const items = await sel.$$('.ng-option');
  console.log(`    visible options: ${items.length}`);
}

// Try to find and click the Download Folder ng-select
const folderSelect = await page.$('ng-select[placeholder="Default"]');
if (folderSelect) {
  console.log('\nFound folder select, clicking...');
  await folderSelect.click();
  await page.waitForTimeout(1000);

  const options = await page.$$('ng-select[placeholder="Default"] .ng-option');
  console.log('Options after click:', options.length);

  // Also check the dropdown panel that may be appended to body
  const dropdownPanel = await page.$('.ng-dropdown-panel');
  if (dropdownPanel) {
    const panelText = await dropdownPanel.textContent();
    console.log('Dropdown panel text:', panelText.substring(0, 200));
  } else {
    console.log('No dropdown panel found');
  }

  await page.screenshot({ path: '/tmp/metube-3-dropdown-open.png', fullPage: true });
} else {
  console.log('\nFolder select (placeholder="Default") NOT FOUND in DOM');

  // Check if the @if block is rendering at all
  const folderLabel = await page.$('text=Download Folder');
  if (folderLabel) {
    const parent = await folderLabel.evaluateHandle(el => el.closest('.mt-advanced-field'));
    const parentHtml = await parent.evaluate(el => el ? el.innerHTML : 'no parent');
    console.log('Parent HTML:', parentHtml.substring(0, 500));
  }
}

// Check what the service has in memory via browser console
const customDirsData = await page.evaluate(() => {
  // Try to access Angular internals
  const appEl = document.querySelector('app-root');
  if (appEl) {
    const ng = window.ng;
    if (ng) {
      const comp = ng.getComponent(appEl);
      if (comp) {
        return {
          customDirs$: !!comp.customDirs$,
          folder: comp.folder,
          downloadsCustomDirs: comp.downloads?.customDirs,
        };
      }
    }
  }
  return 'Could not access Angular component';
});
console.log('\nAngular component data:', JSON.stringify(customDirsData, null, 2));

await browser.close();
