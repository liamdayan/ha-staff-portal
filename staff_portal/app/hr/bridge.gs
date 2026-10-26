/**
 * Staff Access: Google Drive bridge for the HR portal (script version {{VERSION}}).
 *
 * What this is: a small web app that lets the Staff Access add-on in Home Assistant keep HR
 * files in your Google shared drive, as you, without a Google Cloud project or key file.
 *
 * What it can do: only what Staff Access asks, and only inside the shared drive(s) you picked in
 * Staff Access (their IDs are saved below in Script Properties by a signed request). It makes
 * folders, uploads, renames, moves, downloads and trashes files there, and reads the text on a
 * card photo (Google Drive turns a copy into a Google Doc in an "_OCR scratch" folder, takes its
 * text and deletes it straight away). Anything outside those drives is refused.
 *
 * Who can use it: only requests signed with the secret below, which only your Staff Access has.
 * Each request is good for 5 minutes and only once. Keep this script private. To change the
 * secret, click "Make a new secret" in Staff Access, then paste the new script here and deploy a
 * new version.
 *
 * Setup: Services (+) > Drive API (v3) > Add. Deploy > New deployment > Web app, Execute as: Me,
 * Who has access: Anyone. Paste the Web app URL into Staff Access.
 */

var SECRET = "{{SECRET}}";
var VERSION = {{VERSION}};
var PROTOCOL = 1;                          // the request format Staff Access sends
var MAX_BYTES = 25 * 1024 * 1024;          // a file uploaded, downloaded or read
var MAX_BASE64 = Math.ceil(MAX_BYTES / 3) * 4;
var MAX_BODY = MAX_BASE64 + 64 * 1024;
var CLOCK_SKEW_MS = 5 * 60 * 1000;         // a request this far off the clock is refused
var NONCE_SECONDS = 600;                   // a request's nonce is remembered this long
var FOLDER = "application/vnd.google-apps.folder";
var GOOGLE_DOC = "application/vnd.google-apps.document";
var OCR_FOLDER = "_OCR scratch";
var OCR_TYPES = ["image/jpeg", "image/png", "image/gif", "application/pdf"];
var SCRATCH_MINUTES = 60;                  // leftovers of runs cut short are removed after this
var TEST_TEXT = /06\/14\/2029/;            // what the self-test image says ("EXPIRES 06/14/2029")
var ID_RE = /^[A-Za-z0-9_-]{10,200}$/;

var OPS = {ping: ping, setup: setup, ensureFolder: ensureFolder, rename: rename, move: move,
           upload: upload, download: download, trash: trash, ocr: ocr, selfTest: selfTest};

// A web browser opening the address sees only this.
function doGet() {
  return ContentService.createTextOutput("Staff Access HR bridge: it's working. It only answers " +
                                         "requests from Staff Access.")
      .setMimeType(ContentService.MimeType.TEXT);
}

function doPost(e) {
  var reply;
  try {
    reply = {ok: true, result: handle(e)};
  } catch (err) {
    reply = {ok: false, error: errorOf(err)};
  }
  reply.version = VERSION;
  return ContentService.createTextOutput(JSON.stringify(reply))
      .setMimeType(ContentService.MimeType.JSON);
}

// ---------------------------------------------------------------- checking a request

function handle(e) {
  var body = e && e.postData && e.postData.contents;
  if (typeof body !== "string" || !body) throw fail("auth", "That request isn't from Staff Access.");
  if (body.length > MAX_BODY) throw fail("too_big", "That file is bigger than 25 MB.");
  var req;
  try {
    req = JSON.parse(body);
  } catch (err) {
    throw fail("auth", "That request isn't from Staff Access.");
  }
  // The signature first, so nothing else is looked at (or remembered) for a stranger.
  if (!req || typeof req.op !== "string" || !/^[A-Za-z]{1,30}$/.test(req.op) ||
      !isWhole(req.v) || !isWhole(req.ts) || typeof req.nonce !== "string" ||
      !/^[A-Za-z0-9_-]{16,64}$/.test(req.nonce) || typeof req.sig !== "string" ||
      !/^[0-9a-f]{64}$/.test(req.sig) || !req.args || typeof req.args !== "object" ||
      Array.isArray(req.args)) {
    throw fail("auth", "That request isn't from Staff Access.");
  }
  var signed = req.v + "|" + req.op + "|" + req.ts + "|" + req.nonce + "|" + sha256Hex(canonical(req.args));
  if (!sameText(hmacHex(signed), req.sig)) {
    throw fail("auth", "The secret doesn't match. In Staff Access, click Copy the script, paste it " +
                       "here over the old one, and deploy a new version.");
  }
  if (Math.abs(Date.now() - req.ts * 1000) > CLOCK_SKEW_MS) {
    throw fail("auth", "The request's time is more than 5 minutes off. Check the clock on the " +
                       "Home Assistant computer.");
  }
  // Each request works once. (The cache may forget early under load; a repeat could then only
  // redo the same signed request, which is safe to repeat.)
  var cache = CacheService.getScriptCache();
  if (cache.get("nonce:" + req.nonce)) throw fail("auth", "That request was already used.");
  cache.put("nonce:" + req.nonce, "1", NONCE_SECONDS);
  if (req.v !== PROTOCOL || !Object.prototype.hasOwnProperty.call(OPS, req.op)) {
    throw fail("outdated", "This script is older than Staff Access. Paste the new script and " +
                           "deploy a new version.");
  }
  if (typeof Drive === "undefined") {
    throw fail("not_allowed", "The script needs Google's Drive service: next to Services, click +, " +
                              "choose Drive API and click Add, then deploy a new version.");
  }
  return OPS[req.op](req.args);
}

// args as Staff Access signed them: JSON with sorted keys, no spaces, and everything outside
// printable ASCII escaped (\u00e9). Only text, whole numbers, true, false, null, lists and objects.
function canonical(value) {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") {
    return "{" + Object.keys(value).sort().map(function (key) {
      return quote(key) + ":" + canonical(value[key]);
    }).join(",") + "}";
  }
  if (typeof value === "string") return quote(value);
  if (typeof value === "number" && !isWhole(value)) throw fail("auth", "That request isn't from Staff Access.");
  return JSON.stringify(value);
}

function quote(text) {
  return JSON.stringify(text).replace(/[\u007f-\uffff]/g, function (ch) {
    return "\\u" + ("000" + ch.charCodeAt(0).toString(16)).slice(-4);
  });
}

function isWhole(n) {
  return typeof n === "number" && Math.floor(n) === n && Math.abs(n) <= 9007199254740992;
}

function hex(bytes) {
  return bytes.map(function (b) { return ("0" + ((b + 256) % 256).toString(16)).slice(-2); }).join("");
}

function sha256Hex(text) {
  return hex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8));
}

function hmacHex(text) {
  return hex(Utilities.computeHmacSha256Signature(text, SECRET, Utilities.Charset.UTF_8));
}

// Compares every character, so the time taken says nothing about the right signature.
function sameText(a, b) {
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ---------------------------------------------------------------- errors

function fail(code, message) {
  var err = new Error(message);
  err.code = code;
  return err;
}

// {code, message} for Staff Access. Google's own errors are put in plain words.
function errorOf(err) {
  if (err && err.code && typeof err.code === "string") return {code: err.code, message: err.message};
  var text = String(err && err.message || err);
  if (/not found/i.test(text)) {
    return {code: "not_allowed", message: "Google Drive has no such file or folder in the HR drives."};
  }
  if (/rate limit|quota|too many times|limit exceeded|try again later/i.test(text)) {
    return {code: "quota", message: "Google is busy (a usage limit was reached). Staff Access will " +
                                    "try again later."};
  }
  if (/insufficient|permission|forbidden|access denied/i.test(text)) {
    return {code: "not_allowed", message: "Google Drive refused. The Google account that deployed " +
                                          "the script must be a Manager of the HR shared drive."};
  }
  return {code: "unavailable", message: "Google Drive had a problem: " + text.slice(0, 200)};
}

// ---------------------------------------------------------------- the drives allowed

function allowedDrives() {
  var raw = PropertiesService.getScriptProperties().getProperty("drives");
  var ids = [];
  try {
    ids = JSON.parse(raw || "[]");
  } catch (err) {
    ids = [];
  }
  return Array.isArray(ids) ? ids.filter(function (id) { return typeof id === "string" && ID_RE.test(id); }) : [];
}

function checkId(id) {
  if (typeof id !== "string" || !ID_RE.test(id)) throw fail("bad_request", "That isn't a Google Drive ID.");
  return id;
}

// The file or folder `id`, if it's in an allowed drive (by its driveId); refused otherwise.
// A drive's own ID is its top folder.
function inDrives(id, folderOnly) {
  checkId(id);
  var drives = allowedDrives();
  if (!drives.length) {
    throw fail("not_allowed", "Pick the HR shared drive in Staff Access first (Settings, Connections, " +
                              "Google Drive).");
  }
  if (drives.indexOf(id) >= 0) return {id: id, driveId: id, mimeType: FOLDER, parents: [], top: true};
  var file = Drive.Files.get(id, {supportsAllDrives: true,
                                  fields: "id,name,mimeType,driveId,parents,trashed,size,webViewLink"});
  if (!file || !file.driveId || drives.indexOf(file.driveId) < 0) {
    throw fail("not_allowed", "That file or folder isn't in the HR drives.");
  }
  if (folderOnly && (file.mimeType !== FOLDER || file.trashed)) {
    throw fail("bad_request", "That isn't a folder in the HR drives.");
  }
  return file;
}

function checkName(name) {
  if (typeof name !== "string" || !name.trim() || name.length > 250 || /[\u0000-\u001f\u007f]/.test(name)) {
    throw fail("bad_request", "A name needs 1 to 250 characters.");
  }
  return name.trim();
}

// Tags: kept in appProperties and in properties, so a folder stays findable even from a new
// copy of this script (appProperties are only seen by the script that set them).
function checkProps(props) {
  if (!props || typeof props !== "object" || Array.isArray(props) || Object.keys(props).length > 10) {
    throw fail("bad_request", "At most 10 tags.");
  }
  Object.keys(props).forEach(function (key) {
    if (!/^[A-Za-z0-9_.-]{1,40}$/.test(key) || typeof props[key] !== "string" || !props[key] ||
        props[key].length > 100) {
      throw fail("bad_request", "Tags must be short text.");
    }
  });
  return props;
}

function checkData(data) {
  if (typeof data !== "string" || !/^[A-Za-z0-9+\/]*={0,2}$/.test(data)) {
    throw fail("bad_request", "The file didn't arrive whole.");
  }
  if (data.length > MAX_BASE64) throw fail("too_big", "That file is bigger than 25 MB.");
  return Utilities.base64Decode(data);
}

function escapeQuery(text) {
  return String(text).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

// A file or folder in `parent` (not trashed) with all of these tags, or this name when there are
// no tags. kind: "folder", "file" or null for either.
function findChild(parent, props, kind, name) {
  var q = ["'" + escapeQuery(parent.id) + "' in parents", "trashed = false"];
  if (kind === "folder") q.push("mimeType = '" + FOLDER + "'");
  if (kind === "file") q.push("mimeType != '" + FOLDER + "'");
  var keys = Object.keys(props);
  if (!keys.length) q.push("name = '" + escapeQuery(name) + "'");
  keys.forEach(function (key) {
    var pair = "{ key='" + escapeQuery(key) + "' and value='" + escapeQuery(props[key]) + "' }";
    q.push("(appProperties has " + pair + " or properties has " + pair + ")");
  });
  var list = Drive.Files.list({q: q.join(" and "), corpora: "drive", driveId: parent.driveId,
                               includeItemsFromAllDrives: true, supportsAllDrives: true, pageSize: 10,
                               fields: "files(id,name,mimeType,size,webViewLink)"});
  return (list && list.files || [])[0] || null;
}

function fileView(file) {
  return {id: file.id, name: file.name, size: file.size === undefined ? null : Number(file.size),
          webViewLink: file.webViewLink || ""};
}

// Deleted for good; or, for someone who isn't a Manager of the shared drive, moved to its trash.
function removeForGood(id) {
  try {
    Drive.Files.remove(id, {supportsAllDrives: true});
  } catch (err) {
    Drive.Files.update({trashed: true}, id, null, {supportsAllDrives: true});
  }
}

function email() {
  return Session.getEffectiveUser().getEmail();
}

// ---------------------------------------------------------------- operations

// The script's version, whose Google account it runs as, and the shared drives it could use.
function ping() {
  var drives = [];
  var token = null;
  for (var page = 0; page < 10; page++) {
    var options = {pageSize: 100, fields: "nextPageToken,drives(id,name)"};
    if (token) options.pageToken = token;
    var list = Drive.Drives.list(options);
    (list && list.drives || []).forEach(function (d) { drives.push({id: d.id, name: d.name}); });
    token = list && list.nextPageToken;
    if (!token) break;
  }
  return {version: VERSION, email: email(), drives: drives, allowed: allowedDrives()};
}

// Which shared drives this script may use from now on (the HR drive first). Each must be one
// the account can open.
function setup(args) {
  var ids = args.drives;
  if (!Array.isArray(ids) || ids.length < 1 || ids.length > 2 || ids[0] === ids[1]) {
    throw fail("bad_request", "Pick one or two different shared drives.");
  }
  var drives = ids.map(function (id) {
    var drive = Drive.Drives.get(checkId(id), {fields: "id,name"});
    return {id: drive.id, name: drive.name};
  });
  PropertiesService.getScriptProperties().setProperty("drives", JSON.stringify(ids));
  return {drives: drives};
}

function ensureFolder(args) {
  var parent = inDrives(args.parent, true);
  var name = checkName(args.name);
  var props = checkProps(args.props || {});
  var found = findChild(parent, props, "folder", name);
  if (found) return {id: found.id, name: found.name, created: false};
  var folder = Drive.Files.create({name: name, mimeType: FOLDER, parents: [parent.id],
                                   appProperties: props, properties: props},
                                  null, {supportsAllDrives: true, fields: "id,name"});
  return {id: folder.id, name: folder.name, created: true};
}

function rename(args) {
  var file = inDrives(args.id);
  if (file.top) throw fail("bad_request", "A shared drive can't be renamed from here.");
  var done = Drive.Files.update({name: checkName(args.name)}, file.id, null,
                                {supportsAllDrives: true, fields: "id,name"});
  return {id: done.id, name: done.name};
}

function move(args) {
  var file = inDrives(args.id);
  var parent = inDrives(args.parent, true);
  if (file.top) throw fail("bad_request", "A shared drive can't be moved.");
  var parents = file.parents || [];
  if (parents.length === 1 && parents[0] === parent.id) return {id: file.id, parent: parent.id};
  Drive.Files.update({}, file.id, null, {supportsAllDrives: true, addParents: parent.id,
                                         removeParents: parents.join(","), fields: "id,parents"});
  return {id: file.id, parent: parent.id};
}

// A file into a folder. Uploading again with the same upload_id tag returns the one already
// there, so a retry never makes a second copy.
function upload(args) {
  var parent = inDrives(args.parent, true);
  var name = checkName(args.name);
  var props = checkProps(args.props || {});
  if (!props.upload_id) throw fail("bad_request", "An upload needs its upload_id tag.");
  var mime = args.mimeType;
  if (typeof mime !== "string" || !/^[a-z]+\/[A-Za-z0-9.+_-]{1,100}$/.test(mime) ||
      mime.indexOf("application/vnd.google-apps") === 0) {
    throw fail("bad_request", "That file type can't be uploaded.");
  }
  var bytes = checkData(args.data);
  var found = findChild(parent, {upload_id: props.upload_id}, "file");
  if (found) return fileView(found);
  var file = Drive.Files.create({name: name, mimeType: mime, parents: [parent.id],
                                 appProperties: props, properties: props},
                                Utilities.newBlob(bytes, mime, name),
                                {supportsAllDrives: true, fields: "id,name,size,webViewLink"});
  return fileView(file);
}

function download(args) {
  var file = inDrives(args.id);
  if (file.top || file.mimeType === FOLDER || file.mimeType.indexOf("application/vnd.google-apps") === 0) {
    throw fail("bad_request", "Only uploaded files can be downloaded.");
  }
  if (Number(file.size) > MAX_BYTES) throw fail("too_big", "That file is bigger than 25 MB.");
  var bytes = DriveApp.getFileById(file.id).getBlob().getBytes();
  return {data: Utilities.base64Encode(bytes), name: file.name, mimeType: file.mimeType,
          size: bytes.length};
}

function trash(args) {
  var file = inDrives(args.id);
  if (file.top) throw fail("bad_request", "A shared drive can't be trashed.");
  if (!file.trashed) Drive.Files.update({trashed: true}, file.id, null, {supportsAllDrives: true});
  return {id: file.id, trashed: true};
}

function ocr(args) {
  if (OCR_TYPES.indexOf(args.mimeType) < 0) {
    throw fail("bad_request", "Google reads text from JPEG, PNG, GIF and PDF files only.");
  }
  return {text: readText(checkData(args.data), args.mimeType)};
}

// Google Drive reads the text: a copy becomes a Google Doc in "_OCR scratch" in the HR drive,
// its text is taken, and it's deleted at once.
function readText(bytes, mime) {
  var drives = allowedDrives();
  if (!drives.length) throw fail("not_allowed", "Pick the HR shared drive in Staff Access first.");
  var top = {id: drives[0], driveId: drives[0]};
  var scratch = findChild(top, {staff_access: "ocr_scratch"}, "folder") ||
      Drive.Files.create({name: OCR_FOLDER, mimeType: FOLDER, parents: [top.id],
                          appProperties: {staff_access: "ocr_scratch"},
                          properties: {staff_access: "ocr_scratch"}},
                         null, {supportsAllDrives: true, fields: "id,name"});
  clearScratch({id: scratch.id, driveId: top.id});
  var doc = Drive.Files.create({name: "Reading " + new Date().toISOString(), mimeType: GOOGLE_DOC,
                                parents: [scratch.id]},
                               Utilities.newBlob(bytes, mime, "card"),
                               {supportsAllDrives: true, ocrLanguage: "en", fields: "id"});
  try {
    return DocumentApp.openById(doc.id).getBody().getText();
  } finally {
    removeForGood(doc.id);
  }
}

// Copies left by a reading that was cut short (a run may stop at 6 minutes).
function clearScratch(folder) {
  var before = new Date(Date.now() - SCRATCH_MINUTES * 60 * 1000).toISOString();
  var list = Drive.Files.list({q: "'" + escapeQuery(folder.id) + "' in parents and trashed = false " +
                                  "and modifiedTime < '" + before + "'",
                               corpora: "drive", driveId: folder.driveId, includeItemsFromAllDrives: true,
                               supportsAllDrives: true, pageSize: 20, fields: "files(id)"});
  (list && list.files || []).forEach(function (f) {
    try {
      removeForGood(f.id);
    } catch (err) {
      // Next time.
    }
  });
}

// Test connection in Staff Access: each check with a plain result.
function selfTest() {
  var checks = [];
  var drives = allowedDrives();
  function check(name, fn) {
    try {
      checks.push({check: name, ok: true, detail: fn()});
    } catch (err) {
      checks.push({check: name, ok: false, detail: errorOf(err).message});
    }
  }
  check("Sign-in", function () {
    return "Staff Access is signed in to Google as " + email() + ".";
  });
  check("Shared drive", function () {
    if (!drives.length) throw fail("not_allowed", "No shared drive picked yet: pick one in Staff Access and click Save.");
    return "Can use " + drives.map(function (id) {
      return Drive.Drives.get(id, {fields: "id,name"}).name;
    }).join(" and ") + ".";
  });
  check("Make and remove a test folder", function () {
    if (!drives.length) throw fail("not_allowed", "No shared drive picked yet.");
    var top = inDrives(drives[0], true);
    var folder = Drive.Files.create({name: "Staff Access test (safe to delete)", mimeType: FOLDER,
                                     parents: [top.id]}, null, {supportsAllDrives: true, fields: "id"});
    removeForGood(folder.id);
    return "Made and removed a test folder.";
  });
  check("Read text from a test image", function () {
    var text = readText(Utilities.base64Decode(TEST_IMAGE), "image/png");
    var plain = String(text).replace(/\s+/g, " ").trim();
    if (!TEST_TEXT.test(plain.replace(/[Oo]/g, "0").replace(/\s+/g, ""))) {
      throw fail("unavailable", "Google read the test image as \"" + plain.slice(0, 60) + "\".");
    }
    return "Google read the test image: \"" + plain.slice(0, 60) + "\".";
  });
  return {checks: checks};
}

// A small PNG that says "EXPIRES 06/14/2029", for the self-test's reading check.
var TEST_IMAGE = "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";
