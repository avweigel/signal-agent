# Exporting work mail before leaving an organization

Most enterprise email accounts (Microsoft 365, Google Workspace, etc.) become
inaccessible after your last day. To preserve a complete archive in a portable
format that works without that organization's tenant, do this BEFORE you lose
access.

## Microsoft 365 / Outlook (.olm export)

1. Open **Outlook for Mac** while signed in to your work account.
2. **File > Export...**
3. Select Mail, Calendar, Contacts, Notes, Tasks (everything you want).
4. Save the `.olm` file to `~/workspace/agent-data/archives/work-mail/<date>.olm`.
5. The export may take an hour or more depending on mailbox size. Don't quit
   Outlook during it.

The `.olm` is a self-contained archive. You can re-import it later into Outlook
on any account.

### Repeat close to your last day

Mailboxes change. Do an export now to have a safety net, then a final export
in your last week. Name them with dates so you can tell them apart.

### Convert .olm to a portable format (optional)

`.olm` is Microsoft-specific. To make the archive readable by any tool:

- **Thunderbird:** install the [ImportExportTools NG] add-on, import the .olm,
  then export to mbox per folder.
- **Command-line:** [olm2pst] / [readpst] toolchains can convert to mbox or
  per-message .eml files.
- **Pragmatic:** import the .olm into a fresh local Outlook profile, read from
  there.

The mbox or .eml route is the right one if you want a downstream agent to
index it.

## Verify the export worked

Before relying on it:

1. Quit Outlook.
2. Create a fresh local Outlook profile (or use Thunderbird).
3. Import the .olm.
4. Spot-check: search for a recent thread, open it, confirm attachments come
   through.

## Beyond mail: other things to grab before you go

- **Calendar:** the same .olm captures it. Also export individually via
  Outlook > Calendar > File > Save As > .ics for a portable copy.
- **Contacts:** same .olm. Also exportable as .vcf via Outlook.
- **OneDrive / Google Drive:** sign in via web, select all, download zip. Or
  use the desktop sync client to mirror everything locally before your last
  day, then disconnect after.
- **HR / payroll documents:** save as PDFs (W-2s, pay stubs, benefits history,
  retirement account info). Workday, ADP, and similar systems lose access
  with the work account.
- **Code repositories on enterprise GitHub / GitLab:** clone with full history
  while you have access.
- **Authentication:** confirm any benefit accounts (retirement, health) have
  your personal email, not work SSO, before your last day.

[ImportExportTools NG]: https://addons.thunderbird.net/en-US/thunderbird/addon/importexporttools-ng/
[olm2pst]: https://www.npmjs.com/package/olm2pst
[readpst]: https://www.five-ten-sg.com/libpst/
