/**
 * Local WhatsApp Web gateway that mimics a subset of Green API endpoints used by this project.
 * Endpoints (approximate):
 *  - GET  /qr                              -> PNG image of QR (or 204 if not needed)
 *  - GET  /status                          -> { state, info }
 *  - POST /waInstance:instance/sendMessage/:token
 *  - POST /waInstance:instance/sendFileByUrl/:token
 *  - POST /waInstance:instance/SendFileByUpload/:token (multipart/form-data)
 *  - POST /waInstance:instance/uploadFile/:token (multipart -> returns {urlFile})
 *  - GET  /waInstance:instance/ReceiveNotification/:token (long poll-ish)
 *  - DELETE /waInstance:instance/DeleteNotification/:token/:receiptId
 *  - POST   /waInstance:instance/DeleteNotification/:token {receiptId}
 *  - POST /waInstance:instance/sendReaction/:token
 *  - POST /waInstance:instance/sendPoll/:token
 *  - POST /waInstance:instance/sendContact/:token
 *  - POST /waInstance:instance/sendStickerByUrl/:token
 *  - POST /waInstance:instance/SendStickerByUpload/:token (multipart)
 *
 * Notes:
 *  - We do not validate instance or token; they are pass-through to be compatible with the Python client.
 *  - Files uploaded here are served at /files/:name
 */

const express = require("express");
const cors = require("cors");
const multer = require("multer");
const path = require("path");
const fs = require("fs");
const { v4: uuidv4 } = require("uuid");
const axios = require("axios");
const QRCode = require("qrcode");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const app = express();
const PORT = process.env.GATEWAY_PORT || 3000;

// Basic storage
const DATA_DIR = path.join(__dirname, "data");
const UPLOAD_DIR = path.join(DATA_DIR, "uploads");
fs.mkdirSync(UPLOAD_DIR, { recursive: true });

// Middlewares
app.use(cors());
app.use(express.json({ limit: "20mb" }));
app.use("/files", express.static(UPLOAD_DIR));

// Multer for upload
const upload = multer({
  dest: UPLOAD_DIR,
  limits: { fileSize: 50 * 1024 * 1024 }, // 50MB
});

// WhatsApp client
let lastQr = null;
let clientReady = false;

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: path.join(DATA_DIR, "auth") }),
  puppeteer: {
    headless: true,
    executablePath: process.env.CHROME_PATH || undefined,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--no-zygote",
      "--disable-features=VizDisplayCompositor",
    ],
  },
});

client.on("qr", async (qr) => {
  lastQr = qr;
});

client.on("ready", () => {
  clientReady = true;
  lastQr = null;
  console.log("WhatsApp client is ready");
});

client.on("disconnected", (reason) => {
  clientReady = false;
  console.log("WhatsApp client disconnected:", reason);
});

client.initialize().catch((e) => {
  console.error("Failed to init WhatsApp client:", e);
});

// Notification queue (simple)
let notifications = [];
let nextReceiptId = 1;

// Helper to push incoming messages into queue in a Green-API-like shape
client.on("message", async (message) => {
  try {
    const body = {
      typeWebhook: "incomingMessageReceived",
      instanceData: { idInstance: "local" },
      timestamp: Math.floor(Date.now() / 1000),
      senderData: {
        chatId: message.from,
        sender: message.author || message.from,
      },
      messageData: {
        typeMessage: "textMessage",
        textMessageData: { textMessage: message.body || "",
          // keep id for reaction targets
        },
        chatId: message.from,
        idMessage: message.id.id || message.id._serialized,
      },
    };

    // media detection
    if (message.hasMedia) {
      const media = await message.downloadMedia();
      let mimeType = media.mimetype || "application/octet-stream";
      let fileName = `media_${Date.now()}`;
      const ext = mimeType.split("/")[1] || "bin";
      fileName = `${fileName}.${ext}`;
      const outPath = path.join(UPLOAD_DIR, fileName);
      fs.writeFileSync(outPath, Buffer.from(media.data, "base64"));
      const urlFile = `${getBaseUrl()}/files/${fileName}`;
      // Map to Green-API style fields
      body.messageData = {
        ...body.messageData,
        typeMessage: mimeType.startsWith("image/") ? "imageMessage" : "fileMessage",
        imageMessageData: mimeType.startsWith("image/")
          ? { caption: message.body || "", downloadUrl: urlFile, mimeType, fileName }
          : undefined,
        fileMessageData: !mimeType.startsWith("image/")
          ? { caption: message.body || "", downloadUrl: urlFile, mimeType, fileName }
          : undefined,
        medias: [
          {
            url: urlFile,
            mimeType,
            fileName,
            caption: message.body || "",
          },
        ],
      };
    }

    const envelope = {
      receiptId: nextReceiptId++,
      body,
    };
    notifications.push(envelope);
  } catch (e) {
    console.error("Error handling incoming message:", e);
  }
});

function getBaseUrl() {
  const host = process.env.GATEWAY_PUBLIC_URL || `http://localhost:${PORT}`;
  return host.replace(/\/$/, "");
}

// Basic status
app.get("/status", async (req, res) => {
  try {
    const info = client.info || null;
    res.json({
      ok: true,
      ready: clientReady,
      me: info ? { wid: info.wid._serialized, pushname: info.pushname } : null,
    });
  } catch (e) {
    res.json({ ok: false, error: String(e) });
  }
});

// QR as PNG (auto refresh recommended on frontend)
app.get("/qr", async (req, res) => {
  try {
    if (clientReady || !lastQr) {
      return res.status(204).end();
    }
    res.setHeader("Content-Type", "image/png");
    const stream = await QRCode.toBuffer(lastQr, { type: "png", scale: 6 });
    res.send(stream);
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// Helpers
function parseDestination(body) {
  let chatId = body.chatId || null;
  const phoneNumber = body.phoneNumber || null;
  if (!chatId && phoneNumber) {
    chatId = `${String(phoneNumber)}@c.us`;
  }
  return chatId;
}

// Mimic Green-API path handling
function withPaths(p) {
  // Accept both with and without trailing slash
  app.post(new RegExp(`^/waInstance[^/]+/${p}/[^/]+/?), ...Array.prototype.slice.call(arguments, 1));
  app.get(new RegExp(`^/waInstance[^/]+/${p}/[^/]+/?), ...Array.prototype.slice.call(arguments, 1));
  app.delete(new RegExp(`^/waInstance[^/]+/${p}/[^/]+/[^/]+/?), ...Array.prototype.slice.call(arguments, 1));
}

// ReceiveNotification (GET)
app.get(/^\/waInstance[^/]+\/ReceiveNotification\/[^/]+\/?$/, async (req, res) => {
  try {
    // simple poll: return first queued item or 204
    if (notifications.length === 0) {
      return res.status(204).end();
    }
    const env = notifications[0];
    return res.json(env);
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// DeleteNotification: DELETE /.../DeleteNotification/{token}/{receiptId}
app.delete(/^\/waInstance[^/]+\/DeleteNotification\/[^/]+\/(\d+)\/?$/, async (req, res) => {
  try {
    const rid = parseInt(req.params[0], 10);
    notifications = notifications.filter((n) => n.receiptId !== rid);
    res.json({ result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// DeleteNotification: POST /.../DeleteNotification/{token} with {"receiptId":...}
app.post(/^\/waInstance[^/]+\/DeleteNotification\/[^/]+\/?$/, async (req, res) => {
  try {
    const rid = parseInt(req.body && req.body.receiptId, 10);
    if (!isNaN(rid)) {
      notifications = notifications.filter((n) => n.receiptId !== rid);
    }
    res.json({ result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// sendMessage
app.post(/^\/waInstance[^/]+\/sendMessage\/[^/]+\/?$/, async (req, res) => {
  try {
    const { message } = req.body || {};
    const chatId = parseDestination(req.body || {});
    if (!chatId || !message) {
      return res.status(400).json({ ok: false, error: "chatId/phoneNumber and message required" });
    }
    const sent = await client.sendMessage(chatId, message);
    res.json({ idMessage: sent.id._serialized || sent.id.id || null, sent: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// sendFileByUrl
app.post(/^\/waInstance[^/]+\/sendFileByUrl\/[^/]+\/?$/, async (req, res) => {
  try {
    const { urlFile, fileName, caption } = req.body || {};
    const chatId = parseDestination(req.body || {});
    if (!chatId || !urlFile) {
      return res.status(400).json({ ok: false, error: "chatId/phoneNumber and urlFile required" });
    }
    // fetch file to buffer
    const r = await axios.get(urlFile, { responseType: "arraybuffer" });
    const mimeType = r.headers["content-type"] || "application/octet-stream";
    const data = Buffer.from(r.data);
    const media = new MessageMedia(mimeType, data.toString("base64"), fileName || "file");
    const sent = await client.sendMessage(chatId, media, { caption: caption || "" });
    res.json({ idMessage: sent.id._serialized || null, result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// SendFileByUpload (multipart)
app.post(/^\/waInstance[^/]+\/SendFileByUpload\/[^/]+\/?$/, upload.single("file"), async (req, res) => {
  try {
    const chatId = parseDestination(req.body || {});
    const caption = req.body && req.body.caption ? String(req.body.caption) : "";
    if (!chatId || !req.file) {
      return res.status(400).json({ ok: false, error: "chatId/phoneNumber and file required" });
    }
    const filePath = req.file.path;
    const fileName = req.body.fileName || req.file.originalname || path.basename(filePath);
    const mimeType = req.file.mimetype || "application/octet-stream";
    const data = fs.readFileSync(filePath);
    const media = new MessageMedia(mimeType, data.toString("base64"), fileName);
    const sent = await client.sendMessage(chatId, media, { caption });
    res.json({ idMessage: sent.id._serialized || null, result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// uploadFile (store and return URL)
app.post(/^\/waInstance[^/]+\/uploadFile\/[^/]+\/?$/, upload.single("file"), async (req, res) => {
  try {
    if (!req.file) {
      return res.status(400).json({ ok: false, error: "file required" });
    }
    const fileName = req.file.originalname || `upload_${uuidv4()}`;
    const target = path.join(UPLOAD_DIR, fileName);
    fs.renameSync(req.file.path, target);
    const urlFile = `${getBaseUrl()}/files/${encodeURIComponent(fileName)}`;
    res.json({ urlFile, fileName });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// sendReaction
app.post(/^\/waInstance[^/]+\/sendReaction\/[^/]+\/?$/, async (req, res) => {
  try {
    const { idMessage, emoji } = req.body || {};
    const chatId = parseDestination(req.body || {});
    if (!chatId || !idMessage || !emoji) {
      return res.status(400).json({ ok: false, error: "chatId, idMessage and emoji required" });
    }
    const msg = await client.getMessageById(idMessage);
    if (!msg) {
      return res.status(404).json({ ok: false, error: "message not found" });
    }
    await msg.react(emoji);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// sendPoll
app.post(/^\/waInstance[^/]+\/sendPoll\/[^/]+\/?$/, async (req, res) => {
  try {
    const { name, options, selectableCount } = req.body || {};
    const chatId = parseDestination(req.body || {});
    if (!chatId || !name || !Array.isArray(options) || options.length < 2) {
      return res.status(400).json({ ok: false, error: "chatId, name and at least two options required" });
    }
    const opts = { poll: { name, options, selectableCount: Math.max(1, parseInt(selectableCount || 1, 10)) } };
    const sent = await client.sendMessage(chatId, name, opts);
    res.json({ idMessage: sent.id._serialized || null, result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// sendStickerByUrl
app.post(/^\/waInstance[^/]+\/sendStickerByUrl\/[^/]+\/?$/, async (req, res) => {
  try {
    const { urlFile } = req.body || {};
    const chatId = parseDestination(req.body || {});
    if (!chatId || !urlFile) {
      return res.status(400).json({ ok: false, error: "chatId/phoneNumber and urlFile required" });
    }
    const r = await axios.get(urlFile, { responseType: "arraybuffer" });
    const mimeType = r.headers["content-type"] || "image/png";
    const data = Buffer.from(r.data);
    const media = new MessageMedia(mimeType, data.toString("base64"), "sticker");
    const sent = await client.sendMessage(chatId, media, { sendMediaAsSticker: true });
    res.json({ idMessage: sent.id._serialized || null, result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// SendStickerByUpload (multipart)
app.post(/^\/waInstance[^/]+\/SendStickerByUpload\/[^/]+\/?$/, upload.single("file"), async (req, res) => {
  try {
    const chatId = parseDestination(req.body || {});
    if (!chatId || !req.file) {
      return res.status(400).json({ ok: false, error: "chatId/phoneNumber and file required" });
    }
    const filePath = req.file.path;
    const fileName = req.body.fileName || req.file.originalname || path.basename(filePath);
    const mimeType = req.file.mimetype || "image/png";
    const data = fs.readFileSync(filePath);
    const media = new MessageMedia(mimeType, data.toString("base64"), fileName);
    const sent = await client.sendMessage(chatId, media, { sendMediaAsSticker: true });
    res.json({ idMessage: sent.id._serialized || null, result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// sendContact (vCard)
app.post(/^\/waInstance[^/]+\/sendContact\/[^/]+\/?$/, async (req, res) => {
  try {
    const { name, phoneNumber } = req.body || {};
    const chatId = parseDestination(req.body || {});
    if (!chatId || !name || !phoneNumber) {
      return res.status(400).json({ ok: false, error: "chatId, name and phoneNumber required" });
    }
    const vcard =
      "BEGIN:VCARD\n" +
      "VERSION:3.0\n" +
      `FN:${name}\n` +
      `TEL;TYPE=CELL:${phoneNumber}\n` +
      "END:VCARD";
    const media = new MessageMedia("text/vcard", Buffer.from(vcard, "utf8").toString("base64"), "contact.vcf");
    const sent = await client.sendMessage(chatId, media);
    res.json({ idMessage: sent.id._serialized || null, result: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// Health
app.get("/health", (req, res) => {
  res.json({ ok: true, version: "0.1.1" });
});

app.listen(PORT, () => {
  console.log(`WhatsApp gateway listening on ${PORT}`);
});