from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


SUPPORTED_VERSION = "2.17.0"
MARKER = "YUANBAO_NATIVE_AT_PATCH_V1"
DISCOVERY_MARKER = "YUANBAO_MEMBER_DISCOVERY_V1"
SAFETY_MARKER = "YUANBAO_NATIVE_AT_SAFETY_V1"
FILES = [
    "dist/src/business/messaging/handlers/index.js",
    "dist/src/business/pipeline/middlewares/extract-content.js",
    "dist/src/business/actions/text/send.js",
    "dist/src/business/actions/deliver.js",
    "dist/src/infra/transport.js",
    "dist/src/access/ws/biz-codec.js",
    "dist/src/access/ws/proto/biz.json",
]


def replace_required(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"Patch point not found: {label}. Plugin structure may have changed.")
    return text.replace(old, new)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def resolve_plugin_root(openclaw: str, explicit: str | None) -> Path:
    if explicit:
        root = Path(explicit).resolve()
    else:
        completed = subprocess.run(
            [openclaw, "plugins", "inspect", "yuanbao", "--runtime", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = completed.stdout + completed.stderr
        start = output.find("{")
        if completed.returncode or start < 0:
            raise RuntimeError(f"Could not locate Yuanbao plugin: {output.strip()}")
        metadata = json.loads(output[start:])
        if metadata["plugin"]["version"] != SUPPORTED_VERSION:
            raise RuntimeError(
                f"Unsupported Yuanbao plugin version {metadata['plugin']['version']}; "
                f"expected {SUPPORTED_VERSION}."
            )
        root = Path(metadata["plugin"]["rootDir"])
    package = json.loads(read(root / "package.json"))
    if explicit and package.get("version") != SUPPORTED_VERSION:
        print(
            f"Warning: package.json reports {package.get('version')}; "
            "offline mode will validate source patch points instead.",
            file=sys.stderr,
        )
    return root


def patch_handlers(path: Path, mapping_file: Path) -> None:
    text = read(path)
    if MARKER in text:
        return
    if 'import fs from "node:fs";' not in text:
        text = replace_required(
            text,
            'import { videoHandler } from "./video.js";',
            f'import {{ videoHandler }} from "./video.js";\nimport fs from "node:fs"; // {MARKER}',
            "handlers fs import",
        )
    start = text.find("const AT_USER_RE = ")
    end = text.find("export function prepareOutboundContent", start)
    if start < 0 or end < 0:
        raise RuntimeError("Patch point not found: mention resolver")
    mapping_literal = json.dumps(str(mapping_file), ensure_ascii=False)
    resolver = f'''const AT_USER_RE = /(?<=\\s|^)@(\\S+?)(?=\\s|$)/g; // {MARKER}
const MEMBER_MAPPING_FILE = {mapping_literal};
function normalizeMemberName(value) {{
    return value.normalize("NFKC").replace(/[\\u200B-\\u200D\\uFEFF]/g, "").trim();
}}
function lookupConfiguredMember(groupCode, nickName) {{
    try {{
        const mappings = JSON.parse(fs.readFileSync(MEMBER_MAPPING_FILE, "utf8"));
        const target = normalizeMemberName(nickName);
        const entry = Object.entries(mappings?.[groupCode] ?? {{}})
            .find(([name]) => normalizeMemberName(name) === target);
        return entry?.[1] ? {{ nickName: entry[0], userId: entry[1] }} : undefined;
    }} catch {{ return undefined; }}
}}
function resolveAtMentions(text, groupCode, memberInst) {{
    const items = [];
    let lastIndex = 0;
    for (const match of text.matchAll(AT_USER_RE)) {{
        const matchStart = match.index ?? 0;
        const nickName = match[1];
        const cached = groupCode && memberInst ? memberInst.lookupUserByNickName(groupCode, nickName) : undefined;
        const userRecord = cached ?? (groupCode ? lookupConfiguredMember(groupCode, nickName) : undefined);
        if (!userRecord) continue;
        if (matchStart > lastIndex) {{
            const before = text.slice(lastIndex, matchStart);
            if (before.trim()) items.push({{ type: "text", text: before.trim() }});
        }}
        items.push({{
            type: "custom",
            data: JSON.stringify({{
                elem_type: 1002,
                text: `@${{userRecord.nickName}}`,
                user_id: userRecord.userId,
                content: "",
            }}),
        }});
        lastIndex = matchStart + match[0].length;
    }}
    if (lastIndex < text.length) {{
        const trailing = text.slice(lastIndex);
        if (trailing.trim()) items.push({{ type: "text", text: trailing.trim() }});
    }}
    if (items.length === 0 && text.trim()) items.push({{ type: "text", text: text.trim() }});
    return items;
}}
'''
    write(path, text[:start] + resolver + text[end:])


def patch_extract(path: Path, mapping_file: Path) -> None:
    text = read(path)
    if MARKER in text or "function rememberMembers" in text:
        return
    mapping_literal = json.dumps(str(mapping_file), ensure_ascii=False)
    helper = f'''import {{ extractTextFromMsgBody }} from "../../messaging/extract.js";
import fs from "node:fs"; // {MARKER}
const MEMBER_MAPPING_FILE = {mapping_literal};
function rememberMembers(groupCode, records) {{
    if (!groupCode || groupCode === "unknown") return;
    try {{
        const mappings = fs.existsSync(MEMBER_MAPPING_FILE) ? JSON.parse(fs.readFileSync(MEMBER_MAPPING_FILE, "utf8")) : {{}};
        const group = mappings[groupCode] ?? {{}};
        let changed = false;
        for (const record of records) {{
            const name = record.name?.replace(/^@/, "").trim();
            const userId = record.userId?.trim();
            if (name && userId && group[name] !== userId) {{ group[name] = userId; changed = true; }}
        }}
        if (changed) {{
            mappings[groupCode] = group;
            fs.writeFileSync(MEMBER_MAPPING_FILE, JSON.stringify(mappings, null, 2) + "\\n", "utf8");
        }}
    }} catch {{}}
}}
'''
    text = replace_required(
        text,
        'import { extractTextFromMsgBody } from "../../messaging/extract.js";',
        helper.rstrip(),
        "automatic member learning helper",
    )
    needle = "        const { rawBody, isAtBot, medias, mentions, linkUrls } = extractTextFromMsgBody(minCtx, raw.msg_body);"
    replacement = needle + '''
        if (isGroup) {
            rememberMembers(ctx.groupCode, [
                { name: ctx.senderNickname, userId: ctx.fromAccount },
                ...(mentions ?? []).map(mention => ({ name: mention.text, userId: mention.userId })),
            ]);
        }'''
    write(path, replace_required(text, needle, replacement, "automatic member learning call"))


def patch_send(path: Path, mapping_file: Path) -> None:
    text = read(path)
    if MARKER not in text and "function extractAtUserList" not in text:
        helper = f'''function extractAtUserList(msgBody) {{ // {MARKER}
    const users = [];
    for (const elem of msgBody) {{
        if (elem?.msg_type !== "TIMCustomElem") continue;
        try {{
            const data = JSON.parse(elem.msg_content?.data ?? "{{}}");
            if (data?.elem_type === 1002 && data?.user_id && !users.includes(data.user_id)) users.push(data.user_id);
        }} catch {{}}
    }}
    return users;
}}
function requestedAtNames(text) {{ // {SAFETY_MARKER}
    return [...new Set([...text.matchAll(/(?<=\\s|^)@(\\S+?)(?=\\s|$)/g)].map(match => match[1]))];
}}
'''
        text = replace_required(
            text,
            'import { deliver } from "../deliver.js";',
            'import { deliver } from "../deliver.js";\n' + helper,
            "at user list extractor",
        )
    if "return deliver(dt, msgBody, atUserList);" not in text:
        text = replace_required(
            text,
            "    return deliver(dt, msgBody);",
            "    const atUserList = extractAtUserList(msgBody);\n    return deliver(dt, msgBody, atUserList);",
            "send at user list",
        )
    if SAFETY_MARKER not in text:
        text = replace_required(
            text,
            "    return users;\n}\n",
            f"    return users;\n}}\n"
            f"function requestedAtNames(text) {{ // {SAFETY_MARKER}\n"
            "    return [...new Set([...text.matchAll(/(?<=\\s|^)@(\\S+?)(?=\\s|$)/g)].map(match => match[1]))];\n"
            "}\n",
            "native at safety helper",
        )
        text = replace_required(
            text,
            "    const atUserList = extractAtUserList(msgBody);\n    return deliver(dt, msgBody, atUserList);",
            "    const atUserList = extractAtUserList(msgBody);\n"
            "    const requestedNames = isGroup ? requestedAtNames(text) : [];\n"
            "    if (requestedNames.length > atUserList.length) {\n"
            "        throw new Error(`Native @ resolution failed: requested ${requestedNames.length}, resolved ${atUserList.length}`);\n"
            "    }\n"
            "    return deliver(dt, msgBody, atUserList);",
            "native at safety validation",
        )
    if DISCOVERY_MARKER not in text:
        mapping_literal = json.dumps(str(mapping_file), ensure_ascii=False)
        helper = f'''import fs from "node:fs"; // {DISCOVERY_MARKER}
const MEMBER_DISCOVERY_FILE = {mapping_literal};
function persistDiscoveredMembers(groupCode, records) {{
    if (!groupCode || !records?.length) return;
    try {{
        const mappings = fs.existsSync(MEMBER_DISCOVERY_FILE)
            ? JSON.parse(fs.readFileSync(MEMBER_DISCOVERY_FILE, "utf8")) : {{}};
        const group = mappings[groupCode] ?? {{}};
        let changed = false;
        for (const record of records) {{
            const name = record.nickName?.trim();
            const userId = record.userId?.trim();
            if (name && name !== "unknown" && userId && group[name] !== userId) {{
                group[name] = userId;
                changed = true;
            }}
        }}
        if (changed) {{
            mappings[groupCode] = group;
            fs.writeFileSync(MEMBER_DISCOVERY_FILE, JSON.stringify(mappings, null, 2) + "\\n", "utf8");
        }}
    }} catch {{}}
}}
'''
        text = replace_required(
            text,
            'import { getMember } from "../../../infra/cache/member.js";',
            'import { getMember } from "../../../infra/cache/member.js";\n' + helper.rstrip(),
            "member discovery helper",
        )
        needle = "    const memberInst = isGroup ? getMember(account.accountId) : undefined;"
        replacement = needle + '''
    if (isGroup && memberInst) {
        const discovered = await memberInst.queryMembers(groupCode);
        persistDiscoveredMembers(groupCode, discovered);
    }'''
        text = replace_required(text, needle, replacement, "member discovery call")
    write(path, text)


def patch_simple(path: Path, replacements: list[tuple[str, str, str]]) -> None:
    text = read(path)
    if MARKER in text:
        return
    already_patched = {
        "deliver signature": "export async function deliver(dt, msgBody, atUserList = [])",
        "delivery at list": "            atUserList,",
        "transport parameter": "traceContext, atUserList = [],",
        "transport at list": "at_user_list: atUserList",
        "codec at list": "atUserList: data.at_user_list",
    }
    for old, new, label in replacements:
        if already_patched.get(label, "\0") in text:
            continue
        text = replace_required(text, old, new, label)
    write(path, text)


def patch_proto(path: Path) -> None:
    data = json.loads(read(path))
    fields = data["nested"]["trpc"]["nested"]["yuanbao"]["nested"]["yuanbao_conn"]["nested"]["yuanbao_openclaw_proxy"]["nested"]["SendGroupMessageReq"]["fields"]
    fields.setdefault("atUserList", {"rule": "repeated", "type": "string", "id": 10})
    write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def apply_patch(root: Path, project: Path) -> Path:
    mapping = project / "yuanbao_members.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    if not mapping.exists():
        write(mapping, "{}\n")
    backup = project / "patch-backups" / f"yuanbao-native-at-{SUPPORTED_VERSION}"
    for relative in FILES:
        source = root / relative
        if not source.exists():
            raise FileNotFoundError(source)
        target = backup / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    patch_handlers(root / FILES[0], mapping)
    patch_extract(root / FILES[1], mapping)
    patch_send(root / FILES[2], mapping)
    patch_simple(root / FILES[3], [
        ("export async function deliver(dt, msgBody) {", f"export async function deliver(dt, msgBody, atUserList = []) {{ // {MARKER}", "deliver signature"),
        ("            traceContext: dt.traceContext,", "            traceContext: dt.traceContext,\n            atUserList,", "delivery at list"),
    ])
    patch_simple(root / FILES[4], [
        ("    const { account, groupCode, msgBody, fromAccount, refMsgId, refFromAccount, wsClient, traceContext, } = params;", f"    const {{ account, groupCode, msgBody, fromAccount, refMsgId, refFromAccount, wsClient, traceContext, atUserList = [], }} = params; // {MARKER}", "transport parameter"),
        ("            ...(attachRef ? { ref_msg_id: refMsgId } : {}),\n            ...(traceContext ? { trace_id: traceContext.traceId } : {}),\n            ...(traceContext ? { msg_seq: traceContext.nextMsgSeq() } : {}),", "            ...(attachRef ? { ref_msg_id: refMsgId } : {}),\n            ...(traceContext ? { trace_id: traceContext.traceId } : {}),\n            ...(traceContext ? { msg_seq: traceContext.nextMsgSeq() } : {}),\n            ...(atUserList.length ? { at_user_list: atUserList } : {}),", "transport at list"),
    ])
    patch_simple(root / FILES[5], [
        ("        refMsgId: data.ref_msg_id ?? \"\",\n        ...(data.msg_seq !== undefined ? { msgSeq: data.msg_seq } : {}),\n        ...(logExt ? { logExt } : {}),", f"        refMsgId: data.ref_msg_id ?? \"\",\n        ...(data.msg_seq !== undefined ? {{ msgSeq: data.msg_seq }} : {{}}),\n        ...(logExt ? {{ logExt }} : {{}}),\n        ...(data.at_user_list?.length ? {{ atUserList: data.at_user_list }} : {{}}), // {MARKER}", "codec at list"),
    ])
    patch_proto(root / FILES[6])
    return backup


def restore(root: Path, project: Path) -> None:
    backup = project / "patch-backups" / f"yuanbao-native-at-{SUPPORTED_VERSION}"
    if not backup.exists():
        raise FileNotFoundError(f"Backup not found: {backup}")
    for relative in FILES:
        shutil.copy2(backup / relative, root / relative)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Yuanbao 2.17.0 native @ patch")
    parser.add_argument("--openclaw", default=r"D:\OpenClaw\openclaw.cmd")
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--plugin-root")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--skip-restart", action="store_true")
    args = parser.parse_args()

    project = Path(args.project_dir).resolve()
    root = resolve_plugin_root(args.openclaw, args.plugin_root)
    if args.restore:
        restore(root, project)
        print(f"Restored plugin from {project / 'patch-backups'}")
    else:
        backup = apply_patch(root, project)
        print(f"Patched: {root}")
        print(f"Mapping: {project / 'yuanbao_members.json'}")
        print(f"Backup: {backup}")

    if not args.skip_restart and not args.plugin_root:
        subprocess.Popen(
            [args.openclaw, "gateway", "run", "--force"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(5)
        subprocess.run([args.openclaw, "gateway", "status"], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
