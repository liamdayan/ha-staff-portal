// Tests for the HR portal's Google Drive script (app/hr/bridge.gs), which only Google runs: it's
// loaded here with stand-ins for the Apps Script services it uses (Drive, DriveApp, DocumentApp,
// Utilities, CacheService, ContentService, PropertiesService, Session), backed by in-memory
// shared drives. Run by tests/test_hr_bridge_script.py:
//   node bridge_test.js <script with secret filled in> <secret> <Python-signed requests (JSON)>
// Prints each test's name, and exits 1 if any failed.

"use strict";
const assert = require("assert");
const crypto = require("crypto");
const fs = require("fs");
const vm = require("vm");

const [scriptPath, SECRET, vectorsPath] = process.argv.slice(2);
const SOURCE = fs.readFileSync(scriptPath, "utf8");
const FOLDER = "application/vnd.google-apps.folder";
const GOOGLE_DOC = "application/vnd.google-apps.document";
const HR = "0AHrDriveAAAAAAAAAAAA";        // the HR drive
const RESTRICTED = "0ARestrictedAAAAAAAAA";
const OTHER = "0AOtherDriveAAAAAAAAA";      // a shared drive the owner has, not chosen for HR

// ---------------------------------------------------------------- Apps Script stand-ins

const signed = (buf) => Array.from(buf, (b) => (b > 127 ? b - 256 : b));
const unsigned = (bytes) => Buffer.from(bytes.map((b) => (b + 256) % 256));

function makeWorld() {
  const world = {
    email: "owner@hotlake.example",
    drives: { [HR]: "HR Employee Files", [RESTRICTED]: "HR Restricted", [OTHER]: "Marketing" },
    files: new Map(),
    props: {},
    cache: new Map(),
    nextId: 1,
    calls: [],
    failNext: null,        // {method, message}: the next such Drive call throws this
    removeDenied: false,   // permanent delete refused (not a Manager of the drive)
    ocrRead: (bytes) => {
      const text = unsigned(bytes).toString("latin1");
      return text.startsWith("TEXT:") ? text.slice(5) : "";
    },
  };
  world.add = (fields) => {
    const id = fields.id || `file${String(world.nextId++).padStart(8, "0")}`;
    const file = { id, trashed: false, appProperties: {}, properties: {}, parents: [],
      modifiedTime: new Date().toISOString(), ...fields };
    world.files.set(id, file);
    return file;
  };
  return world;
}

function driveOf(world, parentId) {
  if (world.drives[parentId]) return parentId;
  const parent = world.files.get(parentId);
  if (!parent) throw new Error(`File not found: ${parentId}.`);
  return parent.driveId || null;
}

function maybeFail(world, method) {
  world.calls.push(method);
  if (world.failNext && world.failNext.method === method) {
    const message = world.failNext.message;
    world.failNext = null;
    throw new Error(message);
  }
}

// Shared drive items are only seen with supportsAllDrives, as with the real Drive API.
function visible(file, opts) {
  return file && (!file.driveId || (opts && opts.supportsAllDrives));
}

function fileView(f) {
  return { id: f.id, name: f.name, mimeType: f.mimeType, driveId: f.driveId, parents: f.parents.slice(),
    trashed: f.trashed, size: f.bytes ? String(f.bytes.length) : undefined,
    webViewLink: `https://drive.google.com/file/d/${f.id}/view`,
    appProperties: { ...f.appProperties }, properties: { ...f.properties } };
}

// Splits a Drive query at its top-level " and "s.
function clauses(q) {
  const out = [];
  let depth = 0, quoted = false, start = 0;
  for (let i = 0; i < q.length; i++) {
    const ch = q[i];
    if (quoted) {
      if (ch === "\\") i++;
      else if (ch === "'") quoted = false;
    } else if (ch === "'") quoted = true;
    else if (ch === "(" || ch === "{") depth++;
    else if (ch === ")" || ch === "}") depth--;
    else if (depth === 0 && q.startsWith(" and ", i)) {
      out.push(q.slice(start, i));
      start = i + 5;
      i += 4;
    }
  }
  out.push(q.slice(start));
  return out;
}

const Q = "'((?:[^'\\\\]|\\\\.)*)'";
const unq = (s) => s.replace(/\\(.)/g, "$1");

function matcher(clause) {
  let m;
  if ((m = clause.match(new RegExp(`^${Q} in parents$`)))) return (f) => f.parents.includes(unq(m[1]));
  if ((m = clause.match(/^trashed = (true|false)$/))) return (f) => f.trashed === (m[1] === "true");
  if ((m = clause.match(new RegExp(`^mimeType (=|!=) ${Q}$`)))) {
    return (f) => (f.mimeType === unq(m[2])) === (m[1] === "=");
  }
  if ((m = clause.match(new RegExp(`^name = ${Q}$`)))) return (f) => f.name === unq(m[1]);
  if ((m = clause.match(new RegExp(`^modifiedTime < ${Q}$`)))) return (f) => f.modifiedTime < unq(m[1]);
  const pair = `\\{ key=${Q} and value=${Q} \\}`;
  if ((m = clause.match(new RegExp(`^\\(appProperties has ${pair} or properties has ${pair}\\)$`)))) {
    assert.strictEqual(m[1], m[3]);
    assert.strictEqual(m[2], m[4]);
    const key = unq(m[1]), value = unq(m[2]);
    return (f) => f.appProperties[key] === value || f.properties[key] === value;
  }
  throw new Error(`Invalid query: ${clause}`);
}

function makeServices(world) {
  const Drive = {
    Drives: {
      list(opts) {
        maybeFail(world, "Drives.list");
        assert.ok(opts.fields.includes("drives(id,name)"));
        return { drives: Object.entries(world.drives).map(([id, name]) => ({ id, name })) };
      },
      get(id) {
        maybeFail(world, "Drives.get");
        if (!world.drives[id]) throw new Error(`Shared drive not found: ${id}`);
        return { id, name: world.drives[id] };
      },
    },
    Files: {
      get(id, opts) {
        maybeFail(world, "Files.get");
        const f = world.files.get(id);
        if (!visible(f, opts)) throw new Error(`File not found: ${id}.`);
        return fileView(f);
      },
      list(opts) {
        maybeFail(world, "Files.list");
        assert.ok(opts.supportsAllDrives && opts.includeItemsFromAllDrives && opts.corpora === "drive");
        const tests = clauses(opts.q).map(matcher);
        const files = [...world.files.values()].filter((f) => f.driveId === opts.driveId &&
          tests.every((t) => t(f)));
        return { files: files.slice(0, opts.pageSize || 100).map(fileView) };
      },
      create(meta, blob, opts) {
        maybeFail(world, "Files.create");
        assert.ok(opts && opts.supportsAllDrives, "create without supportsAllDrives");
        const parent = meta.parents[0];
        const driveId = driveOf(world, parent);
        const file = world.add({ name: meta.name, mimeType: meta.mimeType, parents: [parent], driveId,
          appProperties: { ...(meta.appProperties || {}) }, properties: { ...(meta.properties || {}) } });
        if (blob) {
          if (meta.mimeType === GOOGLE_DOC) {
            file.text = world.ocrRead(blob.getBytes(), blob.mime);
            file.ocrLanguage = opts.ocrLanguage;
            file.convertedFrom = blob.mime;
          } else {
            file.bytes = blob.getBytes().slice();
          }
        }
        return fileView(file);
      },
      update(meta, id, blob, opts) {
        maybeFail(world, "Files.update");
        const f = world.files.get(id);
        if (!visible(f, opts)) throw new Error(`File not found: ${id}.`);
        if (meta.name !== undefined) f.name = meta.name;
        if (meta.trashed !== undefined) f.trashed = meta.trashed;
        if (opts.addParents) {
          const removed = String(opts.removeParents || "").split(",").filter(Boolean);
          f.parents = f.parents.filter((p) => !removed.includes(p)).concat([opts.addParents]);
          f.driveId = driveOf(world, opts.addParents);
        }
        return fileView(f);
      },
      remove(id, opts) {
        maybeFail(world, "Files.remove");
        const f = world.files.get(id);
        if (!visible(f, opts)) throw new Error(`File not found: ${id}.`);
        if (world.removeDenied) throw new Error("The user does not have sufficient permissions for this file.");
        world.files.delete(id);
      },
    },
  };
  const Utilities = {
    Charset: { UTF_8: "UTF-8" },
    DigestAlgorithm: { SHA_256: "SHA-256" },
    computeDigest(algorithm, text, charset) {
      assert.strictEqual(algorithm, "SHA-256");
      assert.strictEqual(charset, "UTF-8");
      return signed(crypto.createHash("sha256").update(text, "utf8").digest());
    },
    computeHmacSha256Signature(text, key, charset) {
      assert.strictEqual(charset, "UTF-8");
      return signed(crypto.createHmac("sha256", Buffer.from(key, "utf8")).update(text, "utf8").digest());
    },
    base64Decode: (text) => signed(Buffer.from(text, "base64")),
    base64Encode: (bytes) => unsigned(bytes).toString("base64"),
    newBlob: (bytes, mime, name) => ({ mime, name, getBytes: () => bytes }),
  };
  const ContentService = {
    MimeType: { JSON: "application/json", TEXT: "text/plain" },
    createTextOutput(text) {
      return { text, mime: null, setMimeType(m) { this.mime = m; return this; }, getContent() { return this.text; } };
    },
  };
  const CacheService = {
    getScriptCache: () => ({
      get: (k) => {
        const hit = world.cache.get(k);
        return hit && hit.until > Date.now() ? hit.value : null;
      },
      put: (k, value, seconds) => {
        assert.ok(seconds >= 300 && seconds <= 21600);
        world.cache.set(k, { value, until: Date.now() + seconds * 1000 });
      },
    }),
  };
  const PropertiesService = {
    getScriptProperties: () => ({
      getProperty: (k) => (k in world.props ? world.props[k] : null),
      setProperty: (k, v) => { world.props[k] = String(v); },
    }),
  };
  const Session = { getEffectiveUser: () => ({ getEmail: () => world.email }) };
  const DriveApp = {
    getFileById(id) {
      const f = world.files.get(id);
      if (!f) throw new Error("No item with the given ID could be found.");
      return { getBlob: () => ({ getBytes: () => f.bytes.slice() }) };
    },
  };
  const DocumentApp = {
    openById(id) {
      const f = world.files.get(id);
      if (!f || f.mimeType !== GOOGLE_DOC || world.docFails) throw new Error("Document is missing");
      return { getBody: () => ({ getText: () => f.text }) };
    },
  };
  return { Drive, Utilities, ContentService, CacheService, PropertiesService, Session, DriveApp, DocumentApp };
}

function load(world, without = []) {
  const services = makeServices(world);
  for (const name of without) delete services[name];
  const ctx = vm.createContext({ ...services, console });
  vm.runInContext(SOURCE, ctx, { filename: "bridge.gs" });
  return ctx;
}

// ---------------------------------------------------------------- signing, as Staff Access does

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((k) => `${quote(k)}:${canonical(value[k])}`).join(",")}}`;
  }
  return typeof value === "string" ? quote(value) : JSON.stringify(value);
}
const quote = (s) => JSON.stringify(s).replace(/[\u007f-\uffff]/g,
  (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);

let nonceCount = 0;
function request(op, args = {}, over = {}) {
  const r = { v: 1, op, args, ts: Math.floor(Date.now() / 1000),
    nonce: `nonce-${process.pid}-${++nonceCount}-abcdef`, ...over };
  const digest = crypto.createHash("sha256").update(canonical(r.args), "utf8").digest("hex");
  r.sig = crypto.createHmac("sha256", over.secret || SECRET)
    .update(`${r.v}|${r.op}|${r.ts}|${r.nonce}|${digest}`).digest("hex");
  delete r.secret;
  return r;
}

const replies = [];
function postText(ctx, text) {
  const out = ctx.doPost({ postData: { contents: text, type: "application/json" } });
  assert.strictEqual(out.mime, "application/json");
  replies.push(out.getContent());
  return JSON.parse(out.getContent());
}
const post = (ctx, body) => postText(ctx, JSON.stringify(body));

function ok(ctx, op, args) {
  const reply = post(ctx, request(op, args));
  assert.ok(reply.ok, `${op} failed: ${JSON.stringify(reply.error)}`);
  return reply.result;
}

function refused(ctx, op, args, code) {
  const reply = post(ctx, request(op, args));
  assert.strictEqual(reply.ok, false, `${op} should have been refused`);
  assert.strictEqual(reply.error.code, code, JSON.stringify(reply.error));
  assert.ok(reply.error.message.length > 10);
  return reply.error;
}

function ready(drives = [HR, RESTRICTED]) {
  const world = makeWorld();
  const ctx = load(world);
  ok(ctx, "setup", { drives });
  return { world, ctx };
}

const b64 = (text) => Buffer.from(text, "latin1").toString("base64");

// ---------------------------------------------------------------- the tests

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("a browser opening the address sees a short page, without the secret", () => {
  const ctx = load(makeWorld());
  const out = ctx.doGet({});
  assert.strictEqual(out.mime, "text/plain");
  assert.match(out.getContent(), /working/);
  assert.ok(!out.getContent().includes(SECRET));
});

test("ping: version, account and shared drives", () => {
  const world = makeWorld();
  const ctx = load(world);
  const reply = post(ctx, request("ping"));
  assert.ok(reply.ok);
  assert.strictEqual(reply.version, 1);
  assert.strictEqual(reply.result.email, world.email);
  assert.deepStrictEqual(reply.result.drives.map((d) => d.name).sort(),
    ["HR Employee Files", "HR Restricted", "Marketing"]);
  assert.deepStrictEqual(reply.result.allowed, []);
});

test("unsigned, wrongly signed and tampered requests are refused", () => {
  const world = makeWorld();
  const ctx = load(world);
  assert.strictEqual(postText(ctx, "not json").error.code, "auth");
  assert.strictEqual(postText(ctx, "").error.code, "auth");
  assert.strictEqual(ctx.doPost({}).getContent().includes('"auth"'), true);
  assert.strictEqual(post(ctx, request("ping", {}, { secret: "another-secret-another-secret-12" })).error.code, "auth");
  const r = request("setup", { drives: [HR] });
  r.args.drives = [OTHER];
  assert.strictEqual(post(ctx, r).error.code, "auth");
  const s = request("ping");
  s.op = "selfTest";
  assert.strictEqual(post(ctx, s).error.code, "auth");
  const t = request("ping");
  t.ts += 1;
  assert.strictEqual(post(ctx, t).error.code, "auth");
  for (const bad of [{ ...request("ping"), sig: "x" }, { ...request("ping"), nonce: "short" },
    { ...request("ping"), args: [] }, { ...request("ping"), ts: 1.5 }, { ...request("ping"), op: "pi ng" }]) {
    assert.strictEqual(post(ctx, bad).error.code, "auth");
  }
  // A number that isn't whole can't be signed.
  assert.strictEqual(post(ctx, request("ping", { n: 1.5 })).error.code, "auth");
  assert.deepStrictEqual(world.props, {}, "nothing was saved");
  assert.strictEqual(world.calls.length, 0, "Drive wasn't touched");
});

test("a request more than 5 minutes off is refused", () => {
  const ctx = load(makeWorld());
  const now = Math.floor(Date.now() / 1000);
  assert.strictEqual(post(ctx, request("ping", {}, { ts: now - 301 })).error.code, "auth");
  assert.strictEqual(post(ctx, request("ping", {}, { ts: now + 301 })).error.code, "auth");
  assert.ok(post(ctx, request("ping", {}, { ts: now - 290 })).ok);
  assert.ok(post(ctx, request("ping", {}, { ts: now + 290 })).ok);
});

test("each request works once", () => {
  const world = makeWorld();
  const ctx = load(world);
  const r = request("ping");
  assert.ok(post(ctx, r).ok);
  const again = post(ctx, r);
  assert.strictEqual(again.error.code, "auth");
  assert.match(again.error.message, /already used/);
  const entry = world.cache.get(`nonce:${r.nonce}`);
  assert.ok(entry.until - Date.now() > 590 * 1000, "remembered for 10 minutes");
});

test("unknown ops and another request format say the script is out of date", () => {
  const ctx = load(makeWorld());
  for (const op of ["deleteEverything", "constructor", "toString", "hasOwnProperty"]) {
    assert.strictEqual(post(ctx, request(op)).error.code, "outdated");
  }
  assert.strictEqual(post(ctx, request("ping", {}, { v: 2 })).error.code, "outdated");
});

test("without Google's Drive service, it says how to add it", () => {
  const ctx = load(makeWorld(), ["Drive"]);
  const err = post(ctx, request("ping")).error;
  assert.strictEqual(err.code, "not_allowed");
  assert.match(err.message, /Drive API/);
});

test("setup: only drives the account can open, then only those", () => {
  const world = makeWorld();
  const ctx = load(world);
  assert.strictEqual(refused(ctx, "ensureFolder", { parent: HR, name: "Employees", props: { hr: "employees" } },
    "not_allowed").message.includes("Pick the HR shared drive"), true);
  refused(ctx, "setup", { drives: ["0ANotMyDriveAAAAAAAAA"] }, "not_allowed");
  refused(ctx, "setup", { drives: [] }, "bad_request");
  refused(ctx, "setup", { drives: [HR, HR] }, "bad_request");
  refused(ctx, "setup", { drives: [HR, RESTRICTED, OTHER] }, "bad_request");
  refused(ctx, "setup", { drives: ["../../etc"] }, "bad_request");
  assert.deepStrictEqual(world.props, {});
  const result = ok(ctx, "setup", { drives: [HR, RESTRICTED] });
  assert.deepStrictEqual(result.drives, [{ id: HR, name: "HR Employee Files" }, { id: RESTRICTED, name: "HR Restricted" }]);
  assert.deepStrictEqual(JSON.parse(world.props.drives), [HR, RESTRICTED]);
  ok(ctx, "setup", { drives: [HR] });
  refused(ctx, "ensureFolder", { parent: RESTRICTED, name: "Restricted", props: { hr: "restricted" } }, "not_allowed");
});

test("ensureFolder finds by tags first, so asking again never makes a second folder", () => {
  const { world, ctx } = ready();
  const a = ok(ctx, "ensureFolder", { parent: HR, name: "Employees", props: { hr: "employees" } });
  assert.strictEqual(a.created, true);
  const b = ok(ctx, "ensureFolder", { parent: HR, name: "Employees (renamed)", props: { hr: "employees" } });
  assert.strictEqual(b.id, a.id);
  assert.strictEqual(b.created, false);
  const folder = world.files.get(a.id);
  assert.strictEqual(folder.mimeType, FOLDER);
  assert.strictEqual(folder.driveId, HR);
  assert.deepStrictEqual(folder.parents, [HR]);
  assert.deepStrictEqual(folder.appProperties, { hr: "employees" });
  assert.deepStrictEqual(folder.properties, { hr: "employees" });
  // A person's folder inside it, tagged with their Connecteam ID; two people with one name stay apart.
  const jane = ok(ctx, "ensureFolder", { parent: a.id, name: "Smith, Jane (CT 101)", props: { ct_user: "101" } });
  const jane2 = ok(ctx, "ensureFolder", { parent: a.id, name: "Smith, Jane (CT 102)", props: { ct_user: "102" } });
  assert.notStrictEqual(jane.id, jane2.id);
  assert.strictEqual(ok(ctx, "ensureFolder", { parent: a.id, name: "x", props: { ct_user: "101" } }).id, jane.id);
  // A folder tagged by a new copy of the script (public properties only) is still found.
  const old = world.add({ name: "Former employees", mimeType: FOLDER, parents: [HR], driveId: HR,
    properties: { hr: "former" } });
  assert.strictEqual(ok(ctx, "ensureFolder", { parent: HR, name: "Former employees", props: { hr: "former" } }).id, old.id);
  // A trashed folder doesn't count; without tags it goes by name.
  world.files.get(jane2.id).trashed = true;
  assert.notStrictEqual(ok(ctx, "ensureFolder", { parent: a.id, name: "y", props: { ct_user: "102" } }).id, jane2.id);
  const plain = ok(ctx, "ensureFolder", { parent: HR, name: "Notes 'quoted' \\ here", props: {} });
  assert.strictEqual(ok(ctx, "ensureFolder", { parent: HR, name: "Notes 'quoted' \\ here", props: {} }).id, plain.id);
  // Odd tags are refused.
  refused(ctx, "ensureFolder", { parent: HR, name: "x", props: { "bad key": "1" } }, "bad_request");
  refused(ctx, "ensureFolder", { parent: HR, name: "x", props: { k: 5 } }, "bad_request");
  refused(ctx, "ensureFolder", { parent: HR, name: "", props: {} }, "bad_request");
  refused(ctx, "ensureFolder", { parent: HR, name: "a\u0000b", props: {} }, "bad_request");
});

test("every file operation stays inside the chosen drives", () => {
  const { world, ctx } = ready([HR]);
  const outside = world.add({ name: "Marketing plan.pdf", mimeType: "application/pdf", parents: [OTHER],
    driveId: OTHER, bytes: [1, 2, 3] });
  const outsideFolder = world.add({ name: "Ads", mimeType: FOLDER, parents: [OTHER], driveId: OTHER });
  const myDrive = world.add({ name: "Personal.pdf", mimeType: "application/pdf", parents: ["root"], bytes: [1] });
  const restricted = world.add({ name: "I-9.pdf", mimeType: "application/pdf", parents: [RESTRICTED],
    driveId: RESTRICTED, bytes: [1] });
  const inside = ok(ctx, "ensureFolder", { parent: HR, name: "Employees", props: { hr: "employees" } });
  const file = ok(ctx, "upload", { parent: inside.id, name: "a.pdf", mimeType: "application/pdf",
    data: b64("%PDF-1.4"), props: { upload_id: "u1" } });
  for (const target of [outside.id, outsideFolder.id, myDrive.id, restricted.id, OTHER, RESTRICTED]) {
    refused(ctx, "ensureFolder", { parent: target, name: "x", props: { a: "1" } }, "not_allowed");
    refused(ctx, "upload", { parent: target, name: "x.pdf", mimeType: "application/pdf", data: b64("x"),
      props: { upload_id: "u2" } }, "not_allowed");
    refused(ctx, "rename", { id: target, name: "Mine now" }, "not_allowed");
    refused(ctx, "trash", { id: target }, "not_allowed");
    refused(ctx, "download", { id: target }, "not_allowed");
    refused(ctx, "move", { id: target, parent: inside.id }, "not_allowed");   // in from outside
    refused(ctx, "move", { id: file.id, parent: target }, "not_allowed");     // out to outside
  }
  refused(ctx, "rename", { id: "1NoSuchFileAAAAAAAAAA", name: "x" }, "not_allowed");
  refused(ctx, "trash", { id: "../x" }, "bad_request");
  assert.strictEqual(outside.name, "Marketing plan.pdf");
  assert.strictEqual(outside.trashed, false);
  assert.deepStrictEqual(world.files.get(file.id).parents, [inside.id]);
  // The drives themselves can't be renamed, moved or trashed.
  refused(ctx, "rename", { id: HR, name: "x" }, "bad_request");
  refused(ctx, "trash", { id: HR }, "bad_request");
  refused(ctx, "move", { id: HR, parent: inside.id }, "bad_request");
});

test("rename, move and trash", () => {
  const { world, ctx } = ready();
  const employees = ok(ctx, "ensureFolder", { parent: HR, name: "Employees", props: { hr: "employees" } });
  const former = ok(ctx, "ensureFolder", { parent: HR, name: "Former employees", props: { hr: "former" } });
  const jane = ok(ctx, "ensureFolder", { parent: employees.id, name: "Smith, Jane (CT 101)", props: { ct_user: "101" } });
  assert.strictEqual(ok(ctx, "rename", { id: jane.id, name: "Jones, Jane (CT 101)" }).name, "Jones, Jane (CT 101)");
  assert.strictEqual(world.files.get(jane.id).name, "Jones, Jane (CT 101)");
  ok(ctx, "move", { id: jane.id, parent: former.id });
  assert.deepStrictEqual(world.files.get(jane.id).parents, [former.id]);
  ok(ctx, "move", { id: jane.id, parent: former.id });   // already there: nothing changes
  assert.deepStrictEqual(world.files.get(jane.id).parents, [former.id]);
  // Into the restricted drive (also chosen).
  const doc = ok(ctx, "upload", { parent: jane.id, name: "I-9.pdf", mimeType: "application/pdf", data: b64("%PDF"),
    props: { upload_id: "i9" } });
  ok(ctx, "move", { id: doc.id, parent: RESTRICTED });
  assert.strictEqual(world.files.get(doc.id).driveId, RESTRICTED);
  ok(ctx, "trash", { id: doc.id });
  assert.strictEqual(world.files.get(doc.id).trashed, true);
  ok(ctx, "trash", { id: doc.id });   // again: still fine
  refused(ctx, "move", { id: jane.id, parent: doc.id }, "bad_request");   // not a folder
});

test("upload: bytes arrive whole, and a retry returns the same file", () => {
  const { world, ctx } = ready();
  const folder = ok(ctx, "ensureFolder", { parent: HR, name: "Certificates", props: { kind: "certs" } });
  const bytes = crypto.randomBytes(5000);
  const args = { parent: folder.id, name: "2026-10-25 OLCC Service Permit - expires 2031-10-24.jpg",
    mimeType: "image/jpeg", data: bytes.toString("base64"), props: { upload_id: "abc123", ct_user: "101" } };
  const first = ok(ctx, "upload", args);
  assert.strictEqual(first.size, 5000);
  assert.match(first.webViewLink, /^https:\/\/drive\.google\.com\//);
  const stored = world.files.get(first.id);
  assert.ok(unsigned(stored.bytes).equals(bytes));
  assert.deepStrictEqual(stored.appProperties, { upload_id: "abc123", ct_user: "101" });
  assert.deepStrictEqual(stored.properties, { upload_id: "abc123", ct_user: "101" });
  const again = ok(ctx, "upload", args);
  assert.strictEqual(again.id, first.id);
  assert.strictEqual([...world.files.values()].filter((f) => f.name === args.name).length, 1);
  // Another upload_id is another file; one in the trash doesn't count.
  assert.notStrictEqual(ok(ctx, "upload", { ...args, props: { upload_id: "def" } }).id, first.id);
  stored.trashed = true;
  assert.notStrictEqual(ok(ctx, "upload", args).id, first.id);
  refused(ctx, "upload", { ...args, props: {} }, "bad_request");
  refused(ctx, "upload", { ...args, mimeType: GOOGLE_DOC }, "bad_request");
  refused(ctx, "upload", { ...args, mimeType: "text" }, "bad_request");
  refused(ctx, "upload", { ...args, data: "not base64!" }, "bad_request");
});

test("files over 25 MB are refused both ways", () => {
  const { world, ctx } = ready();
  const big = "A".repeat(Math.ceil((25 * 1024 * 1024 + 3) / 3) * 4);
  refused(ctx, "upload", { parent: HR, name: "big.pdf", mimeType: "application/pdf", data: big,
    props: { upload_id: "big" } }, "too_big");
  refused(ctx, "ocr", { data: big, mimeType: "application/pdf" }, "too_big");
  const huge = world.add({ name: "huge.pdf", mimeType: "application/pdf", parents: [HR], driveId: HR,
    bytes: { length: 26 * 1024 * 1024, slice() { return this; } } });
  refused(ctx, "download", { id: huge.id }, "too_big");
});

test("download gives the file back as uploaded", () => {
  const { ctx } = ready();
  const bytes = crypto.randomBytes(3000);
  const up = ok(ctx, "upload", { parent: HR, name: "scan.pdf", mimeType: "application/pdf",
    data: bytes.toString("base64"), props: { upload_id: "scan" } });
  const down = ok(ctx, "download", { id: up.id });
  assert.ok(Buffer.from(down.data, "base64").equals(bytes));
  assert.strictEqual(down.mimeType, "application/pdf");
  assert.strictEqual(down.size, 3000);
  const folder = ok(ctx, "ensureFolder", { parent: HR, name: "F", props: { f: "1" } });
  refused(ctx, "download", { id: folder.id }, "bad_request");
});

test("ocr: a Google Doc in _OCR scratch, its text, then it's deleted", () => {
  const { world, ctx } = ready();
  const result = ok(ctx, "ocr", { data: b64("TEXT:OREGON FOOD HANDLER CARD\nExpires 06/14/2029"),
    mimeType: "image/jpeg" });
  assert.strictEqual(result.text, "OREGON FOOD HANDLER CARD\nExpires 06/14/2029");
  const scratch = [...world.files.values()].filter((f) => f.mimeType === FOLDER && f.name === "_OCR scratch");
  assert.strictEqual(scratch.length, 1);
  assert.strictEqual(scratch[0].driveId, HR);
  assert.deepStrictEqual(scratch[0].parents, [HR]);
  assert.strictEqual([...world.files.values()].filter((f) => f.mimeType === GOOGLE_DOC).length, 0, "doc deleted");
  assert.ok(world.calls.includes("Files.remove"));
  // The scratch folder is reused; a conversion asked for English.
  let seen = null;
  const create = world.calls.length;
  world.ocrRead = (bytes, mime) => { seen = mime; return "Valid through 01/02/2030"; };
  assert.strictEqual(ok(ctx, "ocr", { data: b64("%PDF-1.7"), mimeType: "application/pdf" }).text,
    "Valid through 01/02/2030");
  assert.strictEqual(seen, "application/pdf");
  assert.ok(world.calls.length > create);
  assert.strictEqual([...world.files.values()].filter((f) => f.name === "_OCR scratch").length, 1);
  refused(ctx, "ocr", { data: b64("x"), mimeType: "image/heic" }, "bad_request");
  refused(ctx, "ocr", { data: b64("x"), mimeType: "text/html" }, "bad_request");
});

test("ocr: the doc is removed even when reading fails, and old leftovers are cleared", () => {
  const { world, ctx } = ready();
  ok(ctx, "ocr", { data: b64("TEXT:x"), mimeType: "image/png" });
  const scratch = [...world.files.values()].find((f) => f.name === "_OCR scratch");
  const old = world.add({ name: "Reading old", mimeType: GOOGLE_DOC, parents: [scratch.id], driveId: HR,
    modifiedTime: new Date(Date.now() - 2 * 3600 * 1000).toISOString() });
  const recent = world.add({ name: "Reading now", mimeType: GOOGLE_DOC, parents: [scratch.id], driveId: HR,
    modifiedTime: new Date().toISOString() });
  world.docFails = true;
  refused(ctx, "ocr", { data: b64("TEXT:y"), mimeType: "image/png" }, "unavailable");
  const docs = () => [...world.files.values()].filter((f) => f.mimeType === GOOGLE_DOC);
  assert.deepStrictEqual(docs().map((d) => d.id), [recent.id], "the new doc is gone, and so is the old one");
  assert.ok(!world.files.has(old.id), "an hour-old leftover is removed");
  assert.ok(world.files.has(recent.id), "a recent one may still be in use");
  // Only a Content manager: the doc goes to the drive's trash instead.
  world.docFails = false;
  world.removeDenied = true;
  ok(ctx, "ocr", { data: b64("TEXT:z"), mimeType: "image/png" });
  const left = docs().filter((d) => d.id !== recent.id);
  assert.ok(left.length === 1 && left[0].trashed);
});

test("Google's own errors come back in plain words", () => {
  const { world, ctx } = ready();
  world.failNext = { method: "Files.create", message: "User rate limit exceeded." };
  refused(ctx, "ensureFolder", { parent: HR, name: "x", props: { a: "1" } }, "quota");
  world.failNext = { method: "Files.list", message: "Insufficient permissions for the specified parent." };
  refused(ctx, "ensureFolder", { parent: HR, name: "x", props: { a: "1" } }, "not_allowed");
  world.failNext = { method: "Files.list", message: "Internal Error" };
  const err = refused(ctx, "ensureFolder", { parent: HR, name: "x", props: { a: "1" } }, "unavailable");
  assert.match(err.message, /Internal Error/);
});

test("selfTest: sign-in, the drive, a test folder and reading a test image", () => {
  const { world, ctx } = ready();
  world.ocrRead = (bytes, mime) => {
    assert.strictEqual(mime, "image/png");
    assert.ok(unsigned(bytes).subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])));
    return "EXPIRES O6/14/2029\n";
  };
  const before = world.files.size;
  const { checks } = ok(ctx, "selfTest", {});
  assert.deepStrictEqual(checks.map((c) => c.check), ["Sign-in", "Shared drive",
    "Make and remove a test folder", "Read text from a test image"]);
  assert.ok(checks.every((c) => c.ok), JSON.stringify(checks));
  assert.match(checks[0].detail, /owner@hotlake\.example/);
  assert.match(checks[1].detail, /HR Employee Files and HR Restricted/);
  assert.strictEqual(world.files.size, before + 1, "only the _OCR scratch folder is left");
  // Without a drive chosen, those checks fail with a reason; reading something else fails too.
  const fresh = makeWorld();
  const ctx2 = load(fresh);
  const second = ok(ctx2, "selfTest", {}).checks;
  assert.deepStrictEqual(second.map((c) => c.ok), [true, false, false, false]);
  assert.match(second[1].detail, /No shared drive/);
  world.ocrRead = () => "something else";
  const third = ok(ctx, "selfTest", {}).checks;
  assert.strictEqual(third[3].ok, false);
  assert.match(third[3].detail, /something else/);
});

test("requests signed by Staff Access (Python) are accepted, whatever the text", () => {
  const vectors = JSON.parse(fs.readFileSync(vectorsPath, "utf8"));
  assert.ok(vectors.length >= 3);
  const world = makeWorld();
  const ctx = load(world);
  ok(ctx, "setup", { drives: [HR] });
  for (const v of vectors) {
    const reply = postText(ctx, v.body);
    if (v.expect === "ok") {
      assert.ok(reply.ok, `${v.name}: ${JSON.stringify(reply.error)}`);
      if (v.folder_name) assert.strictEqual(reply.result.name, v.folder_name);
    } else {
      assert.strictEqual(reply.error.code, v.expect, v.name);
    }
  }
});

test("no reply ever holds the secret", () => {
  assert.ok(replies.length > 50);
  for (const text of replies) assert.ok(!text.includes(SECRET));
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`FAILED - ${name}\n${err && err.stack}`);
  }
}
console.log(`${tests.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
