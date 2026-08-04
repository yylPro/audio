import { spawn } from "node:child_process";

const TRIGGER = "#回单整理";
const PYTHON = process.env.COMMUNICATION_PYTHON ||
    "C:\\Users\\14137\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe";
const CONFIG = process.env.COMMUNICATION_CONFIG ||
    "D:\\代维\\工单提醒\\config.json";
const PROJECT_ROOT = "D:\\代维\\工单提醒";
const TIMEOUT_MS = 120_000;

function runIngress(event) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, ["-m", "communication.inbound_adapter", "--config", CONFIG], {
            cwd: PROJECT_ROOT,
            windowsHide: true,
            stdio: ["pipe", "pipe", "pipe"],
            env: { ...process.env, PYTHONUTF8: "1" },
        });
        let stdout = "";
        let stderr = "";
        const timer = setTimeout(() => {
            child.kill();
            reject(new Error("回单整理入站适配器处理超时"));
        }, TIMEOUT_MS);
        child.stdout.setEncoding("utf8");
        child.stderr.setEncoding("utf8");
        child.stdout.on("data", chunk => { stdout += chunk; });
        child.stderr.on("data", chunk => { stderr += chunk; });
        child.on("error", err => {
            clearTimeout(timer);
            reject(err);
        });
        child.on("close", code => {
            clearTimeout(timer);
            const lines = stdout.trim().split(/\r?\n/).filter(Boolean);
            if (lines.length === 0) {
                reject(new Error(stderr.trim() || `回单整理入站适配器无输出，退出码 ${code}`));
                return;
            }
            try {
                resolve(JSON.parse(lines.at(-1)));
            }
            catch {
                reject(new Error(`回单整理入站适配器返回了无效 JSON：${lines.at(-1)}`));
            }
        });
        child.stdin.end(JSON.stringify(event));
    });
}

async function sendReply(ctx, reply) {
    // Use Yuanbao's normal queue so long dual-output replies are split at the
    // channel limit (the direct sender path can be truncated by the server).
    if (ctx.queueSession?.push && ctx.queueSession?.flush) {
        await ctx.queueSession.push({ type: "text", text: reply });
        await ctx.queueSession.flush();
        return;
    }
    await ctx.sender.sendText(reply);
}

export const communicationIngress = {
    name: "communication-ingress",
    when: ctx => ctx.isGroup && typeof ctx.rawBody === "string" && ctx.rawBody.includes(TRIGGER),
    handler: async ctx => {
        const event = {
            message_id: ctx.raw.msg_id ?? String(ctx.raw.msg_seq ?? ""),
            group_id: ctx.groupCode ?? "",
            sender_user_id: ctx.fromAccount,
            sender_name: ctx.senderNickname ?? "",
            text: ctx.rawBody,
            // isAtBot is populated by extract-content. Keep the mention list
            // as a compatibility fallback for plugin builds that expose only
            // structured mentions at this stage.
            is_at_bot: Boolean(ctx.isAtBot ||
                ctx.mentions?.some(item => item?.userId && item.userId === ctx.account?.botId)),
        };
        let reply;
        try {
            const result = await runIngress(event);
            reply = result.reply || "【回单整理】处理失败：入站适配器未返回结果。";
            ctx.log.info("[communication-ingress] handled", {
                messageId: event.message_id,
                taskId: result.task_id,
                created: result.created,
                ok: result.ok,
                generationFailed: result.generation_failed,
            });
        }
        catch (err) {
            ctx.log.error("[communication-ingress] failed", { error: String(err) });
            reply = `【回单整理】处理失败：${String(err)}`;
        }
        if (!ctx.sender) {
            ctx.log.error("[communication-ingress] sender is unavailable");
            return;
        }
        await sendReply(ctx, reply);
        ctx.statusSink?.({ lastOutboundAt: Date.now() });
    },
};
