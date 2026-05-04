#!/usr/bin/env osascript -l JavaScript
//
// Dumps all Apple Notes to stdout as a JSON array.
// Each entry: { id, name, body (HTML), folder, created (ISO), modified (ISO) }
//
// Notes that throw on access (rare, encrypted notes occasionally do) are skipped.
//
// Invoked from Python wrapper. macOS will prompt for Automation permission the
// first time the calling process runs this; grant it to Terminal or your shell.
//
// Writes JSON output to the file path passed as argv[0] — necessary because
// osascript stdout silently truncates to empty for large dumps (~MB+ per
// Apple-event-bridged call appears to exceed an internal buffer). On success,
// prints a small JSON metadata object {path, byteCount, noteCount, totalAvail,
// errCount} to stdout for the Python wrapper to validate.

ObjC.import('Foundation');

function run(argv) {
  if (!argv || argv.length < 1) {
    throw new Error("missing output path argument");
  }
  const outPath = argv[0];

  const Notes = Application('Notes');
  Notes.includeStandardAdditions = true;

  const formatter = $.NSISO8601DateFormatter.alloc.init;
  formatter.formatOptions = $.NSISO8601DateFormatWithInternetDateTime;

  const toIso = (d) => {
    if (!d) return null;
    return ObjC.unwrap(formatter.stringFromDate(d));
  };

  const allNotes = Notes.notes();
  const total = allNotes.length;
  const out = [];
  let errCount = 0;

  for (let i = 0; i < total; i++) {
    const n = allNotes[i];
    try {
      let folderName = '';
      try {
        const c = n.container();
        if (c) folderName = c.name();
      } catch (_) {}

      out.push({
        id: n.id(),
        name: n.name(),
        body: n.body(),
        folder: folderName,
        created: toIso(n.creationDate()),
        modified: toIso(n.modificationDate()),
      });
    } catch (e) {
      errCount++;
    }
  }

  const json = JSON.stringify(out);
  const nsString = $.NSString.alloc.initWithUTF8String(json);
  const nsPath = $.NSString.alloc.initWithUTF8String(outPath);
  const errPtr = $();
  const ok = nsString.writeToFileAtomicallyEncodingError(
    nsPath, true, $.NSUTF8StringEncoding, errPtr
  );
  if (!ok) {
    throw new Error("failed to write " + outPath);
  }
  const byteCount = nsString.lengthOfBytesUsingEncoding($.NSUTF8StringEncoding);

  return JSON.stringify({
    path: outPath,
    byteCount: byteCount,
    noteCount: out.length,
    totalAvail: total,
    errCount: errCount,
  });
}
