import readline from "node:readline";
import AiBot from "@wecom/aibot-node-sdk";

let client = null;
let providerBotId = "";
const MIN_UPLOAD_BYTES = 1024;
const TEXT_FILE_EXTENSIONS = new Set([
  ".csv", ".json", ".log", ".md", ".txt", ".xml", ".yaml", ".yml",
]);

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function uploadBuffer(attachment) {
  const buffer = Buffer.from(attachment.data, "base64");
  const filename = String(attachment.name || "attachment");
  const extension = filename.slice(filename.lastIndexOf(".")).toLowerCase();
  if (
    attachment.kind !== "file"
    || buffer.length >= MIN_UPLOAD_BYTES
    || !TEXT_FILE_EXTENSIONS.has(extension)
  ) {
    return buffer;
  }
  // The WeCom media service rejects very small file uploads. Padding a text
  // upload with newlines preserves its visible content and original filename.
  return Buffer.concat([buffer, Buffer.alloc(MIN_UPLOAD_BYTES - buffer.length, "\n")]);
}

function start({ bot_id, secret }) {
  providerBotId = bot_id;
  client?.disconnect();
  client = new AiBot.WSClient({
    botId: bot_id,
    secret,
    logger: {
      info(...parts) { emit({ type: "log", level: "info", message: parts.join(" ") }); },
      debug(...parts) { emit({ type: "log", level: "debug", message: parts.join(" ") }); },
      warn(...parts) { emit({ type: "log", level: "warning", message: parts.join(" ") }); },
      error(...parts) { emit({ type: "log", level: "error", message: parts.join(" ") }); },
    },
  });
  client.on("authenticated", () => emit({ type: "status", status: "connected" }));
  client.on("error", (error) => emit({ type: "status", status: "error", error: String(error?.message || error) }));
  client.on("disconnected", () => emit({ type: "status", status: "connecting" }));
  client.on("reconnecting", () => emit({ type: "status", status: "connecting" }));
  client.on("message", (frame) => {
    void handleMessage(frame).catch((error) => emit({ type: "status", status: "error", error: String(error?.message || error) }));
  });
  client.connect();
}

async function downloadAttachment(content, kind) {
  if (!content?.url) return null;
  const { buffer, filename } = await client.downloadFile(content.url, content.aeskey);
  return {
    kind,
    name: filename || `${kind}-${Date.now()}`,
    data: buffer.toString("base64"),
  };
}

async function handleMessage(frame) {
  const body = frame?.body;
  const sender = body?.from?.userid;
  const isGroup = body?.chattype === "group";
  const chatId = isGroup ? body?.chatid : sender;
  if (!body?.msgid || !chatId || (body.chattype !== "group" && body.chattype !== "single")) return;
  const attachments = [];
  let text = "";
  if (body.msgtype === "text") text = body?.text?.content || "";
  if (body.msgtype === "voice") text = body?.voice?.content || "";
  if (body.msgtype === "image") {
    const attachment = await downloadAttachment(body.image, "image");
    if (attachment) attachments.push(attachment);
  }
  if (body.msgtype === "file") {
    const attachment = await downloadAttachment(body.file, "file");
    if (attachment) attachments.push(attachment);
  }
  if (body.msgtype === "video") {
    const attachment = await downloadAttachment(body.video, "video");
    if (attachment) attachments.push(attachment);
  }
  if (body.msgtype === "mixed") {
    for (const item of body?.mixed?.msg_item || []) {
      if (item.msgtype === "text") text += `${text ? "\n" : ""}${item?.text?.content || ""}`;
      if (item.msgtype === "image") {
        const attachment = await downloadAttachment(item.image, "image");
        if (attachment) attachments.push(attachment);
      }
    }
  }
  emit({
    type: "inbound",
    provider_bot_id: body.aibotid || providerBotId,
    provider_message_id: body.msgid,
    kind: isGroup ? "group" : "direct",
    remote_conversation_id: chatId,
    text,
    attachments,
    display_name: isGroup ? "企微群聊" : "企微私聊",
    reply_target: { provider_bot_id: body.aibotid || providerBotId, chat_id: chatId },
  });
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", async (line) => {
  try {
    const command = JSON.parse(line);
    if (command.type === "start") start(command);
    if (command.type === "reply" && client) {
      if (command.text) {
        await client.sendMessage(command.chat_id, {
          msgtype: "markdown",
          markdown: { content: command.text },
        });
      }
      for (const attachment of command.attachments || []) {
        const type = attachment.kind === "audio" ? "voice" : attachment.kind === "video" ? "video" : attachment.kind === "image" ? "image" : "file";
        const uploaded = await client.uploadMedia(uploadBuffer(attachment), {
          type,
          filename: attachment.name || "attachment",
        });
        await client.sendMediaMessage(command.chat_id, type, uploaded.media_id, type === "video" ? { title: attachment.name || "视频" } : undefined);
      }
    }
    if (command.type === "stop") {
      client?.disconnect();
      process.exit(0);
    }
  } catch (error) {
    emit({ type: "status", status: "error", error: String(error?.message || error) });
  }
});
