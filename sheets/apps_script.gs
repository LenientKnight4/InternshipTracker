// Google Sheet receiver for the internship tracker.
//
// Setup (about 3 minutes):
//   1. Create a Google Sheet. Extensions -> Apps Script. Paste this whole file.
//   2. Change TOKEN below to any random string. Save.
//   3. Deploy -> New deployment -> type "Web app".
//      Execute as: Me.  Who has access: Anyone.  Deploy, then authorize.
//   4. Copy the Web app URL. In your GitHub repo add two secrets:
//        SHEET_WEBHOOK_URL = that URL
//        SHEET_TOKEN       = the TOKEN string below
//
// New postings get appended as rows. Use the Status column to track your applications;
// the tracker never overwrites rows, it only adds new ones.

const TOKEN = 'change-me-to-a-random-string';

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  if (body.token !== TOKEN) {
    return ContentService.createTextOutput('forbidden');
  }
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(['Found', 'Priority', 'Firm', 'Role', 'Location', 'Link', 'Status', 'Notes']);
    sheet.setFrozenRows(1);
  }
  const lastRow = sheet.getLastRow();
  const seen = new Set(lastRow > 1 ? sheet.getRange(2, 6, lastRow - 1, 1).getValues().flat() : []);
  const priorityLabel = { high: 'AM/ER', medium: 'Related' };
  let added = 0;
  (body.rows || []).forEach(function (r) {
    if (seen.has(r.url)) return;
    sheet.appendRow([r.found, priorityLabel[r.priority] || r.priority, r.firm, r.title, r.location, r.url, 'Not applied', '']);
    seen.add(r.url);
    added++;
  });
  return ContentService.createTextOutput(JSON.stringify({ added: added }));
}
