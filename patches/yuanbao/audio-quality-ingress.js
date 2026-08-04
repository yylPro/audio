import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const TRIGGERS = ["#听音检测", "#听音质检", "#录音质检"];
const PROJECT_ROOT = process.env.WORK_ORDER_PROJECT_ROOT || "D:\\代维\\工单提醒";
const PYTHON = process.env.AUDIO_QUALITY_PYTHON || path.join(PROJECT_ROOT, "audio_quality_runtime", ".venv", "Scripts", "python.exe");
const CONFIG = process.env.AUDIO_QUALITY_CONFIG || path.join(PROJECT_ROOT, "audio_quality_config.json");
const PENDING_ROOT = path.join(PROJECT_ROOT, "audio-inbox", "pending-ingress");
const PENDING_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const AUDIO_SUFFIXES = new Set([".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".amr"]);
const TIMEOUT_MS = 60_000;

function hasTrigger(text) { return TRIGGERS.some(item => String(text ?? "").includes(item)); }
function isAddressed(ctx) { return ctx.isAtBot === true || /@[^\\s]+/.test(String(ctx.rawBody ?? "")); }
function audioPaths(ctx) {
    return (ctx.mediaPaths ?? []).filter(item => AUDIO_SUFFIXES.has(path.extname(String(item)).toLowerCase()));
}
function pendingKey(ctx) {
    return crypto.createHash("sha256").update(`${ctx.groupCode ?? ""}\0${ctx.fromAccount ?? ""}`).digest("hex");
}
function manifestPath(ctx) { return path.join(PENDING_ROOT, `${pendingKey(ctx)}.json`); }
function removeManifestFiles(manifest) {
    for (const item of manifest?.paths ?? []) {
        try { fs.rmSync(item, { force: true }); } catch {}
        const parent = path.dirname(item);
        if (parent.startsWith(`${PENDING_ROOT}${path.sep}`)) { try { fs.rmSync(parent, { recursive: true, force: true }); } catch {} }
    }
}
function stageAudio(ctx) {
    const sources = audioPaths(ctx).filter(item => fs.existsSync(item));
    if (!sources.length) return [];
    fs.mkdirSync(PENDING_ROOT, { recursive: true });
    const manifestFile = manifestPath(ctx);
    try { removeManifestFiles(JSON.parse(fs.readFileSync(manifestFile, "utf8"))); } catch {}
    const key = pendingKey(ctx);
    const staged = sources.map((source, index) => {
        const dir = path.join(PENDING_ROOT, `${key}-${Date.now()}-${index}`);
        fs.mkdirSync(dir, { recursive: true });
        const target = path.join(dir, path.basename(source));
        fs.copyFileSync(source, target);
        return target;
    });
    fs.writeFileSync(manifestFile, JSON.stringify({ createdAt: Date.now(), paths: staged }), "utf8");
    return staged;
}
function loadPending(ctx) {
    try {
        const file = manifestPath(ctx);
        const manifest = JSON.parse(fs.readFileSync(file, "utf8"));
        if (Date.now() - Number(manifest.createdAt ?? 0) > PENDING_MAX_AGE_MS) { removeManifestFiles(manifest); fs.rmSync(file, { force: true }); return []; }
        return (manifest.paths ?? []).filter(item => fs.existsSync(item));
    } catch { return []; }
}
function clearPending(ctx) {
    const file = manifestPath(ctx);
    try { removeManifestFiles(JSON.parse(fs.readFileSync(file, "utf8"))); } catch {}
    try { fs.rmSync(file, { force: true }); } catch {}
}
function runIngress(event) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, ["-m", "audio_quality.inbound_adapter", "--config", CONFIG], { cwd: PROJECT_ROOT, windowsHide: true, stdio: ["pipe", "pipe", "pipe"], env: { ...process.env, PYTHONUTF8: "1" } });
        let stdout = "", stderr = "";
        const timer = setTimeout(() => { child.kill(); reject(new Error("入站适配器处理超时")); }, TIMEOUT_MS);
        child.stdout.setEncoding("utf8"); child.stdout.on("data", chunk => { stdout += chunk; }); child.stderr.on("data", chunk => { stderr += chunk; });
        child.on("error", error => { clearTimeout(timer); reject(error); });
        child.on("close", code => {
            clearTimeout(timer);
            const line = stdout.trim().split(/\r?\n/).filter(Boolean).at(-1);
            if (!line) return reject(new Error(stderr.trim() || `入站适配器无输出，退出码 ${code}`));
            try { resolve(JSON.parse(line)); } catch { reject(new Error(`入站适配器返回无效 JSON: ${line}`)); }
        });
        child.stdin.end(JSON.stringify(event));
    });
}

export const audioQualityIngress = {
    name: "audio-quality-ingress",
    when: ctx => ctx.isGroup && ((hasTrigger(ctx.rawBody) && isAddressed(ctx)) || audioPaths(ctx).length > 0),
    handler: async ctx => {
        const triggered = hasTrigger(ctx.rawBody) && isAddressed(ctx);
        const staged = stageAudio(ctx);
        if (!triggered) { ctx.log.info("[audio-quality-ingress] audio staged", { count: staged.length }); return; }
        const pendingPaths = staged.length ? staged : loadPending(ctx);
        const event = { message_id: ctx.raw.msg_id ?? String(ctx.raw.msg_seq ?? ""), group_id: ctx.groupCode ?? "", sender_user_id: ctx.fromAccount, sender_name: ctx.senderNickname ?? "", text: ctx.rawBody, is_at_bot: ctx.isAtBot === true, media_paths: pendingPaths, media_types: ctx.mediaTypes };
        let reply;
        try {
            const result = await runIngress(event);
            reply = result.reply || "【听音质检】接收失败：入站适配器未返回结果。";
            if (result.ok) clearPending(ctx);
            ctx.log.info("[audio-quality-ingress] handled", { messageId: event.message_id, taskId: result.task_id, created: result.created, ok: result.ok, mediaCount: pendingPaths.length });
        } catch (error) { ctx.log.error("[audio-quality-ingress] failed", { error: String(error) }); reply = `【听音质检】接收失败：${String(error)}`; }
        if (ctx.sender) { await ctx.sender.sendText(reply); ctx.statusSink?.({ lastOutboundAt: Date.now() }); }
    },
};
