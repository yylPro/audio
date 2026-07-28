import { spawn } from "node:child_process";

const TRIGGERS = ["#听音检测", "#听音质检", "#录音质检"];
const PYTHON = process.env.AUDIO_QUALITY_PYTHON ||
    "D:\\代维\\工单提醒\\audio_quality_runtime\\.venv\\Scripts\\python.exe";
const CONFIG = process.env.AUDIO_QUALITY_CONFIG ||
    "D:\\代维\\工单提醒\\audio_quality_config.json";
const PROJECT_ROOT = "D:\\代维\\工单提醒";
const TIMEOUT_MS = 60_000;

function runIngress(event) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, ["-m", "audio_quality.inbound_adapter", "--config", CONFIG], {
            cwd: PROJECT_ROOT,
            windowsHide: true,
            stdio: ["pipe", "pipe", "pipe"],
            env: { ...process.env, PYTHONUTF8: "1" },
        });
        let stdout = "";
        let stderr = "";
        const timer = setTimeout(() => {
            child.kill();
            reject(new Error("入站适配器处理超时"));
        }, TIMEOUT_MS);
        child.stdout.setEncoding("utf8");
        child.stderr.on("data", chunk => { stderr += chunk; });
        child.stdout.on("data", chunk => { stdout += chunk; });
        child.on("error", err => {
            clearTimeout(timer);
            reject(err);
        });
        child.on("close", code => {
            clearTimeout(timer);
            const lines = stdout.trim().split(/\r?\n/).filter(Boolean);
            if (lines.length === 0) {
                reject(new Error(stderr.trim() || `入站适配器无输出，退出码 ${code}`));
                return;
            }
            try {
                resolve(JSON.parse(lines.at(-1)));
            }
            catch {
                reject(new Error(`入站适配器返回了无效 JSON：${lines.at(-1)}`));
            }
        });
        child.stdin.end(JSON.stringify(event));
    });
}

export const audioQualityIngress = {
    name: "audio-quality-ingress",
    when: ctx => ctx.isGroup && TRIGGERS.some(trigger => ctx.rawBody.includes(trigger)),
    handler: async ctx => {
        const event = {
            message_id: ctx.raw.msg_id ?? String(ctx.raw.msg_seq ?? ""),
            group_id: ctx.groupCode ?? "",
            sender_user_id: ctx.fromAccount,
            sender_name: ctx.senderNickname ?? "",
            text: ctx.rawBody,
            is_at_bot: ctx.isAtBot,
            media_paths: ctx.mediaPaths,
            media_types: ctx.mediaTypes,
        };
        let reply;
        try {
            const result = await runIngress(event);
            reply = result.reply || "【听音质检】接收失败：入站适配器未返回结果。";
            ctx.log.info("[audio-quality-ingress] handled", {
                messageId: event.message_id,
                taskId: result.task_id,
                created: result.created,
                ok: result.ok,
            });
        }
        catch (err) {
            ctx.log.error("[audio-quality-ingress] failed", { error: String(err) });
            reply = `【听音质检】接收失败：${String(err)}`;
        }
        if (!ctx.sender) {
            ctx.log.error("[audio-quality-ingress] sender is unavailable");
            return;
        }
        await ctx.sender.sendText(reply);
        ctx.statusSink?.({ lastOutboundAt: Date.now() });
    },
};
